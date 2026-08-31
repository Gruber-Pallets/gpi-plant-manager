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
    agg_segments: dict | None = None,
    agg_segment_display: dict | None = None,
    agg_producers: dict[str, tuple[str, ...]] | None = None,
    is_live: bool = True,
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
            "who": (
                agg_who_today.get(name)
                if not is_range and is_live
                else None
            ),
            "units": units,
            "pct_of_target": round(pct_of_target, 1) if pct_of_target is not None else None,
            "expected": int(round(expected)),
            "color": progress_color(pct_of_target),
            "downtime_minutes": agg_downtime.get(name, 0),
        })

    agg_segments = agg_segments or {}
    agg_segment_display = agg_segment_display or {}
    agg_producers = agg_producers or {}
    for row in out:
        row["uses_split_format"] = bool(
            not is_range and agg_segment_display.get(row["name"], False)
        )
        producer_names = (
            tuple(agg_producers.get(row["name"], ())) if not is_range else ()
        )
        row["producer_names"] = producer_names
        row["sole_producer_name"] = (
            producer_names[0] if len(producer_names) == 1 else None
        )
        row["show_segment_worker_names"] = len(producer_names) >= 2
    spans = {
        row["name"]: sum(
            float(segment.get("runway_units", 0.0) or 0.0)
            for segment in agg_segments.get(row["name"], ())
        )
        for row in out
    }
    base = (
        max(
            spans[row["name"]]
            if row["uses_split_format"] and spans[row["name"]] > 0
            else max(float(row["units"]), float(row["expected"]))
            for row in out
        )
        if out
        else 0.0
    )
    scale = base * 1.1 if base > 0 else 1.0
    has_target_line = any(row["expected"] > 0 for row in out)

    for row in out:
        source_segments = (
            tuple(agg_segments.get(row["name"], ())) if not is_range else ()
        )
        cursor = 0.0
        geometry = []
        for segment in source_segments:
            actual = max(0.0, float(segment.get("actual_units", 0.0) or 0.0))
            goal = max(0.0, float(segment.get("goal_units", 0.0) or 0.0))
            runway = max(actual, goal)
            item = dict(segment)
            item.update(
                {
                    "start_pct": cursor / scale * 100.0,
                    "actual_pct": actual / scale * 100.0,
                    "shortfall_start_pct": (cursor + actual) / scale * 100.0,
                    "shortfall_pct": max(goal - actual, 0.0) / scale * 100.0,
                    "finish_pct": (
                        (cursor + goal) / scale * 100.0 if goal > 0 else None
                    ),
                    "runway_pct": runway / scale * 100.0,
                    "label_below": actual / scale * 100.0 < 18.0,
                }
            )
            geometry.append(item)
            cursor += runway
        row["segments"] = geometry
        row["has_segments"] = bool(row["uses_split_format"] and geometry)
        row["has_worker_history"] = any(
            segment.get("person_name") for segment in geometry
        )
        row["no_one_here_now"] = bool(
            is_live
            and row["has_segments"]
            and row["show_segment_worker_names"]
            and not row["who"]
            and row["has_worker_history"]
        )
        row["pct"] = float(row["units"]) / scale * 100.0
        row["target_pct"] = (
            float(row["expected"]) / scale * 100.0
            if scale and has_target_line and not row["has_segments"]
            else None
        )
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
    categories: tuple[str, ...] = ("Dismantler", "Repair"),
) -> list[dict]:
    """Working/down split per WC for the downtime widget.

    Extracted verbatim from the `_downtime_rows` closure in `_render_recycling`.
    Promoted closed-over vars: `agg_active_names`, `agg_category`,
    `agg_downtime`, `total_elapsed`, `agg_who_today`, `is_range`.
    """
    names = sorted(
        n for n in agg_active_names
        if agg_category.get(n) in categories
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


def compute_per_wc_expected(
    *,
    segments,
    active_wc_names,
    target_per_hour,
    productive_minutes,
    productive_minutes_for_segment=None,
):
    """Prorated expected pallets per ACTIVE work center.

    Mirrors the route wiring exactly: filter segments to the active WCs, sum via
    assignment_windows.expected_by_wc, then default every active WC to 0.0 so the
    dashboard shows a goal even before production. `productive_minutes(name,
    wc_name, start, end)` MUST be the breaks-only shift_config.productive_minutes_in_window
    closure -- NOT effective_minutes_worked, which would wrongly shrink the pace
    goal on partial-leave days (the June 2026 regression)."""
    from . import assignment_windows
    active = [s for s in segments if s.wc_name in active_wc_names]
    out = assignment_windows.expected_by_wc(
        active,
        target_per_hour,
        productive_minutes,
        productive_minutes_for_segment=productive_minutes_for_segment,
    )
    for name in active_wc_names:
        out.setdefault(name, 0.0)
    return out
