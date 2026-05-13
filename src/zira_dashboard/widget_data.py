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

    Returns: {items: [{name, units, expected, pct, target_pct}, ...], total_u, total_e}.
    """
    from . import work_centers_store
    group = (params or {}).get("group")
    if not group:
        return {"items": [], "total_u": 0, "total_e": 0}
    members = work_centers_store.members("group", group) or []
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
    """Per-15-min bar chart with target-based color (green/amber/red).

    Wraps `wc_dashboard_data.fifteen_min_increments`. Returns
    {buckets: [...], target}.
    """
    from . import wc_dashboard_data
    wc_name = (params or {}).get("wc_name")
    if not wc_name:
        return {"buckets": [], "target": 0}
    buckets = wc_dashboard_data.fifteen_min_increments(wc_name, day) or []
    target = buckets[0]["target"] if buckets else 0
    return {"buckets": buckets, "target": target}


def _resolve_cumulative(params: dict, day: date) -> dict:
    """Cumulative bucket data + the WC's full-day target for the goal line.

    Wraps `wc_dashboard_data.daily_progress` (which returns cumulative
    per bucket) and pulls the full-day goal from `pallets_banner`.
    """
    from . import wc_dashboard_data
    wc_name = (params or {}).get("wc_name")
    if not wc_name:
        return {"points": [], "max_y": 0}
    points = wc_dashboard_data.daily_progress(wc_name, day) or []
    banner = wc_dashboard_data.pallets_banner(wc_name, day) or {}
    max_y = banner.get("target_full_day") or 0
    return {"points": points, "max_y": max_y}


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
