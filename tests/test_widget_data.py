"""Unit tests for widget resolvers. Mock the underlying helpers
(`cached_leaderboard`, `awards.goat`, `awards.monthly_badges`,
`work_centers_store.members`) — resolvers must work without DB."""
from __future__ import annotations

from datetime import date


class _Loc:
    def __init__(self, name, meter_id="m1"):
        self.name = name
        self.meter_id = meter_id


def test_resolve_pallets_by_wc_legacy_group_param(monkeypatch):
    """Old shape: {group: 'Repairs'} — kept for back-compat with seeded presets."""
    from zira_dashboard import widget_data, work_centers_store, staffing

    locs = [_Loc("Repair 1"), _Loc("Repair 2")]
    monkeypatch.setattr(staffing, "LOCATIONS", locs)
    monkeypatch.setattr(
        work_centers_store, "members",
        lambda kind, name: locs if (kind, name) == ("group", "Repairs") else [],
    )
    monkeypatch.setattr(work_centers_store, "goal_per_day", lambda loc: 50)
    monkeypatch.setattr(
        widget_data, "_pallets_units_for_wc",
        lambda wc_name, day: {"Repair 1": 42, "Repair 2": 18}.get(wc_name, 0),
    )
    monkeypatch.setattr(widget_data, "_elapsed_fraction", lambda day: 0.5)

    out = widget_data._resolve_pallets_by_wc(
        {"group": "Repairs"}, day=date(2026, 5, 13),
    )
    items = out["items"]
    assert {i["name"] for i in items} == {"Repair 1", "Repair 2"}
    assert out["total_u"] == 60


def test_resolve_pallets_by_wc_multi_wcs(monkeypatch):
    """New shape: {wcs: ['Repair 1', 'Junior 2']} — explicit WC list."""
    from zira_dashboard import widget_data, work_centers_store, staffing

    locs = [_Loc("Repair 1"), _Loc("Repair 2"), _Loc("Junior 2")]
    monkeypatch.setattr(staffing, "LOCATIONS", locs)
    monkeypatch.setattr(work_centers_store, "members", lambda kind, name: [])
    monkeypatch.setattr(work_centers_store, "goal_per_day", lambda loc: 50)
    monkeypatch.setattr(
        widget_data, "_pallets_units_for_wc",
        lambda wc_name, day: {"Repair 1": 10, "Junior 2": 20}.get(wc_name, 0),
    )
    monkeypatch.setattr(widget_data, "_elapsed_fraction", lambda day: 0.5)

    out = widget_data._resolve_pallets_by_wc(
        {"wcs": ["Repair 1", "Junior 2"]}, day=date(2026, 5, 13),
    )
    assert {i["name"] for i in out["items"]} == {"Repair 1", "Junior 2"}


def test_resolve_pallets_by_wc_multi_groups_union(monkeypatch):
    """New shape: {groups: ['Repairs', 'Dismantlers']} — union of group members."""
    from zira_dashboard import widget_data, work_centers_store, staffing

    locs = [_Loc("Repair 1"), _Loc("Repair 2"), _Loc("Dismantler 1")]
    monkeypatch.setattr(staffing, "LOCATIONS", locs)
    monkeypatch.setattr(
        work_centers_store, "members",
        lambda kind, name: {
            ("group", "Repairs"): [_Loc("Repair 1"), _Loc("Repair 2")],
            ("group", "Dismantlers"): [_Loc("Dismantler 1")],
        }.get((kind, name), []),
    )
    monkeypatch.setattr(work_centers_store, "goal_per_day", lambda loc: 50)
    monkeypatch.setattr(widget_data, "_pallets_units_for_wc", lambda wc, d: 0)
    monkeypatch.setattr(widget_data, "_elapsed_fraction", lambda day: 0.5)

    out = widget_data._resolve_pallets_by_wc(
        {"groups": ["Repairs", "Dismantlers"]}, day=date(2026, 5, 13),
    )
    assert {i["name"] for i in out["items"]} == {"Repair 1", "Repair 2", "Dismantler 1"}


