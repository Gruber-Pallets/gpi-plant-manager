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
_ROLLOVER_ERROR = "Configured pay period closed before approval."
_RESTORED_MESSAGE = "PTO was not applied. The absence was restored."
_REVIEW_MESSAGE = "This needs payroll review."
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
    workflow_now = _now(now)
    owner = uuid4()
    current = store.claim_request(request_id, owner, _lease_now(), lease_seconds=120)
    if current is None:
        return ConversionResult("busy", _BUSY_MESSAGE, None)
    try:
        return _resume_claim(
            current, owner, actor_upn, actor_name, source, workflow_now
        )
    finally:
        store.release_claim_safely(
            request_id, owner, now=_lease_now(), context="approval"
        )


def resume(request_id: int, now: datetime | None = None) -> ConversionResult:
    workflow_now = _now(now)
    owner = uuid4()
    current = store.claim_request(request_id, owner, _lease_now(), lease_seconds=120)
    if current is None:
        return ConversionResult("busy", _BUSY_MESSAGE, None)
    try:
        if current.state == "pending":
            return ConversionResult(
                "pending", "This request is waiting for manager approval.", current
            )
        return resume_claimed(current, owner, workflow_now)
    finally:
        store.release_claim_safely(
            request_id, owner, now=_lease_now(), context="resume"
        )


def resume_claimed(
    request: store.AbsencePtoRequest,
    owner: UUID,
    workflow_now: datetime,
) -> ConversionResult:
    """Resume one row already leased by the bounded reconciler claim."""
    current = store.renew_claim(request.id, owner, _lease_now(), lease_seconds=120)
    return _resume_claim(
        current,
        owner,
        current.decided_by_upn,
        current.decided_by_name,
        "reconciler",
        _now(workflow_now),
    )


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
        raise ConversionSafetyError(_ROLLOVER_ERROR)
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
        include_terminal=False,
    )
    if len(rows) > 1:
        raise ConversionSafetyError("More than one matching PTO leave exists in Odoo.")
    if not rows:
        return None
    row = rows[0]
    verified = _verified_snapshot(row["id"], request, request.holiday_status_id)
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
    workflow_now: datetime,
) -> ConversionResult:
    message = str(error) or type(error).__name__
    if request.state in {"pending", "converting", "needs_review"}:
        try:
            request = store.mark_needs_review(
                request.id,
                owner,
                error=message[:500],
                workflow_now=_now(workflow_now),
                lease_now=_lease_now(),
            )
        except store.StaleTransition:
            return ConversionResult("busy", _BUSY_MESSAGE, None)
    return ConversionResult("needs_review", message, request)


def _friendly(*errors: Exception) -> str:
    messages: list[str] = []
    for error in errors:
        message = " ".join((str(error) or type(error).__name__).split())
        if message == _ROLLOVER_ERROR:
            return _ROLLOVER_ERROR
        if message and message not in messages:
            messages.append(message)
    return "; ".join(messages)[:500] or "Odoo recovery could not be verified."


def _transition_to_pending(
    request: store.AbsencePtoRequest,
    owner: UUID,
    error: Exception,
) -> ConversionResult:
    request = store.transition_to_pending(
        request.id,
        owner,
        error=_friendly(error),
        now=_lease_now(),
    )
    return ConversionResult("pending", _RESTORED_MESSAGE, request)


