"""Daily Exception Inbox."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, UTC
from typing import Any

from fastapi import APIRouter, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import HTMLResponse, JSONResponse

from .. import exception_inbox, inbox_keys, inbox_log, plant_day, time_off_audit
from ..deps import templates

router = APIRouter()


@router.get("/exceptions", response_class=HTMLResponse)
def exceptions_page(request: Request):
    # The nav Inbox-count bootstrap is rendered by _topnav.html (via
    # nav_inbox_summary()), so this route no longer needs to pass it.
    snapshot = exception_inbox.build_snapshot()
    return templates.TemplateResponse(
        request,
        "exceptions.html",
        {
            "snapshot": snapshot,
            "sections": snapshot["sections"],
            "queue": snapshot["queue"],
            "work_centers": snapshot.get("work_centers") or [],
            "people": snapshot.get("people") or [],
        },
    )


@router.get("/api/exceptions")
def exceptions_json():
    return JSONResponse(jsonable_encoder(exception_inbox.build_snapshot()))


@router.get("/api/exceptions/summary")
def exceptions_summary_json():
    return JSONResponse(exception_inbox.build_summary())


_TIME_OFF_STATES = {
    "draft",
    "draft_edit",
    "draft_cancel",
    "confirm",
    "validate1",
    "validate",
    "refuse",
    "cancel",
}
_PENDING_TIME_OFF_STATES = {"draft", "draft_edit", "confirm", "validate1"}
_TERMINAL_TIME_OFF_STATES = {"refuse", "cancel"}

_UNDOABLE = {
    ("missing_wc", "assign"),
    ("missing_wc", "dismiss"),
    ("late", "absent"),
    ("late", "reason"),
    ("breakdown", "transfer"),
    ("breakdown", "dismiss"),
}
_UNDO_WINDOW = timedelta(minutes=10)


def _load_time_off_request(request_id: int) -> dict[str, Any] | None:
    from .. import db

    rows = db.query(
        "SELECT r.id, r.person_odoo_id, r.originating_kiosk_user, r.shape, "
        "r.holiday_status_id, r.date_from, r.date_to, r.hour_from, r.hour_to, "
        "r.note, r.state, r.odoo_leave_id, r.sync_error, r.local_record, "
        "COALESCE(p.name, '#' || r.person_odoo_id::text) AS person_name, "
        "COALESCE(lt.name, 'Time off') AS leave_type "
        "FROM time_off_requests r "
        "LEFT JOIN people p ON p.odoo_id = r.person_odoo_id "
        "LEFT JOIN leave_types_cache lt ON lt.holiday_status_id = r.holiday_status_id "
        "WHERE r.id = %s",
        (request_id,),
    )
    return rows[0] if rows else None


def _json_error(message: str, status_code: int) -> JSONResponse:
    return JSONResponse({"ok": False, "error": message}, status_code=status_code)


# Odoo's hr.leave ValidationError when the employee's Working Schedule has
# no attendance on the requested day(s). Locale-dependent by nature — the
# API user's language must stay English for this (and _friendly_odoo_error)
# to match.
_WORK_SCHEDULE_CONFLICT_SNIPPET = "not supposed to work during that period"


def _fault_text(e: Exception) -> str:
    """The useful text of an Odoo/xmlrpc exception, whitespace-collapsed.
    xmlrpc Faults stringify as the noisy ``<Fault N: '...'>`` repr; the
    real message lives on ``.faultString``."""
    msg = getattr(e, "faultString", None) or str(e)
    return " ".join(str(msg).split())


def _is_work_schedule_conflict(e: Exception) -> bool:
    return _WORK_SCHEDULE_CONFLICT_SNIPPET in _fault_text(e)


def _friendly_odoo_error(e: Exception) -> str:
    """Turn an Odoo/xmlrpc exception into a clean, user-facing message.

    Collapse whitespace so it fits the inbox's one-line status. For Odoo's
    work-schedule rejection ("not supposed to work during that period"),
    prepend a hint on how to resolve it — that one is a Working Schedule
    data issue HR fixes in Odoo, not something the manager can force here.
    (The approve path normally records such absences locally instead; this
    message only surfaces when that fallback itself could not settle the
    Odoo copy.)
    """
    msg = _fault_text(e)
    if _WORK_SCHEDULE_CONFLICT_SNIPPET in msg:
        return (
            "Odoo won't approve this — the employee's Working Schedule in "
            "Odoo doesn't include the requested day(s). Ask HR to fix their "
            "Working Schedule, then try again. Odoo said: " + msg
        )
    return msg


def _actor_from(request: Request) -> tuple[str | None, str | None]:
    return (
        getattr(request.state, "user_upn", None),
        getattr(request.state, "user_name", None),
    )


def _iso_day(value: Any) -> str | None:
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def _decision_time_label(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(plant_day.SITE_TZ).strftime("%-m/%-d %-I:%M %p")


def _group_archive_by_day(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Group newest-first events into plant-local day buckets for the archive."""
    today = plant_day.today()
    yesterday = today - timedelta(days=1)
    groups: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for r in rows:
        resolved = r["resolved_at"]
        if resolved.tzinfo is None:
            resolved = resolved.replace(tzinfo=UTC)
        local = resolved.astimezone(plant_day.SITE_TZ)
        day = local.date()
        if day == today:
            label = "Today"
        elif day == yesterday:
            label = "Yesterday"
        else:
            label = local.strftime("%A, %b %-d")
        if current is None or current["day"] != day.isoformat():
            current = {"day": day.isoformat(), "label": label, "events": []}
            groups.append(current)
        current["events"].append({
            "id": r["id"],
            "item_kind": r.get("item_kind"),
            "item_key": r.get("item_key"),
            "person_name": r.get("person_name"),
            "category_label": r.get("category_label"),
            "action": r.get("action"),
            "outcome": r.get("outcome"),
            "before_value": r.get("before_value"),
            "after_value": r.get("after_value"),
            "reason": r.get("reason"),
            "actor_name": r.get("actor_name"),
            "actor_upn": r.get("actor_upn"),
            "auto": r.get("actor_upn") is None,
            "time_label": local.strftime("%-I:%M %p"),
        })
    return groups


