"""Daily Exception Inbox."""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse

from .. import exception_inbox
from ..deps import templates

router = APIRouter()


@router.get("/exceptions", response_class=HTMLResponse)
def exceptions_page(request: Request):
    snapshot = exception_inbox.build_snapshot()
    return templates.TemplateResponse(
        request,
        "exceptions.html",
        {
            "snapshot": snapshot,
            "sections": snapshot["sections"],
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
        "SELECT id, person_odoo_id, originating_kiosk_user, shape, "
        "holiday_status_id, date_from, date_to, hour_from, hour_to, "
        "note, state, odoo_leave_id, sync_error "
        "FROM time_off_requests WHERE id = %s",
        (request_id,),
    )
    return rows[0] if rows else None


def _json_error(message: str, status_code: int) -> JSONResponse:
    return JSONResponse({"ok": False, "error": message}, status_code=status_code)


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


def _approve_time_off_sync(request_id: int) -> JSONResponse:
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
    return JSONResponse({"ok": True, "state": final_state, "approved": final_state == "validate"})


@router.post("/api/exceptions/time-off/{request_id}/approve")
async def approve_time_off_request(request_id: int):
    return await asyncio.to_thread(_approve_time_off_sync, request_id)


def _refuse_time_off_sync(request_id: int) -> JSONResponse:
    from .. import odoo_client

    row = _load_time_off_request(request_id)
    if row is None:
        return _json_error("request not found", 404)
    state = str(row.get("state") or "")
    if state in _TERMINAL_TIME_OFF_STATES:
        return JSONResponse({"ok": True, "state": state, "no_op": True})

    if row.get("odoo_leave_id") is not None:
        try:
            odoo_client.refuse_leave(int(row["odoo_leave_id"]))
        except Exception as e:
            return _json_error(str(e), 500)
    _set_time_off_state(row, "refuse")
    return JSONResponse({"ok": True, "state": "refuse"})


@router.post("/api/exceptions/time-off/{request_id}/refuse")
async def refuse_time_off_request(request_id: int):
    return await asyncio.to_thread(_refuse_time_off_sync, request_id)
