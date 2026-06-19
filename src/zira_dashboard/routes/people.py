"""Player card route. The People directory was folded into the People
Matrix — clicking a name in the matrix opens that person's player card.
The /staffing/people path now redirects to the matrix so old bookmarks
keep working.
"""

from __future__ import annotations

import asyncio
from datetime import date, timedelta

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from .. import staffing
from ..deps import templates
from ..plant_day import today as plant_today

router = APIRouter()


@router.get("/staffing/people")
def staffing_people_landing():
    """Land on the first active roster member's card. With the player-card
    name picklist, the user can immediately switch to anyone else.
    """
    roster = staffing.load_roster()
    actives = sorted(
        (p.name for p in roster if p.active),
        key=str.lower,
    )
    if not actives:
        # Fallback: the matrix is the only place that handles an empty roster.
        return RedirectResponse(url="/staffing/skills", status_code=307)
    return RedirectResponse(
        url=f"/staffing/people/{actives[0]}",
        status_code=307,
    )


@router.get("/staffing/people/{name}", response_class=HTMLResponse)
def staffing_player_card(
    request: Request,
    name: str,
    start: str | None = Query(default=None),
    end: str | None = Query(default=None),
):
    from .. import production_history, _http_cache
    today = plant_today()
    end_d = date.fromisoformat(end) if end else today
    start_d = date.fromisoformat(start) if start else (end_d - timedelta(days=29))

    # Response cache, keyed per person + range. A today-inclusive range goes
    # in the 60s today bucket (busted by attribution/attendance/roster writes
    # via invalidate_today_cache); a past-only range is immutable for the
    # 5min past bucket (only the nightly precompute changes past attribution).
    includes_today = end_d >= today
    response_cache_key = ("player_card", name, start_d.isoformat(), end_d.isoformat())
    cached_resp = _http_cache.get_cached_response(
        response_cache_key, includes_today=includes_today
    )
    if cached_resp is not None:
        return cached_resp

    range_out = production_history.attribution_range(start_d, end_d)
    person = range_out.get(name, {})
    rows = sorted(
        ({"wc": wc, **t} for wc, t in person.items()),
        key=lambda r: -r["units"],
    )
    for r in rows:
        hrs = r.get("hours", 0.0)
        r["avg_pph"] = round(r["units"] / hrs, 1) if hrs > 0 else 0
    total_units    = sum(r["units"] for r in rows)
    total_downtime = sum(r["downtime"] for r in rows)
    total_days     = sum(r["days_worked"] for r in rows)

    # Group averages — one entry per registered group with hours > 0.
    # Hours-weighted pph across the group's WCs. Order follows
    # registered_groups() (which sorts by lower(name)).
    from .. import work_centers_store
    group_avgs: list[dict] = []
    for group_name in work_centers_store.registered_groups():
        wc_names = {loc.name for loc in work_centers_store.members("group", group_name)}
        if not wc_names:
            continue
        units_sum = 0.0
        hours_sum = 0.0
        for wc_name, totals in person.items():
            if wc_name in wc_names:
                units_sum += totals.get("units", 0.0)
                hours_sum += totals.get("hours", 0.0)
        if hours_sum > 0:
            group_avgs.append({
                "name": group_name,
                "pph": round(units_sum / hours_sum, 1),
            })
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
    for day, daily in production_history.attribution_per_day(start_d, end_d):
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
    # Roster names for the picklist — active people first (alphabetical),
    # so the dropdown lets you jump straight to anyone's card without
    # bouncing back through the matrix.
    roster_names = sorted(
        (p.name for p in roster.values() if p.active),
        key=str.lower,
    )
    from .. import awards
    awards_earned = awards.awards_earned_by(name, today)
    response = templates.TemplateResponse(
        request,
        "player_card.html",
        {
            "active": "people",
            "name": name,
            "start": start_d.isoformat(),
            "end": end_d.isoformat(),
            "today": today.isoformat(),
            "rows": rows,
            "group_avgs": group_avgs,
            "total_units": round(total_units, 1),
            "total_downtime": round(total_downtime, 1),
            "total_days": total_days,
            "skills": skills,
            "day_rows": day_rows,
            "attendance_rows": attendance_rows,
            "total_absent_days": total_absent_days,
            "total_late_days": total_late_days,
            "roster_names": roster_names,
            "awards_earned": awards_earned,
        },
    )
    _http_cache.set_cache_headers(response, includes_today=includes_today)
    _http_cache.store_cached_response(
        response_cache_key, includes_today=includes_today, response=response
    )
    return response


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

    def _work():
        db.execute(
            f"UPDATE {table} SET reason = %s WHERE day = %s AND name = %s",
            (reason, d, name),
        )
        from .. import _http_cache
        _http_cache.invalidate_today_cache()
        return JSONResponse({"ok": True})

    return await asyncio.to_thread(_work)
