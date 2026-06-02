"""Admin / operational endpoints. Diagnostics + manual backfill jobs.

Not user-facing. Each endpoint is GET-able from a browser and returns
JSON so Dale can paste the result into a chat to share status.
"""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from threading import Lock

from fastapi import APIRouter, Form, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from .. import device_tokens as _dt, shift_config, staffing
from ..deps import client, templates
from ..leaderboard import leaderboard
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


@router.get("/admin/person-state")
def person_state(name: str = Query(...)):
    """Diagnostic: dump the people-row(s) matching `name` + every
    manual_absences / late_arrivals row referencing that name. Used to
    debug why an archived person still shows in the Time Off section.
    """
    from .. import db
    people_rows = db.query(
        "SELECT id, odoo_id, name, active, excluded, reserve, last_pulled_at "
        "FROM people WHERE lower(name) LIKE lower(%s)",
        (f"%{name}%",),
    )
    absent_rows = db.query(
        "SELECT day, emp_id, name, declared_at, reason "
        "FROM manual_absences WHERE lower(name) LIKE lower(%s) "
        "ORDER BY day DESC LIMIT 50",
        (f"%{name}%",),
    )
    late_rows = db.query(
        "SELECT day, emp_id, name, declared_at, reason "
        "FROM late_arrivals WHERE lower(name) LIKE lower(%s) "
        "ORDER BY day DESC LIMIT 50",
        (f"%{name}%",),
    )
    return JSONResponse({
        "query": name,
        "people": [dict(r, last_pulled_at=str(r.get("last_pulled_at"))) for r in people_rows],
        "manual_absences": [dict(r, day=str(r["day"]), declared_at=str(r["declared_at"])) for r in absent_rows],
        "late_arrivals": [dict(r, day=str(r["day"]), declared_at=str(r["declared_at"])) for r in late_rows],
    })


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


@router.get("/admin/pph-debug")
def pph_debug(day: str | None = Query(default=None)):
    """Dump the per-person man-hours math for the recycling pph_per_person KPI."""
    try:
        return _pph_debug_impl(day)
    except Exception as e:
        import traceback
        return JSONResponse({
            "error": str(e),
            "traceback": traceback.format_exc(),
        }, status_code=500)


