"""Daily shift handoff log."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from .. import exception_inbox, plant_day
from ..deps import templates

router = APIRouter()


def _created_by(request: Request, submitted: str | None = None) -> str:
    submitted = (submitted or "").strip()
    if submitted:
        return submitted[:160]
    return (
        getattr(request.state, "user_name", None)
        or getattr(request.state, "user_upn", None)
        or "Unknown"
    )[:160]


def _created_at_label(value) -> str:
    if isinstance(value, datetime):
        return value.astimezone(plant_day.SITE_TZ).strftime("%-m/%-d %-I:%M %p")
    return str(value or "")


def _resolved_at_label(value) -> str:
    return _created_at_label(value)


def _json_value(value, fallback):
    if value in (None, ""):
        return fallback
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return fallback
    return value


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _shape_handoff_row(row: dict) -> dict:
    source_errors = _json_value(row.get("source_errors"), [])
    snapshot = _json_value(row.get("exception_snapshot"), {})
    source_errors = source_errors if isinstance(source_errors, list) else []
    shaped = {
        **row,
        "source_errors": source_errors,
        "exception_snapshot": snapshot if isinstance(snapshot, dict) else {},
        "created_at_label": _created_at_label(row.get("created_at")),
        "resolved_at_label": _resolved_at_label(row.get("resolved_at")),
        "has_source_errors": bool(source_errors),
        "source_error_label": ", ".join(e.get("source", "") for e in source_errors),
        "detail_href": f"/handoff/{row['id']}",
    }
    shaped["is_open_followup"] = bool(
        shaped.get("follow_up_required") and not shaped.get("resolved_at")
    )
    return shaped


def _recent_handoffs(limit: int = 10) -> list[dict]:
    from .. import db

    rows = db.query(
        "SELECT id, handoff_date, shift_label, created_by, notes, open_total, "
        "urgent_total, source_errors, follow_up_required, resolved_at, resolved_by, "
        "resolution_note, created_at "
        "FROM plant_shift_handoffs "
        "ORDER BY created_at DESC LIMIT %s",
        (limit,),
    )
    return [_shape_handoff_row(row) for row in rows]


def _open_followups(limit: int = 6) -> list[dict]:
    from .. import db

    rows = db.query(
        "SELECT id, handoff_date, shift_label, created_by, notes, open_total, "
        "urgent_total, source_errors, follow_up_required, resolved_at, resolved_by, "
        "resolution_note, created_at "
        "FROM plant_shift_handoffs "
        "WHERE follow_up_required = TRUE AND resolved_at IS NULL "
        "ORDER BY created_at DESC LIMIT %s",
        (limit,),
    )
    return [_shape_handoff_row(row) for row in rows]


def _load_handoff(handoff_id: int) -> dict | None:
    from .. import db

    rows = db.query(
        "SELECT id, handoff_date, shift_label, created_by, notes, open_total, "
        "urgent_total, source_errors, exception_snapshot, follow_up_required, "
        "resolved_at, resolved_by, resolution_note, created_at, updated_at "
        "FROM plant_shift_handoffs WHERE id = %s",
        (handoff_id,),
    )
    if not rows:
        return None
    return _shape_handoff_row(rows[0])


def _create_handoff(
    *,
    shift_label: str,
    created_by: str,
    notes: str,
    follow_up_required: bool = False,
) -> dict:
    from .. import db

    snapshot = exception_inbox.build_snapshot()
    source_errors = snapshot.get("source_errors") or []
    shift_label = (shift_label or "Day").strip()[:80] or "Day"
    created_by = (created_by or "Unknown").strip()[:160] or "Unknown"
    notes = (notes or "").strip()
    rows = db.query(
        "INSERT INTO plant_shift_handoffs "
        "(handoff_date, shift_label, created_by, notes, follow_up_required, "
        "open_total, urgent_total, source_errors, exception_snapshot) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb) "
        "RETURNING id, handoff_date, shift_label, created_by, notes, open_total, "
        "urgent_total, source_errors, follow_up_required, resolved_at, resolved_by, "
        "resolution_note, created_at",
        (
            plant_day.today(),
            shift_label,
            created_by,
            notes,
            follow_up_required,
            int(snapshot.get("total") or 0),
            int(snapshot.get("urgent_total") or 0),
            json.dumps(source_errors),
            json.dumps(snapshot, default=str),
        ),
    )
    return _shape_handoff_row(rows[0])


def _resolve_handoff(*, handoff_id: int, resolved_by: str, resolution_note: str) -> dict | None:
    from .. import db

    rows = db.query(
        "UPDATE plant_shift_handoffs SET "
        "resolved_at = now(), resolved_by = %s, resolution_note = %s, updated_at = now() "
        "WHERE id = %s AND follow_up_required = TRUE "
        "RETURNING id, handoff_date, shift_label, created_by, notes, open_total, "
        "urgent_total, source_errors, exception_snapshot, follow_up_required, "
        "resolved_at, resolved_by, resolution_note, created_at, updated_at",
        ((resolved_by or "Unknown").strip()[:160], (resolution_note or "").strip(), handoff_id),
    )
    return _shape_handoff_row(rows[0]) if rows else None


@router.get("/handoff", response_class=HTMLResponse)
def handoff_page(request: Request, saved: int | None = None):
    summary = exception_inbox.build_summary()
    return templates.TemplateResponse(
        request,
        "handoff.html",
        {
            "today": plant_day.today().isoformat(),
            "summary": summary,
            "recent": _recent_handoffs(),
            "open_followups": _open_followups(),
            "saved": saved,
            "default_created_by": _created_by(request),
        },
    )


@router.get("/handoff/{handoff_id}", response_class=HTMLResponse)
def handoff_detail_page(request: Request, handoff_id: int):
    row = _load_handoff(handoff_id)
    if row is None:
        raise HTTPException(status_code=404, detail="handoff not found")
    snapshot = row.get("exception_snapshot") or {}
    return templates.TemplateResponse(
        request,
        "handoff_detail.html",
        {
            "today": plant_day.today().isoformat(),
            "handoff": row,
            "snapshot": snapshot,
            "sections": snapshot.get("sections") or [],
        },
    )


@router.post("/handoff")
def create_handoff_form(
    request: Request,
    shift_label: str = Form("Day"),
    created_by: str = Form(""),
    notes: str = Form(""),
    follow_up_required: bool = Form(False),
):
    row = _create_handoff(
        shift_label=shift_label,
        created_by=_created_by(request, created_by),
        notes=notes,
        follow_up_required=follow_up_required,
    )
    return RedirectResponse(url=f"/handoff?saved={row['id']}", status_code=303)


@router.post("/handoff/{handoff_id}/resolve")
def resolve_handoff_form(
    request: Request,
    handoff_id: int,
    resolved_by: str = Form(""),
    resolution_note: str = Form(""),
):
    row = _resolve_handoff(
        handoff_id=handoff_id,
        resolved_by=_created_by(request, resolved_by),
        resolution_note=resolution_note,
    )
    if row is None:
        raise HTTPException(status_code=404, detail="handoff follow-up not found")
    return RedirectResponse(url=f"/handoff/{handoff_id}", status_code=303)


@router.post("/api/handoff")
async def create_handoff_json(request: Request):
    body: dict[str, Any] = await request.json()
    row = await asyncio.to_thread(
        _create_handoff,
        shift_label=str(body.get("shift_label") or "Day"),
        created_by=_created_by(request, str(body.get("created_by") or "")),
        notes=str(body.get("notes") or ""),
        follow_up_required=_as_bool(body.get("follow_up_required")),
    )
    return JSONResponse({"ok": True, "id": row["id"]})


@router.post("/api/handoff/{handoff_id}/resolve")
async def resolve_handoff_json(request: Request, handoff_id: int):
    body: dict[str, Any] = await request.json()
    row = await asyncio.to_thread(
        _resolve_handoff,
        handoff_id=handoff_id,
        resolved_by=_created_by(request, str(body.get("resolved_by") or "")),
        resolution_note=str(body.get("resolution_note") or ""),
    )
    if row is None:
        return JSONResponse({"ok": False, "error": "handoff follow-up not found"}, status_code=404)
    return JSONResponse({"ok": True, "id": row["id"]})