@router.get("/api/exceptions/archive")
def exceptions_archive_json(
    before: str | None = None,
    actor: str | None = None,
    include_auto: bool = False,
    limit: int = 200,
):
    before_dt = None
    if before:
        try:
            before_dt = datetime.fromisoformat(before)
        except ValueError:
            return _json_error("bad 'before' cursor", 400)
    limit = max(1, min(int(limit), 500))
    rows = inbox_log.archive(
        before=before_dt, actor_upn=actor, include_auto=include_auto, limit=limit
    )
    next_before = (
        rows[-1]["resolved_at"].isoformat() if len(rows) == limit and rows else None
    )
    return JSONResponse({
        "groups": _group_archive_by_day(rows),
        "next_before": next_before,
    })


def _hour_value(value: Any) -> float | None:
    if value is None:
        return None
    return float(value)


def _hour_label(value: Any) -> str:
    total_minutes = int(round(float(value) * 60))
    hour = (total_minutes // 60) % 24
    minute = total_minutes % 60
    suffix = "AM" if hour < 12 else "PM"
    display_hour = hour % 12 or 12
    return f"{display_hour}:{minute:02d} {suffix}"


def _decision_date_label(row: dict[str, Any]) -> str:
    start = _iso_day(row.get("date_from")) or ""
    end = _iso_day(row.get("date_to")) or ""
    label = f"{start} to {end}" if end and end != start else start
    if row.get("hour_from") is not None and row.get("hour_to") is not None:
        label += f" - {_hour_label(row['hour_from'])} to {_hour_label(row['hour_to'])}"
    return label


def _decision_summary(
    row: dict[str, Any],
    *,
    action: str,
    result_state: str,
    reason: str | None,
    actor_upn: str | None,
    actor_name: str | None,
    source: str | None,
) -> dict[str, Any]:
    decided_at = plant_day.now()
    return {
        "action": action,
        "person_name": row.get("person_name"),
        "leave_type": row.get("leave_type"),
        "date_from": _iso_day(row.get("date_from")),
        "date_to": _iso_day(row.get("date_to")),
        "hour_from": _hour_value(row.get("hour_from")),
        "hour_to": _hour_value(row.get("hour_to")),
        "date_label": _decision_date_label(row),
        "reason": reason,
        "actor_name": actor_name,
        "actor_upn": actor_upn,
        "source": source,
        "result_state": result_state,
        "decided_at": decided_at.isoformat(),
        "decided_label": _decision_time_label(decided_at),
    }


def _refresh_time_off_surfaces() -> None:
    from .. import _http_cache
    from .staffing import _bust_after_mutation

    _bust_after_mutation()
    _http_cache.invalidate_all_cache()


def _sync_to_odoo_if_needed(row: dict[str, Any]) -> dict[str, Any] | JSONResponse:
    """Make sure a pending local draft/edit has an Odoo leave id before action."""
    if row.get("odoo_leave_id") is not None and row.get("state") != "draft_edit":
        return row

    from .. import time_off_sync

    time_off_sync.push_one(int(row["id"]))
    refreshed = _load_time_off_request(int(row["id"]))
    if refreshed is None:
        return _json_error("request was removed during sync", 409)
    if refreshed.get("odoo_leave_id") is None:
        return _json_error(refreshed.get("sync_error") or "request is not synced to Odoo yet", 409)
    return refreshed


def _set_time_off_state(old: dict[str, Any], state: str) -> None:
    from .. import db, time_off_sync

    # local_record = FALSE: a state set through a route matches what Odoo
    # holds (or is about to hold), so a previously locally-owned row hands
    # ownership back to the poller.
    db.execute(
        "UPDATE time_off_requests SET state = %s, synced_to_odoo = TRUE, "
        "sync_error = NULL, local_record = FALSE, "
        "last_pushed_at = now(), updated_at = now() "
        "WHERE id = %s",
        (state, old["id"]),
    )
    new = dict(old)
    new["state"] = state
    time_off_sync.cascade_on_state_change(old, new)
    _refresh_time_off_surfaces()


_LOCAL_RECORD_DECISION_REASON = (
    "Recorded in Plant Manager only — Odoo Working Schedule does not "
    "include the requested day(s)"
)
_LOCAL_RECORD_CHATTER = (
    "Approved and recorded in GPI Plant Manager. Odoo could not validate "
    "this request because the employee's Working Schedule does not include "
    "the requested day(s), so this Odoo copy was closed as refused. The "
    "Plant Manager record is authoritative for this absence."
)
_LOCAL_RECORD_WARNING = (
    "Odoo couldn't validate this (the Working Schedule doesn't include the "
    "day(s)); recorded here instead and the Odoo copy was closed with a note."
)


def _record_time_off_locally(old: dict[str, Any]) -> None:
    """Sibling of ``_set_time_off_state`` for the local-record fallback:
    approve the row locally and flag it ``local_record`` so the poller
    neither overwrites nor deletes it. ``sync_error`` is cleared — the
    kiosk detail page renders it as a red error, and the why lives in the
    decision audit, the inbox log, and the Odoo chatter note."""
    from .. import db, time_off_sync

    db.execute(
        "UPDATE time_off_requests SET state = 'validate', "
        "local_record = TRUE, synced_to_odoo = TRUE, sync_error = NULL, "
        "last_pushed_at = now(), updated_at = now() WHERE id = %s",
        (old["id"],),
    )
    new = dict(old)
    new["state"] = "validate"
    new["local_record"] = True
    time_off_sync.cascade_on_state_change(old, new)
    _refresh_time_off_surfaces()


def _approve_locally_despite_schedule_conflict(
    row: dict[str, Any],
    *,
    actor_upn: str | None,
    actor_name: str | None,
    source: str | None,
) -> JSONResponse | None:
    """Odoo won't validate a leave whose Working Schedule lacks the
    requested day(s) — record the absence locally instead of hard-failing.

    Order matters: (1) pre-suppress the would-be "denied" kiosk popup (the
    poller may observe the refuse before our local write lands), (2) refuse
    the Odoo copy — the only settled state Odoo allows here, (3) approve the
    local row as a poller-proof ``local_record``, (4) best-effort chatter
    note on the refused leave. Returns None when the Odoo refuse fails, so
    the caller falls back to the friendly 500 and nothing is half-recorded.
    """
    import logging

    from .. import employee_notifications, odoo_client

    log = logging.getLogger(__name__)
    leave_id = row.get("odoo_leave_id")
    try:
        employee_notifications.suppress_resolution(
            row["person_odoo_id"], row, kind="time_off_denied")
    except Exception:  # noqa: BLE001 — belt-and-braces guard, not load-bearing
        log.warning("denied-popup suppression failed for request %s",
                    row["id"], exc_info=True)
    if leave_id is not None:
        try:
            odoo_client.refuse_leave(int(leave_id))
        except Exception:  # noqa: BLE001 — abort: leave must not stay pending
            log.warning("local-record fallback aborted: Odoo refuse failed "
                        "for leave %s", leave_id, exc_info=True)
            try:
                # Don't leak the suppression row: a later genuine
                # Odoo-side denial of this still-pending request must
                # still be able to notify.
                employee_notifications.unsuppress_resolution(
                    row["id"], kind="time_off_denied")
            except Exception:  # noqa: BLE001
                log.warning("suppression cleanup failed for request %s",
                            row["id"], exc_info=True)
            return None
    _record_time_off_locally(row)
    if leave_id is not None:
        try:
            odoo_client.post_leave_message(int(leave_id), _LOCAL_RECORD_CHATTER)
        except Exception as e:  # noqa: BLE001 — record already settled
            log.warning("chatter post failed for leave %s (local record "
                        "still applied): %s", leave_id, e)
    time_off_audit.record_decision(
        request_id=row["id"],
        odoo_leave_id=leave_id,
        person_odoo_id=row.get("person_odoo_id"),
        person_name=row.get("person_name"),
        leave_type=row.get("leave_type"),
        date_from=row.get("date_from"),
        date_to=row.get("date_to"),
        hour_from=row.get("hour_from"),
        hour_to=row.get("hour_to"),
        action="approve",
        result_state="validate",
        reason=_LOCAL_RECORD_DECISION_REASON,
        actor_upn=actor_upn,
        actor_name=actor_name,
        source=source,
    )
    inbox_log.log_event_safe(
        item_kind="time_off",
        item_key=inbox_keys.time_off(row["id"]),
        person_name=row.get("person_name"),
        category_label="Time off",
        action="approve",
        outcome="Approved (recorded locally)",
        after_value="validate",
        actor_upn=actor_upn,
        actor_name=actor_name,
        source=source,
        reversible=False,
    )
    return JSONResponse({
        "ok": True,
        "state": "validate",
        "approved": True,
        "recorded_locally": True,
        "warning": _LOCAL_RECORD_WARNING,
        "decision": _decision_summary(
            row,
            action="approve",
            result_state="validate",
            reason=_LOCAL_RECORD_DECISION_REASON,
            actor_upn=actor_upn,
            actor_name=actor_name,
            source=source,
        ),
    })


def _approve_time_off_sync(
    request_id: int,
    actor_upn: str | None = None,
    actor_name: str | None = None,
    source: str | None = None,
) -> JSONResponse:
    from .. import odoo_client

    row = _load_time_off_request(request_id)
    if row is None:
        return _json_error("request not found", 404)
    state = str(row.get("state") or "")
    if state == "validate":
        return JSONResponse({"ok": True, "state": state, "no_op": True})
    if state in _TERMINAL_TIME_OFF_STATES or state == "draft_cancel":
        return _json_error("request is already closed", 409)
    if state not in _PENDING_TIME_OFF_STATES:
        return _json_error(f"request cannot be approved from state {state}", 409)

    synced = _sync_to_odoo_if_needed(row)
    if isinstance(synced, JSONResponse):
        return synced
    try:
        final_state = odoo_client.approve_leave(int(synced["odoo_leave_id"])) or synced["state"]
    except Exception as e:
        if _is_work_schedule_conflict(e):
            fallback = _approve_locally_despite_schedule_conflict(
                synced, actor_upn=actor_upn, actor_name=actor_name,
                source=source)
            if fallback is not None:
                return fallback
        return _json_error(_friendly_odoo_error(e), 500)
    if final_state not in _TIME_OFF_STATES:
        return _json_error(f"unexpected Odoo state {final_state}", 500)
    _set_time_off_state(row, final_state)
    time_off_audit.record_decision(
        request_id=row["id"],
        odoo_leave_id=synced.get("odoo_leave_id"),
        person_odoo_id=row.get("person_odoo_id"),
        person_name=row.get("person_name"),
        leave_type=row.get("leave_type"),
        date_from=row.get("date_from"),
        date_to=row.get("date_to"),
        hour_from=row.get("hour_from"),
        hour_to=row.get("hour_to"),
        action="approve",
        result_state=final_state,
        reason=None,
        actor_upn=actor_upn,
        actor_name=actor_name,
        source=source,
    )
    inbox_log.log_event_safe(
        item_kind="time_off",
        item_key=inbox_keys.time_off(row["id"]),
        person_name=row.get("person_name"),
        category_label="Time off",
        action="approve",
        outcome="Approved",
        after_value=final_state,
        actor_upn=actor_upn,
        actor_name=actor_name,
        source=source,
        reversible=False,
    )
    return JSONResponse({
        "ok": True,
        "state": final_state,
        "approved": final_state == "validate",
        "decision": _decision_summary(
            row,
            action="approve",
            result_state=final_state,
            reason=None,
            actor_upn=actor_upn,
            actor_name=actor_name,
            source=source,
        ),
    })


@router.post("/api/exceptions/time-off/{request_id}/approve")
async def approve_time_off_request(request_id: int, request: Request):
    try:
        body = await request.json()
    except Exception:
        body = {}
    source = (body or {}).get("source")
    actor_upn, actor_name = _actor_from(request)
    return await asyncio.to_thread(
        _approve_time_off_sync,
        request_id,
        actor_upn,
        actor_name,
        source,
    )


def _refuse_time_off_sync(
    request_id: int,
    reason: str,
    actor_upn: str | None = None,
    actor_name: str | None = None,
    source: str | None = None,
) -> JSONResponse:
    import logging

    from .. import odoo_client

    reason = (reason or "").strip()
    if not reason:
        return _json_error("a reason is required to deny", 400)

    row = _load_time_off_request(request_id)
    if row is None:
        return _json_error("request not found", 404)
    state = str(row.get("state") or "")
    if state in _TERMINAL_TIME_OFF_STATES:
        return JSONResponse({"ok": True, "state": state, "no_op": True})

    leave_id = row.get("odoo_leave_id")
    if leave_id is not None:
        # Locally-recorded approvals already hold a refused Odoo copy —
        # action_refuse on it raises. The deny settles locally; the reason
        # still lands on the Odoo chatter below.
        if not row.get("local_record"):
            try:
                odoo_client.refuse_leave(int(leave_id))
            except Exception as e:
                return _json_error(_friendly_odoo_error(e), 500)
        try:
            odoo_client.post_leave_message(int(leave_id), reason)
        except Exception as e:  # noqa: BLE001 -- denial already succeeded
            logging.getLogger(__name__).warning(
                "chatter post failed for leave %s (denial still applied): %s",
                leave_id,
                e,
            )
    _set_time_off_state(row, "refuse")
    time_off_audit.record_decision(
        request_id=row["id"],
        odoo_leave_id=leave_id,
        person_odoo_id=row.get("person_odoo_id"),
        person_name=row.get("person_name"),
        leave_type=row.get("leave_type"),
        date_from=row.get("date_from"),
        date_to=row.get("date_to"),
        hour_from=row.get("hour_from"),
        hour_to=row.get("hour_to"),
        action="deny",
        result_state="refuse",
        reason=reason,
        actor_upn=actor_upn,
        actor_name=actor_name,
        source=source,
    )
    inbox_log.log_event_safe(
        item_kind="time_off",
        item_key=inbox_keys.time_off(row["id"]),
        person_name=row.get("person_name"),
        category_label="Time off",
        action="deny",
        outcome="Denied",
        reason=reason,
        actor_upn=actor_upn,
        actor_name=actor_name,
        source=source,
        reversible=False,
    )
    return JSONResponse({
        "ok": True,
        "state": "refuse",
        "decision": _decision_summary(
            row,
            action="deny",
            result_state="refuse",
            reason=reason,
            actor_upn=actor_upn,
            actor_name=actor_name,
            source=source,
        ),
    })


@router.post("/api/exceptions/time-off/{request_id}/refuse")
async def refuse_time_off_request(request_id: int, request: Request):
    try:
        body = await request.json()
    except Exception:
        body = {}
    reason = (body or {}).get("reason", "")
    source = (body or {}).get("source")
    actor_upn, actor_name = _actor_from(request)
    return await asyncio.to_thread(
        _refuse_time_off_sync,
        request_id,
        reason,
        actor_upn,
        actor_name,
        source,
    )


def _event_detail(ev: dict[str, Any]) -> dict:
    """ev['detail'] is written as jsonb; normalize to a dict regardless of
    whether the driver returned it already-parsed or as a raw JSON string."""
    import json
    detail = ev.get("detail")
    if isinstance(detail, dict):
        return detail
    if isinstance(detail, str) and detail:
        try:
            return json.loads(detail)
        except (TypeError, ValueError):
            return {}
    return {}


def _reverse_event(ev: dict[str, Any]) -> None:
    """Reverse a resolved inbox action. Assumes (item_kind, action) is undoable."""
    from .. import absence_sync, late_report, machine_breakdown, missing_wc, odoo_client, wc_attributions

    kind, action, key = ev["item_kind"], ev["action"], ev["item_key"]
    if kind == "missing_wc":
        att_id = int(key.split(":")[1])
        if action == "assign":
            odoo_client.clear_attendance_wc(att_id)
        missing_wc.unresolve(att_id)
    elif kind == "late":
        _, emp_id, day = key.split(":", 2)
        if action == "absent":
            absence_sync.refuse_absence_leave(
                late_report.odoo_leave_id_for_absence(day, emp_id)
            )
            late_report.undo_absent(day, emp_id)
        elif action == "reason":
            late_report.undo_late_arrival(day, emp_id)
    elif kind == "breakdown":
        detail = _event_detail(ev)
        if action == "transfer":
            closed_id, new_id = detail.get("closed_id"), detail.get("new_id")
            if new_id is not None:
                odoo_client.undo_transfer(closed_id, new_id)
            attribution_id = detail.get("attribution_id")
            if attribution_id is not None:
                wc_attributions.reopen_breakdown(attribution_id)
        elif action == "dismiss":
            incident_id = detail.get("incident_id")
            machine_breakdown.reopen_incident(incident_id)
            for row in detail.get("rows") or []:
                wc_attributions.add(
                    day=row["day"], wc_name=row["wc_name"], person_name=row["person_name"],
                    start_utc=row["start_utc"], end_utc=row.get("end_utc"),
                    source=wc_attributions.BREAKDOWN_SOURCE, breakdown_id=incident_id,
                )


def _undo_sync(
    event_id: int,
    actor_upn: str | None = None,
    actor_name: str | None = None,
) -> JSONResponse:
    from .. import inbox_log

    ev = inbox_log.get_event(event_id)
    if ev is None:
        return _json_error("event not found", 404)
    if ev.get("undone_at") is not None:
        return _json_error("already undone", 409)
    if (ev["item_kind"], ev["action"]) not in _UNDOABLE:
        return _json_error("this action can't be undone", 400)
    resolved = ev["resolved_at"]
    if resolved.tzinfo is None:
        resolved = resolved.replace(tzinfo=UTC)
    if plant_day.now() - resolved > _UNDO_WINDOW:
        return _json_error("undo window expired", 409)
    try:
        _reverse_event(ev)
    except Exception as e:  # noqa: BLE001 -- surface reversal failure to caller
        return _json_error(_friendly_odoo_error(e), 500)
    undo_id = inbox_log.log_event_safe(
        item_kind=ev["item_kind"],
        item_key=ev["item_key"],
        person_name=ev.get("person_name"),
        category_label=ev.get("category_label"),
        action="undo",
        outcome="Undone",
        actor_upn=actor_upn,
        actor_name=actor_name,
        source="inbox",
    )
    inbox_log.mark_undone(event_id, undo_id)
    _refresh_time_off_surfaces()
    return JSONResponse({"ok": True})


@router.post("/api/exceptions/undo/{event_id}")
async def undo_inbox_event(event_id: int, request: Request):
    actor_upn, actor_name = _actor_from(request)
    return await asyncio.to_thread(_undo_sync, event_id, actor_upn, actor_name)


def _breakdown_transfer_sync(body: dict, actor_upn=None, actor_name=None) -> JSONResponse:
    """Blocking half of /api/exceptions/breakdown/transfer: caps the
    operator's breakdown exclusion at the detected stop time, then runs the
    normal transfer chokepoint from that same timestamp."""
    from .. import inbox_keys, inbox_log, machine_breakdown, staffing_transfer, wc_attributions

    incident_id = body.get("incident_id")
    person_name = str(body.get("person_name") or "").strip()
    to_wc = str(body.get("to_wc") or "").strip()
    if not incident_id or not person_name or not to_wc:
        return _json_error("incident_id, person_name, and to_wc are required", 400)

    incident = machine_breakdown.get_incident(incident_id)
    if incident is None:
        return _json_error("incident not found", 404)

    transfer_at = incident["detected_stop_utc"]
    row = wc_attributions.open_breakdown_row(incident["day"], incident["wc_name"], person_name)
    if row is not None:
        wc_attributions.cap_breakdown(row["id"], transfer_at)

    # Note: the breakdown-row cap above is not rolled back if decide_and_apply
    # fails below; a failed transfer leaves the operator's exclusion capped
    # as-if-departed. Retrying the transfer re-caps harmlessly (cap_breakdown
    # is idempotent on an already-closed row) but does not reopen the
    # exclusion -- a manual wc_attributions.reopen_breakdown would be needed
    # if this matters in practice. Matches this file's existing convention
    # for Odoo-calling handlers (_approve_time_off_sync, _refuse_time_off_sync):
    # log and return a friendly error, no rollback.
    try:
        result = staffing_transfer.decide_and_apply(person_name, to_wc, transfer_at)
    except Exception as e:
        return _json_error(_friendly_odoo_error(e), 500)

    eid = inbox_log.log_event_safe(
        item_kind="breakdown",
        item_key=inbox_keys.breakdown(incident["wc_name"], incident["detected_stop_utc"].isoformat(), person_name),
        person_name=person_name,
        category_label="Machine Breakdown",
        action="transfer",
        outcome=f"Transferred to {to_wc}",
        after_value=to_wc,
        actor_upn=actor_upn,
        actor_name=actor_name,
        source="inbox",
        reversible=True,
        detail={
            "closed_id": result.get("closed_id"),
            "new_id": result.get("new_id"),
            "attribution_id": row["id"] if row is not None else None,
        },
    )
    return JSONResponse({"ok": True, "event_id": eid, "transfer": result.get("transfer")})


@router.post("/api/exceptions/breakdown/transfer")
async def breakdown_transfer(request: Request):
    """Transfer an operator off a broken machine.

    Body (JSON): {incident_id, person_name, to_wc}
    """
    from .. import inbox_log
    body = await request.json()
    actor_upn, actor_name = inbox_log.actor_from(request)
    return await asyncio.to_thread(_breakdown_transfer_sync, body, actor_upn, actor_name)


def _breakdown_snooze_sync(body: dict) -> JSONResponse:
    """Blocking half of /api/exceptions/breakdown/snooze."""
    from .. import machine_breakdown

    incident_id = body.get("incident_id")
    person_name = str(body.get("person_name") or "").strip()
    if not incident_id or not person_name:
        return _json_error("incident_id and person_name are required", 400)
    machine_breakdown.snooze_operator(incident_id, person_name)
    return JSONResponse({"ok": True})


@router.post("/api/exceptions/breakdown/snooze")
async def breakdown_snooze(request: Request):
    """Silence one operator's row on a breakdown card for 15 minutes.

    Body (JSON): {incident_id, person_name}
    """
    body = await request.json()
    return await asyncio.to_thread(_breakdown_snooze_sync, body)


def _breakdown_dismiss_sync(body: dict, actor_upn=None, actor_name=None) -> JSONResponse:
    """Blocking half of /api/exceptions/breakdown/dismiss ("Not a
    breakdown"): snapshots the incident's exclusion rows into the undo
    detail BEFORE deleting them, then resolves the incident."""
    from .. import inbox_keys, inbox_log, machine_breakdown, wc_attributions

    incident_id = body.get("incident_id")
    if not incident_id:
        return _json_error("incident_id is required", 400)
    incident = machine_breakdown.get_incident(incident_id)
    if incident is None:
        return _json_error("incident not found", 404)

    # for_day()'s SELECT does not include `day` (it's the WHERE filter, not a
    # returned column) -- stamp it back on before storing, since undo needs
    # the full row shape to re-insert via wc_attributions.add().
    snapshot_rows = [
        {**r, "day": incident["day"]}
        for r in wc_attributions.for_day(incident["day"])
        if r.get("wc_name") == incident["wc_name"] and r.get("source") == wc_attributions.BREAKDOWN_SOURCE
    ]
    wc_attributions.delete_breakdown_rows_for_incident(incident_id)
    machine_breakdown.resolve_incident(incident_id, "dismissed")

    eid = inbox_log.log_event_safe(
        item_kind="breakdown",
        item_key=inbox_keys.breakdown(incident["wc_name"], incident["detected_stop_utc"].isoformat()),
        person_name=None,
        category_label="Machine Breakdown",
        action="dismiss",
        outcome="Not a breakdown",
        actor_upn=actor_upn,
        actor_name=actor_name,
        source="inbox",
        reversible=True,
        detail={"rows": snapshot_rows, "incident_id": incident_id},
    )
    return JSONResponse({"ok": True, "event_id": eid})


@router.post("/api/exceptions/breakdown/dismiss")
async def breakdown_dismiss(request: Request):
    """"Not a breakdown": resolve the incident and delete its exclusion rows.

    Body (JSON): {incident_id}
    """
    from .. import inbox_log
    body = await request.json()
    actor_upn, actor_name = inbox_log.actor_from(request)
    return await asyncio.to_thread(_breakdown_dismiss_sync, body, actor_upn, actor_name)


def _breakdown_report_sync(body: dict) -> JSONResponse:
    """Blocking half of /api/exceptions/breakdown/report (the manual
    "+ Report a breakdown" button)."""
    from .. import machine_breakdown, staffing

    wc_name = str(body.get("wc_name") or "").strip()
    if wc_name not in {loc.name for loc in staffing.LOCATIONS}:
        return _json_error("unknown work center", 400)
    result = machine_breakdown.report_manual(wc_name)
    return JSONResponse(result)


@router.post("/api/exceptions/breakdown/report")
async def breakdown_report(request: Request):
    """Manually report a machine as broken down.

    Body (JSON): {wc_name}
    """
    body = await request.json()
    return await asyncio.to_thread(_breakdown_report_sync, body)
