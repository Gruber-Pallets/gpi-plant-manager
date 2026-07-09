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

from .. import production_metrics, shift_config, staffing
from ..deps import templates
from ..plant_day import today as plant_today

router = APIRouter()

# Forklift player-card lookback for the windowed stat block (calls / on-time /
# avg response / utilization). Independent of the production card's range.
_FORKLIFT_LOOKBACK_DAYS = 90


def _resolve_forklift_name(name: str) -> str | None:
    """Map a plant `name` to its forklift driver name (manual name-map
    override, else the person's first name when unique in the roster —
    how the forklift app labels them). Returns None for a shared first
    name so one driver's forklift stats never land on the wrong card.
    Shared with the forklift leaderboard via forklift_store."""
    from .. import forklift_store

    return forklift_store.resolve_plant_to_forklift(name)


def _forklift_for_person(name: str, today: date, cfg) -> dict | None:
    """Resolve a plant `name` to a forklift driver and return that driver's
    recent forklift stats + earned trophies, or None when the person doesn't
    map to a driver. Defensive — any store/compute failure yields None so the
    block is simply hidden (never 500s the player card)."""
    try:
        from .. import forklift_awards, forklift_score, forklift_store

        forklift_name = _resolve_forklift_name(name)

        start = today - timedelta(days=_FORKLIFT_LOOKBACK_DAYS)
        rows = forklift_store.driver_days_between(start, today)
        mine = [
            r for r in rows
            if r.get("name") == forklift_name or r.get("driver_id") == forklift_name
        ]
        if not mine:
            return None

        calls = sum(int(r.get("calls") or 0) for r in mine)
        on_time = sum(int(r.get("on_time") or 0) for r in mine)
        late = sum(int(r.get("late") or 0) for r in mine)
        ms_weighted = sum((r.get("avg_ms") or 0) * (r.get("calls") or 0) for r in mine)
        denom = on_time + late
        ontime_pct = (on_time / denom * 100) if denom else 0.0
        avg_ms = (ms_weighted / calls) if calls else 0
        # Utilization: use the most recent day that reports one (it's a
        # point-in-time ratio, not summable).
        util = 0.0
        for r in sorted(mine, key=lambda r: r["day"], reverse=True):
            if r.get("utilization_pct"):
                util = float(r["utilization_pct"])
                break

        # Best-day GOAT score over the window (None below the gate), carrying
        # the winning day's component breakdown for the card's compact line.
        best_score = None
        best_components = None
        for r in mine:
            b = forklift_score.daily_score(r, cfg)
            if b is not None and (best_score is None or b.score > best_score):
                best_score = b.score
                best_components = b.components

        # Awards match on the forklift display name carried in
        # forklift_driver_daily, so look them up by `forklift_name` — passing
        # the plant `name` would silently miss every driver whose forklift
        # name differs from their plant name.
        trophies = forklift_awards.awards_earned_by_driver(forklift_name, today, cfg)
        return {
            "calls": calls,
            "ontime_pct": ontime_pct,
            "avg_ms": avg_ms,
            "utilization_pct": util,
            "best_score": best_score,
            "best_components": best_components,
            "trophies": trophies,
        }
    except Exception:  # noqa: BLE001 - hide the block rather than 500
        return None


def _forklift_days_for_person(
    name: str, start_d: date, end_d: date, cfg
) -> list[dict]:
    """Per-day forklift performances for a mapped driver over [start_d, end_d],
    newest first. One dict per day the driver had calls (>0) — sub-gate days are
    listed with score/components None. Mirrors the repair/dismantling per-day
    breakdown. Defensive: any store/compute failure yields [] (section hidden)."""
    try:
        from .. import forklift_score, forklift_store

        forklift_name = _resolve_forklift_name(name)
        if forklift_name is None:
            return []
        rows = forklift_store.driver_days_between(start_d, end_d)
        mine = [
            r for r in rows
            if r.get("name") == forklift_name or r.get("driver_id") == forklift_name
        ]
        days: list[dict] = []
        for r in sorted(mine, key=lambda r: r["day"], reverse=True):
            calls = int(r.get("calls") or 0)
            if calls <= 0:
                continue  # no activity that day -> not a performance
            on_time = int(r.get("on_time") or 0)
            late = int(r.get("late") or 0)
            denom = on_time + late
            b = forklift_score.daily_score(r, cfg)
            days.append({
                "date": r["day"].isoformat(),
                "calls": calls,
                "on_time": on_time,
                "late": late,
                "ontime_pct": (on_time / denom * 100) if denom else 0.0,
                "avg_ms": r.get("avg_ms") or 0,
                "max_ms": r.get("max_ms") or 0,
                "utilization_pct": float(r.get("utilization_pct") or 0),
                "score": b.score if b is not None else None,
                "components": b.components if b is not None else None,
            })
        return days
    except Exception:  # noqa: BLE001 - hide the section rather than 500
        return []


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
    metric_records_all = production_history.normalized_daily_records(start_d, end_d)
    metric_records = [r for r in metric_records_all if r["person"] == name]
    standard_full_day_hours = shift_config.productive_minutes_per_day() / 60.0
    rows = sorted(
        ({"wc": wc, **t} for wc, t in person.items()),
        key=lambda r: -r["units"],
    )
    for r in rows:
        metric = production_metrics.normalized_average_by_person(
            metric_records,
            wc_names={r["wc"]},
            standard_full_day_hours=standard_full_day_hours,
        )
        if metric:
            r["full_day_avg"] = round(metric[0]["avg_units"], 1)
            r["days_worked"] = metric[0]["days"]
        else:
            r["full_day_avg"] = None
            r["days_worked"] = 0
    total_units    = sum(r["units"] for r in rows)
    total_downtime = sum(r["downtime"] for r in rows)
    total_days     = sum(r["days_worked"] for r in rows)

    # Group averages — one entry per registered group with qualified days.
    # Order follows
    # registered_groups() (which sorts by lower(name)).
    from .. import work_centers_store
    group_avgs: list[dict] = []
    for group_name in work_centers_store.registered_groups():
        wc_names = {loc.name for loc in work_centers_store.members("group", group_name)}
        if not wc_names:
            continue
        metric = production_metrics.normalized_average_by_person(
            metric_records,
            wc_names=wc_names,
            standard_full_day_hours=standard_full_day_hours,
        )
        mine = next((m for m in metric if m["name"] == name), None)
        if mine:
            group_avgs.append({
                "name": group_name,
                "avg_units": round(mine["avg_units"], 1),
                "days": mine["days"],
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

    # Forklift block — only rendered for people who map to a forklift driver.
    # Resolve the score config (don't-care algo_throughput; score_config reads
    # only the score-override fields), falling back to the algorithm defaults
    # if settings are unreachable so a settings hiccup never hides a mapped
    # driver's stats. _forklift_for_person is itself defensive (returns None).
    from .. import forklift_score
    _cfg = forklift_score.DEFAULT_SCORE_CONFIG
    try:
        from .. import forklift_settings
        _cfg = forklift_settings.resolve(
            forklift_settings.current(), algo_throughput=0.0
        ).score_config()
    except Exception:  # noqa: BLE001 - fall back to default score config
        pass
    forklift = _forklift_for_person(name, today, _cfg)
    # Per-day forklift performances over the page's picker range — only for
    # people who map to a forklift driver (forklift block present). Empty list
    # when the driver had no forklift days in the selected range.
    forklift_days = (
        _forklift_days_for_person(name, start_d, end_d, _cfg) if forklift else []
    )

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
            "forklift": forklift,
            "forklift_days": forklift_days,
            "forklift_min_calls": _cfg.min_calls,
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
