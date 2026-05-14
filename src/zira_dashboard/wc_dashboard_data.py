"""Per-WC dashboard data-prep helpers.

Pure functions over the existing `cached_leaderboard`, `awards`, and
`work_centers_store` modules. Each helper takes a WC name (or slug) +
a date and returns a widget-ready dict the template can iterate.

The single-page dashboard at /wc/{slug} (editor) and /tv/wc/{slug}
(TV) compose these helpers into one render. No FastAPI / template
imports here — keep this module testable without standing up the app.
"""
from __future__ import annotations

import re


def slug_for_wc(name: str) -> str:
    """URL-safe slug derived from a work-center name.

    Lowercase, alphanumerics + hyphens; everything else collapses to
    a single hyphen. Used as the dashboard layout key (`wc:{slug}`)
    and in URLs (`/wc/{slug}`).

    Examples:
      'Repair 1'       -> 'repair-1'
      'Hand Build #1'  -> 'hand-build-1'
      'Trim Saw 12'    -> 'trim-saw-12'
    """
    s = (name or "").strip().lower()
    # Replace every run of non-alphanumeric chars with a single hyphen.
    s = re.sub(r"[^a-z0-9]+", "-", s)
    # Strip leading + trailing hyphens.
    return s.strip("-")


from datetime import date, datetime, timedelta, timezone


def _load_wc(name: str):
    """Return the Location for `name`, or None.

    Indirection so tests can monkeypatch this single function. Note
    that the canonical work-center list lives in `staffing.LOCATIONS`
    (work_centers_store re-uses it but doesn't expose `all_locations`).
    """
    from . import staffing
    for loc in staffing.LOCATIONS:
        if loc.name == name:
            return loc
    return None


def wc_by_slug(slug: str):
    """Reverse lookup: slug -> Location. Returns None if no match.

    Linear scan since the WC list is tens of items, not thousands.
    """
    from . import staffing
    target = (slug or "").strip().lower()
    if not target:
        return None
    for loc in staffing.LOCATIONS:
        if slug_for_wc(loc.name) == target:
            return loc
    return None


def assigned_operators_for_wc(wc_name: str, day: date) -> list[str]:
    """Return the names assigned to this specific WC in the published
    schedule for `day`. Empty list if unassigned. Only this WC — not
    the whole group.
    """
    from . import staffing
    try:
        sched = staffing.load_schedule(day)
    except Exception:
        return []
    return list(sched.assignments.get(wc_name, []) or [])


def _shift_elapsed_fraction(day: date) -> float:
    """Fraction of today's shift that has elapsed, 0.0..1.0.

    For days other than today, returns 1.0 (full shift counted). For
    today before shift-start, returns 0.0.

    "Today" is evaluated in SITE_TZ (America/Chicago), matching the
    rest of the codebase. Using UTC would silently misreport the
    banner + GOAT race during evening hours when UTC has already
    rolled over to tomorrow.
    """
    from . import shift_config
    today_local = datetime.now(shift_config.SITE_TZ).date()
    if day < today_local:
        return 1.0
    if day > today_local:
        return 0.0
    elapsed = shift_config.shift_elapsed_minutes(day, datetime.now(timezone.utc))
    total = shift_config.productive_minutes_for(day) or 1
    return max(0.0, min(1.0, elapsed / total))


def _units_today_for_wc(wc_name: str, day: date) -> int:
    """Today's pallet count for one WC. Reads from the cached Zira
    leaderboard (shared with /recycling), so this is a fast lookup.
    Returns 0 if the WC has no meter or no data yet.
    """
    from .deps import client
    from .leaderboard import cached_leaderboard
    from .stations import Station
    loc = _load_wc(wc_name)
    if loc is None or not loc.meter_id:
        return 0
    stations = [Station(meter_id=loc.meter_id, name=loc.name, category=loc.skill, cell=loc.bay)]
    try:
        results = cached_leaderboard(client, stations, day)
    except Exception:
        return 0
    for r in results:
        if r.station.name == wc_name:
            return int(r.units)
    return 0


def pallets_banner(wc_name: str, day: date) -> dict:
    """Pallets-banner widget data. Today's units for THIS WC against
    the prorated daily target.

    Returns: {units_today, target_today, target_full_day, pct_of_target}.
    """
    from . import work_centers_store
    loc = _load_wc(wc_name)
    if loc is None:
        return {"units_today": 0, "target_today": 0, "target_full_day": 0, "pct_of_target": None}
    full = int(work_centers_store.goal_per_day(loc) or 0)
    frac = _shift_elapsed_fraction(day)
    target_today = int(round(full * frac))
    units = _units_today_for_wc(wc_name, day)
    pct = (units / target_today * 100.0) if target_today > 0 else None
    return {
        "units_today": units,
        "target_today": target_today,
        "target_full_day": full,
        "pct_of_target": pct,
    }


