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


def _default_shift_label(now: datetime | None = None) -> str:
    now = now or plant_day.now()
    local_now = now.astimezone(plant_day.SITE_TZ) if now.tzinfo else now
    if local_now.weekday() >= 5:
        return "Weekend"
    hour = local_now.hour
    if 5 <= hour < 15:
        return "Day"
    if 15 <= hour < 23:
        return "Evening"
    return "Night"


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


_SOURCE_SECTION_IDS = {
    "Assignments To Do": "assignments",
    "Late / Absence": "late",
    "Missing Work Center": "missing_wc",
    "Missed Punch Out": "missed_punch_out",
    "Pending Time Off": "time_off",
}


def _snapshot_row_keys(snapshot: dict) -> set[str]:
    keys: set[str] = set()
    for section in snapshot.get("sections") or []:
        for row in section.get("rows") or []:
            key = row.get("row_key")
            if key:
                keys.add(str(key))
    return keys


def _degraded_section_ids(snapshot: dict) -> set[str]:
    out: set[str] = set()
    for err in snapshot.get("source_errors") or []:
        source = err.get("source") if isinstance(err, dict) else None
        section_id = _SOURCE_SECTION_IDS.get(str(source or ""))
        if section_id:
            out.add(section_id)
    return out


def _annotate_snapshot_sections(
    sections: list[dict],
    *,
    current_keys: set[str],
    degraded_section_ids: set[str],
) -> list[dict]:
    annotated: list[dict] = []
    for section in sections:
        section_id = str(section.get("id") or "")
        rows = []
        for row in section.get("rows") or []:
            shaped = dict(row)
            key = shaped.get("row_key")
            if key:
                if section_id in degraded_section_ids:
                    shaped["current_status"] = "unknown"
                    shaped["current_status_label"] = "Check unavailable"
                elif str(key) in current_keys:
                    shaped["current_status"] = "still_open"
                    shaped["current_status_label"] = "Still open"
                else:
                    shaped["current_status"] = "cleared"
                    shaped["current_status_label"] = "Cleared"
            rows.append(shaped)
        annotated.append({**section, "rows": rows})
    return annotated


def _snapshot_status_summary(sections: list[dict]) -> dict:
    counts = {"still_open": 0, "cleared": 0, "unknown": 0}
    for section in sections:
        for row in section.get("rows") or []:
            status = row.get("current_status")
            if status in counts:
                counts[status] += 1

    parts = []
    if counts["still_open"]:
        parts.append(f"{counts['still_open']} still open")
    if counts["cleared"]:
        parts.append(f"{counts['cleared']} cleared")
    if counts["unknown"]:
        noun = "check" if counts["unknown"] == 1 else "checks"
        parts.append(f"{counts['unknown']} {noun} unavailable")

    total = sum(counts.values())
    return {**counts, "total": total, "label": " · ".join(parts)}


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


def _open_followups_with_count(limit: int = 6) -> tuple[list[dict], int]:
    from .. import db

    rows = db.query(
        "SELECT id, handoff_date, shift_label, created_by, notes, open_total, "
        "urgent_total, source_errors, follow_up_required, resolved_at, resolved_by, "
        "resolution_note, created_at, COUNT(*) OVER () AS total_count "
        "FROM plant_shift_handoffs "
        "WHERE follow_up_required = TRUE AND resolved_at IS NULL "
        "ORDER BY created_at DESC LIMIT %s",
        (limit,),
    )
    if not rows:
        return [], 0
    return [_shape_handoff_row(row) for row in rows], int(rows[0].get("total_count") or 0)


def _open_followup_count() -> int:
    from .. import db

    rows = db.query(
        "SELECT COUNT(*) AS count FROM plant_shift_handoffs "
        "WHERE follow_up_required = TRUE AND resolved_at IS NULL"
    )
    return int((rows[0] if rows else {}).get("count") or 0)


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


