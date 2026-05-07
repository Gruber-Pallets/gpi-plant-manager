"""Player card route. The People directory was folded into the People
Matrix — clicking a name in the matrix opens that person's player card.
The /staffing/people path now redirects to the matrix so old bookmarks
keep working.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from .. import staffing
from ..deps import client, templates

router = APIRouter()


@router.get("/staffing/people")
def staffing_people_redirect():
    return RedirectResponse(url="/staffing/skills", status_code=307)


@router.get("/staffing/people/{name}", response_class=HTMLResponse)
def staffing_player_card(
    request: Request,
    name: str,
    start: str | None = Query(default=None),
    end: str | None = Query(default=None),
):
    from .. import production_history
    today = datetime.now(timezone.utc).date()
    end_d = date.fromisoformat(end) if end else today
    start_d = date.fromisoformat(start) if start else (end_d - timedelta(days=29))
    range_out = production_history.attribution_range(start_d, end_d, client)
    person = range_out.get(name, {})
    rows = sorted(
        ({"wc": wc, **t} for wc, t in person.items()),
        key=lambda r: -r["units"],
    )
    total_units    = sum(r["units"] for r in rows)
    total_downtime = sum(r["downtime"] for r in rows)
    total_days     = sum(r["days_worked"] for r in rows)
    roster = {p.name: p for p in staffing.load_roster()}
    p = roster.get(name)
    skills = []
    if p:
        skills = sorted(
            ((s, lvl) for s, lvl in p.skills.items() if lvl >= 1),
            key=lambda kv: -kv[1],
        )
    # Per-day-per-WC rows for the breakdown table. Newest first.
    day_rows: list[dict] = []
    for day, daily in production_history.attribution_per_day(start_d, end_d, client):
        person_data = daily.get(name, {})
        for wc_name, totals in person_data.items():
            day_rows.append({
                "date": day.isoformat(),
                "wc": wc_name,
                "units": totals["units"],
                "downtime": totals["downtime"],
            })
    day_rows.sort(key=lambda r: (r["date"], r["wc"]), reverse=True)
    # Attendance history — absences + late arrivals in the range.
    from .. import late_report
    abs_rows = late_report.absences_history_for_name(name, start_d, end_d)
    late_rows = late_report.late_arrivals_history_for_name(name, start_d, end_d)
    attendance_rows = (
        [{"date": r["day"].isoformat(), "type": "Absent", "reason": r["reason"] or ""}
         for r in abs_rows]
        + [{"date": r["day"].isoformat(), "type": "Late", "reason": r["reason"] or ""}
           for r in late_rows]
    )
    attendance_rows.sort(key=lambda r: (r["date"], r["type"]), reverse=True)
    total_absent_days = len(abs_rows)
    total_late_days = len(late_rows)
    return templates.TemplateResponse(
        request,
        "player_card.html",
        {
            "active": "people",
            "name": name,
            "start": start_d.isoformat(),
            "end": end_d.isoformat(),
            "today": today.isoformat(),
            "rows": rows,
            "total_units": round(total_units, 1),
            "total_downtime": round(total_downtime, 1),
            "total_days": total_days,
            "skills": skills,
            "day_rows": day_rows,
            "attendance_rows": attendance_rows,
            "total_absent_days": total_absent_days,
            "total_late_days": total_late_days,
        },
    )


@router.post("/api/staffing/people/{name}/attendance/reason")
async def update_attendance_reason(name: str, request: Request):
    """Inline-edit endpoint for the Attendance section's Reason cells.

    Body (JSON): {date: YYYY-MM-DD, type: "absent"|"late", reason: str}
    Updates the matching row in manual_absences or late_arrivals.
    """
    from .. import db
    body = await request.json()
    try:
        d = date.fromisoformat(str(body.get("date") or ""))
    except ValueError:
        return JSONResponse({"ok": False, "error": "bad date"}, status_code=400)
    type_ = str(body.get("type") or "").strip().lower()
    if type_ not in ("absent", "late"):
        return JSONResponse({"ok": False, "error": "type must be absent or late"}, status_code=400)
    reason_raw = body.get("reason")
    reason = (str(reason_raw).strip() or None) if reason_raw is not None else None
    table = "manual_absences" if type_ == "absent" else "late_arrivals"
    db.execute(
        f"UPDATE {table} SET reason = %s WHERE day = %s AND name = %s",
        (reason, d, name),
    )
    return JSONResponse({"ok": True})
