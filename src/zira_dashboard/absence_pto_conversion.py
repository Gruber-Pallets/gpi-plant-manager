"""Verified, leased conversion of one recorded absence into approved PTO."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import logging
from typing import Literal
from uuid import UUID, uuid4

from . import (
    _http_cache,
    absence_pto,
    absence_pto_store as store,
    absence_sync,
    db,
    odoo_client,
    staffing,
    staffing_hours,
    time_off_balances,
)
from .plant_day import today as plant_today


_PENDING_BALANCE_MESSAGE = "The current PTO balance is below one day."
_BUSY_MESSAGE = "This request is already being checked."
_log = logging.getLogger(__name__)


class ConversionSafetyError(RuntimeError):
    """Live state no longer proves that the next conversion action is safe."""


class _LowBalance(ConversionSafetyError):
    pass


@dataclass(frozen=True)
class ConversionResult:
    status: Literal["approved", "pending", "needs_review", "busy"]
    message: str
    request: store.AbsencePtoRequest | None


def _now(value: datetime | None) -> datetime:
    current = datetime.now(UTC) if value is None else value
    if current.tzinfo is None or current.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    return current


def _clock() -> datetime:
    """Injectable live clock for every lease and safety boundary."""
    return datetime.now(UTC)


def _lease_now() -> datetime:
    return _now(_clock())


def approve(
    request_id: int,
    actor_upn: str | None,
    actor_name: str | None,
    source: str | None,
    now: datetime | None = None,
) -> ConversionResult:
    if now is not None:
        _now(now)
    owner = uuid4()
    current = store.claim_request(request_id, owner, _lease_now(), lease_seconds=120)
    if current is None:
        return ConversionResult("busy", _BUSY_MESSAGE, None)
    try:
        return _resume_claim(current, owner, actor_upn, actor_name, source)
    finally:
        store.release_claim(request_id, owner, now=_lease_now())


def resume(request_id: int, now: datetime | None = None) -> ConversionResult:
    if now is not None:
        _now(now)
    owner = uuid4()
    current = store.claim_request(request_id, owner, _lease_now(), lease_seconds=120)
    if current is None:
        return ConversionResult("busy", _BUSY_MESSAGE, None)
    try:
        if current.state == "pending":
            return ConversionResult(
                "pending", "This request is waiting for manager approval.", current
            )
        return _resume_claim(
            current,
            owner,
            current.decided_by_upn,
            current.decided_by_name,
            "reconciler",
        )
    finally:
        store.release_claim(request_id, owner, now=_lease_now())


def _manual_absence(request: store.AbsencePtoRequest) -> dict:
    rows = db.query(
        "SELECT day, emp_id, odoo_leave_id FROM manual_absences WHERE day = %s AND emp_id = %s",
        (request.absence_day, request.emp_id),
    )
    if len(rows) != 1:
        raise ConversionSafetyError("The recorded attendance absence was not found.")
    return rows[0]


def _balance(request: store.AbsencePtoRequest) -> float:
    refreshed = time_off_balances.refresh_for_employee(request.person_odoo_id)
    if not isinstance(refreshed, int) or isinstance(refreshed, bool) or refreshed <= 0:
        raise ConversionSafetyError("The current PTO balance could not be refreshed.")
    rows = [
        row
        for row in time_off_balances.get_for_employee(request.person_odoo_id)
        if row.get("holiday_status_id") == request.holiday_status_id
    ]
    if len(rows) != 1 or rows[0].get("available_practical") is None:
        raise ConversionSafetyError("The current PTO balance could not be verified.")
    return float(rows[0]["available_practical"])


def _preflight(
    request: store.AbsencePtoRequest,
    now: datetime,
    *,
    own_pto: dict | None = None,
) -> dict:
    absence = _manual_absence(request)
    today = plant_today(now)
    start, end = staffing_hours.current_pay_period_bounds(today)
    if request.absence_day >= today or not start <= request.absence_day <= end:
        raise ConversionSafetyError("The absence is no longer in the current pay period.")
    pto_type = absence_pto.resolve_paid_time_off_type()
    if (
        pto_type.holiday_status_id != request.holiday_status_id
        or pto_type.name != request.leave_type_name
    ):
        raise ConversionSafetyError("The configured Paid Time Off type changed.")
    expected_link = (
        request.pto_leave_id if request.state == "approved" else request.original_absence_leave_id
    )
    if absence.get("odoo_leave_id") != expected_link:
        raise ConversionSafetyError("The attendance absence Odoo link changed.")
    if request.original_absence_leave_id is not None:
        original = _verified_original(request)
        if request.conversion_step != "not_started" and original["state"] != "refuse":
            raise ConversionSafetyError("The original absence is no longer refused.")
    if request.pto_leave_id is not None:
        matched_pto = _matching_pto(request)
        if matched_pto is None or matched_pto["id"] != request.pto_leave_id:
            raise ConversionSafetyError("The linked PTO leave could not be matched.")
        own_pto = matched_pto
    available = _balance(request)
    # Once this exact PTO has entered Odoo's pending workflow it is already
    # included in the practical-balance deduction. Add back only that one day
    # so its own reservation cannot make a safe resume look underfunded.
    owns_pending_day = bool(
        own_pto and own_pto.get("state") in {"confirm", "validate1", "validate"}
    )
    if available + (1.0 if owns_pending_day else 0.0) < 1.0:
        raise _LowBalance(_PENDING_BALANCE_MESSAGE)
    return absence


def _verified_snapshot(
    leave_id: int,
    request: store.AbsencePtoRequest,
    type_id: int,
) -> dict:
    snapshot = odoo_client.fetch_leave_snapshot(leave_id)
    if snapshot is None:
        raise ConversionSafetyError("A linked Odoo leave is missing.")
    if (
        snapshot.get("id") != leave_id
        or snapshot.get("employee_id") != request.person_odoo_id
        or snapshot.get("holiday_status_id") != type_id
        or snapshot.get("date_from") != request.absence_day
        or snapshot.get("date_to") != request.absence_day
    ):
        raise ConversionSafetyError("A linked Odoo leave has the wrong identity.")
    return snapshot


def _verified_original(request: store.AbsencePtoRequest) -> dict | None:
    if request.original_absence_leave_id is None:
        return None
    absence_type_id = absence_sync.resolve_absence_leave_type_id()
    return _verified_snapshot(request.original_absence_leave_id, request, absence_type_id)


def _matching_pto(request: store.AbsencePtoRequest) -> dict | None:
    rows = odoo_client.find_matching_leaves(
        request.person_odoo_id,
        request.holiday_status_id,
        request.absence_day,
        include_terminal=True,
    )
    if len(rows) > 1:
        raise ConversionSafetyError("More than one matching PTO leave exists in Odoo.")
    if not rows:
        return None
    row = rows[0]
    verified = _verified_snapshot(row["id"], request, request.holiday_status_id)
    if verified["state"] in {"cancel", "refuse"}:
        raise ConversionSafetyError("The matching PTO leave is already closed.")
    if request.pto_leave_id is not None and verified["id"] != request.pto_leave_id:
        raise ConversionSafetyError("The linked PTO leave changed identity.")
    return verified


def _renew_and_preflight(
    request: store.AbsencePtoRequest,
    owner: UUID,
    *,
    own_pto: dict | None = None,
) -> store.AbsencePtoRequest:
    """Renew in a short transaction, then rebuild the complete live safety view."""
    renewed = store.renew_claim(request.id, owner, _lease_now(), lease_seconds=120)
    _preflight(renewed, _lease_now(), own_pto=own_pto)
    return renewed


def _fence_mutation(
    request: store.AbsencePtoRequest,
    owner: UUID,
) -> store.AbsencePtoRequest:
    """CAS-renew ownership at the live clock immediately before one mutation."""
    return store.renew_claim(request.id, owner, _lease_now(), lease_seconds=120)


def _pending_for_low_balance(
    request: store.AbsencePtoRequest,
    owner: UUID,
) -> ConversionResult:
    if request.state == "converting" and request.conversion_step == "not_started":
        try:
            request = store.transition(
                request.id,
                owner,
                expected_state="converting",
                expected_step="not_started",
                new_state="pending",
                new_step="not_started",
                sync_error=None,
                now=_lease_now(),
            )
        except store.StaleTransition:
            return ConversionResult("busy", _BUSY_MESSAGE, None)
    return ConversionResult("pending", _PENDING_BALANCE_MESSAGE, request)


def _needs_review(
    request: store.AbsencePtoRequest,
    owner: UUID,
    error: Exception,
) -> ConversionResult:
    message = str(error) or type(error).__name__
    if request.state in {"pending", "converting", "needs_review"}:
        try:
            request = store.mark_needs_review(
                request.id,
                owner,
                error=message[:500],
                now=_lease_now(),
            )
        except store.StaleTransition:
            return ConversionResult("busy", _BUSY_MESSAGE, None)
    return ConversionResult("needs_review", message, request)


def _already_approved(request: store.AbsencePtoRequest) -> ConversionResult:
    if request.pto_leave_id is None:
        return ConversionResult("needs_review", "The approved request has no PTO leave.", request)
    absence = _manual_absence(request)
    if absence.get("odoo_leave_id") != request.pto_leave_id:
        return ConversionResult(
            "needs_review", "The approved attendance link does not match its PTO leave.", request
        )
    snapshot = _matching_pto(request)
    if snapshot is None or snapshot["id"] != request.pto_leave_id:
        return ConversionResult(
            "needs_review", "The approved PTO leave could not be matched.", request
        )
    if snapshot["state"] != "validate":
        return ConversionResult(
            "needs_review", "The linked PTO leave is not approved in Odoo.", request
        )
    return ConversionResult("approved", "This PTO request is approved.", request)


def _resume_claim(
    request: store.AbsencePtoRequest,
    owner: UUID,
    actor_upn: str | None,
    actor_name: str | None,
    source: str | None,
) -> ConversionResult:
    try:
        if request.state == "approved":
            return _already_approved(request)
        if request.state == "needs_review":
            return ConversionResult(
                "needs_review", request.sync_error or "Review is required.", request
            )
        if request.state != "pending" and request.state != "converting":
            raise ConversionSafetyError("This request cannot be approved from its current state.")

        _preflight(request, _lease_now())
        if request.state == "pending":
            request = store.transition(
                request.id,
                owner,
                expected_state="pending",
                expected_step="not_started",
                new_state="converting",
                new_step="not_started",
                decided_by_upn=actor_upn,
                decided_by_name=actor_name,
                decided_at=_lease_now(),
                sync_error=None,
                now=_lease_now(),
            )

        if request.conversion_step == "not_started":
            original = _verified_original(request)
            if original is not None and original["state"] == "validate":
                request = _renew_and_preflight(request, owner)
                original = _verified_original(request)
                if original is None or original["state"] != "validate":
                    raise ConversionSafetyError("The original absence changed before refusal.")
                leave_id = original["id"]
                request = _fence_mutation(request, owner)
                odoo_client.refuse_leave(leave_id)
                original = _verified_original(request)
                if original is None or original["state"] != "refuse":
                    raise ConversionSafetyError("Odoo did not verify the absence refusal.")
            elif original is not None and original["state"] != "refuse":
                raise ConversionSafetyError("The original absence is in an unsafe Odoo state.")
            request = store.transition(
                request.id,
                owner,
                expected_state="converting",
                expected_step="not_started",
                new_state="converting",
                new_step="absence_refused",
                now=_lease_now(),
            )

        if request.conversion_step == "absence_refused":
            _preflight(request, _lease_now())
            original = _verified_original(request)
            if original is not None and original["state"] != "refuse":
                raise ConversionSafetyError("The original absence is no longer refused.")
            pto = _matching_pto(request)
            if pto is None:
                request = _renew_and_preflight(request, owner)
                if _matching_pto(request) is not None:
                    raise ConversionSafetyError("A PTO leave appeared before creation.")
                create_args = {
                    "employee_odoo_id": request.person_odoo_id,
                    "holiday_status_id": request.holiday_status_id,
                    "date_from": request.absence_day,
                    "date_to": request.absence_day,
                    "hour_from": None,
                    "hour_to": None,
                    "note": "Paid Time Off for recorded absence",
                }
                request = _fence_mutation(request, owner)
                try:
                    leave_id = odoo_client.create_leave(**create_args)
                    pto = _verified_snapshot(int(leave_id), request, request.holiday_status_id)
                except Exception:
                    # A timeout can hide a successful create. Bounded exact
                    # lookup adopts that one record; zero/multiple fail closed.
                    pto = _matching_pto(request)
                    if pto is None:
                        raise
            request = store.transition(
                request.id,
                owner,
                expected_state="converting",
                expected_step="absence_refused",
                new_state="converting",
                new_step="pto_created",
                pto_leave_id=pto["id"],
                now=_lease_now(),
            )

        if request.conversion_step == "pto_created":
            pto = _verified_snapshot(request.pto_leave_id, request, request.holiday_status_id)
            if pto["state"] == "draft":
                request = _renew_and_preflight(request, owner, own_pto=pto)
                pto = _verified_snapshot(request.pto_leave_id, request, request.holiday_status_id)
                if pto["state"] != "draft":
                    raise ConversionSafetyError("The PTO leave changed before confirmation.")
                leave_id = pto["id"]
                request = _fence_mutation(request, owner)
                odoo_client.confirm_leave(leave_id)
                pto = _verified_snapshot(request.pto_leave_id, request, request.holiday_status_id)
                if pto["state"] not in {"confirm", "validate1", "validate"}:
                    raise ConversionSafetyError("Odoo did not verify the PTO confirmation.")
                request = store.transition(
                    request.id,
                    owner,
                    expected_state="converting",
                    expected_step="pto_created",
                    new_state="converting",
                    new_step="pto_created",
                    now=_lease_now(),
                )
            while pto["state"] in {"confirm", "validate1"}:
                expected_odoo_state = pto["state"]
                request = _renew_and_preflight(request, owner, own_pto=pto)
                pto = _verified_snapshot(request.pto_leave_id, request, request.holiday_status_id)
                if pto["state"] != expected_odoo_state:
                    raise ConversionSafetyError("The PTO leave changed before approval.")
                leave_id = pto["id"]
                request = _fence_mutation(request, owner)
                odoo_client.approve_leave_once(leave_id)
                pto = _verified_snapshot(request.pto_leave_id, request, request.holiday_status_id)
                allowed_states = (
                    {"validate1", "validate"} if expected_odoo_state == "confirm" else {"validate"}
                )
                if pto["state"] not in allowed_states:
                    raise ConversionSafetyError("Odoo did not verify one PTO approval transition.")
                next_step = "pto_approved" if pto["state"] == "validate" else "pto_created"
                request = store.transition(
                    request.id,
                    owner,
                    expected_state="converting",
                    expected_step="pto_created",
                    new_state="converting",
                    new_step=next_step,
                    now=_lease_now(),
                )
            if pto["state"] != "validate":
                raise ConversionSafetyError("Odoo did not verify the PTO approval.")
            if request.conversion_step == "pto_created":
                request = store.transition(
                    request.id,
                    owner,
                    expected_state="converting",
                    expected_step="pto_created",
                    new_state="converting",
                    new_step="pto_approved",
                    now=_lease_now(),
                )

        if request.conversion_step != "pto_approved":
            raise ConversionSafetyError("The conversion step is not supported.")
        pto = _verified_snapshot(request.pto_leave_id, request, request.holiday_status_id)
        request = _renew_and_preflight(request, owner, own_pto=pto)
        pto = _verified_snapshot(request.pto_leave_id, request, request.holiday_status_id)
        if pto["state"] != "validate":
            raise ConversionSafetyError("The PTO approval changed before finalization.")
        request = store.finalize_approved(
            request.id,
            owner,
            original_absence_leave_id=request.original_absence_leave_id,
            pto_leave_id=pto["id"],
            actor_upn=actor_upn or request.decided_by_upn,
            actor_name=actor_name or request.decided_by_name,
            source=source,
            now=_lease_now(),
        )
        try:
            _invalidate_after_commit(request)
        except Exception:  # noqa: BLE001 - approved transaction already committed
            _log.warning(
                "absence PTO cache invalidation failed for request %s",
                request.id,
                exc_info=True,
            )
        return ConversionResult("approved", "The absence now uses approved PTO.", request)
    except store.StaleTransition:
        return ConversionResult("busy", _BUSY_MESSAGE, None)
    except _LowBalance as error:
        if request.conversion_step == "not_started":
            return _pending_for_low_balance(request, owner)
        return _needs_review(request, owner, error)
    except Exception as error:  # noqa: BLE001 - preserve safe durable status
        return _needs_review(request, owner, error)


def _invalidate_after_commit(request: store.AbsencePtoRequest) -> None:
    """Invalidate only after the local finalization transaction committed."""
    time_off_balances.invalidate(request.person_odoo_id)
    staffing.invalidate_schedule_cache(request.absence_day)
    _http_cache.invalidate_all_cache()


__all__ = ["ConversionResult", "approve", "resume"]
