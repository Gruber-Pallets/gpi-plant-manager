"""Late/Absence Report mutation endpoints.

All four are POST handlers that write to Postgres state behind the
late-report (manual_absences, late_arrivals, late_snoozes) and then
invalidate shared caches so the next GET /api/late-report picks up
the change.

The READ path — `GET /api/late-report` — and its 30-second response
cache stay in `routes/staffing.py` because they share `_safe_attendance`
(and its StratusTime/Odoo live-cache fallback chain) with the
`/staffing` day-view page handler. Splitting that would mean
duplicating the helper. Cache invalidation runs through
`_bust_after_mutation` over there.
"""
from __future__ import annotations

import asyncio
import logging
import re
from datetime import UTC, datetime, time as dt_time

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from .. import (
    absence_sync,
    db,
    inbox_keys,
    inbox_log,
    late_report,
    shift_config,
    timeclock_sync,
)
from ..plant_day import now as plant_now, today as plant_today

_log = logging.getLogger(__name__)

router = APIRouter()


def _bust_caches() -> None:
    """Drop every cache that could now be stale after a late-report write.
    Delegates to `routes.staffing._bust_after_mutation` so all mutation
    paths share the same invalidation set."""
    from .staffing import _bust_after_mutation
    _bust_after_mutation()


def _declare_absent_sync(body: dict, actor_upn=None, actor_name=None) -> JSONResponse:
    """Blocking half of /api/late-report/declare-absent (Postgres writes +
    cache busting); runs in a worker thread via asyncio.to_thread."""
    emp_id = str(body.get("emp_id") or "").strip()
    name = str(body.get("name") or "").strip()
    reason_raw = body.get("reason")
    reason = str(reason_raw).strip() if reason_raw is not None else ""
    if not emp_id or not name:
        return JSONResponse({"ok": False, "error": "emp_id and name required"}, status_code=400)
    if not reason:
        return JSONResponse(
            {"ok": False, "error": "reason required — no record posts until a reason is given"},
            status_code=400,
        )
    try:
        employee_odoo_id = int(emp_id)
    except ValueError:
        return JSONResponse(
            {"ok": False, "error": "emp_id must be an Odoo employee id"},
            status_code=400,
        )
    today = plant_today()
    # The Odoo Time Off sync is best-effort: the local manual_absences row is
    # the source of truth for the scheduler/inbox (see the absent-in-timeoff
    # design). Odoo can legitimately refuse the leave — e.g. the employee's
    # Odoo work schedule shows no hours that day ("not supposed to work during
    # that period") even though our plant schedule had them on — and that must
    # NOT block the manager from recording the absence. So sync first, but on
    # failure fall through with no linked leave id and surface a warning.
    odoo_leave_id = None
    odoo_warning = None
    try:
        absence = absence_sync.create_absence_for_day(
            employee_odoo_id=employee_odoo_id,
            employee_name=name,
            day=today,
            reason=reason,
        )
        odoo_leave_id = absence["leave_id"]
    except Exception as e:  # noqa: BLE001 -- sync is best-effort; record locally regardless
        odoo_warning = absence_sync.describe_sync_failure(e)
        _log.warning("absence Odoo sync failed for %s (emp %s): %s", name, emp_id, e)
    if odoo_leave_id is not None:
        try:
            absence_sync.mirror_approved_absence(
                employee_odoo_id=employee_odoo_id,
                holiday_status_id=absence["holiday_status_id"],
                leave_id=odoo_leave_id,
                day=today,
                employee_name=name,
                reason=reason,
            )
        except Exception as e:  # noqa: BLE001 -- manual absence remains authoritative
            odoo_warning = (
                "absence approved in Odoo, but the local Time Off mirror "
                f"wasn't updated — {e}"
            )
            _log.warning(
                "absence local mirror failed for %s (emp %s, leave %s): %s",
                name, emp_id, odoo_leave_id, e,
                exc_info=True,
            )
    try:
        late_report.declare_absent(
            today,
            emp_id,
            name,
            reason=reason,
            odoo_leave_id=odoo_leave_id,
        )
        db.execute(
            "DELETE FROM late_snoozes WHERE day = %s AND emp_id = %s",
            (today, emp_id),
        )
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)
    eid = inbox_log.log_event_safe(
        item_kind="late",
        item_key=inbox_keys.late(emp_id, today.isoformat()),
        person_name=name,
        category_label="Late",
        action="absent",
        outcome="Marked absent",
        reason=reason,
        actor_upn=actor_upn,
        actor_name=actor_name,
        source="inbox",
        reversible=True,
    )
    _bust_caches()
    result: dict = {"ok": True, "event_id": eid}
    if odoo_warning:
        result["odoo_synced"] = False
        result["warning"] = odoo_warning
    return JSONResponse(result)


