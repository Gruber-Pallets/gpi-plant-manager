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


@router.get("/recycling", response_class=HTMLResponse)
def recycling(request: Request, day: str | None = Query(default=None)):
    d = _parse_day(day)
    today = datetime.now(timezone.utc).date()
    is_today = d == today
    # Try cached HTML response.
    from .._http_cache import get_cached_response, set_cache_headers, store_cached_response
    cache_key = ("recycling", d.isoformat())
    cached = get_cached_response(cache_key, includes_today=is_today)
    if cached is not None:
        return cached
    stations = recycling_stations()
    now = datetime.now(timezone.utc)
    results = leaderboard(client, stations, d, now_utc=now if is_today else None)

    # Load schedule first so we can decide which WCs were "active" today.
    # A WC is active iff someone was scheduled to it OR it produced more than
    # the noise floor (5 units). Inactive WCs are dropped from every roll-up:
    # uptime, group goals, bars, downtime — so the dashboard reflects only
    # the staffing and stations that actually ran today.
    sched_for_labels = staffing.load_schedule(d)
    who_by_wc: dict[str, str] = {}
    for wc_name, ops in sched_for_labels.assignments.items():
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
    uptime_pct = (uptime_minutes / available * 100.0) if available > 0 else 0.0
    pallets_per_hour = (total_units / (elapsed / 60.0)) if elapsed > 0 else 0.0
    # pallets/hr/person — metered pallet total divided by total recycling
    # man-hours. Every person scheduled to a Recycled-stream WC contributes
    # their full elapsed shift hours, metered or not (Trim Saw, Master
    # Recycler, unmetered Repairs all count). Matches a clipboard calc.
    elapsed_hours = elapsed / 60.0 if elapsed else 0.0
    total_recycling_people = sum(
        len(sched_for_labels.assignments.get(loc.name, []))
        for loc in staffing.LOCATIONS
        if work_centers_store.value_stream(loc) == "Recycled"
    )
    total_man_hours = total_recycling_people * elapsed_hours
    pph_per_person = (total_units / total_man_hours) if total_man_hours > 0 else 0.0

    dismantlers = [r for r in active_results if r.station.category == "Dismantler"]
    dismantlers.sort(key=lambda r: r.station.name)
    repairs = [r for r in active_results if r.station.category == "Repair"]
    repairs.sort(key=lambda r: r.station.name)

    # ---- Productive intervals per WC ----
    # active_intervals (from leaderboard's transfer rule) = time the WC was
    # producing or in its 60-min grace tail. We also count the first 60 min of
    # the shift as productive for any *scheduled* WC, since the 60-min rule
    # can't possibly have triggered yet — that's the user-supplied "first 60
    # min fixed" rule for the progress charts.
    shift_start_local = datetime.combine(d, shift_config.shift_start_for(d), tzinfo=shift_config.SITE_TZ)
    grace_end_local   = shift_start_local + timedelta(minutes=60)
    # Cap grace at `now` for today, so a freshly-started shift doesn't bill
    # the full 60-min grace as productive when only a few minutes have passed.
    grace_end_capped_local = min(grace_end_local, now.astimezone(shift_config.SITE_TZ)) if is_today else grace_end_local
    grace_interval_utc = (
        shift_start_local.astimezone(timezone.utc),
        grace_end_capped_local.astimezone(timezone.utc),
    )
    people_by_wc: dict[str, int] = {
        wc: len(ops) for wc, ops in sched_for_labels.assignments.items()
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

    # Break windows in UTC for the day — subtracted from productive intervals
    # so a station "productive all day" sums to *productive shift hours* (not
    # wall-clock), matching the settings goal which is daily / productive_hours.
    breaks_utc: list[tuple[datetime, datetime]] = []
    for b in shift_config.breaks_for(d):
        bs = datetime.combine(d, b.start, tzinfo=shift_config.SITE_TZ).astimezone(timezone.utc)
        be = datetime.combine(d, b.end,   tzinfo=shift_config.SITE_TZ).astimezone(timezone.utc)
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

    # ---- Per-bucket target for progress chart ----
    # First 60 min of shift: goal is sum across SCHEDULED WCs of
    # (hourly_target × people_scheduled × bucket_fraction). After that:
    # standard productive-interval-overlap math, so the dotted goal line steps
    # up/down as stations transfer off and back on.
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

    dism_progress = progress_buckets(dismantlers, d, now, target_fn=_make_target_fn(dismantlers))
    repair_progress = progress_buckets(repairs, d, now, target_fn=_make_target_fn(repairs))

    # Group hourly target shown in the legend: total productive expected for
    # the shift so far, averaged over elapsed hours.
    elapsed_hours_for_avg = (elapsed / 60.0) if elapsed else 0.0
    def _group_goal(rows):
        if elapsed_hours_for_avg <= 0:
            return 0.0
        total_expected = sum(
            settings_store.station_target(r.station) * (_productive_minutes(r.station.name) / 60.0)
            for r in rows
        )
        return total_expected / elapsed_hours_for_avg
    dism_group_target = _group_goal(dismantlers)
    repair_group_target = _group_goal(repairs)

    customs_all = widget_customizer.load_all("recycling")

    # Time-of-day label for the target-line marker on the bar widgets.
    now_local = now.astimezone(shift_config.SITE_TZ)
    now_label = now_local.strftime("%H:%M")

    def _sorted_bars(items: list, widget_id: str) -> list:
        s = customs_all.get(widget_id, {}).get("sort", "preset")
        if s == "desc":  return sorted(items, key=lambda x: -x["units"])
        if s == "asc":   return sorted(items, key=lambda x: x["units"])
        if s == "alpha": return sorted(items, key=lambda x: x["name"].lower())
        return items

    def _progress_color(pct_of_target: float | None) -> str | None:
        """Vivid 25-step palette: dark red → white (at 100%) → dark green.
        Saturation stays high across the scale; lightness ramps from mid to dark
        so even small deviations from target look clearly green or red (not washed-out)."""
        if pct_of_target is None:
            return None
        p = max(0.0, min(200.0, pct_of_target))
        delta = p - 100.0
        if abs(delta) < 1.0:  # within 1% of target counts as at-target → white
            return "#ffffff"
        step = min(12, max(1, round(abs(delta) / 100.0 * 12)))
        sat = 55.0 + step * 2.0          # 57% → 79%
        light = 65.0 - step * 3.5        # 61.5% → 23%
        hue = 130 if delta > 0 else 0
        return f"hsl({hue:.0f}, {sat:.0f}%, {light:.0f}%)"

    def _bars(items: list) -> list[dict]:
        out = []
        for r in items:
            station_tgt_hr = settings_store.station_target(r.station)  # pallets/hr
            # Per-WC expected uses the station's *productive* time (active
            # intervals + first-60-min grace if scheduled). A WC scheduled and
            # producing all day reaches the full settings goal; a WC
            # transferred away after 2h sees expected sized to its 2h of work.
            station_active_hours = _productive_minutes(r.station.name) / 60.0
            expected = station_tgt_hr * station_active_hours
            pct_of_target = (r.units / expected * 100.0) if expected > 0 else None
            out.append(
                {
                    "name": r.station.name,
                    "who": who_by_wc.get(r.station.name, r.station.name),
                    "units": r.units,
                    "pct_of_target": round(pct_of_target, 1) if pct_of_target is not None else None,
                    "expected": int(round(expected)),
                    "color": _progress_color(pct_of_target),
                    "downtime_minutes": r.downtime_minutes,
                    "state": _state(r, now, is_today),
                }
            )
        # Shared scale for bar width + target line position.
        max_u = max((r["units"] for r in out), default=0)
        max_e = max((r["expected"] for r in out), default=0)
        base = max(max_u, max_e)
        scale = (base * 1.1) if base > 0 else 1.0
        has_target_line = max_e > 0
        for r in out:
            r["pct"] = (r["units"] / scale * 100.0) if scale else 0.0
            r["target_pct"] = (r["expected"] / scale * 100.0) if (scale and has_target_line) else None
        return out

    def _downtime_rows(items: list) -> list[dict]:
        out = []
        for r in items:
            working = max(0, elapsed - r.downtime_minutes)
            total = elapsed if elapsed else 1
            out.append(
                {
                    "name": r.station.name,
                    "who": who_by_wc.get(r.station.name),
                    "working": working,
                    "down": r.downtime_minutes,
                    "working_pct": working / total * 100.0,
                    "down_pct": r.downtime_minutes / total * 100.0,
                }
            )
        return out

    response = templates.TemplateResponse(
        request,
        "recycling.html",
        {
            "active_vs": "recycling",
            "day": d.isoformat(),
            "today": today.isoformat(),
            "is_today": is_today,
            "total_units": total_units,
            "total_downtime_minutes": total_downtime,
            "total_downtime_display": f"{total_downtime / 60:.1f} h",
            "uptime_pct": round(uptime_pct, 1),
            "pallets_per_hour": round(pallets_per_hour, 1),
            "pph_per_person": round(pph_per_person, 1),
            "elapsed_minutes": elapsed,
            "dismantler_bars": _sorted_bars(_bars(dismantlers), "dismantler-bars"),
            "repair_bars": _sorted_bars(_bars(repairs), "repair-bars"),
            "downtime_rows": _downtime_rows(dismantlers + repairs),
            "dismantler_progress": dism_progress,
            "repair_progress": repair_progress,
            "dismantler_group_target": dism_group_target,
            "repair_group_target": repair_group_target,
            "dismantler_people": sum(
                len(sched_for_labels.assignments.get(r.station.name, []))
                for r in dismantlers
            ),
            "repair_people": sum(
                len(sched_for_labels.assignments.get(r.station.name, []))
                for r in repairs
            ),
            "layout": layout_store.layout_map("recycling"),
            "customs": customs_all,
            "now_label": now_label,
            "shift_start_label": shift_start_local.strftime("%H:%M"),
            "refreshed_at": now.strftime("%H:%M:%S UTC"),
        },
    )
    set_cache_headers(response, includes_today=is_today)
    store_cached_response(cache_key, includes_today=is_today, response=response)
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
    people_count = sum(
        len(ops) for wc, ops in sched_for_labels.assignments.items()
        if wc != staffing.TIME_OFF_KEY and ops and wc in station_names
    )
    pph_per_person = (
        pallets_per_hour / people_count if people_count > 0 else 0.0
    )

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

    response = templates.TemplateResponse(
        request,
        "new_vs.html",
        {
            "active_vs": "new",
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