def monthly_ribbons(wc_name: str, year: int, month: int) -> dict:
    """Top-3 person-days in this WC's group for the given month."""
    from . import awards, work_centers_store
    loc = _load_wc(wc_name)
    if loc is None:
        return {"group": None, "entries": []}
    grp_list = work_centers_store.groups(loc) or []
    if not grp_list:
        return {"group": None, "entries": []}
    group = grp_list[0]
    entries = awards.monthly_badges(group, year, month) or []
    return {"group": group, "entries": entries}


def goat_race(wc_name: str, day: date) -> dict:
    """GOAT-race widget. Compares today's pace at this WC against the
    WC's group's all-time GOAT day, prorated by elapsed shift fraction.

    status: 'AHEAD' / 'ON_PACE' / 'BEHIND' / None (if no GOAT yet).
    """
    from . import awards, work_centers_store
    loc = _load_wc(wc_name)
    if loc is None:
        return {"group": None, "goat": None, "units_today": 0, "goat_pace_today": 0, "status": None}
    grp_list = work_centers_store.groups(loc) or []
    group = grp_list[0] if grp_list else None
    goat = awards.goat(group) if group else None
    units = _units_today_for_wc(wc_name, day)
    if goat is None:
        return {"group": group, "goat": None, "units_today": units, "goat_pace_today": 0, "status": None}
    frac = _shift_elapsed_fraction(day)
    goat_pace_today = float(goat.get("units", 0)) * frac
    # Status thresholds — within ±5 % of pace is "ON_PACE", otherwise
    # AHEAD / BEHIND.
    if goat_pace_today <= 0:
        status = None
    else:
        delta_pct = (units - goat_pace_today) / goat_pace_today * 100.0
        if delta_pct > 5:
            status = "AHEAD"
        elif delta_pct < -5:
            status = "BEHIND"
        else:
            status = "ON_PACE"
    return {
        "group": group,
        "goat": goat,
        "units_today": units,
        "goat_pace_today": goat_pace_today,
        "status": status,
    }


def _station_total_for_wc(wc_name: str, day: date):
    """Return the StationTotal for one WC + day, or None.

    Reads via `cached_leaderboard` (in-process TODAY cache + Postgres
    past-day cache via `_zira_persist`). Both paths return the same
    StationTotal dataclass shape with `.samples`, `.active_intervals`,
    `.units`, `.downtime_minutes`, etc.
    """
    from .deps import client
    from .leaderboard import cached_leaderboard
    from .stations import Station
    loc = _load_wc(wc_name)
    if loc is None or not loc.meter_id:
        return None
    stations = [Station(meter_id=loc.meter_id, name=loc.name, category=loc.skill, cell=loc.bay)]
    try:
        results = cached_leaderboard(client, stations, day)
    except Exception:
        return None
    for r in results:
        if r.station.name == wc_name:
            return r
    return None


def _readings_for_wc_today(wc_name: str, day: date) -> list[dict]:
    """Per-event readings for one WC + day, normalized to a list of
    `{ts_utc, units}` dicts. Extracted from StationTotal.samples which
    is a tuple of (datetime, int) pairs.

    Empty list if no meter / no data. Tests can monkeypatch this
    directly instead of stubbing the entire cached_leaderboard chain.
    """
    total = _station_total_for_wc(wc_name, day)
    if total is None:
        return []
    return [
        {"ts_utc": ts, "units": int(units)}
        for (ts, units) in (total.samples or [])
        if ts is not None
    ]