def _recovery_view(
    request: store.AbsencePtoRequest,
    now: datetime,
) -> tuple[dict | None, list[dict]]:
    """Rebuild every identity and rollover fact relevant to compensation."""
    absence = _manual_absence(request)
    today = plant_today(now)
    start, end = staffing_hours.current_pay_period_bounds(today)
    if request.absence_day >= today or not start <= request.absence_day <= end:
        raise ConversionSafetyError(_ROLLOVER_ERROR)
    pto_type = absence_pto.resolve_paid_time_off_type()
    if (
        pto_type.holiday_status_id != request.holiday_status_id
        or pto_type.name != request.leave_type_name
    ):
        raise ConversionSafetyError("The configured Paid Time Off type changed.")
    if absence.get("odoo_leave_id") != request.original_absence_leave_id:
        raise ConversionSafetyError("The attendance absence Odoo link changed.")

    original = _verified_original(request)
    rows = odoo_client.find_matching_leaves(
        request.person_odoo_id,
        request.holiday_status_id,
        request.absence_day,
        include_terminal=False,
    )
    if len(rows) > 1:
        raise ConversionSafetyError("More than one active matching PTO leave exists in Odoo.")
    active = [
        _verified_snapshot(row["id"], request, request.holiday_status_id)
        for row in rows
    ]
    if request.pto_leave_id is not None:
        known = _verified_snapshot(
            request.pto_leave_id,
            request,
            request.holiday_status_id,
        )
        if known["state"] not in {"cancel", "refuse"}:
            if not active or active[0]["id"] != known["id"]:
                raise ConversionSafetyError("The linked PTO leave could not be matched.")
    return original, active


def _renew_and_recovery_view(
    request: store.AbsencePtoRequest,
    owner: UUID,
) -> tuple[store.AbsencePtoRequest, dict | None, list[dict]]:
    request = store.renew_claim(request.id, owner, _lease_now(), lease_seconds=120)
    original, active = _recovery_view(request, _lease_now())
    return request, original, active


def _recovery_step_fence(
    request: store.AbsencePtoRequest,
    owner: UUID,
) -> store.AbsencePtoRequest:
    return store.transition(
        request.id,
        owner,
        expected_state="converting",
        expected_step=request.conversion_step,
        new_state="converting",
        new_step=request.conversion_step,
        pto_leave_id=request.pto_leave_id,
        now=_lease_now(),
    )


def _adopt_recovery_pto(
    request: store.AbsencePtoRequest,
    owner: UUID,
    pto: dict,
) -> store.AbsencePtoRequest:
    if request.pto_leave_id is not None:
        if request.pto_leave_id != pto["id"]:
            raise ConversionSafetyError("The linked PTO leave changed identity.")
        return request
    return store.transition(
        request.id,
        owner,
        expected_state="converting",
        expected_step=request.conversion_step,
        new_state="converting",
        new_step=request.conversion_step,
        pto_leave_id=pto["id"],
        now=_lease_now(),
    )


def _close_incomplete_pto(
    request: store.AbsencePtoRequest,
    owner: UUID,
) -> store.AbsencePtoRequest:
    original, active = _recovery_view(request, _lease_now())
    del original
    if not active:
        return request
    pto = active[0]
    request = _adopt_recovery_pto(request, owner, pto)
    if pto["state"] == "validate":
        raise ConversionSafetyError("An approved matching PTO leave remains active in Odoo.")
    if pto["state"] not in {"draft", "confirm", "validate1"}:
        raise ConversionSafetyError("The matching PTO leave is in an unsafe Odoo state.")

    expected_state = pto["state"]
    request, _, active = _renew_and_recovery_view(request, owner)
    if len(active) != 1 or active[0]["id"] != pto["id"]:
        raise ConversionSafetyError("The matching PTO leave changed before closure.")
    if active[0]["state"] != expected_state:
        raise ConversionSafetyError("The matching PTO leave changed before closure.")
    leave_id = pto["id"]
    request = _fence_mutation(request, owner)
    try:
        odoo_client.refuse_leave(leave_id)
    except Exception as error:
        closed = _verified_snapshot(leave_id, request, request.holiday_status_id)
        if closed["state"] not in {"cancel", "refuse"}:
            raise ConversionSafetyError(_friendly(error, RuntimeError("PTO would not close")))
    closed = _verified_snapshot(leave_id, request, request.holiday_status_id)
    if closed["state"] not in {"cancel", "refuse"}:
        raise ConversionSafetyError("Odoo did not verify the incomplete PTO closure.")
    request = _recovery_step_fence(request, owner)
    _, active = _recovery_view(request, _lease_now())
    if active:
        raise ConversionSafetyError("An active matching PTO leave remains after closure.")
    return request


