"""Unit tests for widget resolvers. Mock the underlying helpers
(`cached_leaderboard`, `awards.goat`, `awards.monthly_badges`,
`work_centers_store.members`) — resolvers must work without DB."""
from __future__ import annotations

from datetime import date


def test_resolve_pallets_by_wc_returns_items_and_total(monkeypatch):
    from zira_dashboard import widget_data, work_centers_store

    class _Loc:
        def __init__(self, name, meter_id="m1"):
            self.name = name
            self.meter_id = meter_id

    monkeypatch.setattr(
        work_centers_store, "members",
        lambda kind, name: [_Loc("Repair 1"), _Loc("Repair 2")] if (kind, name) == ("group", "Repairs") else [],
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
    assert isinstance(out, dict)
    items = out["items"]
    assert {i["name"] for i in items} == {"Repair 1", "Repair 2"}
    total = sum(i["units"] for i in items)
    assert out["total_u"] == total


def test_resolve_pallets_by_wc_missing_group_returns_empty():
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
