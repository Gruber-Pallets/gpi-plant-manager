"""Authenticated manager actions for recorded-absence PTO requests."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from .. import (
    _http_cache,
    absence_pto,
    absence_pto_conversion,
    absence_pto_review,
    inbox_log,
    staffing,
)


router = APIRouter()
_log = logging.getLogger(__name__)


def _response(body: dict[str, Any], status_code: int = 200) -> JSONResponse:
    return JSONResponse(body, status_code=status_code)


def _refresh_surfaces(request) -> None:
    """Refresh the manager queue and Staffing after a committed decision."""
    try:
        staffing.invalidate_schedule_cache(request.absence_day)
    except Exception:  # noqa: BLE001 - a committed decision stays successful
        _log.warning("absence PTO Staffing cache refresh failed", exc_info=True)
    try:
        _http_cache.invalidate_all_cache()
    except Exception:  # noqa: BLE001 - a committed decision stays successful
        _log.warning("absence PTO page cache refresh failed", exc_info=True)


def _approve_sync(
    request_id: int,
    actor_upn: str | None,
    actor_name: str | None,
    source: str | None,
) -> JSONResponse:
    result = absence_pto_conversion.approve(
        request_id, actor_upn, actor_name, source
    )
    if result.request is not None:
        _refresh_surfaces(result.request)
    if result.status == "approved":
        return _response(
            {"ok": True, "status": result.status, "message": result.message}
        )
    if result.status == "needs_review":
        return _response(
            {"ok": True, "status": result.status, "warning": result.message}
        )
    key = "error" if result.status == "busy" else "warning"
    return _response(
        {"ok": False, "status": result.status, key: result.message},
        status_code=409,
    )


def _deny_sync(
    request_id: int,
    actor_upn: str | None,
    actor_name: str | None,
    reason: str,
    source: str | None,
) -> JSONResponse:
    try:
        denied = absence_pto.deny(
            request_id, actor_upn, actor_name, reason, source
        )
    except absence_pto.DecisionError as error:
        return _response({"ok": False, "error": str(error)}, status_code=409)
    _refresh_surfaces(denied)
    return _response(
        {
            "ok": True,
            "status": "denied",
            "message": "The past PTO request was denied.",
        }
    )


def _handled_sync(
    request_id: int,
    actor_upn: str | None,
    actor_name: str | None,
    note: str,
) -> JSONResponse:
    if not actor_upn or not actor_name:
        return _response(
            {"ok": False, "error": "A signed-in manager is required."},
            status_code=403,
        )
    try:
        result = absence_pto_review.resolve_manually(
            request_id, actor_upn, actor_name, note
        )
    except ValueError as error:
        return _response({"ok": False, "error": str(error)}, status_code=400)
    if result.request is not None and result.status == "resolved_manually":
        _refresh_surfaces(result.request)
        return _response(
            {"ok": True, "status": result.status, "message": result.message}
        )
    return _response(
        {"ok": False, "status": result.status, "error": result.message},
        status_code=409,
    )


async def _body(request: Request) -> dict[str, Any]:
    try:
        value = await request.json()
    except Exception:  # noqa: BLE001 - malformed JSON becomes an empty action body
        return {}
    return value if isinstance(value, dict) else {}


@router.post("/api/exceptions/absence-pto/{request_id}/approve")
async def approve_absence_pto(request_id: int, request: Request):
    body = await _body(request)
    actor_upn, actor_name = inbox_log.actor_from(request)
    return await asyncio.to_thread(
        _approve_sync,
        request_id,
        actor_upn,
        actor_name,
        body.get("source"),
    )


@router.post("/api/exceptions/absence-pto/{request_id}/deny")
async def deny_absence_pto(request_id: int, request: Request):
    body = await _body(request)
    reason = str(body.get("reason") or "").strip()
    if not reason:
        return _response(
            {"ok": False, "error": "A reason is required to deny."},
            status_code=400,
        )
    actor_upn, actor_name = inbox_log.actor_from(request)
    return await asyncio.to_thread(
        _deny_sync,
        request_id,
        actor_upn,
        actor_name,
        reason,
        body.get("source"),
    )


@router.post("/api/exceptions/absence-pto/{request_id}/handled")
async def handle_absence_pto(request_id: int, request: Request):
    body = await _body(request)
    note = str(body.get("note") or "").strip()
    if not note:
        return _response(
            {"ok": False, "error": "A note is required to mark this handled."},
            status_code=400,
        )
    actor_upn, actor_name = inbox_log.actor_from(request)
    return await asyncio.to_thread(
        _handled_sync, request_id, actor_upn, actor_name, note
    )