def _restore_original_absence(
    request: store.AbsencePtoRequest,
    owner: UUID,
) -> store.AbsencePtoRequest:
    if request.original_absence_leave_id is None:
        _, active = _recovery_view(request, _lease_now())
        if active:
            raise ConversionSafetyError("An active matching PTO leave remains in Odoo.")
        return request

    original, active = _recovery_view(request, _lease_now())
    if active:
        raise ConversionSafetyError("An active matching PTO leave remains in Odoo.")
    if original is None:
        raise ConversionSafetyError("The original Odoo Absence is missing.")
    if original["state"] in {"refuse", "cancel"}:
        expected_state = original["state"]
        request, original, active = _renew_and_recovery_view(request, owner)
        if active or original is None or original["state"] != expected_state:
            raise ConversionSafetyError("The original Absence changed before reset.")
        leave_id = original["id"]
        request = _fence_mutation(request, owner)
        try:
            odoo_client.reset_leave_to_confirm(leave_id)
        except Exception as error:
            reset = _verified_original(request)
            if reset is None or reset["state"] not in {"confirm", "validate1", "validate"}:
                raise ConversionSafetyError(_friendly(error))
        reset = _verified_original(request)
        if reset is None or reset["state"] not in {"confirm", "validate1", "validate"}:
            raise ConversionSafetyError("Odoo did not verify the Absence reset.")
        request = _recovery_step_fence(request, owner)
        original = reset

    if original["state"] not in {"confirm", "validate1", "validate"}:
        raise ConversionSafetyError("The original Absence is in an unsafe recovery state.")
    while original["state"] in {"confirm", "validate1"}:
        expected_state = original["state"]
        request, original, active = _renew_and_recovery_view(request, owner)
        if active or original is None or original["state"] != expected_state:
            raise ConversionSafetyError("The original Absence changed before approval.")
        leave_id = original["id"]
        request = _fence_mutation(request, owner)
        try:
            odoo_client.approve_leave_once(leave_id)
        except Exception as error:
            advanced = _verified_original(request)
            allowed = (
                {"validate1", "validate"}
                if expected_state == "confirm"
                else {"validate"}
            )
            if advanced is None or advanced["state"] not in allowed:
                raise ConversionSafetyError(_friendly(error))
        advanced = _verified_original(request)
        allowed = (
            {"validate1", "validate"}
            if expected_state == "confirm"
            else {"validate"}
        )
        if advanced is None or advanced["state"] not in allowed:
            raise ConversionSafetyError("Odoo did not verify one Absence approval transition.")
        request = _recovery_step_fence(request, owner)
        original = advanced

    final_original, active = _recovery_view(request, _lease_now())
    if active or final_original is None or final_original["state"] != "validate":
        raise ConversionSafetyError("The original Absence restoration was not verified.")
    return request


