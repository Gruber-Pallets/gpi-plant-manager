"""Missed-punch-out alert endpoints: badge/modal read + time correction.

Mirrors routes/missing_wc.py. The READ is a cheap local DB read. The correct
endpoint validates the entered time is after clock-in and on the check-in day,
rewrites the Odoo hr.attendance check_out (exactly, no rounding) via
odoo_client.clock_out, then resolves the flag.
"""
from __future__ import annotations

from datetime import datetime, time as _time

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

router = APIRouter()


def _clock_label(dt) -> str:
    """'4:30 PM' for an already site-local datetime — platform-safe (no %-I)."""
    return dt.strftime("%I:%M %p").lstrip("0")


@router.get("/api/missed-punch-out")
def missed_punch_out_json():
    """Badge/modal snapshot: {count, rows}. All local reads."""
    from .. import missed_punch_out
    try:
        rows = missed_punch_out.current_rows()
    except Exception:
        rows = []
    return JSONResponse({"count": len(rows), "rows": rows})


@router.post("/missed-punch-out/correct")
async def missed_punch_out_correct(request: Request):
    """Rewrite a flagged attendance's check_out to the entered time.

    Body (JSON): {attendance_id, time}  where time is "HH:MM" (24-hour).
    """
    import asyncio
    from .. import inbox_log
    body = await request.json()
    actor_upn, actor_name = inbox_log.actor_from(request)
    # The lookup + Odoo clock_out + resolve are all blocking (psycopg2 +
    # XML-RPC) — run them off the event loop so an Odoo round-trip can't
    # stall every in-flight request (same pattern as the other mutators).
    return await asyncio.to_thread(_correct_sync, body, actor_upn, actor_name)


def _correct_sync(body: dict, actor_upn=None, actor_name=None):
    from .. import inbox_keys, inbox_log, missed_punch_out, odoo_client
    from ..shift_config import SITE_TZ
    try:
        att_id = int(body.get("attendance_id"))
    except (TypeError, ValueError):
        return JSONResponse({"ok": False, "error": "bad attendance_id"}, status_code=400)
    raw = str(body.get("time") or "").strip()
    try:
        hh, mm = raw.split(":")
        parsed = _time(int(hh), int(mm))
    except (ValueError, TypeError):
        return JSONResponse({"ok": False, "error": "bad time"}, status_code=400)

    row = missed_punch_out.get_unresolved(att_id)
    if row is None:
        return JSONResponse({"ok": False, "error": "not found"}, status_code=404)

    check_in = row["check_in"].astimezone(SITE_TZ)
    midnight = row["auto_closed_at"].astimezone(SITE_TZ)
    corrected = datetime.combine(check_in.date(), parsed, tzinfo=SITE_TZ)
    if not (check_in < corrected <= midnight):
        return JSONResponse(
            {"ok": False, "error": "time must be after clock-in and on the clock-in day"},
            status_code=400)

    try:
        odoo_client.clock_out(att_id, corrected, mode="manual")
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)
    missed_punch_out.correct(att_id, corrected)
    inbox_log.log_event_safe(
        item_kind="missed_punch_out",
        item_key=inbox_keys.missed_punch_out(att_id),
        person_name=row.get("name"),
        category_label="Missed punch out",
        action="correct",
        outcome=f"Punch-out corrected to {_clock_label(corrected)}",
        before_value=_clock_label(midnight),
        after_value=_clock_label(corrected),
        actor_upn=actor_upn,
        actor_name=actor_name,
        source="inbox",
        reversible=False,
    )
    return JSONResponse({"ok": True})