def test_resolve_pallets_by_wc_wcs_plus_groups_dedupes(monkeypatch):
    """wcs + groups together: deduplicated union."""
    from zira_dashboard import widget_data, work_centers_store, staffing

    locs = [_Loc("Repair 1"), _Loc("Repair 2"), _Loc("Junior 2")]
    monkeypatch.setattr(staffing, "LOCATIONS", locs)
    monkeypatch.setattr(
        work_centers_store, "members",
        lambda kind, name: [_Loc("Repair 1"), _Loc("Repair 2")] if (kind, name) == ("group", "Repairs") else [],
    )
    monkeypatch.setattr(work_centers_store, "goal_per_day", lambda loc: 50)
    monkeypatch.setattr(widget_data, "_pallets_units_for_wc", lambda wc, d: 0)
    monkeypatch.setattr(widget_data, "_elapsed_fraction", lambda day: 0.5)

    out = widget_data._resolve_pallets_by_wc(
        # Repair 1 appears both explicitly and via the group — should dedupe.
        {"wcs": ["Repair 1", "Junior 2"], "groups": ["Repairs"]},
        day=date(2026, 5, 13),
    )
    names = [i["name"] for i in out["items"]]
    assert sorted(names) == ["Junior 2", "Repair 1", "Repair 2"]


def test_resolve_pallets_by_wc_no_scope_returns_empty():
    from zira_dashboard import widget_data
    out = widget_data._resolve_pallets_by_wc({}, day=date(2026, 5, 13))
    assert out == {"items": [], "total_u": 0, "total_e": 0}


def test_resolve_goat_race_with_goat(monkeypatch):
    from zira_dashboard import widget_data, awards

    monkeypatch.setattr(
        awards, "goat",
        lambda group_name: {"name": "Alice", "units": 100, "day": "2025-03-15"} if group_name == "Repairs" else None,
    )
    monkeypatch.setattr(
        widget_data, "_units_today_for_group",
        lambda group, day: 60,
    )
    monkeypatch.setattr(widget_data, "_elapsed_fraction", lambda day: 0.5)

    out = widget_data._resolve_goat_race(
        {"group": "Repairs"}, day=date(2026, 5, 13),
    )
    assert out["group"] == "Repairs"
    assert out["goat"]["name"] == "Alice"
    assert out["units_today"] == 60
    assert out["goat_pace_today"] == 50
    assert out["status"] == "AHEAD"


def test_resolve_goat_race_no_goat_yet(monkeypatch):
    from zira_dashboard import widget_data, awards

    monkeypatch.setattr(awards, "goat", lambda group_name: None)
    monkeypatch.setattr(widget_data, "_units_today_for_group", lambda g, d: 30)

    out = widget_data._resolve_goat_race(
        {"group": "Repairs"}, day=date(2026, 5, 13),
    )
    assert out["status"] is None
    assert out["goat"] is None
    assert out["units_today"] == 30


def test_resolve_goat_race_missing_group_returns_empty():
    from zira_dashboard import widget_data
    out = widget_data._resolve_goat_race({}, day=date(2026, 5, 13))
    assert out["group"] is None
    assert out["status"] is None


def test_resolve_ribbons_returns_entries(monkeypatch):
    from zira_dashboard import widget_data, awards

    monkeypatch.setattr(
        awards, "monthly_badges",
        lambda group, year, month: [
            {"position": 1, "name": "Alice", "units": 90},
            {"position": 2, "name": "Bob",   "units": 80},
            {"position": 3, "name": "Carol", "units": 70},
        ] if group == "Repairs" else [],
    )
    out = widget_data._resolve_ribbons(
        {"group": "Repairs"}, day=date(2026, 5, 13),
    )
    assert out["group"] == "Repairs"
    assert len(out["entries"]) == 3
    assert out["entries"][0]["name"] == "Alice"


def test_resolve_ribbons_missing_group_returns_empty():
    from zira_dashboard import widget_data
    out = widget_data._resolve_ribbons({}, day=date(2026, 5, 13))
    assert out == {"group": None, "entries": []}


def test_resolve_pallets_banner_delegates(monkeypatch):
    from zira_dashboard import widget_data, wc_dashboard_data

    monkeypatch.setattr(
        wc_dashboard_data, "pallets_banner",
        lambda wc, d: {
            "units_today": 42, "target_today": 30,
            "target_full_day": 80, "pct_of_target": 140.0,
        } if wc == "Repair 1" else None,
    )
    out = widget_data._resolve_pallets_banner({"wc_name": "Repair 1"}, day=date(2026, 5, 13))
    assert out["units_today"] == 42
    assert out["target_full_day"] == 80


