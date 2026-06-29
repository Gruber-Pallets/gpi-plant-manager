"""Daily Exception Inbox."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
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
}
_UNDO_WINDOW = timedelta(minutes=10)


def _load_time_off_request(request_id: int) -> dict[str, Any] | None:
    from .. import db

    rows = db.query(
        "SELECT r.id, r.person_odoo_id, r.originating_kiosk_user, r.shape, "
        "r.holiday_status_id, r.date_from, r.date_to, r.hour_from, r.hour_to, "
        "r.note, r.state, r.odoo_leave_id, r.sync_error, "
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


def _friendly_odoo_error(e: Exception) -> str:
    """Turn an Odoo/xmlrpc exception into a clean, user-facing message.

    xmlrpc Faults stringify as the noisy ``<Fault N: '...'>`` repr (with
    literal ``\\n``); the useful text lives on ``.faultString``. Collapse
    whitespace so it fits the inbox's one-line status. For Odoo's
    work-schedule rejection ("not supposed to work during that period"),
    prepend a hint on how to resolve it — that one is a Working Schedule
    data issue HR fixes in Odoo, not something the manager can force here.
    """
    msg = getattr(e, "faultString", None) or str(e)
    msg = " ".join(str(msg).split())
    if "not supposed to work during that period" in msg:
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
        value = value.replace(tzinfo=timezone.utc)
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
            resolved = resolved.replace(tzinfo=timezone.utc)
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

    db.execute(
        "UPDATE time_off_requests SET state = %s, synced_to_odoo = TRUE, "
        "sync_error = NULL, last_pushed_at = now(), updated_at = now() "
        "WHERE id = %s",
        (state, old["id"]),
    )
    new = dict(old)
    new["state"] = state
    time_off_sync.cascade_on_state_change(old, new)
    _refresh_time_off_surfaces()


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


def _reverse_event(ev: dict[str, Any]) -> None:
    """Reverse a resolved inbox action. Assumes (item_kind, action) is undoable."""
    from .. import absence_sync, late_report, missing_wc, odoo_client

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
        resolved = resolved.replace(tzinfo=timezone.utc)
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
