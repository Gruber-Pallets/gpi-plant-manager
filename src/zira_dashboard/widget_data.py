"""Data resolvers for the widget type registry.

Each resolver takes `params: dict` (the merged definition.default_data +
placement.data_overrides) and a `day: date`. Returns a dict the type's
Jinja partial consumes.

Resolvers must be robust to missing params — return an empty-state dict
rather than raising. The render layer treats empty data as a graceful
"no data yet" rather than an error.
"""
from __future__ import annotations

from datetime import date
from typing import Optional


def _elapsed_fraction(day: date) -> float:
    """Wrap the existing shift-elapsed-fraction helper so tests can monkeypatch."""
    from .wc_dashboard_data import _shift_elapsed_fraction
    return _shift_elapsed_fraction(day)


def _pallets_units_for_wc(wc_name: str, day: date) -> int:
    """Today's units for one WC. Wraps the existing helper so tests can monkeypatch."""
    from .wc_dashboard_data import _units_today_for_wc
    return _units_today_for_wc(wc_name, day)


def _units_today_for_group(group_name: str, day: date) -> int:
    """Sum of today's units across every WC in `group_name`."""
    from . import work_centers_store
    total = 0
    for loc in work_centers_store.members("group", group_name):
        total += _pallets_units_for_wc(loc.name, day)
    return total


def _resolve_pallets_by_wc(params: dict, day: date) -> dict:
    """Horizontal bar chart, one bar per WC in the group.

    Accepts (any combination):
      - wcs: list of WC names (explicit)
      - groups: list of group names (each expanded to its WCs)
      - group: single group name (legacy back-compat with the original
        pallets_by_wc schema, before the multi-select extension)

    The resulting WC set is the deduplicated UNION of all three.

    Returns: {items: [{name, units, expected, pct, target_pct}, ...], total_u, total_e}.
    """
    from . import staffing, work_centers_store
    params = params or {}
    wc_set: list[str] = []
    seen: set[str] = set()

    def _add(name: str):
        if name and name not in seen:
            seen.add(name)
            wc_set.append(name)

    # Explicit WCs
    for n in (params.get("wcs") or []):
        if isinstance(n, str):
            _add(n)
    # Multi-group expansion
    for g in (params.get("groups") or []):
        if not isinstance(g, str):
            continue
        for loc in work_centers_store.members("group", g) or []:
            _add(loc.name)
    # Legacy single-group back-compat
    legacy_group = params.get("group")
    if isinstance(legacy_group, str) and legacy_group:
        for loc in work_centers_store.members("group", legacy_group) or []:
            _add(loc.name)

    if not wc_set:
        return {"items": [], "total_u": 0, "total_e": 0}

    # Resolve each WC name to its Location for the goal lookup.
    locs_by_name = {loc.name: loc for loc in staffing.LOCATIONS}
    members = [locs_by_name[n] for n in wc_set if n in locs_by_name]
    if not members:
        return {"items": [], "total_u": 0, "total_e": 0}
    frac = _elapsed_fraction(day)
    items: list[dict] = []
    total_u = 0
    total_e = 0
    max_scale = 0
    for loc in members:
        units = _pallets_units_for_wc(loc.name, day)
        full = int(work_centers_store.goal_per_day(loc) or 0)
        expected = full * frac
        total_u += units
        total_e += int(expected)
        scale_target = max(units, expected, full)
        if scale_target > max_scale:
            max_scale = scale_target
        items.append({
            "name": loc.name,
            "units": units,
            "expected": int(expected),
            "full_day_target": full,
        })
    for it in items:
        scale = max_scale if max_scale > 0 else 1
        it["pct"] = (it["units"] / scale * 100.0) if scale else 0.0
        it["target_pct"] = (it["expected"] / scale * 100.0) if scale else None
    return {"items": items, "total_u": total_u, "total_e": total_e}