def _update_handoff_notes(*, handoff_id: int, notes: str) -> dict | None:
    from .. import db

    rows = db.query(
        "UPDATE plant_shift_handoffs SET notes = %s, updated_at = now() "
        "WHERE id = %s "
        "RETURNING id, handoff_date, shift_label, created_by, notes, open_total, "
        "urgent_total, source_errors, exception_snapshot, follow_up_required, "
        "resolved_at, resolved_by, resolution_note, created_at, updated_at",
        ((notes or "").strip(), handoff_id),
    )
    return _shape_handoff_row(rows[0]) if rows else None


def _mark_handoff_followup(*, handoff_id: int) -> dict | None:
    from .. import db

    rows = db.query(
        "UPDATE plant_shift_handoffs SET "
        "follow_up_required = TRUE, resolved_at = NULL, resolved_by = '', "
        "resolution_note = '', updated_at = now() "
        "WHERE id = %s "
        "RETURNING id, handoff_date, shift_label, created_by, notes, open_total, "
        "urgent_total, source_errors, exception_snapshot, follow_up_required, "
        "resolved_at, resolved_by, resolution_note, created_at, updated_at",
        (handoff_id,),
    )
    return _shape_handoff_row(rows[0]) if rows else None


@router.get("/handoff", response_class=HTMLResponse)
def handoff_page(request: Request, saved: int | None = None):
    summary = exception_inbox.build_summary()
    open_followups, open_followup_count = _open_followups_with_count()
    return templates.TemplateResponse(
        request,
        "handoff.html",
        {
            "today": plant_day.today().isoformat(),
            "summary": summary,
            "recent": _recent_handoffs(),
            "open_followups": open_followups,
            "open_followup_count": open_followup_count,
            "saved": saved,
            "default_created_by": _created_by(request),
            "default_shift_label": _default_shift_label(),
        },
    )


@router.get("/handoff/{handoff_id}", response_class=HTMLResponse)
def handoff_detail_page(request: Request, handoff_id: int):
    row = _load_handoff(handoff_id)
    if row is None:
        raise HTTPException(status_code=404, detail="handoff not found")
    snapshot = row.get("exception_snapshot") or {}
    sections = snapshot.get("sections") or []
    try:
        current_snapshot = exception_inbox.build_snapshot()
        current_keys = _snapshot_row_keys(current_snapshot)
        degraded_section_ids = _degraded_section_ids(current_snapshot)
    except Exception:  # noqa: BLE001 -- detail history should still render if live checks fail
        current_keys = set()
        degraded_section_ids = {str(section.get("id") or "") for section in sections}
    sections = _annotate_snapshot_sections(
        sections,
        current_keys=current_keys,
        degraded_section_ids=degraded_section_ids,
    )
    status_summary = _snapshot_status_summary(sections)
    return templates.TemplateResponse(
        request,
        "handoff_detail.html",
        {
            "today": plant_day.today().isoformat(),
            "handoff": row,
            "snapshot": snapshot,
            "sections": sections,
            "snapshot_status_summary": status_summary,
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


@router.post("/handoff/{handoff_id}/notes")
def update_handoff_notes_form(
    handoff_id: int,
    notes: str = Form(""),
):
    row = _update_handoff_notes(handoff_id=handoff_id, notes=notes)
    if row is None:
        raise HTTPException(status_code=404, detail="handoff not found")
    return RedirectResponse(url=f"/handoff/{handoff_id}", status_code=303)


@router.post("/handoff/{handoff_id}/follow-up")
def mark_handoff_followup_form(handoff_id: int):
    row = _mark_handoff_followup(handoff_id=handoff_id)
    if row is None:
        raise HTTPException(status_code=404, detail="handoff not found")
    return RedirectResponse(url=f"/handoff/{handoff_id}", status_code=303)


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


@router.get("/api/handoff/summary")
def handoff_summary_json():
    return JSONResponse({"open_followups": _open_followup_count()})


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
