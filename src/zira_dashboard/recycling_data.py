"""Pure data/computation helpers for the recycling dashboards, extracted from
routes/departments.py. No DB / Odoo / Request / template imports — callers pass
already-loaded data + injected callables. Lets the goal math be unit-tested
without a backend.
"""

from __future__ import annotations


def progress_color(pct_of_target: float | None) -> str | None:
    """HSL color for an actual-vs-goal percentage.

    At/above goal ramps green, close under-goal near misses stay neutral gray,
    and larger misses ramp red. Saturation/lightness step in 12 buckets so big
    misses stand out and small ones are subtle.
    """
    if pct_of_target is None:
        return None
    delta = max(-100.0, min(100.0, pct_of_target - 100.0))
    if -1.0 <= delta < 0.0:
        return "#9ca3af"  # neutral gray — readable on both light + dark
    step = min(12, max(1, round(abs(delta) / 100.0 * 12)))
    sat = 55.0 + step * 2.0
    light = 65.0 - step * 3.5
    hue = 130 if delta >= 0 else 0
    return f"hsl({hue:.0f}, {sat:.0f}%, {light:.0f}%)"


def aggregate_buckets(per_day_buckets: list[list[dict]]) -> list[dict]:
    """Sum per-day time-of-day buckets into a single label-keyed series.

    Extracted verbatim from the `_aggregate_buckets` closure in
    `_render_recycling`. Closes over nothing — fully driven by its arg.
    """
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


def group_goal(category: str, *, elapsed_hours_total: float, agg_expected: dict, agg_category: dict) -> float:
    """Group hourly target — average over total elapsed hours, summing per-WC
    expected for the given category.

    Extracted verbatim from the `_group_goal` closure in `_render_recycling`.
    Promoted closed-over vars: `elapsed_hours_total`, `agg_expected`,
    `agg_category`.
    """
    if elapsed_hours_total <= 0:
        return 0.0
    total_expected = sum(
        agg_expected[name]
        for name in agg_expected
        if agg_category.get(name) == category
    )
    return total_expected / elapsed_hours_total


def build_bars(
    category: str,
    *,
    agg_active_names,
    agg_category: dict,
    agg_units: dict,
    agg_expected: dict,
    agg_who_today: dict,
    is_range: bool,
    agg_downtime: dict,
) -> list[dict]:
    """Per-WC bar rows for a category, with progress color + scaled bar widths.

    Extracted verbatim from the `_bars` closure in `_render_recycling`.
    Promoted closed-over vars: `agg_active_names`, `agg_category`, `agg_units`,
    `agg_expected`, `agg_who_today`, `is_range`, `agg_downtime`. Calls
    `progress_color` (this module).
    """
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
            "color": progress_color(pct_of_target),
            "downtime_minutes": agg_downtime.get(name, 0),
        })
    max_u = max((r["units"] for r in out), default=0)
    max_e = max((r["expected"] for r in out), default=0)
    base = max(max_u, max_e)
    scale = (base * 1.1) if base > 0 else 1.0
    has_target_line = (max_e > 0)
    for r in out:
        r["pct"] = (r["units"] / scale * 100.0) if scale else 0.0
        r["target_pct"] = (r["expected"] / scale * 100.0) if (scale and has_target_line) else None
    return out


def sort_bars(items: list, widget_id: str, *, customs_all: dict) -> list:
    """Apply the widget's saved sort preference to a list of bar rows.

    Extracted verbatim from the `_sorted_bars` closure in `_render_recycling`.
    Promoted closed-over var: `customs_all`.
    """
    s = customs_all.get(widget_id, {}).get("sort", "preset")
    if s == "desc":  return sorted(items, key=lambda x: -x["units"])
    if s == "asc":   return sorted(items, key=lambda x: x["units"])
    if s == "alpha": return sorted(items, key=lambda x: x["name"].lower())
    return items


def build_downtime_rows(
    *,
    agg_active_names,
    agg_category: dict,
    agg_downtime: dict,
    total_elapsed: float,
    agg_who_today: dict,
    is_range: bool,
) -> list[dict]:
    """Working/down split per WC for the downtime widget.

    Extracted verbatim from the `_downtime_rows` closure in `_render_recycling`.
    Promoted closed-over vars: `agg_active_names`, `agg_category`,
    `agg_downtime`, `total_elapsed`, `agg_who_today`, `is_range`.
    """
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


def compute_per_wc_expected(*, segments, active_wc_names, target_per_hour, productive_minutes):
    """Prorated expected pallets per ACTIVE work center.

    Mirrors the route wiring exactly: filter segments to the active WCs, sum via
    assignment_windows.expected_by_wc, then default every active WC to 0.0 so the
    dashboard shows a goal even before production. `productive_minutes(name,
    start, end)` MUST be the breaks-only shift_config.productive_minutes_in_window
    closure -- NOT effective_minutes_worked, which would wrongly shrink the pace
    goal on partial-leave days (the June 2026 regression)."""
    from . import assignment_windows
    active = [s for s in segments if s.wc_name in active_wc_names]
    out = assignment_windows.expected_by_wc(active, target_per_hour, productive_minutes)
    for name in active_wc_names:
        out.setdefault(name, 0.0)
    return out