def _resolve_goat_race(params: dict, day: date) -> dict:
    """Vs. Goat Pace widget — status + race stats vs the group's GOAT,
    prorated by elapsed shift fraction.
    """
    from . import awards
    group = (params or {}).get("group")
    if not group:
        return {
            "group": None, "goat": None, "units_today": 0,
            "goat_pace_today": 0, "status": None,
        }
    goat = awards.goat(group)
    units = _units_today_for_group(group, day)
    if goat is None:
        return {
            "group": group, "goat": None, "units_today": units,
            "goat_pace_today": 0, "status": None,
        }
    frac = _elapsed_fraction(day)
    pace_today = float(goat.get("units", 0)) * frac
    if pace_today <= 0:
        status: Optional[str] = None
    else:
        delta_pct = (units - pace_today) / pace_today * 100.0
        if delta_pct > 5:
            status = "AHEAD"
        elif delta_pct < -5:
            status = "BEHIND"
        else:
            status = "ON_PACE"
    return {
        "group": group, "goat": goat, "units_today": units,
        "goat_pace_today": pace_today, "status": status,
    }


def _resolve_ribbons(params: dict, day: date) -> dict:
    """Top-3 person-days for the group this month."""
    from . import awards
    group = (params or {}).get("group")
    if not group:
        return {"group": None, "entries": []}
    entries = awards.monthly_badges(group, day.year, day.month) or []
    return {"group": group, "entries": entries}


def _resolve_pallets_banner(params: dict, day: date) -> dict:
    """Single-WC pallets banner: today's units vs prorated daily target.

    Wraps `wc_dashboard_data.pallets_banner`. Returns the same dict
    shape: {units_today, target_today, target_full_day, pct_of_target}.
    """
    from . import wc_dashboard_data
    wc_name = (params or {}).get("wc_name")
    if not wc_name:
        return {"units_today": 0, "target_today": 0,
                "target_full_day": 0, "pct_of_target": None}
    return wc_dashboard_data.pallets_banner(wc_name, day)


def _resolve_daily_progress(params: dict, day: date) -> dict:
    """15-min progress chart aggregated across a set of WCs. Same data
    shape as /recycling's `progress_chart` macro consumes:
      {buckets: [{label, actual, target, in_progress}, ...], bucket_target}

    Accepts wcs + groups + legacy single wc_name (back-compat).
    """
    from datetime import datetime, timedelta
    from . import shift_config, wc_dashboard_data, work_centers_store

    params = params or {}
    wc_set: list[str] = []
    seen: set[str] = set()

    def _add(name: str):
        if name and name not in seen:
            seen.add(name)
            wc_set.append(name)

    for n in (params.get("wcs") or []):
        if isinstance(n, str):
            _add(n)
    for g in (params.get("groups") or []):
        if not isinstance(g, str):
            continue
        for loc in work_centers_store.members("group", g) or []:
            _add(loc.name)
    legacy = params.get("wc_name")
    if isinstance(legacy, str) and legacy:
        _add(legacy)

    if not wc_set:
        return {"buckets": [], "bucket_target": 0}

    # Current elapsed shift minutes — used to flag the in-progress bucket.
    try:
        full_minutes = shift_config.productive_minutes_per_day()
    except Exception:
        full_minutes = 480
    elapsed = int(full_minutes * _elapsed_fraction(day))

    # Sum per-bucket {units, target} across all WCs in scope.
    agg: dict[int, dict] = {}
    for name in wc_set:
        per_bucket = wc_dashboard_data.fifteen_min_increments(name, day) or []
        for b in per_bucket:
            idx = b["bucket_index"]
            entry = agg.setdefault(idx, {
                "bucket_index": idx,
                "minute_offset": b["minute_offset"],
                "actual": 0,
                "target": 0,
            })
            entry["actual"] += int(b.get("units") or 0)
            entry["target"] += int(b.get("target") or 0)

    if not agg:
        return {"buckets": [], "bucket_target": 0}

    try:
        shift_start = shift_config.shift_start_for(day)
    except Exception:
        shift_start = None

    buckets = []
    for idx in sorted(agg):
        b = agg[idx]
        if shift_start is not None:
            bucket_dt = datetime.combine(day, shift_start) + timedelta(minutes=b["minute_offset"])
            hour = bucket_dt.hour
            am_pm = "a" if hour < 12 else "p"
            hour_12 = hour % 12 or 12
            label = f"{hour_12}:{bucket_dt.minute:02d}{am_pm}"
        else:
            label = f"+{b['minute_offset']}m"
        in_progress = b["minute_offset"] <= elapsed < b["minute_offset"] + 15
        buckets.append({
            "label": label,
            "actual": b["actual"],
            "target": b["target"],
            "in_progress": in_progress,
        })

    # bucket_target = first non-zero target (they're all equal across the
    # day for a fixed WC count; the legend just needs a single number).
    bucket_target = next((b["target"] for b in buckets if b["target"]), 0)
    return {"buckets": buckets, "bucket_target": bucket_target}


