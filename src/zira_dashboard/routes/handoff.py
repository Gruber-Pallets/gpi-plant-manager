"""Daily shift handoff log."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Form, Request
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


def _recent_handoffs(limit: int = 10) -> list[dict]:
    from .. import db

    rows = db.query(
        "SELECT id, handoff_date, shift_label, created_by, notes, open_total, "
        "urgent_total, source_errors, created_at "
        "FROM plant_shift_handoffs "
        "ORDER BY created_at DESC LIMIT %s",
        (limit,),
    )
    out = []
    for row in rows:
        source_errors = row.get("source_errors") or []
        out.append({
            **row,
            "created_at_label": _created_at_label(row.get("created_at")),
            "has_source_errors": bool(source_errors),
            "source_error_label": ", ".join(e.get("source", "") for e in source_errors),
        })
    return out


def _create_handoff(*, shift_label: str, created_by: str, notes: str) -> dict:
    from .. import db

    snapshot = exception_inbox.build_snapshot()
    source_errors = snapshot.get("source_errors") or []
    shift_label = (shift_label or "Day").strip()[:80] or "Day"
    created_by = (created_by or "Unknown").strip()[:160] or "Unknown"
    notes = (notes or "").strip()
    rows = db.query(
        "INSERT INTO plant_shift_handoffs "
        "(handoff_date, shift_label, created_by, notes, open_total, urgent_total, "
        "source_errors, exception_snapshot) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb) "
        "RETURNING id, handoff_date, shift_label, created_by, notes, open_total, "
        "urgent_total, source_errors, created_at",
        (
            plant_day.today(),
            shift_label,
            created_by,
            notes,
            int(snapshot.get("total") or 0),
            int(snapshot.get("urgent_total") or 0),
            json.dumps(source_errors),
            json.dumps(snapshot, default=str),
        ),
    )
    return rows[0]


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
            "saved": saved,
            "default_created_by": _created_by(request),
        },
    )


@router.post("/handoff")
def create_handoff_form(
    request: Request,
    shift_label: str = Form("Day"),
    created_by: str = Form(""),
    notes: str = Form(""),
):
    row = _create_handoff(
        shift_label=shift_label,
        created_by=_created_by(request, created_by),
        notes=notes,
    )
    return RedirectResponse(url=f"/handoff?saved={row['id']}", status_code=303)


@router.post("/api/handoff")
async def create_handoff_json(request: Request):
    body: dict[str, Any] = await request.json()
    row = await asyncio.to_thread(
        _create_handoff,
        shift_label=str(body.get("shift_label") or "Day"),
        created_by=_created_by(request, str(body.get("created_by") or "")),
        notes=str(body.get("notes") or ""),
    )
    return JSONResponse({"ok": True, "id": row["id"]})