@router.post("/api/late-report/declare-absent")
async def late_report_declare_absent(request: Request):
    """Mark a person as Absent for today.

    Body (JSON): {emp_id, name, reason}

    Reason is REQUIRED — no manual_absences row gets written until
    a non-empty reason is captured. Side effects: writes to
    manual_absences; clears any pending snooze; busts caches.
    """
    body = await request.json()
    actor_upn, actor_name = inbox_log.actor_from(request)
    return await asyncio.to_thread(_declare_absent_sync, body, actor_upn, actor_name)


def _parse_clock_time(value) -> dt_time | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = dt_time.fromisoformat(text)
    except ValueError:
        return None
    return parsed.replace(second=0, microsecond=0, tzinfo=None)


def _forgot_punch_in_sync(
    body: dict,
    actor_upn=None,
    actor_name=None,
) -> JSONResponse:
    """Record an exact manager-entered clock-in for a no-punch late row.

    This deliberately bypasses normal punch rounding: the manager is
    correcting a forgotten punch to a known time, matching the missed
    punch-out correction posture.
    """
    emp_id = str(body.get("emp_id") or "").strip()
    name = str(body.get("name") or "").strip()
    wc_name = str(body.get("wc_name") or "").strip()
    clock_time = _parse_clock_time(body.get("time"))
    if not emp_id or not name:
        return JSONResponse({"ok": False, "error": "emp_id and name required"}, status_code=400)
    if not wc_name:
        return JSONResponse({"ok": False, "error": "work center required"}, status_code=400)
    if clock_time is None:
        return JSONResponse({"ok": False, "error": "valid clock-in time required"}, status_code=400)
    try:
        employee_odoo_id = int(emp_id)
    except ValueError:
        return JSONResponse(
            {"ok": False, "error": "emp_id must be an Odoo employee id"},
            status_code=400,
        )

    today = plant_today()
    punch_at = datetime.combine(today, clock_time, tzinfo=shift_config.SITE_TZ)
    try:
        rows = db.query(
            "INSERT INTO timeclock_punches_log "
            "(person_odoo_id, action, wc_name, occurred_at, rounded_at) "
            "VALUES (%s, %s, %s, %s, %s) RETURNING id",
            (employee_odoo_id, "clock_in", wc_name, punch_at, punch_at),
        )
        log_id = int(rows[0]["id"])
        db.execute(
            "DELETE FROM late_snoozes WHERE day = %s AND emp_id = %s",
            (today, emp_id),
        )
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)

    # Keep the same Odoo sync machinery as kiosk punches. sync_one_by_id
    # records a sync_error and leaves the row retryable if Odoo is down.
    timeclock_sync.sync_one_by_id(log_id)
    eid = inbox_log.log_event_safe(
        item_kind="late",
        item_key=inbox_keys.late(emp_id, today.isoformat()),
        person_name=name,
        category_label="Late",
        action="clock_in",
        outcome="Clocked in",
        after_value=f"{clock_time.strftime('%H:%M')} at {wc_name}",
        actor_upn=actor_upn,
        actor_name=actor_name,
        source="inbox",
        reversible=False,
        detail={
            "log_id": log_id,
            "time": clock_time.strftime("%H:%M"),
            "wc_name": wc_name,
        },
    )
    _bust_caches()
    return JSONResponse({"ok": True, "log_id": log_id, "event_id": eid})


@router.post("/api/late-report/forgot-punch-in")
async def late_report_forgot_punch_in(request: Request):
    """Clock in a no-punch employee at a manager-entered exact time."""
    body = await request.json()
    actor_upn, actor_name = inbox_log.actor_from(request)
    return await asyncio.to_thread(_forgot_punch_in_sync, body, actor_upn, actor_name)


def _save_late_arrival_sync(body: dict, actor_upn=None, actor_name=None) -> JSONResponse:
    """Blocking half of /api/late-report/save-late-arrival (Postgres write +
    cache busting); runs in a worker thread via asyncio.to_thread."""
    from .. import late_report
    emp_id = str(body.get("emp_id") or "").strip()
    name = str(body.get("name") or "").strip()
    reason_raw = body.get("reason")
    reason = str(reason_raw).strip() if reason_raw is not None else ""
    if not emp_id or not name:
        return JSONResponse({"ok": False, "error": "emp_id and name required"}, status_code=400)
    if not reason:
        return JSONResponse(
            {"ok": False, "error": "reason required — no record posts until a reason is given"},
            status_code=400,
        )
    today = plant_today()
    try:
        late_report.save_late_arrival(today, emp_id, name, reason=reason)
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)
    eid = inbox_log.log_event_safe(
        item_kind="late",
        item_key=inbox_keys.late(emp_id, today.isoformat()),
        person_name=name,
        category_label="Late",
        action="reason",
        outcome="Late reason recorded",
        reason=reason,
        actor_upn=actor_upn,
        actor_name=actor_name,
        source="inbox",
        reversible=True,
    )
    _bust_caches()
    return JSONResponse({"ok": True, "event_id": eid})