def _wc_target_per_bucket(wc_name: str, day: date) -> int:
    """Target units per 15-min bucket. daily_target / (shift_minutes/15)."""
    from . import work_centers_store, shift_config
    loc = _load_wc(wc_name)
    if loc is None:
        return 0
    full = int(work_centers_store.goal_per_day(loc) or 0)
    shift_minutes = shift_config.productive_minutes_for(day) or 1
    buckets = max(1, shift_minutes // 15)
    return max(0, int(round(full / buckets)))


def _bucket_index(reading_ts, shift_start_utc) -> int:
    """Map an event timestamp to its 15-min bucket from shift-start."""
    if not reading_ts or not shift_start_utc:
        return 0
    delta = (reading_ts - shift_start_utc).total_seconds() / 60.0
    if delta < 0:
        return 0
    return int(delta // 15)


def _bucket_count_for_day(day: date) -> int:
    """Number of 15-min buckets in the shift on `day`."""
    from . import shift_config
    return max(1, (shift_config.productive_minutes_for(day) or 0) // 15)


def daily_progress(wc_name: str, day: date) -> list[dict]:
    """Cumulative units per 15-min bucket from shift-start to shift-end.

    Returns a list of {bucket_index, minute_offset, cumulative_units}
    one entry per bucket. Used by the daily-progress SVG chart.
    """
    from . import shift_config

    readings = _readings_for_wc_today(wc_name, day)
    n_buckets = _bucket_count_for_day(day)
    shift_start_local = datetime.combine(
        day, shift_config.shift_start_for(day), tzinfo=shift_config.SITE_TZ,
    )
    shift_start_utc = shift_start_local.astimezone(timezone.utc)

    per_bucket = [0] * n_buckets
    for r in readings:
        ts = r.get("ts_utc")
        if ts is None:
            continue
        idx = _bucket_index(ts, shift_start_utc)
        if 0 <= idx < n_buckets:
            per_bucket[idx] += int(r.get("units") or 0)

    cumulative = 0
    out = []
    for i, val in enumerate(per_bucket):
        cumulative += val
        out.append({
            "bucket_index": i,
            "minute_offset": i * 15,
            "cumulative_units": cumulative,
        })
    return out


def fifteen_min_increments(wc_name: str, day: date) -> list[dict]:
    """Per-bucket units + color flag (green ≥ target, amber ≥ 75%, red < 75%).

    Mirrors `daily_progress` but emits per-bucket (not cumulative) units
    and a color-coded status against the per-bucket target.
    """
    from . import shift_config

    readings = _readings_for_wc_today(wc_name, day)
    n_buckets = _bucket_count_for_day(day)
    target = _wc_target_per_bucket(wc_name, day)
    shift_start_local = datetime.combine(
        day, shift_config.shift_start_for(day), tzinfo=shift_config.SITE_TZ,
    )
    shift_start_utc = shift_start_local.astimezone(timezone.utc)

    per_bucket = [0] * n_buckets
    for r in readings:
        ts = r.get("ts_utc")
        if ts is None:
            continue
        idx = _bucket_index(ts, shift_start_utc)
        if 0 <= idx < n_buckets:
            per_bucket[idx] += int(r.get("units") or 0)

    def _color(units):
        if target <= 0:
            return "neutral"
        if units >= target:
            return "green"
        if units >= 0.75 * target:
            return "amber"
        return "red"

    return [
        {
            "bucket_index": i,
            "minute_offset": i * 15,
            "units": v,
            "color": _color(v),
            "target": target,
        }
        for i, v in enumerate(per_bucket)
    ]


def _downtime_events_for_wc(wc_name: str, day: date) -> list[dict]:
    """Downtime events derived from gaps in StationTotal.active_intervals.

    Each entry: `{time, duration_minutes}` where `time` is the local
    HH:MMa display of when the down period started. Reason data isn't
    captured by Zira so we don't include it. Intervals are sorted
    chronologically before gap detection.

    Indirection so tests can monkeypatch a fixed list.
    """
    from . import shift_config
    total = _station_total_for_wc(wc_name, day)
    if total is None:
        return []
    intervals = sorted(
        [(a, b) for (a, b) in (total.active_intervals or []) if a and b],
        key=lambda ab: ab[0],
    )
    if not intervals:
        return []
    events: list[dict] = []
    prev_end = intervals[0][1]
    for start, end in intervals[1:]:
        if start > prev_end:
            gap_minutes = int((start - prev_end).total_seconds() // 60)
            if gap_minutes >= 1:
                local = prev_end.astimezone(shift_config.SITE_TZ)
                # Format: "9:42a", "11:15a", "1:38p"
                hour = local.hour
                minute = local.minute
                am_pm = "a" if hour < 12 else "p"
                hour_12 = hour % 12 or 12
                events.append({
                    "time": f"{hour_12}:{minute:02d}{am_pm}",
                    "duration_minutes": gap_minutes,
                })
        prev_end = max(prev_end, end)
    return events


def downtime_report(wc_name: str, day: date) -> dict:
    """Downtime widget data: {events: [...], total_minutes: int}.

    total_minutes pulls from StationTotal.downtime_minutes (Zira's
    own count); events are derived from active_intervals gaps. The
    two may differ slightly — the total is the authoritative number.
    """
    events = _downtime_events_for_wc(wc_name, day)
    total = _station_total_for_wc(wc_name, day)
    total_minutes = int(total.downtime_minutes) if total else 0
    return {"events": events, "total_minutes": total_minutes}


def kpi_tiles(wc_name: str, day: date) -> dict:
    """KPI tile values for the operator dashboard. Single-WC scope.

    Returns:
      units_today (int)
      downtime_minutes (int)
      hours_elapsed (float) — elapsed shift hours so far (0 on weekends/pre-shift)
      up_time_pct (float) — 0..100; 0 if no shift has elapsed
      pallets_per_hour (float, 1 decimal)
    """
    from . import shift_config
    units = _units_today_for_wc(wc_name, day)
    report = downtime_report(wc_name, day) or {}
    down = int(report.get("total_minutes", 0))
    try:
        full_minutes = shift_config.productive_minutes_per_day()
    except Exception:
        full_minutes = 480
    elapsed_minutes = int(full_minutes * _shift_elapsed_fraction(day))
    hours_elapsed = elapsed_minutes / 60.0 if elapsed_minutes > 0 else 0.0
    if elapsed_minutes > 0:
        up_time_pct = max(0.0, (elapsed_minutes - down) / elapsed_minutes * 100.0)
    else:
        up_time_pct = 0.0
    pallets_per_hour = round(units / hours_elapsed, 1) if hours_elapsed > 0 else 0.0
    return {
        "units_today": units,
        "downtime_minutes": down,
        "hours_elapsed": round(hours_elapsed, 2),
        "up_time_pct": round(up_time_pct, 1),
        "pallets_per_hour": pallets_per_hour,
    }


def _elapsed_wall_clock_minutes(day: date) -> int:
    """Wall-clock minutes since shift_start on `day`.

    Used by the progress chart to decide which 15-min buckets are
    past/current/future. Wall-clock (not productive) because bucket
    offsets are `i * 15` minutes from shift_start in wall-clock — if
    we used productive elapsed (which subtracts break time) the chart
    appears frozen for `break_minutes` after every break.

    Returns a large value for past days (so every bucket shows) and 0
    for future days. Catches exceptions to keep the helper safe to
    call without a configured shift.
    """
    from . import shift_config
    try:
        today_local = datetime.now(shift_config.SITE_TZ).date()
        if day < today_local:
            return 10_000
        if day > today_local:
            return 0
        shift_start = shift_config.shift_start_for(day)
        shift_start_dt = datetime.combine(day, shift_start, tzinfo=shift_config.SITE_TZ)
        now_local = datetime.now(shift_config.SITE_TZ)
        return max(0, int((now_local - shift_start_dt).total_seconds() / 60))
    except Exception:
        return 10_000


def fifteen_min_progress_buckets(wc_name: str, day: date) -> dict:
    """Per-15-min progress buckets in the shape /recycling's progress_chart
    macro consumes:
      {buckets: [{label, actual, target, in_progress}, ...], bucket_target}
    """
    from . import shift_config
    raw = fifteen_min_increments(wc_name, day) or []
    if not raw:
        return {"buckets": [], "bucket_target": 0}
    try:
        shift_start = shift_config.shift_start_for(day)
    except Exception:
        shift_start = None
    elapsed = _elapsed_wall_clock_minutes(day)

    buckets: list[dict] = []
    for b in raw:
        offset = b["minute_offset"]
        if shift_start is not None:
            bucket_dt = datetime.combine(day, shift_start) + timedelta(minutes=offset)
            hour = bucket_dt.hour
            am_pm = "a" if hour < 12 else "p"
            hour_12 = hour % 12 or 12
            label = f"{hour_12}:{bucket_dt.minute:02d}{am_pm}"
        else:
            label = f"+{offset}m"
        buckets.append({
            "label": label,
            "offset": offset,
            "actual": int(b.get("units") or 0),
            "target": int(b.get("target") or 0),
            "in_progress": offset <= elapsed < offset + 15,
        })
    # Drop future buckets so the chart stops at "now" (matches /recycling).
    # On past days `elapsed` was set to `full_minutes` so this is a no-op.
    buckets = [b for b in buckets if b["offset"] <= elapsed]
    bucket_target = next((b["target"] for b in buckets if b["target"]), 0)
    return {"buckets": buckets, "bucket_target": bucket_target}
