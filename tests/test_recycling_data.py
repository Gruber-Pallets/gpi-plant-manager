"""Characterization tests for zira_dashboard.recycling_data.

These pin the EXACT current behavior of the pure helpers extracted from
routes/departments.py, so a future refactor cannot silently change a
dashboard color (or any other computed value). Pure -- no backend needed.
"""

from zira_dashboard import recycling_data as rd


def test_progress_color_none_goal_and_near_miss_band():
    # None in -> None out.
    assert rd.progress_color(None) is None
    # At or over goal -> green, even when the overage is tiny.
    assert rd.progress_color(100.0) == "hsl(130, 57%, 62%)"
    assert rd.progress_color(100.5) == "hsl(130, 57%, 62%)"
    # Close but still under goal -> neutral gray.
    assert rd.progress_color(99.5) == "#9ca3af"
    assert rd.progress_color(99.0) == "#9ca3af"


def test_progress_color_ramps_and_clamps():
    # Just over the target and just below the near-miss band.
    assert rd.progress_color(101.0) == "hsl(130, 57%, 62%)"
    assert rd.progress_color(98.9) == "hsl(0, 57%, 62%)"
    # Mid ramp (50% off goal in either direction): step 6 bucket.
    assert rd.progress_color(150.0) == "hsl(130, 67%, 44%)"
    assert rd.progress_color(50.0) == "hsl(0, 67%, 44%)"
    # Far end of the ramp: step 12 bucket (delta clamps at +/-100).
    assert rd.progress_color(200.0) == "hsl(130, 79%, 23%)"
    assert rd.progress_color(0.0) == "hsl(0, 79%, 23%)"
    # Beyond the clamp stays pinned to the step-12 extreme.
    assert rd.progress_color(300.0) == "hsl(130, 79%, 23%)"
    assert rd.progress_color(-50.0) == "hsl(0, 79%, 23%)"


def test_aggregate_buckets_sums_across_days_by_label():
    # Two days with an overlapping "06:00" label and disjoint others. The
    # helper sums actual/target per label, ORs in_progress, and returns rows
    # in sorted-label order.
    day1 = [
        {"label": "06:00", "actual": 5, "target": 10, "in_progress": False},
        {"label": "07:00", "actual": 3, "target": 8, "in_progress": True},
    ]
    day2 = [
        {"label": "06:00", "actual": 2, "target": 4, "in_progress": False},
        {"label": "08:00", "actual": 1, "target": 2, "in_progress": False},
    ]
    out = rd.aggregate_buckets([day1, day2])
    assert out == [
        {"label": "06:00", "actual": 7, "target": 14, "in_progress": False},
        {"label": "07:00", "actual": 3, "target": 8, "in_progress": True},
        {"label": "08:00", "actual": 1, "target": 2, "in_progress": False},
    ]


def test_aggregate_buckets_empty():
    assert rd.aggregate_buckets([]) == []
    assert rd.aggregate_buckets([[], []]) == []


def test_group_goal_sums_category_expected_over_hours():
    agg_expected = {"D1": 48.0, "D2": 24.0, "R1": 12.0}
    agg_category = {"D1": "Dismantler", "D2": "Dismantler", "R1": "Repair"}
    # Dismantler: (48 + 24) / 4h = 18.0; Repair: 12 / 4h = 3.0.
    assert rd.group_goal(
        "Dismantler", elapsed_hours_total=4.0,
        agg_expected=agg_expected, agg_category=agg_category,
    ) == 18.0
    assert rd.group_goal(
        "Repair", elapsed_hours_total=4.0,
        agg_expected=agg_expected, agg_category=agg_category,
    ) == 3.0


def test_group_goal_zero_hours_short_circuits():
    # elapsed_hours_total <= 0 -> 0.0, no division.
    assert rd.group_goal(
        "Dismantler", elapsed_hours_total=0.0,
        agg_expected={"D1": 48.0}, agg_category={"D1": "Dismantler"},
    ) == 0.0


def test_build_bars_pct_color_scale_and_filtering():
    agg_active_names = {"D1", "D2", "R1"}
    agg_category = {"D1": "Dismantler", "D2": "Dismantler", "R1": "Repair"}
    agg_units = {"D1": 60, "D2": 12, "R1": 99}
    agg_expected = {"D1": 48.0, "D2": 24.0, "R1": 50.0}
    agg_who_today = {"D1": "Alice", "D2": "Bob"}
    agg_downtime = {"D1": 15}
    bars = rd.build_bars(
        "Dismantler",
        agg_active_names=agg_active_names,
        agg_category=agg_category,
        agg_units=agg_units,
        agg_expected=agg_expected,
        agg_who_today=agg_who_today,
        is_range=False,
        agg_downtime=agg_downtime,
    )
    # Only Dismantler WCs, sorted alpha by name.
    assert [b["name"] for b in bars] == ["D1", "D2"]
    d1, d2 = bars
    # pct_of_target = units / expected * 100, rounded to 1dp.
    assert d1["pct_of_target"] == 125.0   # 60/48*100
    assert d2["pct_of_target"] == 50.0    # 12/24*100
    # expected coerced to rounded int; downtime defaulted to 0.
    assert d1["expected"] == 48 and d2["expected"] == 24
    assert d1["downtime_minutes"] == 15 and d2["downtime_minutes"] == 0
    # who comes through because is_range is False.
    assert d1["who"] == "Alice" and d2["who"] == "Bob"
    # color is exactly progress_color(pct_of_target) — pin the integration.
    assert d1["color"] == rd.progress_color(125.0)
    assert d2["color"] == rd.progress_color(50.0)
    # Bar widths scale to base = max(max_units, max_expected) * 1.1.
    # max_u = 60, max_e = 48 -> base = 60, scale = 66.0, has_target_line True.
    assert d1["pct"] == 60 / 66.0 * 100.0
    assert d1["target_pct"] == 48 / 66.0 * 100.0
    assert d2["pct"] == 12 / 66.0 * 100.0
    assert d2["target_pct"] == 24 / 66.0 * 100.0


