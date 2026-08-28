"""Restart-safe Odoo review delivery and resolution for absence PTO."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
import html
import logging
from typing import Literal
from uuid import UUID, uuid4
import xmlrpc.client

from . import absence_pto_conversion as conversion
from . import absence_pto_store as store
from . import odoo_client, shift_config, staffing_hours
from .plant_day import today as plant_today


WENDY_LOGIN = "wendy@gruberpallets.com"
_REVIEW_URL = "https://gpiplantmanager.com/staffing/time-off"
_ROLLOVER_ERROR = "Configured pay period closed before approval."
_DUPLICATE_TASK_ERROR = "More than one active exact Odoo task exists for this request."
_DUPLICATE_PROJECT_ERROR = "More than one active exact Plant Manager project exists."
_POLL_INTERVAL = timedelta(minutes=15)
_RETRY_DELAYS = (60, 300, 900, 3600)
_MAX_ATTEMPTS = 10
# Two months is deliberately bounded while covering long planned shutdowns.
_BUSINESS_DAY_SEARCH_DAYS = 62
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
    for _ in range(_BUSINESS_DAY_SEARCH_DAYS):
        try:
            if shift_config.is_workday(candidate):
                return candidate
        except Exception as error:
            raise ReviewDeliveryError(
                "The configured plant calendar is unavailable."
            ) from error
        candidate += timedelta(days=1)
    raise PermanentReviewDeliveryError(
        f"No configured plant business day exists in the next "
        f"{_BUSINESS_DAY_SEARCH_DAYS} days."
    )


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
    workflow_now: datetime,
    permanent: bool = False,
) -> store.AbsencePtoRequest:
    current = _now(workflow_now)
    attempts = min(row.task_attempts + 1, _MAX_ATTEMPTS)
    if permanent or attempts >= _MAX_ATTEMPTS:
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
        workflow_now=current,
        lease_now=_lease_now(),
    )


def _save_task_id(
    row: store.AbsencePtoRequest,
    owner: UUID,
    task_id: int,
    workflow_now: datetime,
) -> store.AbsencePtoRequest:
    current = _now(workflow_now)
    return store.save_task_delivery(
        row.id,
        owner,
        task_id=task_id,
        attempts=row.task_attempts,
        next_at=current + _POLL_INTERVAL,
        error=_stop_reason(row.sync_error),
        workflow_now=current,
        lease_now=_lease_now(),
    )


def _save_delivery_success(
    row: store.AbsencePtoRequest, owner: UUID, workflow_now: datetime
) -> store.AbsencePtoRequest:
    current = _now(workflow_now)
    return store.save_task_delivery(
        row.id,
        owner,
        task_id=row.odoo_task_id,
        attempts=0,
        next_at=current + _POLL_INTERVAL,
        error=_stop_reason(row.sync_error),
        workflow_now=current,
        lease_now=_lease_now(),
    )


def _exact_task_ids(project_id: int, name: str) -> list[int]:
    task_ids = odoo_client.find_active_feedback_task_ids(project_id, name)
    if len(task_ids) > 1:
        raise PermanentReviewDeliveryError(_DUPLICATE_TASK_ERROR)
    return task_ids


def _exact_project_id() -> int:
    project_ids = odoo_client.find_active_feedback_project_ids(
        odoo_client.FEEDBACK_PROJECT_NAME
    )
    if len(project_ids) > 1:
        raise PermanentReviewDeliveryError(_DUPLICATE_PROJECT_ERROR)
    if not project_ids:
        raise ReviewDeliveryError("The active exact Plant Manager project was not found.")
    return project_ids[0]


def _compatibility_fault(error: Exception) -> bool:
    return (
        isinstance(error, xmlrpc.client.Fault)
        and "user_ids" in (error.faultString or "")
    )


def _created_task_id(value: object) -> int:
    if type(value) is not int or value <= 0:
        raise PermanentReviewDeliveryError("Odoo returned an invalid created task ID.")
    return value


def _refresh_task(
    row: store.AbsencePtoRequest,
    owner: UUID,
    *,
    task_id: int,
    wendy_uid: int,
    deadline: str,
    project_id: int,
    name: str,
) -> None:
    store.renew_claim(row.id, owner, _lease_now(), lease_seconds=120)
    values = {
        "task_id": task_id,
        "project_id": project_id,
        "name": name,
        "description_html": task_body(row),
        "assignee_uid": wendy_uid,
        "deadline": deadline,
    }
    try:
        odoo_client.update_review_task_user_ids(**values)
    except Exception as error:
        if not _compatibility_fault(error):
            raise
        # The fallback is a separate mutation, so ownership is freshly fenced.
        store.renew_claim(row.id, owner, _lease_now(), lease_seconds=120)
        odoo_client.update_review_task_user_id(**values)


def _create_task(
    row: store.AbsencePtoRequest,
    owner: UUID,
    *,
    project_id: int,
    name: str,
    wendy_uid: int,
    deadline: str,
) -> int:
    values = {
        "project_id": project_id,
        "name": name,
        "description_html": task_body(row),
        "assignee_uid": wendy_uid,
        "tag_id": None,
        "deadline": deadline,
    }
    store.renew_claim(row.id, owner, _lease_now(), lease_seconds=120)
    try:
        return _created_task_id(odoo_client.create_review_task_user_ids(**values))
    except Exception as primary_error:
        # A create response can be lost even when Odoo committed it. Always
        # search the exact active identity before considering another create.
        task_ids = _exact_task_ids(project_id, name)
        if task_ids:
            return task_ids[0]
        if not _compatibility_fault(primary_error):
            raise primary_error
        store.renew_claim(row.id, owner, _lease_now(), lease_seconds=120)
        try:
            return _created_task_id(odoo_client.create_review_task_user_id(**values))
        except Exception as fallback_error:
            task_ids = _exact_task_ids(project_id, name)
            if task_ids:
                return task_ids[0]
            raise fallback_error


def _sync_claimed_task(
    row: store.AbsencePtoRequest,
    owner: UUID,
    workflow_now: datetime | None = None,
) -> Literal["escalated", "failed"]:
    """Create/adopt/update one task while a caller owns the durable row lease."""
    if row.state != "needs_review":
        return "failed"
    current = _now(workflow_now)
    if row.task_attempts >= _MAX_ATTEMPTS:
        if row.task_next_at != datetime.max.replace(tzinfo=UTC):
            _save_retry(
                row,
                owner,
                PermanentReviewDeliveryError("Maximum task delivery attempts reached."),
                workflow_now=current,
                permanent=True,
            )
        return "escalated"
    try:
        deadline = _next_business_day(plant_today(current)).isoformat()
        wendy_uid = _wendy_uid()
        name = task_name(row)
        project_id = _exact_project_id()
        task_ids = _exact_task_ids(project_id, name)
        if task_ids:
            task_id = task_ids[0]
        elif row.odoo_task_id is not None:
            identity = odoo_client.fetch_feedback_task_identity(row.odoo_task_id)
            if (
                identity is None
                or identity.get("id") != row.odoo_task_id
                or identity.get("name") != name
                or identity.get("project_id") != project_id
                or identity.get("active") is not False
            ):
                raise PermanentReviewDeliveryError(
                    "The saved Odoo task no longer has its exact trusted identity."
                )
            task_id = row.odoo_task_id
        else:
            task_id = _create_task(
                row,
                owner,
                project_id=project_id,
                name=name,
                wendy_uid=wendy_uid,
                deadline=deadline,
            )
        # Save immediately after verified creation/adoption, before any later call.
        row = _save_task_id(row, owner, int(task_id), current)
        _refresh_task(
            row,
            owner,
            task_id=int(task_id),
            wendy_uid=wendy_uid,
            deadline=deadline,
            project_id=project_id,
            name=name,
        )
        _save_delivery_success(row, owner, current)
        return "escalated"
    except PermanentReviewDeliveryError as error:
        _save_retry(row, owner, error, workflow_now=current, permanent=True)
        return "escalated"
    except Exception as error:  # noqa: BLE001 - remote delivery is durably retried
        _save_retry(row, owner, error, workflow_now=current)
        return "escalated"


def sync_review_task(request_id: int, now: datetime | None = None) -> ReviewResult:
    current = _now(now)
    owner = uuid4()
    row = store.claim_request(request_id, owner, _lease_now(), lease_seconds=120)
    if row is None:
        return ReviewResult("busy", None, None, "This review is already being checked.")
    try:
        _sync_claimed_task(row, owner, current)
        saved = store.get_request(request_id)
        if saved is None:
            return ReviewResult("blocked", None, None, "The review request is missing.")
        if saved.task_next_at == datetime.max.replace(tzinfo=UTC):
            status = "blocked"
        elif saved.odoo_task_id is None or " Task delivery error:" in (
            saved.sync_error or ""
        ):
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


def _resolution_marker(row: store.AbsencePtoRequest) -> str:
    return f"gpi-pm-absence-pto-{row.id}"


def _resolution_message(row: store.AbsencePtoRequest) -> str:
    marker = _resolution_marker(row)
    if row.state == "approved":
        text = (
            "✅ Plant Manager verified one exact approved PTO record and updated "
            "the local case."
        )
    else:
        text = (
            "✅ Marked handled in Plant Manager.<br>"
            f"<strong>Note:</strong> {html.escape(row.manual_resolution_note or '')}"
        )
    return f'{text}<span style="display:none">{marker}</span>'


def _save_resolution_retry(
    row: store.AbsencePtoRequest,
    owner: UUID,
    error: Exception,
    workflow_now: datetime,
    *,
    permanent: bool = False,
) -> store.AbsencePtoRequest:
    current = _now(workflow_now)
    attempts = (
        _MAX_ATTEMPTS if permanent else row.task_resolution_attempts + 1
    )
    if permanent:
        next_at = datetime.max.replace(tzinfo=UTC)
    else:
        delay = _RETRY_DELAYS[min(attempts - 1, len(_RETRY_DELAYS) - 1)]
        next_at = current + timedelta(seconds=delay)
    message = " ".join((str(error) or type(error).__name__).split())[:300]
    return store.save_resolution_delivery(
        row.id,
        owner,
        expected_step=row.task_resolution_step,
        new_step=row.task_resolution_step,
        attempts=attempts,
        next_at=next_at,
        error=message,
        workflow_now=current,
        lease_now=_lease_now(),
    )


def _checkpoint_resolution(
    row: store.AbsencePtoRequest,
    owner: UUID,
    new_step: Literal["message_posted", "closed"],
    workflow_now: datetime,
) -> store.AbsencePtoRequest:
    current = _now(workflow_now)
    return store.save_resolution_delivery(
        row.id,
        owner,
        expected_step=row.task_resolution_step,
        new_step=new_step,
        attempts=0,
        next_at=None if new_step == "closed" else current,
        error=None,
        workflow_now=current,
        lease_now=_lease_now(),
    )


def _deliver_terminal_claimed(
    row: store.AbsencePtoRequest,
    owner: UUID,
    workflow_now: datetime | None = None,
) -> Literal["closed", "retry"]:
    """Resume idempotent terminal message/archive delivery from a durable step."""
    current = _now(workflow_now)
    if row.state not in {"approved", "resolved_manually"}:
        return "retry"
    if row.task_resolution_step == "closed":
        return "closed"
    if row.task_resolution_next_at == datetime.max.replace(tzinfo=UTC):
        return "retry"
    if row.odoo_task_id is None:
        _checkpoint_resolution(row, owner, "closed", current)
        return "closed"

    try:
        project_id = _exact_project_id()
        identity = odoo_client.fetch_feedback_task_identity(row.odoo_task_id)
        if (
            identity is None
            or identity.get("id") != row.odoo_task_id
            or identity.get("name") != task_name(row)
            or identity.get("project_id") != project_id
            or not isinstance(identity.get("active"), bool)
        ):
            raise PermanentReviewDeliveryError(
                "The saved Odoo task no longer has its exact trusted identity."
            )

        if row.task_resolution_step == "none":
            marker = _resolution_marker(row)
            messages = odoo_client.find_task_message_ids(row.odoo_task_id, marker)
            if not messages:
                store.renew_claim(row.id, owner, _lease_now(), lease_seconds=120)
                try:
                    odoo_client.post_task_message(
                        row.odoo_task_id, _resolution_message(row)
                    )
                except Exception as post_error:
                    # A lost response may still mean the chatter write committed.
                    messages = odoo_client.find_task_message_ids(
                        row.odoo_task_id, marker
                    )
                    if not messages:
                        raise post_error
            row = _checkpoint_resolution(row, owner, "message_posted", current)

        if not identity["active"]:
            _checkpoint_resolution(row, owner, "closed", current)
            return "closed"

        store.renew_claim(row.id, owner, _lease_now(), lease_seconds=120)
        try:
            odoo_client.close_task(row.odoo_task_id)
        except Exception as close_error:
            after = odoo_client.fetch_feedback_task_identity(row.odoo_task_id)
            if after is None or after.get("active") is not False:
                raise close_error
        _checkpoint_resolution(row, owner, "closed", current)
        return "closed"
    except store.StaleTransition:
        return "retry"
    except PermanentReviewDeliveryError as error:
        try:
            _save_resolution_retry(
                row, owner, error, current, permanent=True
            )
        except store.StaleTransition:
            pass
        return "retry"
    except Exception as error:  # noqa: BLE001 - terminal truth is already committed
        try:
            _save_resolution_retry(row, owner, error, current)
        except store.StaleTransition:
            pass
        return "retry"


def _resolve_external_claimed(
    row: store.AbsencePtoRequest,
    owner: UUID,
    workflow_now: datetime | None = None,
) -> store.AbsencePtoRequest | None:
    current = _now(workflow_now)
    pto = _matching_validated_pto(row)
    if pto is None:
        return None
    row = store.adopt_external_pto(
        row.id,
        owner,
        pto_leave_id=pto["id"],
        workflow_now=current,
        lease_now=_lease_now(),
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
        workflow_now=current,
        lease_now=_lease_now(),
    )
    try:
        conversion._invalidate_after_commit(approved)
    except Exception:  # noqa: BLE001 - committed truth must not be rolled back
        _log.warning("absence PTO review cache invalidation failed", exc_info=True)
    _deliver_terminal_claimed(approved, owner, current)
    return approved


def resolve_external_pto(request_id: int, now: datetime | None = None) -> ReviewResult:
    current = _now(now)
    owner = uuid4()
    row = store.claim_request(request_id, owner, _lease_now(), lease_seconds=120)
    if row is None:
        return ReviewResult("busy", None, None, "This review is already being checked.")
    try:
        if row.state == "approved":
            _deliver_terminal_claimed(row, owner, current)
            return ReviewResult(
                "approved", row.odoo_task_id, row, "The external PTO was verified."
            )
        if row.state != "needs_review":
            return ReviewResult(
                "needs_review", row.odoo_task_id, row, "This request is not awaiting review."
            )
        approved = _resolve_external_claimed(row, owner, current)
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
    current = _now(now)
    owner = uuid4()
    row = store.claim_request(request_id, owner, _lease_now(), lease_seconds=120)
    if row is None:
        return ReviewResult("busy", None, None, "This review is already being checked.")
    try:
        if row.state == "resolved_manually":
            _deliver_terminal_claimed(row, owner, current)
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
            workflow_now=current,
            lease_now=_lease_now(),
        )
        _deliver_terminal_claimed(resolved, owner, current)
        return ReviewResult(
            "resolved_manually",
            resolved.odoo_task_id,
            resolved,
            "The review was marked handled.",
        )
    finally:
        store.release_claim(request_id, owner, now=_lease_now())


def _reconcile_claimed(
    request: store.AbsencePtoRequest,
    owner: UUID,
    workflow_now: datetime | None = None,
) -> str:
    current = _now(workflow_now)
    if request.state in {"approved", "resolved_manually"}:
        return (
            "resumed"
            if _deliver_terminal_claimed(request, owner, current) == "closed"
            else "escalated"
        )
    if request.state == "pending":
        reviewed = store.mark_needs_review(
            request.id,
            owner,
            error=_ROLLOVER_ERROR,
            workflow_now=current,
            lease_now=_lease_now(),
        )
        if reviewed.state == "needs_review":
            return _sync_claimed_task(reviewed, owner, current)
        return "escalated"
    if request.state == "needs_review":
        approved = _resolve_external_claimed(request, owner, current)
        if approved is not None:
            return "resumed"
        return _sync_claimed_task(request, owner, current)
    if request.state != "converting":
        return "failed"
    result = conversion.resume_claimed(request, owner, current)
    if result.status == "needs_review":
        if result.request is not None and result.request.state == "needs_review":
            return _sync_claimed_task(result.request, owner, current)
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
        lease_now=_lease_now(),
        period_start=period_start,
        period_end=period_end,
        limit=limit,
    )
    counts = {"resumed": 0, "escalated": 0, "failed": 0}
    for request in requests:
        operation_error = None
        try:
            outcome = _reconcile_claimed(request, owner, current)
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