def _pph_debug_impl(day: str | None):
    from .. import staffing, work_centers_store, attendance

    today = datetime.now(timezone.utc).date()
    try:
        d = date.fromisoformat(day) if day else today
    except ValueError:
        return JSONResponse({"error": "day must be YYYY-MM-DD"}, status_code=400)

    is_today = d == today
    sched = staffing.load_schedule(d)

    # Same window math as _recycling_day_data.
    shift_start_local = datetime.combine(d, shift_config.shift_start_for(d), tzinfo=shift_config.SITE_TZ)
    shift_end_local = datetime.combine(d, shift_config.shift_end_for(d), tzinfo=shift_config.SITE_TZ)
    now_local = datetime.now(timezone.utc).astimezone(shift_config.SITE_TZ)
    window_end_local = min(now_local, shift_end_local) if is_today else shift_end_local
    window_start_utc = shift_start_local.astimezone(timezone.utc)
    window_end_utc = window_end_local.astimezone(timezone.utc)

    try:
        absent_today = sorted(attendance.full_day_absent_names(d))
    except Exception as e:
        absent_today = [f"<error: {e}>"]

    locations_dump: list[dict] = []
    total_man_minutes = 0
    total_recycling_people = 0
    for loc in staffing.LOCATIONS:
        try:
            vs = work_centers_store.department(loc)
        except Exception as e:
            vs = f"<error: {e}>"
        is_recycled = vs == "Recycled"
        assigned = list(sched.assignments.get(loc.name, []))
        per_person: list[dict] = []
        if is_recycled:
            for person_name in assigned:
                if person_name in set(absent_today):
                    per_person.append({"name": person_name, "absent": True, "minutes": 0})
                    continue
                try:
                    mins = staffing.effective_minutes_worked(
                        person_name, d, window_start_utc, window_end_utc
                    )
                except Exception as e:
                    mins = -1
                    per_person.append({"name": person_name, "error": str(e), "minutes": -1})
                    continue
                per_person.append({"name": person_name, "minutes": mins})
                total_recycling_people += 1
                total_man_minutes += mins
        locations_dump.append({
            "name": loc.name,
            "loc_skill": loc.skill,
            "loc_department": loc.department,
            "wc_store_department": vs,
            "counted_as_recycled": is_recycled,
            "assigned": assigned,
            "per_person": per_person,
        })

    window_minutes = int((window_end_utc - window_start_utc).total_seconds() // 60)
    return JSONResponse({
        "day": d.isoformat(),
        "is_today": is_today,
        "window_local": {
            "start": shift_start_local.isoformat(),
            "end": window_end_local.isoformat(),
            "minutes": window_minutes,
        },
        "absent_today": absent_today,
        "totals": {
            "total_recycling_people": total_recycling_people,
            "total_man_minutes": total_man_minutes,
            "total_man_hours": round(total_man_minutes / 60.0, 2),
        },
        "locations": locations_dump,
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

    target_days: list[date] = []
    cursor = start_d
    while cursor <= end_d:
        # shift_config.is_workday() opens the gate for published Saturdays
        # (and any other non-standard published weekday). Critical for the
        # case this endpoint typically gets called: refreshing a Saturday
        # row that the leaderboard cached with empty samples before the
        # in_shift_on Saturday fix landed.
        if shift_config.is_workday(cursor) and cursor < today:
            target_days.append(cursor)
        cursor += timedelta(days=1)

    if not target_days:
        return JSONResponse({
            "days_checked": 0,
            "with_units": 0,
            "no_units": [],
            "errors": [],
            "note": "no work-days in range (unpublished weekends + today are always skipped)",
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


def _check_admin_secret(request: Request) -> bool:
    expected = os.environ.get("ZIRA_ADMIN_SECRET", "")
    if not expected:
        return False
    provided = request.headers.get("X-Admin-Secret", "")
    return provided == expected


@router.post("/admin/precompute-run")
def precompute_run(
    request: Request,
    from_: str | None = Query(default=None, alias="from"),
    to: str | None = Query(default=None),
):
    """Run the production_daily precompute for one or more days.

    Default (no params): precompute yesterday.
    With `from` + `to`: precompute every day in that inclusive range.

    Auth: X-Admin-Secret header must match $ZIRA_ADMIN_SECRET.
    Idempotent — re-running a day overwrites cleanly.
    """
    import time
    from .. import precompute
    from ..deps import client

    if not _check_admin_secret(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    today = datetime.now(timezone.utc).date()
    if from_ or to:
        if not (from_ and to):
            return JSONResponse(
                {"error": "must supply both `from` and `to`, or neither"},
                status_code=400,
            )
        try:
            start_d = date.fromisoformat(from_)
            end_d = date.fromisoformat(to)
        except ValueError:
            return JSONResponse(
                {"error": "from/to must be YYYY-MM-DD"}, status_code=400
            )
    else:
        start_d = end_d = today - timedelta(days=1)

    if end_d < start_d:
        return JSONResponse({"error": "to must be >= from"}, status_code=400)

    started = time.time()
    days_processed = 0
    rows_written = 0
    errors: list[dict] = []

    cursor = start_d
    while cursor <= end_d:
        try:
            result = precompute.precompute_day(cursor, client)
            rows_written += int(result.get("rows_written", 0))
        except Exception as e:
            errors.append({"day": cursor.isoformat(), "error": str(e)[:200]})
        days_processed += 1
        cursor += timedelta(days=1)

    # Past production_daily rows changed → bust the response cache so
    # corrected leaderboards / player cards (past + today buckets) refresh
    # immediately instead of waiting out the 5-min past-bucket TTL.
    if rows_written > 0:
        from .. import _http_cache
        _http_cache.invalidate_all_cache()

    return JSONResponse({
        "from": start_d.isoformat(),
        "to": end_d.isoformat(),
        "days_processed": days_processed,
        "rows_written": rows_written,
        "duration_ms": int((time.time() - started) * 1000),
        "errors": errors,
    })


# ---------- Device tokens admin ----------


@router.get("/admin/devices", response_class=HTMLResponse)
def admin_devices_list(request: Request):
    return templates.TemplateResponse(
        request, "admin_devices.html",
        {
            "tokens": _dt.list_all(),
            "host": request.url.netloc,
            "just_minted": None,
            "active": "admin",
        },
    )


@router.post("/admin/devices", response_class=HTMLResponse)
def admin_devices_create(request: Request, name: str = Form(...)):
    # The middleware stashes the authed user's UPN on request.state in
    # Task 13 — until then, fall back to "admin" so this still works.
    created_by = getattr(request.state, "user_upn", "admin")
    new_id, signed = _dt.mint(name=name, created_by=created_by)
    minted = next((t for t in _dt.list_all() if t["id"] == new_id), None)
    return templates.TemplateResponse(
        request, "admin_devices.html",
        {
            "tokens": _dt.list_all(),
            "host": request.url.netloc,
            "just_minted": {
                "name": (minted or {}).get("name", name),
                "signed": signed,
            },
            "active": "admin",
        },
    )


@router.post("/admin/devices/{token_id}/revoke")
def admin_devices_revoke(token_id: int):
    _dt.revoke(token_id)
    return RedirectResponse(url="/admin/devices", status_code=303)