@router.post("/api/late-report/save-late-arrival")
async def late_report_save_late_arrival(request: Request):
    """Record a late-arrival event for today.

    Body (JSON): {emp_id, name, reason}

    Reason is REQUIRED — no late_arrivals row gets written until a
    non-empty reason is captured. Side effects: writes to
    late_arrivals; busts the report cache so the row drops out of
    needs_reason on the next poll.
    """
    body = await request.json()
    actor_upn, actor_name = inbox_log.actor_from(request)
    return await asyncio.to_thread(_save_late_arrival_sync, body, actor_upn, actor_name)


def _snooze_sync(body: dict) -> JSONResponse:
    """Blocking half of /api/late-report/snooze (Postgres write + cache
    busting); runs in a worker thread via asyncio.to_thread."""
    from .. import late_report
    emp_id = str(body.get("emp_id") or "").strip()
    name = str(body.get("name") or "").strip()
    try:
        minutes = int(body.get("minutes") or late_report.DEFAULT_SNOOZE_MINUTES)
    except (TypeError, ValueError):
        minutes = late_report.DEFAULT_SNOOZE_MINUTES
    minutes = max(1, min(minutes, 8 * 60))
    if not emp_id or not name:
        return JSONResponse({"ok": False, "error": "emp_id and name required"}, status_code=400)
    today = plant_today()
    try:
        late_report.snooze(today, emp_id, name, minutes)
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)
    _bust_caches()
    return JSONResponse({"ok": True, "minutes": minutes})


@router.post("/api/late-report/snooze")
async def late_report_snooze(request: Request):
    """Silence a person from the Late/Absence Report for `minutes` (default 30).

    Body (JSON): {emp_id, name, minutes?}
    """
    body = await request.json()
    return await asyncio.to_thread(_snooze_sync, body)


def _parse_running_late_time(value) -> dt_time | None:
    """Accept only the exact ``HH:MM`` form used by Running Late."""
    if not isinstance(value, str) or re.fullmatch(r"[0-9]{2}:[0-9]{2}", value) is None:
        return None
    return _parse_clock_time(value)


def _running_late_sync(body: dict) -> JSONResponse:
    """Record a manager-confirmed expected arrival for a late employee."""
    emp_id = str(body.get("emp_id") or "").strip()
    name = str(body.get("name") or "").strip()
    selected = _parse_running_late_time(body.get("expected_time"))
    if not emp_id or not name:
        return JSONResponse({"ok": False, "error": "emp_id and name required"}, status_code=400)
    if selected is None:
        return JSONResponse({"ok": False, "error": "expected_time must be HH:MM"}, status_code=400)

    today = plant_today()
    expected_local = datetime.combine(today, selected, tzinfo=shift_config.SITE_TZ)
    if expected_local <= plant_now():
        return JSONResponse(
            {"ok": False, "error": "expected time must be later than now"}, status_code=400
        )
    try:
        late_report.set_expected_arrival(
            today, emp_id, name, expected_local.astimezone(UTC)
        )
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)
    _bust_caches()
    return JSONResponse({"ok": True, "expected_at": expected_local.isoformat()})


@router.post("/api/late-report/running-late")
async def late_report_running_late(request: Request):
    """Record when a no-punch employee is expected to arrive today."""
    return await asyncio.to_thread(_running_late_sync, await request.json())


def _undo_absent_sync(body: dict) -> JSONResponse:
    """Blocking half of /api/late-report/undo-absent (Postgres write + cache
    busting); runs in a worker thread via asyncio.to_thread."""
    emp_id = str(body.get("emp_id") or "").strip()
    if not emp_id:
        return JSONResponse({"ok": False, "error": "emp_id required"}, status_code=400)
    today = plant_today()
    try:
        absence_sync.refuse_absence_leave(
            late_report.odoo_leave_id_for_absence(today, emp_id)
        )
        late_report.undo_absent(today, emp_id)
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)
    _bust_caches()
    return JSONResponse({"ok": True})


@router.post("/api/late-report/undo-absent")
async def late_report_undo_absent(request: Request):
    """Reverse a declared absence (e.g., manager mis-clicked).

    Body (JSON): {emp_id}
    """
    body = await request.json()
    return await asyncio.to_thread(_undo_absent_sync, body)
