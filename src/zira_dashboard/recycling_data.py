"""Pure data/computation helpers for the recycling dashboards, extracted from
routes/departments.py. No DB / Odoo / Request / template imports — callers pass
already-loaded data + injected callables. Lets the goal math be unit-tested
without a backend.
"""

from __future__ import annotations


def progress_color(pct_of_target: float | None) -> str | None:
    """HSL color for an actual-vs-goal percentage. Neutral gray at 100%
    (was pure white, invisible on light-mode backgrounds); ramps to red
    below and green above. Saturation/lightness step in 12 buckets so
    big misses stand out and small ones are subtle.
    """
    if pct_of_target is None:
        return None
    delta = max(-100.0, min(100.0, pct_of_target - 100.0))
    if abs(delta) < 1.0:
        return "#9ca3af"  # neutral gray — readable on both light + dark
    step = min(12, max(1, round(abs(delta) / 100.0 * 12)))
    sat = 55.0 + step * 2.0
    light = 65.0 - step * 3.5
    hue = 130 if delta > 0 else 0
    return f"hsl({hue:.0f}, {sat:.0f}%, {light:.0f}%)"


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
