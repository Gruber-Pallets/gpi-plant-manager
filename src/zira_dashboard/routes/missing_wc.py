"""Missing-Work-Center alert endpoints: badge/modal read + assign + dismiss.

Mirrors routes/late_report.py. The READ is a cheap local-cache read (the warmer
owns the Odoo query). Assign writes the work center + resolved department onto
the Odoo hr.attendance via odoo_client.set_attendance_wc, then suppresses the
row. Odoo-origin records have no local kiosk punch, so there's nothing to
re-round — the department tag is the fix.
"""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

router = APIRouter()


@router.get("/api/missing-wc")
def missing_wc_json():
    """Badge/modal snapshot: {count, rows, work_centers}. All local reads."""
    from .. import missing_wc, staffing
    try:
        rows = missing_wc.current_rows()
    except Exception:
        rows = []
    return JSONResponse({
        "count": len(rows),
        "rows": rows,
        "work_centers": [loc.name for loc in staffing.LOCATIONS],
    })


def _assign_sync(body: dict) -> JSONResponse:
    """Blocking half of /missing-wc/assign (Odoo XML-RPC + Postgres write);
    runs in a worker thread via asyncio.to_thread."""
    from .. import missing_wc, odoo_client, staffing
    try:
        att_id = int(body.get("attendance_id"))
    except (TypeError, ValueError):
        return JSONResponse({"ok": False, "error": "bad attendance_id"}, status_code=400)
    wc_name = str(body.get("wc_name") or "").strip()
    name = (str(body.get("name") or "").strip() or None)
    if wc_name not in {loc.name for loc in staffing.LOCATIONS}:
        return JSONResponse({"ok": False, "error": "unknown work center"}, status_code=400)
    try:
        odoo_client.set_attendance_wc(att_id, wc_name)
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)
    missing_wc.resolve(att_id, "assigned", name=name, wc_name=wc_name)
    return JSONResponse({"ok": True})


@router.post("/missing-wc/assign")
async def missing_wc_assign(request: Request):
    """Assign a work center to a flagged attendance record.

    Body (JSON): {attendance_id, wc_name, name?}
    """
    body = await request.json()
    return await asyncio.to_thread(_assign_sync, body)


def _dismiss_sync(body: dict) -> JSONResponse:
    """Blocking half of /missing-wc/dismiss (Postgres write); runs in a
    worker thread via asyncio.to_thread."""
    from .. import missing_wc
    try:
        att_id = int(body.get("attendance_id"))
    except (TypeError, ValueError):
        return JSONResponse({"ok": False, "error": "bad attendance_id"}, status_code=400)
    name = (str(body.get("name") or "").strip() or None)
    missing_wc.resolve(att_id, "dismissed", name=name)
    return JSONResponse({"ok": True})


@router.post("/missing-wc/dismiss")
async def missing_wc_dismiss(request: Request):
    """Dismiss a record that legitimately has no work center.

    Body (JSON): {attendance_id, name?}
    """
    body = await request.json()
    return await asyncio.to_thread(_dismiss_sync, body)
