"""Restart-safe Odoo review delivery and resolution for absence PTO."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
import html
import logging
from typing import Literal
from uuid import UUID, uuid4

from . import absence_pto_conversion as conversion
from . import absence_pto_store as store
from . import odoo_client, schedule_store, shift_config, staffing_hours
from .plant_day import today as plant_today


WENDY_LOGIN = "wendy@gruberpallets.com"
_REVIEW_URL = "https://gpiplantmanager.com/staffing/time-off"
_ROLLOVER_ERROR = "Configured pay period closed before approval."
_DUPLICATE_TASK_ERROR = "More than one active exact Odoo task exists for this request."
_POLL_INTERVAL = timedelta(minutes=15)
_RETRY_DELAYS = (60, 300, 900, 3600)
_MAX_ATTEMPTS = 10
_log = logging.getLogger(__name__)


class ReviewDeliveryError(RuntimeError):
    """A delivery precondition is not currently safe."""


class PermanentReviewDeliveryError(ReviewDeliveryError):
    """A delivery error that automated retries cannot make safe."""


@dataclass(frozen=True)
class ReviewResult:
    status: Literal[
        "delivered",
        "retry",
        "blocked",
        "busy",
        "approved",
        "needs_review",
        "resolved_manually",
    ]
    task_id: int | None
    request: store.AbsencePtoRequest | None
    message: str


@dataclass(frozen=True)
class ReconcileResult:
    scanned: int
    resumed: int
    escalated: int
    failed: int


def _clock() -> datetime:
    return datetime.now(UTC)


def _now(value: datetime | None) -> datetime:
    current = _clock() if value is None else value
    if current.tzinfo is None or current.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    return current


def _lease_now() -> datetime:
    return _now(_clock())


def _required_text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must not be blank")
    return value.strip()


def _next_business_day(day: date) -> date:
    candidate = day + timedelta(days=1)
    for _ in range(14):
        try:
            if shift_config.is_workday(candidate):
                return candidate
        except Exception:
            try:
                weekdays = schedule_store.current().work_weekdays
            except Exception:
                weekdays = frozenset()
            if candidate.weekday() in (weekdays or frozenset({0, 1, 2, 3, 4})):
                return candidate
        candidate += timedelta(days=1)
    return day + timedelta(days=1)


def task_name(row: store.AbsencePtoRequest) -> str:
    return f"[GPI-PM-PTO-{row.id}] Review {row.person_name} — {row.absence_day.isoformat()}"


def task_body(row: store.AbsencePtoRequest, app_url: str = _REVIEW_URL) -> str:
    """Render only the operational facts needed to settle this request."""
    return (
        "<p>Plant Manager could not safely finish a past PTO request.</p>"
        f"<p><strong>Worker:</strong> {html.escape(row.person_name)}<br>"
        f"<strong>Missed day:</strong> {row.absence_day.isoformat()}<br>"
        f"<strong>Requested PTO:</strong> {html.escape(row.leave_type_name)}<br>"
        f"<strong>Balance when requested:</strong> {row.balance_at_submit}<br>"
        f"<strong>Original Absence ID:</strong> {row.original_absence_leave_id or 'None'}<br>"
        f"<strong>Replacement PTO ID:</strong> {row.pto_leave_id or 'None'}<br>"
        f"<strong>Requested:</strong> {row.requested_at.date().isoformat()}<br>"
        f"<strong>Manager attempt:</strong> {html.escape(row.decided_by_name or 'None')}<br>"
        f"<strong>Stopped because:</strong> {html.escape(row.sync_error or 'Review required')}<br>"
        f"<strong>Last safe step:</strong> {html.escape(row.conversion_step)}<br>"
        f'<strong>Review:</strong> <a href="{html.escape(app_url, quote=True)}">'
        "Open Plant Manager</a></p>"
    )


def _wendy_uid() -> int:
    users = odoo_client.find_active_users_by_login(WENDY_LOGIN, limit=2)
    exact = [
        row
        for row in users
        if isinstance(row.get("login"), str)
        and row["login"].strip().casefold() == WENDY_LOGIN
    ]
    if len(exact) != 1:
        raise ReviewDeliveryError(
            f"Wendy lookup returned {len(exact)} exact active users; exactly one is required."
        )
    return int(exact[0]["id"])


def _stop_reason(error: str | None) -> str:
    value = error or "Review required."
    return value.split(" Task delivery error:", 1)[0]


def _delivery_error(row: store.AbsencePtoRequest, error: Exception) -> str:
    message = " ".join((str(error) or type(error).__name__).split())[:300]
    return f"{_stop_reason(row.sync_error)} Task delivery error: {message}"[:500]


def _save_retry(
    row: store.AbsencePtoRequest,
    owner: UUID,
    error: Exception,
    *,
    permanent: bool = False,
) -> store.AbsencePtoRequest:
    current = _lease_now()
    attempts = min(row.task_attempts + 1, _MAX_ATTEMPTS)
    if permanent:
        next_at = datetime.max.replace(tzinfo=UTC)
    else:
        delay = _RETRY_DELAYS[min(attempts - 1, len(_RETRY_DELAYS) - 1)]
        next_at = current + timedelta(seconds=delay)
    return store.save_task_delivery(
        row.id,
        owner,
        task_id=row.odoo_task_id,
        attempts=attempts,
        next_at=next_at,
        error=_delivery_error(row, error),
        now=current,
    )


def _save_task_id(
    row: store.AbsencePtoRequest, owner: UUID, task_id: int
) -> store.AbsencePtoRequest:
    current = _lease_now()
    return store.save_task_delivery(
        row.id,
        owner,
        task_id=task_id,
        attempts=0,
        next_at=current + _POLL_INTERVAL,
        error=_stop_reason(row.sync_error),
        now=current,
    )


def _exact_task_ids(project_id: int, name: str) -> list[int]:
    task_ids = odoo_client.find_active_feedback_task_ids(project_id, name)
    if len(task_ids) > 1:
        raise PermanentReviewDeliveryError(_DUPLICATE_TASK_ERROR)
    return task_ids


def _refresh_task(
    row: store.AbsencePtoRequest,
    owner: UUID,
    *,
    task_id: int,
    wendy_uid: int,
    deadline: str,
) -> None:
    store.renew_claim(row.id, owner, _lease_now(), lease_seconds=120)
    odoo_client.update_feedback_task(
        task_id,
        description_html=task_body(row),
        assignee_uid=wendy_uid,
        deadline=deadline,
        active=True,
    )


def _sync_claimed_task(
    row: store.AbsencePtoRequest, owner: UUID
) -> Literal["escalated", "failed"]:
    """Create/adopt/update one task while a caller owns the durable row lease."""
    if row.state != "needs_review":
        return "failed"
    try:
        wendy_uid = _wendy_uid()
        deadline = _next_business_day(plant_today(_lease_now())).isoformat()
        name = task_name(row)

        if row.odoo_task_id is not None:
            row = _save_task_id(row, owner, row.odoo_task_id)
            _refresh_task(
                row,
                owner,
                task_id=row.odoo_task_id,
                wendy_uid=wendy_uid,
                deadline=deadline,
            )
            return "escalated"

        project_id = odoo_client.ensure_feedback_project()
        task_ids = _exact_task_ids(project_id, name)
        if task_ids:
            row = _save_task_id(row, owner, task_ids[0])
            _refresh_task(
                row,
                owner,
                task_id=task_ids[0],
                wendy_uid=wendy_uid,
                deadline=deadline,
            )
            return "escalated"

        store.renew_claim(row.id, owner, _lease_now(), lease_seconds=120)
        try:
            task_id = odoo_client.create_feedback_task(
                project_id=project_id,
                name=name,
                description_html=task_body(row),
                assignee_uid=wendy_uid,
                tag_id=None,
                deadline=deadline,
            )
        except Exception as create_error:
            try:
                task_ids = _exact_task_ids(project_id, name)
            except PermanentReviewDeliveryError:
                raise
            except Exception:
                raise create_error
            if not task_ids:
                raise create_error
            task_id = task_ids[0]
        row = _save_task_id(row, owner, int(task_id))
        _refresh_task(
            row,
            owner,
            task_id=int(task_id),
            wendy_uid=wendy_uid,
            deadline=deadline,
        )
        return "escalated"
    except PermanentReviewDeliveryError as error:
        _save_retry(row, owner, error, permanent=True)
        return "escalated"
    except Exception as error:  # noqa: BLE001 - remote delivery is durably retried
        _save_retry(row, owner, error)
        return "escalated"


def sync_review_task(request_id: int, now: datetime | None = None) -> ReviewResult:
    _now(now)
    owner = uuid4()
    row = store.claim_request(request_id, owner, _lease_now(), lease_seconds=120)
    if row is None:
        return ReviewResult("busy", None, None, "This review is already being checked.")
    try:
        _sync_claimed_task(row, owner)
        saved = store.get_request(request_id)
        if saved is None:
            return ReviewResult("blocked", None, None, "The review request is missing.")
        if saved.task_next_at == datetime.max.replace(tzinfo=UTC):
            status = "blocked"
        elif saved.odoo_task_id is None:
            status = "retry"
        else:
            status = "delivered"
        return ReviewResult(status, saved.odoo_task_id, saved, saved.sync_error or status)
    finally:
        store.release_claim(request_id, owner, now=_lease_now())


def _matching_validated_pto(row: store.AbsencePtoRequest) -> dict | None:
    matches = odoo_client.find_matching_leaves(
        row.person_odoo_id,
        row.holiday_status_id,
        row.absence_day,
        include_terminal=False,
    )
    if len(matches) != 1:
        return None
    candidate = matches[0]
    snapshot = odoo_client.fetch_leave_snapshot(candidate.get("id"))
    if snapshot is None:
        return None
    identity = (
        snapshot.get("id") == candidate.get("id")
        and snapshot.get("employee_id") == row.person_odoo_id
        and snapshot.get("holiday_status_id") == row.holiday_status_id
        and snapshot.get("date_from") == row.absence_day
        and snapshot.get("date_to") == row.absence_day
        and snapshot.get("state") == "validate"
    )
    return snapshot if identity else None


def _post_resolved_and_close(row: store.AbsencePtoRequest, owner: UUID) -> None:
    if row.odoo_task_id is None:
        return
    store.renew_claim(row.id, owner, _lease_now(), lease_seconds=120)
    odoo_client.post_task_message(
        row.odoo_task_id,
        "✅ Plant Manager verified one exact approved PTO record and updated the local case.",
    )
    store.renew_claim(row.id, owner, _lease_now(), lease_seconds=120)
    odoo_client.close_task(row.odoo_task_id)


def _resolve_external_claimed(
    row: store.AbsencePtoRequest, owner: UUID
) -> store.AbsencePtoRequest | None:
    pto = _matching_validated_pto(row)
    if pto is None:
        return None
    row = store.adopt_external_pto(
        row.id, owner, pto_leave_id=pto["id"], now=_lease_now()
    )
    row = store.renew_claim(row.id, owner, _lease_now(), lease_seconds=120)
    verified = _matching_validated_pto(row)
    if verified is None or verified["id"] != row.pto_leave_id:
        return None
    approved = store.finalize_approved(
        row.id,
        owner,
        original_absence_leave_id=row.original_absence_leave_id,
        pto_leave_id=verified["id"],
        actor_upn=row.decided_by_upn,
        actor_name=row.decided_by_name,
        source="absence_pto_review",
        now=_lease_now(),
    )
    try:
        conversion._invalidate_after_commit(approved)
    except Exception:  # noqa: BLE001 - committed truth must not be rolled back
        _log.warning("absence PTO review cache invalidation failed", exc_info=True)
    _post_resolved_and_close(approved, owner)
    return approved


def resolve_external_pto(request_id: int, now: datetime | None = None) -> ReviewResult:
    _now(now)
    owner = uuid4()
    row = store.claim_request(request_id, owner, _lease_now(), lease_seconds=120)
    if row is None:
        return ReviewResult("busy", None, None, "This review is already being checked.")
    try:
        if row.state == "approved":
            _post_resolved_and_close(row, owner)
            return ReviewResult(
                "approved", row.odoo_task_id, row, "The external PTO was verified."
            )
        if row.state != "needs_review":
            return ReviewResult(
                "needs_review", row.odoo_task_id, row, "This request is not awaiting review."
            )
        approved = _resolve_external_claimed(row, owner)
        if approved is None:
            return ReviewResult(
                "needs_review",
                row.odoo_task_id,
                row,
                "Exactly one matching approved PTO record was not verified.",
            )
        return ReviewResult(
            "approved", approved.odoo_task_id, approved, "The external PTO was verified."
        )
    finally:
        store.release_claim(request_id, owner, now=_lease_now())


def resolve_manually(
    request_id: int,
    actor_upn: str,
    actor_name: str,
    note: str,
    now: datetime | None = None,
) -> ReviewResult:
    safe_note = _required_text(note, "note")
    safe_upn = _required_text(actor_upn, "actor_upn")
    safe_name = _required_text(actor_name, "actor_name")
    _now(now)
    owner = uuid4()
    row = store.claim_request(request_id, owner, _lease_now(), lease_seconds=120)
    if row is None:
        return ReviewResult("busy", None, None, "This review is already being checked.")
    try:
        if row.state == "resolved_manually":
            if row.odoo_task_id is not None:
                stored_note = row.manual_resolution_note or safe_note
                store.renew_claim(row.id, owner, _lease_now(), lease_seconds=120)
                odoo_client.post_task_message(
                    row.odoo_task_id,
                    "✅ Marked handled in Plant Manager.<br>"
                    f"<strong>Note:</strong> {html.escape(stored_note)}",
                )
                store.renew_claim(row.id, owner, _lease_now(), lease_seconds=120)
                odoo_client.close_task(row.odoo_task_id)
            return ReviewResult(
                "resolved_manually",
                row.odoo_task_id,
                row,
                "The review was marked handled.",
            )
        if row.state != "needs_review":
            return ReviewResult(
                "needs_review", row.odoo_task_id, row, "This request is not awaiting review."
            )
        resolved = store.finalize_manual(
            row.id,
            owner,
            actor_upn=safe_upn,
            actor_name=safe_name,
            note=safe_note,
            now=_lease_now(),
        )
        if resolved.odoo_task_id is not None:
            store.renew_claim(resolved.id, owner, _lease_now(), lease_seconds=120)
            odoo_client.post_task_message(
                resolved.odoo_task_id,
                "✅ Marked handled in Plant Manager.<br>"
                f"<strong>Note:</strong> {html.escape(safe_note)}",
            )
            store.renew_claim(resolved.id, owner, _lease_now(), lease_seconds=120)
            odoo_client.close_task(resolved.odoo_task_id)
        return ReviewResult(
            "resolved_manually",
            resolved.odoo_task_id,
            resolved,
            "The review was marked handled.",
        )
    finally:
        store.release_claim(request_id, owner, now=_lease_now())


def _reconcile_claimed(request: store.AbsencePtoRequest, owner: UUID) -> str:
    if request.state == "pending":
        reviewed = store.mark_needs_review(
            request.id, owner, error=_ROLLOVER_ERROR, now=_lease_now()
        )
        if reviewed.state == "needs_review":
            return _sync_claimed_task(reviewed, owner)
        return "escalated"
    if request.state == "needs_review":
        approved = _resolve_external_claimed(request, owner)
        if approved is not None:
            return "resumed"
        return _sync_claimed_task(request, owner)
    if request.state != "converting":
        return "failed"
    result = conversion.resume_claimed(request, owner)
    if result.status == "needs_review":
        if result.request is not None and result.request.state == "needs_review":
            return _sync_claimed_task(result.request, owner)
        return "escalated"
    if result.status == "busy":
        return "failed"
    return "resumed"


def _log_exception(request_id: int, phase: str, error: Exception) -> None:
    _log.error(
        "absence PTO reconcile %s failed for request %s: %s",
        phase,
        request_id,
        error,
        exc_info=(type(error), error, error.__traceback__),
    )


def reconcile_once(now: datetime | None = None, limit: int = 25) -> ReconcileResult:
    """Claim and isolate a bounded batch of rollover, recovery, and review work."""
    current = _now(now)
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("limit must be between 1 and 100")
    period_start, period_end = staffing_hours.current_pay_period_bounds(
        plant_today(current)
    )
    owner = uuid4()
    requests = store.claim_due(
        owner,
        current,
        period_start=period_start,
        period_end=period_end,
        limit=limit,
    )
    counts = {"resumed": 0, "escalated": 0, "failed": 0}
    for request in requests:
        operation_error = None
        try:
            outcome = _reconcile_claimed(request, owner)
        except Exception as error:  # noqa: BLE001 - isolate each recovery row
            operation_error = error
            outcome = "failed"

        release_error = None
        try:
            released = store.release_claim(request.id, owner, now=_lease_now())
            if not released:
                release_error = RuntimeError("lease release returned false")
        except Exception as error:  # noqa: BLE001 - isolate lease cleanup too
            release_error = error

        if operation_error is not None:
            _log_exception(request.id, "operation", operation_error)
        if release_error is not None:
            _log_exception(request.id, "release", release_error)
            outcome = "failed"
        counts[outcome] += 1
    return ReconcileResult(
        scanned=len(requests),
        resumed=counts["resumed"],
        escalated=counts["escalated"],
        failed=counts["failed"],
    )


__all__ = [
    "ReconcileResult",
    "ReviewResult",
    "reconcile_once",
    "resolve_external_pto",
    "resolve_manually",
    "sync_review_task",
    "task_body",
    "task_name",
]