def test_resolve_pallets_banner_missing_wc_returns_empty():
    from zira_dashboard import widget_data
    out = widget_data._resolve_pallets_banner({}, day=date(2026, 5, 13))
    assert out["units_today"] == 0
    assert out["target_today"] == 0
    assert out["pct_of_target"] is None


def test_resolve_daily_progress_aggregates_buckets(monkeypatch):
    from zira_dashboard import widget_data, wc_dashboard_data, work_centers_store, shift_config
    from datetime import time

    monkeypatch.setattr(
        wc_dashboard_data, "fifteen_min_increments",
        lambda wc, d: {
            "Repair 1": [
                {"bucket_index": 0, "minute_offset": 0, "units": 5, "target": 4},
                {"bucket_index": 1, "minute_offset": 15, "units": 2, "target": 4},
            ],
            "Repair 2": [
                {"bucket_index": 0, "minute_offset": 0, "units": 3, "target": 4},
                {"bucket_index": 1, "minute_offset": 15, "units": 1, "target": 4},
            ],
        }.get(wc, []),
    )
    monkeypatch.setattr(work_centers_store, "members", lambda kind, name: [])
    monkeypatch.setattr(shift_config, "productive_minutes_per_day", lambda: 480)
    monkeypatch.setattr(shift_config, "shift_start_for", lambda d: time(7, 0))
    monkeypatch.setattr(widget_data, "_elapsed_fraction", lambda day: 0.0)

    out = widget_data._resolve_daily_progress(
        {"wcs": ["Repair 1", "Repair 2"]}, day=date(2026, 5, 13),
    )
    assert len(out["buckets"]) == 2
    # Bucket 0 actual = 5 + 3 = 8, target = 4 + 4 = 8
    assert out["buckets"][0]["actual"] == 8
    assert out["buckets"][0]["target"] == 8
    # Bucket 1 actual = 2 + 1 = 3
    assert out["buckets"][1]["actual"] == 3
    assert out["buckets"][0]["label"] == "7:00a"
    assert out["buckets"][1]["label"] == "7:15a"


def test_resolve_daily_progress_missing_scope_returns_empty():
    from zira_dashboard import widget_data
    out = widget_data._resolve_daily_progress({}, day=date(2026, 5, 13))
    assert out == {"buckets": [], "bucket_target": 0}


def test_resolve_cumulative_uses_same_buckets_as_daily_progress(monkeypatch):
    """Cumulative widget shares the per-15-min bucket data shape with
    daily_progress; the partial running-totals them in-template."""
    from zira_dashboard import widget_data, wc_dashboard_data, work_centers_store, shift_config
    from datetime import time

    monkeypatch.setattr(
        wc_dashboard_data, "fifteen_min_increments",
        lambda wc, d: [
            {"bucket_index": 0, "minute_offset": 0, "units": 5, "target": 4},
            {"bucket_index": 1, "minute_offset": 15, "units": 3, "target": 4},
        ] if wc == "Repair 1" else [],
    )
    monkeypatch.setattr(work_centers_store, "members", lambda kind, name: [])
    monkeypatch.setattr(shift_config, "productive_minutes_per_day", lambda: 480)
    monkeypatch.setattr(shift_config, "shift_start_for", lambda d: time(7, 0))
    monkeypatch.setattr(widget_data, "_elapsed_fraction", lambda day: 0.0)

    out = widget_data._resolve_cumulative({"wcs": ["Repair 1"]}, day=date(2026, 5, 13))
    assert "buckets" in out and "bucket_target" in out
    assert len(out["buckets"]) == 2
    assert out["buckets"][0]["actual"] == 5
    assert out["buckets"][0]["target"] == 4


def test_resolve_cumulative_missing_scope_returns_empty():
    from zira_dashboard import widget_data
    out = widget_data._resolve_cumulative({}, day=date(2026, 5, 13))
    assert out == {"buckets": [], "bucket_target": 0}


