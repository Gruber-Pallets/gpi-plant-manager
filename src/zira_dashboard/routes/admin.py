"""Admin / operational endpoints. Diagnostics + manual backfill jobs.

Not user-facing. Each endpoint is GET-able from a browser and returns
JSON so Dale can paste the result into a chat to share status.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from threading import Lock

from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse

from .. import shift_config, staffing
from ..deps import client
from ..leaderboard import cached_leaderboard, leaderboard
from ..stations import Station

router = APIRouter()


# Cap per-request work so a single backfill stays under typical browser
# timeouts. ~90 working days × ~1s/day with 3 days in parallel ≈ 30s.
MAX_BACKFILL_DAYS_PER_REQUEST = 90


def _metered_stations() -> list[Station]:
    return [
        Station(meter_id=loc.meter_id, name=loc.name, category=loc.skill, cell=loc.bay)
        for loc in staffing.LOCATIONS
        if loc.meter_id
    ]


@router.get("/admin/data-status")
def data_status(
    start: str = Query(default="2024-01-01"),
    end: str | None = Query(default=None),
):
    """Quick counts: for each day in [start, end], does it have Zira
    cached data? Does it have a saved schedule? Does it have any
    saved assignments? No fetching, just inspecting the DB.
    """
    from .. import db

    today = datetime.now(timezone.utc).date()
    try:
        start_d = date.fromisoformat(start)
        end_d = date.fromisoformat(end) if end else today
    except ValueError:
        return JSONResponse({"error": "start/end must be YYYY-MM-DD"}, status_code=400)

    zira_rows = db.query(
        "SELECT day, COUNT(*) AS station_count "
        "FROM zira_daily_cache WHERE day BETWEEN %s AND %s "
        "GROUP BY day ORDER BY day",
        (start_d, end_d),
    )
    sched_rows = db.query(
        "SELECT day, published FROM schedules WHERE day BETWEEN %s AND %s ORDER BY day",
        (start_d, end_d),
    )
    asg_rows = db.query(
        "SELECT day, COUNT(*) AS assignment_count "
        "FROM schedule_assignments WHERE day BETWEEN %s AND %s "
        "GROUP BY day ORDER BY day",
        (start_d, end_d),
    )

    zira_by_day = {r["day"].isoformat(): r["station_count"] for r in zira_rows}
    sched_by_day = {r["day"].isoformat(): bool(r["published"]) for r in sched_rows}
    asg_by_day = {r["day"].isoformat(): r["assignment_count"] for r in asg_rows}

    return JSONResponse({
        "range": {"start": start_d.isoformat(), "end": end_d.isoformat()},
        "zira_cached_days": len(zira_by_day),
        "schedule_rows": len(sched_by_day),
        "schedules_published": sum(1 for v in sched_by_day.values() if v),
        "schedules_draft": sum(1 for v in sched_by_day.values() if not v),
        "days_with_assignments": len(asg_by_day),
        "by_day": {
            day: {
                "zira_stations": zira_by_day.get(day, 0),
                "schedule_published": sched_by_day.get(day),  # None if no schedule row
                "assignments": asg_by_day.get(day, 0),
            }
            for day in sorted(set(zira_by_day) | set(sched_by_day) | set(asg_by_day))
        },
    })


@router.get("/admin/zira-probe")
def zira_probe(
    day: str = Query(...),
    meter_id: str | None = Query(default=None),
    sample_rows: int = Query(default=5),
):
    """Hit Zira directly for one (meter_id, day) and dump the raw response.

    Diagnostic only. Use to figure out whether Zira itself has data for a
    given day or whether our filtering is dropping it.

    `meter_id` defaults to the first metered LOCATION (the spec sheet's
    Bay-1 Repair 1). Pass an explicit one to probe a specific WC.
    `sample_rows` caps how many rows from Zira's response we echo back —
    default 5 to keep the JSON small.
    """
    try:
        d = date.fromisoformat(day)
    except ValueError:
        return JSONResponse({"error": "day must be YYYY-MM-DD"}, status_code=400)

    if meter_id is None:
        for loc in staffing.LOCATIONS:
            if loc.meter_id:
                meter_id = loc.meter_id
                wc_name = loc.name
                break
        else:
            return JSONResponse({"error": "no metered stations configured"}, status_code=500)
    else:
        wc_name = next(
            (loc.name for loc in staffing.LOCATIONS if loc.meter_id == meter_id),
            "(unknown)",
        )

    # Same window construction as cached_leaderboard.
    from ..leaderboard import day_window_utc
    start_iso, end_iso = day_window_utc(d)

    try:
        payload = client.get_readings(
            meter_id=meter_id,
            end_time=end_iso,
            start_time=start_iso,
            limit=500,
        )
    except Exception as e:
        return JSONResponse({
            "error": f"Zira call failed: {e}",
            "meter_id": meter_id,
            "wc_name": wc_name,
            "day": d.isoformat(),
            "window": {"start": start_iso, "end": end_iso},
        }, status_code=502)

    # Normalize the envelope so the response is predictable regardless of
    # whether Zira returned a list or a dict-wrapped envelope.
    if isinstance(payload, dict):
        rows = payload.get("data") or []
        cursor = payload.get("lastValue")
        envelope_type = "dict"
    elif isinstance(payload, list):
        rows = payload
        cursor = None
        envelope_type = "list"
    else:
        rows = []
        cursor = None
        envelope_type = type(payload).__name__

    if not isinstance(rows, list):
        rows = []

    # Tally event-type / status / units distribution so the response is
    # diagnostic-rich without requiring a huge sample dump.
    units_total = 0
    rows_with_units = 0
    event_counts: dict[str, int] = {}
    status_counts: dict[str, int] = {}
    submitted_by_counts: dict[str, int] = {}
    for r in rows:
        u = r.get("units")
        u_int = int(u) if isinstance(u, (int, float)) else 0
        if u_int > 0:
            rows_with_units += 1
            units_total += u_int
        event_counts[str(r.get("event"))] = event_counts.get(str(r.get("event")), 0) + 1
        status_counts[str(r.get("status"))] = status_counts.get(str(r.get("status")), 0) + 1
        submitted_by_counts[str(r.get("submitted_by"))] = submitted_by_counts.get(str(r.get("submitted_by")), 0) + 1

    # First N rows that actually have units > 0 — the real production events,
    # if any. Used to compare against meta/shift-end rows in `sample_rows`.
    rows_with_units_sample = [r for r in rows if isinstance(r.get("units"), (int, float)) and r.get("units") > 0][:sample_rows]

    return JSONResponse({
        "meter_id": meter_id,
        "wc_name": wc_name,
        "day": d.isoformat(),
        "window": {"start": start_iso, "end": end_iso},
        "envelope_type": envelope_type,
        "row_count": len(rows),
        "rows_with_units": rows_with_units,
        "units_total": units_total,
        "event_counts": event_counts,
        "status_counts": status_counts,
        "submitted_by_counts": submitted_by_counts,
        "cursor": cursor,
        "sample_rows": rows[:sample_rows],
        "rows_with_units_sample": rows_with_units_sample,
    })


@router.get("/admin/zira-backfill")
def zira_backfill(
    start: str = Query(...),
    end: str = Query(...),
):
    """Proactively populate zira_daily_cache for [start, end] inclusive.

    Idempotent — already-cached days are no-op'd by cached_leaderboard's
    Postgres-first lookup. Capped at MAX_BACKFILL_DAYS_PER_REQUEST per
    request to stay under typical browser timeouts; for longer ranges,
    invoke multiple times with different windows.

    Skips weekends and today (today is always live; the in-process
    cache handles it).

    Returns JSON with counts and a list of dates that returned zero
    units (those days might genuinely have had no production, or might
    indicate a Zira gap worth investigating).
    """
    try:
        start_d = date.fromisoformat(start)
        end_d = date.fromisoformat(end)
    except ValueError:
        return JSONResponse({"error": "start/end must be YYYY-MM-DD"}, status_code=400)

    days_in_range = (end_d - start_d).days + 1
    if days_in_range > MAX_BACKFILL_DAYS_PER_REQUEST:
        return JSONResponse({
            "error": f"range too large ({days_in_range} days). "
                     f"Max {MAX_BACKFILL_DAYS_PER_REQUEST} days per request — "
                     f"call multiple times with smaller windows."
        }, status_code=400)

    today = datetime.now(timezone.utc).date()
    stations = _metered_stations()
    if not stations:
        return JSONResponse({"error": "no metered stations configured"}, status_code=500)

    work_days = shift_config.work_weekdays()
    target_days: list[date] = []
    cursor = start_d
    while cursor <= end_d:
        if cursor.weekday() in work_days and cursor < today:
            target_days.append(cursor)
        cursor += timedelta(days=1)

    if not target_days:
        return JSONResponse({
            "days_checked": 0,
            "with_units": 0,
            "no_units": [],
            "errors": [],
            "note": "no work-days in range (weekends + today are always skipped)",
        })

    counts_lock = Lock()
    no_units: list[str] = []
    errors: list[dict] = []
    with_units = [0]
    days_checked = [0]

    def _do_day(d: date):
        try:
            # Bypass cached_leaderboard's Postgres-first lookup — we may be
            # backfilling specifically because the cache has stale/empty
            # data we want to overwrite. Call live Zira and persist
            # unconditionally; save_day uses ON CONFLICT DO UPDATE so the
            # row gets replaced with the fresh payload.
            result = leaderboard(client, stations, d)
            total = sum(r.units for r in result)
            if result:
                from .. import _zira_persist
                _zira_persist.save_day(result, d)
                # Also invalidate the in-process _PAST_CACHE entry so the
                # next pageview gets the fresh payload from Postgres
                # rather than the (possibly empty) in-memory cache.
                from ..leaderboard import _PAST_CACHE
                key = (tuple(sorted(s.meter_id for s in stations)), d.isoformat(), False)
                _PAST_CACHE.invalidate(key)
            with counts_lock:
                days_checked[0] += 1
                if total > 0:
                    with_units[0] += 1
                else:
                    no_units.append(d.isoformat())
        except Exception as e:
            with counts_lock:
                days_checked[0] += 1
                errors.append({"day": d.isoformat(), "error": str(e)[:200]})

    # 3 days in parallel × 10 stations/day inside leaderboard.py = 30 concurrent
    # Zira calls. Leaves headroom under most rate-limit ceilings.
    with ThreadPoolExecutor(max_workers=3) as pool:
        list(pool.map(_do_day, target_days))

    return JSONResponse({
        "range": {"start": start_d.isoformat(), "end": end_d.isoformat()},
        "days_checked": days_checked[0],
        "with_units": with_units[0],
        "no_units": no_units,
        "errors": errors,
    })
