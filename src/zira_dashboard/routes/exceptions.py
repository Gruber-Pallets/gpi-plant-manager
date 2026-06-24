"""Daily Exception Inbox."""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse

from .. import exception_inbox, time_off_audit
from ..deps import templates

router = APIRouter()


@router.get("/exceptions", response_class=HTMLResponse)
def exceptions_page(request: Request):
    snapshot = exception_inbox.build_snapshot()
    initial_nav_summary = {
        "total": int(snapshot.get("total") or 0),
        "urgent_total": int(snapshot.get("urgent_total") or 0),
        "source_errors": snapshot.get("source_errors") or [],
    }
    return templates.TemplateResponse(
        request,
        "exceptions.html",
        {
            "snapshot": snapshot,
            "sections": snapshot["sections"],
            "initial_nav_summary": initial_nav_summary,
        },
    )


@router.get("/api/exceptions")
def exceptions_json():
    return JSONResponse(exception_inbox.build_snapshot())


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


def _actor_from(request: Request) -> tuple[str | None, str | None]:
    return (
        getattr(request.state, "user_upn", None),
        getattr(request.state, "user_name", None),
    )


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
        return _json_error(str(e), 500)
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
        action="approve",
        result_state=final_state,
        reason=None,
        actor_upn=actor_upn,
        actor_name=actor_name,
        source=source,
    )
    return JSONResponse({"ok": True, "state": final_state, "approved": final_state == "validate"})


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
            return _json_error(str(e), 500)
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
        action="deny",
        result_state="refuse",
        reason=reason,
        actor_upn=actor_upn,
        actor_name=actor_name,
        source=source,
    )
    return JSONResponse({"ok": True, "state": "refuse"})


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