def test_resolve_kpi_units_today_wc(monkeypatch):
    from zira_dashboard import widget_data, wc_dashboard_data
    monkeypatch.setattr(
        wc_dashboard_data, "_units_today_for_wc",
        lambda wc, d: 42 if wc == "Repair 1" else 0,
    )
    out = widget_data._resolve_kpi(
        {"metric": "units_today_wc", "wc_name": "Repair 1"}, day=date(2026, 5, 13),
    )
    assert out["value"] == 42
    assert out["label"] == "Units · Repair 1"


def test_resolve_kpi_units_today_group(monkeypatch):
    from zira_dashboard import widget_data
    monkeypatch.setattr(widget_data, "_units_today_for_group", lambda g, d: 200)
    out = widget_data._resolve_kpi(
        {"metric": "units_today_group", "group": "Repairs"}, day=date(2026, 5, 13),
    )
    assert out["value"] == 200
    assert out["label"] == "Units · Repairs"


def test_resolve_kpi_downtime_minutes(monkeypatch):
    from zira_dashboard import widget_data, wc_dashboard_data
    monkeypatch.setattr(
        wc_dashboard_data, "downtime_report",
        lambda wc, d: {"events": [], "total_minutes": 17} if wc == "Repair 1" else {},
    )
    out = widget_data._resolve_kpi(
        {"metric": "downtime_minutes_wc", "wc_name": "Repair 1"}, day=date(2026, 5, 13),
    )
    assert out["value"] == 17
    assert out["suffix"] == "m"


def test_resolve_kpi_unknown_metric_returns_placeholder():
    from zira_dashboard import widget_data
    out = widget_data._resolve_kpi({"metric": "garbage"}, day=date(2026, 5, 13))
    assert out["value"] == 0
    assert "garbage" in out["label"]


def test_resolve_downtime_returns_per_wc_rows(monkeypatch):
    from zira_dashboard import widget_data, wc_dashboard_data, work_centers_store, shift_config

    monkeypatch.setattr(
        wc_dashboard_data, "downtime_report",
        lambda wc, d: {
            "Repair 1": {"total_minutes": 20, "events": []},
            "Repair 2": {"total_minutes": 5, "events": []},
        }.get(wc, {"total_minutes": 0, "events": []}),
    )
    monkeypatch.setattr(work_centers_store, "members", lambda kind, name: [])
    monkeypatch.setattr(shift_config, "productive_minutes_per_day", lambda: 480)
    monkeypatch.setattr(widget_data, "_elapsed_fraction", lambda day: 0.5)
    # total_elapsed = 480 * 0.5 = 240

    out = widget_data._resolve_downtime(
        {"wcs": ["Repair 1", "Repair 2"]}, day=date(2026, 5, 13),
    )
    assert out["total_elapsed"] == 240
    rows = {r["name"]: r for r in out["rows"]}
    assert rows["Repair 1"]["down"] == 20
    assert rows["Repair 1"]["working"] == 220
    # 220 / 240 * 100 ≈ 91.67
    assert abs(rows["Repair 1"]["working_pct"] - (220 / 240 * 100.0)) < 0.01


def test_resolve_downtime_legacy_wc_name(monkeypatch):
    """Old shape: {wc_name: 'Repair 1'} kept for back-compat."""
    from zira_dashboard import widget_data, wc_dashboard_data, work_centers_store, shift_config

    monkeypatch.setattr(
        wc_dashboard_data, "downtime_report",
        lambda wc, d: {"total_minutes": 10, "events": []},
    )
    monkeypatch.setattr(work_centers_store, "members", lambda kind, name: [])
    monkeypatch.setattr(shift_config, "productive_minutes_per_day", lambda: 480)
    monkeypatch.setattr(widget_data, "_elapsed_fraction", lambda day: 0.5)

    out = widget_data._resolve_downtime({"wc_name": "Repair 1"}, day=date(2026, 5, 13))
    assert len(out["rows"]) == 1
    assert out["rows"][0]["name"] == "Repair 1"


def test_resolve_downtime_missing_scope_returns_empty():
    from zira_dashboard import widget_data
    out = widget_data._resolve_downtime({}, day=date(2026, 5, 13))
    assert out == {"rows": [], "total_elapsed": 0}
