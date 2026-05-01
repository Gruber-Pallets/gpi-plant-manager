"""Value-stream pages: GET /recycling and GET /new-vs."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse

from .. import layout_store, settings_store, shift_config, staffing, widget_customizer, work_centers_store
from ..deps import _parse_day, _state, client, templates
from ..leaderboard import cached_leaderboard as leaderboard
from ..progress import progress_buckets
from ..shift_config import shift_elapsed_minutes
from ..stations import Station, recycling_stations

router = APIRouter()


def _recycling_day_data(d, now, is_today_d):
    """Compute the per-day numbers for the recycling dashboard.

    Returns a dict with the keys the route handler needs to aggregate:
      total_units, total_downtime, elapsed, available, uptime_minutes,
      total_man_hours, total_recycling_people,
      per_wc_units {name: int}, per_wc_downtime {name: int},
      per_wc_expected {name: float}, per_wc_who {name: str|None},
      per_wc_state {name: str},  # only meaningful when is_today_d
      dism_buckets, repair_buckets,  # list[dict] from progress_buckets
      shift_start_label, schedule_assignments,
      active_wc_names, per_wc_category, per_wc_station_obj.
    Days outside the working schedule (weekends) return zero-shaped values.
    """
    stations = recycling_stations()
    results = leaderboard(client, stations, d, now_utc=now if is_today_d else None)

    sched = staffing.load_schedule(d)
    who_by_wc: dict[str, str] = {}
    for wc_name, ops in sched.assignments.items():
        if wc_name == staffing.TIME_OFF_KEY or not ops:
            continue
        who_by_wc[wc_name] = " + ".join(ops)

    ACTIVE_UNITS_THRESHOLD = 5
    active_wc_names: set[str] = set(who_by_wc.keys())
    for r in results:
        if r.units > ACTIVE_UNITS_THRESHOLD:
            active_wc_names.add(r.station.name)

    active_results = [r for r in results if r.station.name in active_wc_names]
    active_stations = [s for s in stations if s.name in active_wc_names]

    total_units = sum(r.units for r in active_results)
    total_downtime = sum(r.downtime_minutes for r in active_results)
    elapsed = shift_elapsed_minutes(d, now)
    available = elapsed * len(active_stations)
    uptime_minutes = max(0, available - total_downtime)
    elapsed_hours = elapsed / 60.0 if elapsed else 0.0

    # Per-person effective minutes during [shift_start, now-or-shift-end] on this day,
    # subtracting any partial-day StratusTime off intervals that overlap.
    shift_start_local_for_mh = datetime.combine(d, shift_config.shift_start_for(d), tzinfo=shift_config.SITE_TZ)
    shift_end_local_for_mh = datetime.combine(d, shift_config.shift_end_for(d), tzinfo=shift_config.SITE_TZ)
    window_end_local = (
        min(now.astimezone(shift_config.SITE_TZ), shift_end_local_for_mh)
        if is_today_d else shift_end_local_for_mh
    )
    window_start_utc = shift_start_local_for_mh.astimezone(timezone.utc)
    window_end_utc = window_end_local.astimezone(timezone.utc)

    total_man_minutes = 0
    total_recycling_people = 0
    for loc in staffing.LOCATIONS:
        if work_centers_store.value_stream(loc) != "Recycled":
            continue
        for person_name in sched.assignments.get(loc.name, []):
            total_recycling_people += 1
            total_man_minutes += staffing.effective_minutes_worked(
                person_name, d, window_start_utc, window_end_utc,
            )
    total_man_hours = total_man_minutes / 60.0

    dismantlers = [r for r in active_results if r.station.category == "Dismantler"]
    dismantlers.sort(key=lambda r: r.station.name)
    repairs = [r for r in active_results if r.station.category == "Repair"]
    repairs.sort(key=lambda r: r.station.name)

    # ---- Productive intervals per WC (existing logic copied verbatim). ----
    shift_start_local = datetime.combine(d, shift_config.shift_start_for(d), tzinfo=shift_config.SITE_TZ)
    grace_end_local = shift_start_local + timedelta(minutes=60)
    grace_end_capped_local = min(grace_end_local, now.astimezone(shift_config.SITE_TZ)) if is_today_d else grace_end_local
    grace_interval_utc = (
        shift_start_local.astimezone(timezone.utc),
        grace_end_capped_local.astimezone(timezone.utc),
    )
    people_by_wc: dict[str, int] = {
        wc: len(ops) for wc, ops in sched.assignments.items()
        if wc != staffing.TIME_OFF_KEY and ops
    }

    def _merge(intervals):
        if not intervals:
            return []
        intervals = sorted(intervals, key=lambda x: x[0])
        out = [intervals[0]]
        for s, e in intervals[1:]:
            if s <= out[-1][1]:
                out[-1] = (out[-1][0], max(out[-1][1], e))
            else:
                out.append((s, e))
        return out

    breaks_utc: list[tuple[datetime, datetime]] = []
    for b in shift_config.breaks_for(d):
        bs = datetime.combine(d, b.start, tzinfo=shift_config.SITE_TZ).astimezone(timezone.utc)
        be = datetime.combine(d, b.end, tzinfo=shift_config.SITE_TZ).astimezone(timezone.utc)
        if be > bs:
            breaks_utc.append((bs, be))

    def _subtract_breaks(intervals):
        if not breaks_utc:
            return intervals
        chunks = list(intervals)
        for b_s, b_e in breaks_utc:
            new_chunks = []
            for c_s, c_e in chunks:
                if b_e <= c_s or b_s >= c_e:
                    new_chunks.append((c_s, c_e))
                    continue
                if c_s < b_s:
                    new_chunks.append((c_s, b_s))
                if c_e > b_e:
                    new_chunks.append((b_e, c_e))
            chunks = new_chunks
        return chunks

    grace_has_duration = grace_interval_utc[1] > grace_interval_utc[0]
    productive_by_wc: dict[str, list[tuple[datetime, datetime]]] = {}
    for r in active_results:
        ints = list(r.active_intervals)
        if r.station.name in people_by_wc and grace_has_duration:
            ints.append(grace_interval_utc)
        productive_by_wc[r.station.name] = _subtract_breaks(_merge(ints))

    def _productive_minutes(name: str) -> float:
        return sum((b - a).total_seconds() / 60.0 for a, b in productive_by_wc.get(name, []))

    def _make_target_fn(group):
        def fn(b_start_local: datetime, b_end_local: datetime) -> float:
            bucket_min = (b_end_local - b_start_local).total_seconds() / 60.0
            if b_end_local <= grace_end_local:
                tot = 0.0
                for r in group:
                    name = r.station.name
                    if name not in people_by_wc:
                        continue
                    tot += settings_store.station_target(r.station) * people_by_wc[name] * bucket_min / 60.0
                return tot
            tot = 0.0
            for r in group:
                hr = settings_store.station_target(r.station)
                if hr <= 0:
                    continue
                for ai_s_utc, ai_e_utc in productive_by_wc.get(r.station.name, []):
                    ai_s = ai_s_utc.astimezone(shift_config.SITE_TZ)
                    ai_e = ai_e_utc.astimezone(shift_config.SITE_TZ)
                    o_s = max(ai_s, b_start_local)
                    o_e = min(ai_e, b_end_local)
                    if o_e > o_s:
                        tot += hr * (o_e - o_s).total_seconds() / 60.0 / 60.0
            return tot
        return fn

    dism_buckets = progress_buckets(dismantlers, d, now, target_fn=_make_target_fn(dismantlers))
    repair_buckets = progress_buckets(repairs, d, now, target_fn=_make_target_fn(repairs))

    # Per-WC dicts the aggregator can sum.
    per_wc_units = {r.station.name: r.units for r in active_results}
    per_wc_downtime = {r.station.name: r.downtime_minutes for r in active_results}
    per_wc_expected = {
        r.station.name: settings_store.station_target(r.station) * (_productive_minutes(r.station.name) / 60.0)
        for r in active_results
    }
    per_wc_state = {r.station.name: _state(r, now, is_today_d) for r in active_results}
    per_wc_who = {r.station.name: who_by_wc.get(r.station.name) for r in active_results}
    per_wc_category = {r.station.name: r.station.category for r in active_results}
    per_wc_station_obj = {r.station.name: r.station for r in active_results}

    return {
        "total_units": total_units,
        "total_downtime": total_downtime,
        "elapsed": elapsed,
        "available": available,
        "uptime_minutes": uptime_minutes,
        "total_man_hours": total_man_hours,
        "total_recycling_people": total_recycling_people,
        "per_wc_units": per_wc_units,
        "per_wc_downtime": per_wc_downtime,
        "per_wc_expected": per_wc_expected,
        "per_wc_state": per_wc_state,
        "per_wc_who": per_wc_who,
        "per_wc_category": per_wc_category,
        "per_wc_station_obj": per_wc_station_obj,
        "active_wc_names": active_wc_names,
        "schedule_assignments": dict(sched.assignments),
        "dism_buckets": dism_buckets,
        "repair_buckets": repair_buckets,
        "shift_start_label": shift_start_local.strftime("%H:%M"),
    }


@router.get("/recycling", response_class=HTMLResponse)
def recycling(
    request: Request,
    window: str = Query(default="today"),
    start: str | None = Query(default=None),
    end: str | None = Query(default=None),
):
    from datetime import date as _date
    from ..deps import _window_dates

    today = datetime.now(timezone.utc).date()
    custom_range_active = False
    start_d: _date
    end_d: _date
    if start and end:
        try:
            start_d = _date.fromisoformat(start)
            end_d = _date.fromisoformat(end)
            if end_d >= start_d:
                custom_range_active = True
        except ValueError:
            start_d, end_d = _window_dates(window, today)
    if not custom_range_active:
        start_d, end_d = _window_dates(window, today)

    is_today = (start_d == end_d == today)
    is_range = (start_d != end_d)
    range_includes_today = (start_d <= today <= end_d)

    # Cache key includes both bounds.
    from .._http_cache import get_cached_response, set_cache_headers, store_cached_response
    cache_key = ("recycling", start_d.isoformat(), end_d.isoformat())
    cached = get_cached_response(cache_key, includes_today=range_includes_today)
    if cached is not None:
        return cached

    now = datetime.now(timezone.utc)

    # Walk every day in the range, computing per-day data.
    days: list = []
    cursor = start_d
    while cursor <= end_d:
        days.append(cursor)
        cursor += timedelta(days=1)

    per_day = [_recycling_day_data(d, now, d == today) for d in days]

    # Aggregate top-line stats.
    total_units = sum(p["total_units"] for p in per_day)
    total_downtime = sum(p["total_downtime"] for p in per_day)
    total_elapsed = sum(p["elapsed"] for p in per_day)
    total_available = sum(p["available"] for p in per_day)
    total_uptime_minutes = sum(p["uptime_minutes"] for p in per_day)
    total_man_hours = sum(p["total_man_hours"] for p in per_day)

    uptime_pct = (total_uptime_minutes / total_available * 100.0) if total_available > 0 else 0.0
    pallets_per_hour = (total_units / (total_elapsed / 60.0)) if total_elapsed > 0 else 0.0
    pph_per_person = (total_units / total_man_hours) if total_man_hours > 0 else 0.0

    # Per-WC aggregation.
    agg_units: dict[str, int] = {}
    agg_downtime: dict[str, int] = {}
    agg_expected: dict[str, float] = {}
    agg_who_today: dict[str, str | None] = {}
    agg_category: dict[str, str] = {}
    agg_station_obj: dict[str, object] = {}
    agg_active_names: set[str] = set()
    schedule_today_assignments: dict[str, list[str]] = {}

    for p, d in zip(per_day, days):
        agg_active_names |= p["active_wc_names"]
        for name, units in p["per_wc_units"].items():
            agg_units[name] = agg_units.get(name, 0) + units
        for name, dt in p["per_wc_downtime"].items():
            agg_downtime[name] = agg_downtime.get(name, 0) + dt
        for name, exp in p["per_wc_expected"].items():
            agg_expected[name] = agg_expected.get(name, 0.0) + exp
        for name, cat in p["per_wc_category"].items():
            agg_category[name] = cat
        for name, obj in p["per_wc_station_obj"].items():
            agg_station_obj[name] = obj
        if d == today:
            agg_who_today = p["per_wc_who"]
            schedule_today_assignments = p["schedule_assignments"]

    # Buckets aggregated by time-of-day label.
    def _aggregate_buckets(per_day_buckets: list[list[dict]]) -> list[dict]:
        agg: dict[str, dict] = {}
        order: list[str] = []
        for day_buckets in per_day_buckets:
            for b in day_buckets:
                lbl = b["label"]
                if lbl not in agg:
                    agg[lbl] = {"label": lbl, "actual": 0, "target": 0, "in_progress": False}
                    order.append(lbl)
                agg[lbl]["actual"] += b["actual"]
                agg[lbl]["target"] += b["target"]
                if b["in_progress"]:
                    agg[lbl]["in_progress"] = True
        order.sort()
        return [agg[lbl] for lbl in order]

    dism_progress = _aggregate_buckets([p["dism_buckets"] for p in per_day])
    repair_progress = _aggregate_buckets([p["repair_buckets"] for p in per_day])

    # Group hourly target — average over total elapsed hours, summing per-WC expected.
    elapsed_hours_total = total_elapsed / 60.0 if total_elapsed else 0.0
    def _group_goal(category: str) -> float:
        if elapsed_hours_total <= 0:
            return 0.0
        total_expected = sum(
            agg_expected[name]
            for name in agg_expected
            if agg_category.get(name) == category
        )
        return total_expected / elapsed_hours_total
    dism_group_target = _group_goal("Dismantler")
    repair_group_target = _group_goal("Repair")

    customs_all = widget_customizer.load_all("recycling")

    def _progress_color(pct_of_target):
        if pct_of_target is None:
            return None
        p = max(0.0, min(200.0, pct_of_target))
        delta = p - 100.0
        if abs(delta) < 1.0:
            return "#ffffff"
        step = min(12, max(1, round(abs(delta) / 100.0 * 12)))
        sat = 55.0 + step * 2.0
        light = 65.0 - step * 3.5
        hue = 130 if delta > 0 else 0
        return f"hsl({hue:.0f}, {sat:.0f}%, {light:.0f}%)"

    def _bars(category: str) -> list[dict]:
        names = sorted(n for n in agg_active_names if agg_category.get(n) == category)
        out = []
        for name in names:
            units = agg_units.get(name, 0)
            expected = agg_expected.get(name, 0.0)
            pct_of_target = (units / expected * 100.0) if expected > 0 else None
            out.append({
                "name": name,
                "who": agg_who_today.get(name) if not is_range else None,
                "units": units,
                "pct_of_target": round(pct_of_target, 1) if pct_of_target is not None else None,
                "expected": int(round(expected)),
                "color": _progress_color(pct_of_target),
                "downtime_minutes": agg_downtime.get(name, 0),
            })
        max_u = max((r["units"] for r in out), default=0)
        max_e = max((r["expected"] for r in out), default=0)
        base = max(max_u, max_e)
        scale = (base * 1.1) if base > 0 else 1.0
        # Hide target tick line for multi-day ranges: it represents "where you should be by now"
        # which only makes sense for an in-progress single day.
        has_target_line = (max_e > 0) and not is_range
        for r in out:
            r["pct"] = (r["units"] / scale * 100.0) if scale else 0.0
            r["target_pct"] = (r["expected"] / scale * 100.0) if (scale and has_target_line) else None
        return out

    def _sorted_bars(items: list, widget_id: str) -> list:
        s = customs_all.get(widget_id, {}).get("sort", "preset")
        if s == "desc":  return sorted(items, key=lambda x: -x["units"])
        if s == "asc":   return sorted(items, key=lambda x: x["units"])
        if s == "alpha": return sorted(items, key=lambda x: x["name"].lower())
        return items

    def _downtime_rows():
        names = sorted(
            n for n in agg_active_names
            if agg_category.get(n) in ("Dismantler", "Repair")
        )
        out = []
        for name in names:
            down = agg_downtime.get(name, 0)
            working = max(0, total_elapsed - down)
            total = total_elapsed if total_elapsed else 1
            out.append({
                "name": name,
                "who": agg_who_today.get(name) if not is_range else None,
                "working": working,
                "down": down,
                "working_pct": working / total * 100.0,
                "down_pct": down / total * 100.0,
            })
        return out

    now_local = now.astimezone(shift_config.SITE_TZ)
    now_label = now_local.strftime("%H:%M")
    shift_start_label = per_day[-1]["shift_start_label"] if per_day else ""

    # People count: total person-days across the range for ranges; today's count for Today.
    if is_range:
        dism_people = 0
        repair_people = 0
        for p in per_day:
            for name, ops in p["schedule_assignments"].items():
                if name == staffing.TIME_OFF_KEY or not ops:
                    continue
                cat = p["per_wc_category"].get(name)
                if cat == "Dismantler":
                    dism_people += len(ops)
                elif cat == "Repair":
                    repair_people += len(ops)
    else:
        dism_people = sum(
            len(schedule_today_assignments.get(name, []))
            for name in agg_active_names
            if agg_category.get(name) == "Dismantler"
        )
        repair_people = sum(
            len(schedule_today_assignments.get(name, []))
            for name in agg_active_names
            if agg_category.get(name) == "Repair"
        )

    # Inline-assign popover: for single-day "today" view, list any unattributed
    # WCs so the dashboard's "(no assignment)" lines can become click-to-attribute.
    assignments_todo_by_wc: dict[str, dict] = {}
    all_active_people: list[str] = []
    if is_today:
        try:
            from .. import staffing as _staffing, wc_attributions
            todo = wc_attributions.unattributed_for_day(today, client)
            site_tz = shift_config.SITE_TZ
            for item in todo:
                first = item["first_sample_utc"].astimezone(site_tz)
                last = item["last_sample_utc"].astimezone(site_tz)
                assignments_todo_by_wc[item["wc_name"]] = {
                    "wc_name": item["wc_name"],
                    "units": item["units"],
                    "first_label": first.strftime("%I:%M %p").lstrip("0"),
                    "last_label": last.strftime("%I:%M %p").lstrip("0"),
                    "first_iso": item["first_sample_utc"].isoformat(),
                    "last_iso": item["last_sample_utc"].isoformat(),
                }
            roster = _staffing.load_roster()
            all_active_people = sorted((p.name for p in roster if p.active), key=str.lower)
        except Exception:
            assignments_todo_by_wc = {}
            all_active_people = []

    response = templates.TemplateResponse(
        request,
        "recycling.html",
        {
            "active_vs": "recycling",
            "assignments_todo_by_wc": assignments_todo_by_wc,
            "all_active_people": all_active_people,
            "window": window,
            "start": start_d.isoformat(),
            "end": end_d.isoformat(),
            "today": today.isoformat(),
            "is_today": is_today,
            "is_range": is_range,
            "range_includes_today": range_includes_today,
            "custom_range_active": custom_range_active,
            "total_units": total_units,
            "total_downtime_minutes": total_downtime,
            "total_downtime_display": f"{total_downtime / 60:.1f} h",
            "uptime_pct": round(uptime_pct, 1),
            "pallets_per_hour": round(pallets_per_hour, 1),
            "pph_per_person": round(pph_per_person, 1),
            "elapsed_minutes": total_elapsed,
            "dismantler_bars": _sorted_bars(_bars("Dismantler"), "dismantler-bars"),
            "repair_bars": _sorted_bars(_bars("Repair"), "repair-bars"),
            "downtime_rows": _downtime_rows(),
            "dismantler_progress": dism_progress,
            "repair_progress": repair_progress,
            "dismantler_group_target": dism_group_target,
            "repair_group_target": repair_group_target,
            "dismantler_people": dism_people,
            "repair_people": repair_people,
            "layout": layout_store.layout_map("recycling"),
            "customs": customs_all,
            "now_label": now_label,
            "shift_start_label": shift_start_label,
            "refreshed_at": now.strftime("%H:%M:%S UTC"),
        },
    )
    set_cache_headers(response, includes_today=range_includes_today)
    store_cached_response(cache_key, includes_today=range_includes_today, response=response)
    return response


@router.get("/new-vs", response_class=HTMLResponse)
def new_vs(request: Request, day: str | None = Query(default=None)):
    """Value Streams → New subtab. Shows only work centers whose Settings
    value_stream is "New" and that have a meter ID. Sparse data is the norm
    here today since most "New" stations aren't metered yet."""
    d = _parse_day(day)
    today = datetime.now(timezone.utc).date()
    is_today = d == today
    # Try cached HTML response.
    from .._http_cache import get_cached_response, set_cache_headers, store_cached_response
    cache_key = ("new_vs", d.isoformat())
    cached = get_cached_response(cache_key, includes_today=is_today)
    if cached is not None:
        return cached
    now = datetime.now(timezone.utc)

    new_locs = [
        loc for loc in staffing.LOCATIONS
        if work_centers_store.value_stream(loc) == "New" and loc.meter_id
    ]
    stations = [
        Station(
            meter_id=loc.meter_id,
            name=loc.name,
            category=loc.skill or "Other",
            cell="New",
        )
        for loc in new_locs
    ]
    results = leaderboard(client, stations, d, now_utc=now if is_today else None) if stations else []

    total_units = sum(r.units for r in results)
    total_downtime = sum(r.downtime_minutes for r in results)
    elapsed = shift_elapsed_minutes(d, now)
    available = elapsed * len(stations)
    uptime_minutes = max(0, available - total_downtime)
    uptime_pct = (uptime_minutes / available * 100.0) if available > 0 else 0.0
    pallets_per_hour = (total_units / (elapsed / 60.0)) if elapsed > 0 else 0.0
    elapsed_hours = elapsed / 60.0 if elapsed else 0.0

    sched_for_labels = staffing.load_schedule(d)
    station_names = {s.name for s in stations}

    # Per-person effective minutes during [shift_start, now-or-shift-end],
    # subtracting StratusTime partial-off intervals.
    shift_start_local_for_mh = datetime.combine(d, shift_config.shift_start_for(d), tzinfo=shift_config.SITE_TZ)
    shift_end_local_for_mh = datetime.combine(d, shift_config.shift_end_for(d), tzinfo=shift_config.SITE_TZ)
    window_end_local = (
        min(now.astimezone(shift_config.SITE_TZ), shift_end_local_for_mh)
        if is_today else shift_end_local_for_mh
    )
    window_start_utc = shift_start_local_for_mh.astimezone(timezone.utc)
    window_end_utc = window_end_local.astimezone(timezone.utc)

    total_man_minutes_new = 0
    total_new_vs_people = 0
    for wc_name, ops in sched_for_labels.assignments.items():
        if wc_name == staffing.TIME_OFF_KEY or not ops or wc_name not in station_names:
            continue
        for person_name in ops:
            total_new_vs_people += 1
            total_man_minutes_new += staffing.effective_minutes_worked(
                person_name, d, window_start_utc, window_end_utc,
            )
    total_man_hours = total_man_minutes_new / 60.0
    people_count = total_new_vs_people
    pph_per_person = (total_units / total_man_hours) if total_man_hours > 0 else 0.0

    def _color(pct: float | None) -> str | None:
        if pct is None:
            return None
        if abs(pct - 100.0) < 1.0:
            return "#ffffff"
        delta = max(-100.0, min(100.0, pct - 100.0))
        step = min(12, max(1, round(abs(delta) / 100.0 * 12)))
        sat = 55.0 + step * 2.0
        light = 65.0 - step * 3.5
        hue = 130 if delta > 0 else 0
        return f"hsl({hue:.0f}, {sat:.0f}%, {light:.0f}%)"

    who_by_wc: dict[str, str] = {}
    for wc_name, ops in sched_for_labels.assignments.items():
        if wc_name == staffing.TIME_OFF_KEY or not ops:
            continue
        who_by_wc[wc_name] = " + ".join(ops)

    bars: list[dict] = []
    for r in results:
        station_tgt_hr = settings_store.station_target(r.station)
        expected = station_tgt_hr * elapsed_hours
        pct_of_target = (r.units / expected * 100.0) if expected > 0 else None
        bars.append({
            "name": r.station.name,
            "who": who_by_wc.get(r.station.name, r.station.name),
            "units": r.units,
            "expected": int(round(expected)),
            "color": _color(pct_of_target),
            "pct_of_target": round(pct_of_target, 1) if pct_of_target is not None else None,
        })
    base = max((max(b["units"], b["expected"]) for b in bars), default=0)
    scale = (base * 1.1) if base > 0 else 1.0
    for b in bars:
        b["pct"] = (b["units"] / scale * 100.0) if scale else 0.0
    bars.sort(key=lambda x: -x["units"])

    # ---- Per-bucket dismantler / repair progress (cumulative widgets) ----
    # New VS has sparse metering, so we use a flat target function instead of
    # the full break-aware machinery the Recycling route uses. Each 15-min
    # bucket gets a target of (sum of group hourly targets) * (bucket_min/60).
    new_dismantlers = [r for r in results if r.station.category == "Dismantler"]
    new_repairs    = [r for r in results if r.station.category == "Repair"]

    def _flat_target_fn(group):
        def fn(b_start_local, b_end_local):
            bucket_min = (b_end_local - b_start_local).total_seconds() / 60.0
            total_hourly = sum(settings_store.station_target(r.station) for r in group)
            return total_hourly * bucket_min / 60.0
        return fn

    new_dism_progress = (
        progress_buckets(new_dismantlers, d, now, target_fn=_flat_target_fn(new_dismantlers))
        if new_dismantlers else []
    )
    new_repair_progress = (
        progress_buckets(new_repairs, d, now, target_fn=_flat_target_fn(new_repairs))
        if new_repairs else []
    )

    def _flat_group_goal(rows):
        if not rows:
            return 0.0
        return sum(settings_store.station_target(r.station) for r in rows)
    new_dism_group_target = _flat_group_goal(new_dismantlers)
    new_repair_group_target = _flat_group_goal(new_repairs)

    downtime_rows = []
    for r in results:
        working = max(0, elapsed - r.downtime_minutes)
        total = elapsed if elapsed else 1
        downtime_rows.append({
            "name": r.station.name,
            "who": who_by_wc.get(r.station.name),
            "working": working,
            "down": r.downtime_minutes,
            "working_pct": working / total * 100.0,
            "down_pct": r.downtime_minutes / total * 100.0,
        })

    # Inline-assign popover: today only. Mirrors the recycling route so the
    # "(no assignment)" bars on /new-vs become click-to-attribute buttons.
    assignments_todo_by_wc: dict[str, dict] = {}
    all_active_people: list[str] = []
    if is_today:
        try:
            from .. import staffing as _staffing, wc_attributions
            todo = wc_attributions.unattributed_for_day(today, client)
            site_tz = shift_config.SITE_TZ
            for item in todo:
                first = item["first_sample_utc"].astimezone(site_tz)
                last = item["last_sample_utc"].astimezone(site_tz)
                assignments_todo_by_wc[item["wc_name"]] = {
                    "wc_name": item["wc_name"],
                    "units": item["units"],
                    "first_label": first.strftime("%I:%M %p").lstrip("0"),
                    "last_label": last.strftime("%I:%M %p").lstrip("0"),
                    "first_iso": item["first_sample_utc"].isoformat(),
                    "last_iso": item["last_sample_utc"].isoformat(),
                }
            roster = _staffing.load_roster()
            all_active_people = sorted((p.name for p in roster if p.active), key=str.lower)
        except Exception:
            assignments_todo_by_wc = {}
            all_active_people = []

    response = templates.TemplateResponse(
        request,
        "new_vs.html",
        {
            "active_vs": "new",
            "assignments_todo_by_wc": assignments_todo_by_wc,
            "all_active_people": all_active_people,
            "day": d.isoformat(),
            "today": today.isoformat(),
            "is_today": is_today,
            "total_units": total_units,
            "total_downtime_display": f"{total_downtime / 60:.1f} h",
            "uptime_pct": round(uptime_pct, 1),
            "pallets_per_hour": round(pallets_per_hour, 1),
            "pph_per_person": round(pph_per_person, 1),
            "elapsed_minutes": elapsed,
            "bars": bars,
            "downtime_rows": downtime_rows,
            "has_dismantlers": bool(new_dismantlers),
            "has_repairs": bool(new_repairs),
            "new_dism_progress": new_dism_progress,
            "new_repair_progress": new_repair_progress,
            "new_dism_group_target": new_dism_group_target,
            "new_repair_group_target": new_repair_group_target,
            "new_dism_people": sum(
                len(sched_for_labels.assignments.get(r.station.name, []))
                for r in new_dismantlers
            ),
            "new_repair_people": sum(
                len(sched_for_labels.assignments.get(r.station.name, []))
                for r in new_repairs
            ),
            "refreshed_at": now.strftime("%H:%M:%S UTC"),
        },
    )
    set_cache_headers(response, includes_today=is_today)
    store_cached_response(cache_key, includes_today=is_today, response=response)
    return response