def _compensate(
    request: store.AbsencePtoRequest,
    owner: UUID,
    error: Exception,
    workflow_now: datetime,
) -> ConversionResult:
    try:
        request = _close_incomplete_pto(request, owner)
        request = _restore_original_absence(request, owner)
        return _transition_to_pending(request, owner, error)
    except store.StaleTransition:
        return ConversionResult("busy", _BUSY_MESSAGE, None)
    except Exception as compensation_error:  # noqa: BLE001 - fail closed to review
        combined = ConversionSafetyError(_friendly(error, compensation_error))
        result = _needs_review(request, owner, combined, workflow_now)
        return ConversionResult(result.status, _REVIEW_MESSAGE, result.request)


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
    workflow_now: datetime,
) -> ConversionResult:
    post_refusal = request.conversion_step != "not_started"
    try:
        if request.state == "approved":
            return _already_approved(request)
        if request.state == "needs_review":
            return ConversionResult(
                "needs_review", request.sync_error or "Review is required.", request
            )
        if request.state != "pending" and request.state != "converting":
            raise ConversionSafetyError("This request cannot be approved from its current state.")

        if (
            request.state == "converting"
            and request.conversion_step == "not_started"
            and request.original_absence_leave_id is not None
        ):
            original_before_preflight = _verified_original(request)
            post_refusal = bool(
                original_before_preflight
                and original_before_preflight["state"] == "refuse"
            )

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
                post_refusal = True
                try:
                    odoo_client.refuse_leave(leave_id)
                except Exception as error:
                    after_refusal = _verified_original(request)
                    if after_refusal is not None and after_refusal["state"] == "validate":
                        pending = store.transition_to_pending(
                            request.id,
                            owner,
                            error=_friendly(error),
                            now=_lease_now(),
                        )
                        return ConversionResult("pending", _friendly(error), pending)
                    if after_refusal is None or after_refusal["state"] != "refuse":
                        raise ConversionSafetyError(_friendly(error))
                original = _verified_original(request)
                if original is None or original["state"] != "refuse":
                    if original is not None and original["state"] == "validate":
                        error = ConversionSafetyError(
                            "Odoo did not verify the absence refusal."
                        )
                        pending = store.transition_to_pending(
                            request.id,
                            owner,
                            error=_friendly(error),
                            now=_lease_now(),
                        )
                        return ConversionResult("pending", _friendly(error), pending)
                    raise ConversionSafetyError("Odoo did not verify the absence refusal.")
            elif original is not None and original["state"] != "refuse":
                raise ConversionSafetyError("The original absence is in an unsafe Odoo state.")
            elif original is not None:
                post_refusal = True
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
                leave_id = None
                try:
                    leave_id = odoo_client.create_leave(**create_args)
                    pto = _verified_snapshot(int(leave_id), request, request.holiday_status_id)
                except Exception:
                    if leave_id is not None and request.pto_leave_id is None:
                        request = store.transition(
                            request.id,
                            owner,
                            expected_state="converting",
                            expected_step="absence_refused",
                            new_state="converting",
                            new_step="absence_refused",
                            pto_leave_id=int(leave_id),
                            now=_lease_now(),
                        )
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
                try:
                    odoo_client.confirm_leave_once(leave_id)
                except Exception as error:
                    confirmed = _verified_snapshot(
                        request.pto_leave_id,
                        request,
                        request.holiday_status_id,
                    )
                    if confirmed["state"] not in {"confirm", "validate1", "validate"}:
                        raise ConversionSafetyError(_friendly(error))
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
                allowed_states = (
                    {"validate1", "validate"} if expected_odoo_state == "confirm" else {"validate"}
                )
                try:
                    odoo_client.approve_leave_once(leave_id)
                except Exception as error:
                    approved = _verified_snapshot(
                        request.pto_leave_id,
                        request,
                        request.holiday_status_id,
                    )
                    if approved["state"] not in allowed_states:
                        raise ConversionSafetyError(_friendly(error))
                pto = _verified_snapshot(request.pto_leave_id, request, request.holiday_status_id)
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
            workflow_now=_now(workflow_now),
            lease_now=_lease_now(),
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
        if request.conversion_step == "not_started" and not post_refusal:
            return _pending_for_low_balance(request, owner)
        return _compensate(request, owner, error, workflow_now)
    except Exception as error:  # noqa: BLE001 - preserve safe durable status
        if request.state == "converting" and (
            request.conversion_step != "not_started" or post_refusal
        ):
            return _compensate(request, owner, error, workflow_now)
        return _needs_review(request, owner, error, workflow_now)


def _invalidate_after_commit(request: store.AbsencePtoRequest) -> None:
    """Invalidate only after the local finalization transaction committed."""
    time_off_balances.invalidate(request.person_odoo_id)
    staffing.invalidate_schedule_cache(request.absence_day)
    _http_cache.invalidate_all_cache()


__all__ = ["ConversionResult", "approve", "resume", "resume_claimed"]