def test_build_bars_no_expected_drops_target_line_and_pct():
    # Expected == 0 -> pct_of_target None, color None; max_e == 0 -> no target line.
    bars = rd.build_bars(
        "Dismantler",
        agg_active_names={"D1"},
        agg_category={"D1": "Dismantler"},
        agg_units={"D1": 10},
        agg_expected={"D1": 0.0},
        agg_who_today={},
        is_range=False,
        agg_downtime={},
    )
    (d1,) = bars
    assert d1["pct_of_target"] is None
    assert d1["color"] is None
    assert d1["target_pct"] is None
    # base = max(10, 0) = 10 -> scale = 11.0.
    assert d1["pct"] == 10 / 11.0 * 100.0


def test_build_bars_is_range_suppresses_who():
    bars = rd.build_bars(
        "Repair",
        agg_active_names={"R1"},
        agg_category={"R1": "Repair"},
        agg_units={"R1": 5},
        agg_expected={"R1": 10.0},
        agg_who_today={"R1": "Carol"},
        is_range=True,
        agg_downtime={},
    )
    assert bars[0]["who"] is None


def test_sort_bars_honors_widget_preference():
    items = [
        {"name": "Beta", "units": 5},
        {"name": "alpha", "units": 20},
        {"name": "Gamma", "units": 12},
    ]
    customs = {
        "w-desc": {"sort": "desc"},
        "w-asc": {"sort": "asc"},
        "w-alpha": {"sort": "alpha"},
        "w-preset": {"sort": "preset"},
    }
    assert [b["units"] for b in rd.sort_bars(items, "w-desc", customs_all=customs)] == [20, 12, 5]
    assert [b["units"] for b in rd.sort_bars(items, "w-asc", customs_all=customs)] == [5, 12, 20]
    # alpha is case-insensitive.
    assert [b["name"] for b in rd.sort_bars(items, "w-alpha", customs_all=customs)] == ["alpha", "Beta", "Gamma"]
    # preset and unknown widget both return the list unchanged (identity).
    assert rd.sort_bars(items, "w-preset", customs_all=customs) is items
    assert rd.sort_bars(items, "missing", customs_all=customs) is items


def test_build_downtime_rows_split_and_filtering():
    out = rd.build_downtime_rows(
        agg_active_names={"D1", "R1", "Other"},
        agg_category={"D1": "Dismantler", "R1": "Repair", "Other": "Other"},
        agg_downtime={"D1": 60},
        total_elapsed=480,
        agg_who_today={"D1": "Alice"},
        is_range=False,
    )
    # "Other" category filtered out; rows sorted by name.
    assert [r["name"] for r in out] == ["D1", "R1"]
    d1, r1 = out
    # D1: 60 down of 480 -> 420 working, 87.5% up / 12.5% down.
    assert d1["down"] == 60 and d1["working"] == 420
    assert d1["working_pct"] == 87.5 and d1["down_pct"] == 12.5
    assert d1["who"] == "Alice"
    # R1: no downtime -> full working, who defaults to None (not in who map).
    assert r1["down"] == 0 and r1["working"] == 480
    assert r1["working_pct"] == 100.0 and r1["down_pct"] == 0.0
    assert r1["who"] is None


def test_build_downtime_rows_zero_elapsed_avoids_div_by_zero():
    # total_elapsed == 0 -> denominator forced to 1; working clamped at 0.
    out = rd.build_downtime_rows(
        agg_active_names={"D1"},
        agg_category={"D1": "Dismantler"},
        agg_downtime={"D1": 30},
        total_elapsed=0,
        agg_who_today={},
        is_range=True,
    )
    (d1,) = out
    assert d1["working"] == 0
    assert d1["down"] == 30
    assert d1["working_pct"] == 0.0
    assert d1["down_pct"] == 30 / 1 * 100.0
    assert d1["who"] is None


def test_compute_per_wc_expected_filters_active_and_defaults_zero():
    from datetime import datetime, timezone
    from zira_dashboard import assignment_windows as aw
    def u(h):
        return datetime(2026, 6, 2, h, tzinfo=timezone.utc)
    segs = [aw.WorkSegment("Dismantler 1", "A", u(12), u(20), "schedule"),
            aw.WorkSegment("Inactive WC", "B", u(12), u(20), "schedule")]
    out = rd.compute_per_wc_expected(
        segments=segs, active_wc_names={"Dismantler 1", "Dismantler 4"},
        target_per_hour={"Dismantler 1": 6.0, "Inactive WC": 6.0},
        productive_minutes=lambda n, s, e: (e - s).total_seconds() / 60.0)
    assert out["Dismantler 1"] == 48.0      # active + worked (8h * 6/hr)
    assert out["Dismantler 4"] == 0.0       # active, no segment -> defaulted
    assert "Inactive WC" not in out          # not in active set -> filtered
