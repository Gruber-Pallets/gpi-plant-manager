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
    body = await request.json()
    # The lookup + Odoo clock_out + resolve are all blocking (psycopg2 +
    # XML-RPC) — run them off the event loop so an Odoo round-trip can't
    # stall every in-flight request (same pattern as the other mutators).
    return await asyncio.to_thread(_correct_sync, body)


def _correct_sync(body: dict):
    from .. import missed_punch_out, odoo_client
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
        odoo_client.clock_out(att_id, corrected)
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)
    missed_punch_out.correct(att_id, corrected)
    return JSONResponse({"ok": True})