def _resolve_cumulative(params: dict, day: date) -> dict:
    """Cumulative progress data — same per-15-min buckets as
    `_resolve_daily_progress` produces; the `cumulative_progress_chart`
    macro running-totals them in the template.

    Shape: {buckets: [{label, actual, target, in_progress}, ...], bucket_target}
    """
    return _resolve_daily_progress(params, day)


def _resolve_kpi(params: dict, day: date) -> dict:
    """KPI tile — single big number with a label.

    Returns: {label, value, suffix}. The widget partial concatenates
    "{value}{suffix}" and renders `label` above it.
    """
    from . import wc_dashboard_data
    params = params or {}
    metric = params.get("metric") or "units_today_wc"
    if metric == "units_today_wc":
        wc = params.get("wc_name")
        if not wc:
            return {"label": "Units today", "value": 0, "suffix": ""}
        units = wc_dashboard_data._units_today_for_wc(wc, day)
        return {"label": f"Units · {wc}", "value": units, "suffix": ""}
    if metric == "units_today_group":
        group = params.get("group")
        if not group:
            return {"label": "Units today (group)", "value": 0, "suffix": ""}
        units = _units_today_for_group(group, day)
        return {"label": f"Units · {group}", "value": units, "suffix": ""}
    if metric == "downtime_minutes_wc":
        wc = params.get("wc_name")
        if not wc:
            return {"label": "Downtime today", "value": 0, "suffix": "m"}
        report = wc_dashboard_data.downtime_report(wc, day) or {}
        return {"label": f"Downtime · {wc}", "value": int(report.get("total_minutes", 0)), "suffix": "m"}
    return {"label": f"Unknown metric: {metric}", "value": 0, "suffix": ""}


def _resolve_downtime(params: dict, day: date) -> dict:
    """Per-WC downtime — working vs down minutes as stacked bars.

    Same data shape as /recycling's downtime widget:
      {rows: [{name, working, down, working_pct, down_pct}, ...], total_elapsed}

    Accepts wcs + groups + legacy single wc_name (back-compat).
    """
    from . import shift_config, staffing, wc_dashboard_data, work_centers_store

    params = params or {}
    wc_set: list[str] = []
    seen: set[str] = set()

    def _add(name: str):
        if name and name not in seen:
            seen.add(name)
            wc_set.append(name)

    for n in (params.get("wcs") or []):
        if isinstance(n, str):
            _add(n)
    for g in (params.get("groups") or []):
        if not isinstance(g, str):
            continue
        for loc in work_centers_store.members("group", g) or []:
            _add(loc.name)
    # Legacy single-WC back-compat with previously saved presets.
    legacy = params.get("wc_name")
    if isinstance(legacy, str) and legacy:
        _add(legacy)

    if not wc_set:
        return {"rows": [], "total_elapsed": 0}

    # Total elapsed shift minutes today — matches /recycling's per-day
    # elapsed_minutes calculation.
    try:
        full_minutes = shift_config.productive_minutes_per_day()
    except Exception:
        full_minutes = 480  # safe fallback (8 h)
    frac = _elapsed_fraction(day)
    total_elapsed = int(full_minutes * frac)

    rows = []
    for name in wc_set:
        report = wc_dashboard_data.downtime_report(name, day) or {}
        down = int(report.get("total_minutes", 0))
        working = max(0, total_elapsed - down)
        denom = total_elapsed if total_elapsed else 1
        rows.append({
            "name": name,
            "working": working,
            "down": down,
            "working_pct": working / denom * 100.0,
            "down_pct": down / denom * 100.0,
        })
    return {"rows": rows, "total_elapsed": total_elapsed}
