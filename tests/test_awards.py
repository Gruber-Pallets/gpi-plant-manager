"""Unit tests for awards.py — computation engine.

These tests stub production_history.daily_records and
work_centers_store.members so they don't need DATABASE_URL or a
running Zira/cached_leaderboard.
"""
from __future__ import annotations

from datetime import date


def _stub_data(monkeypatch, *, records, members_map):
    """records: list of dicts (day, person, wc, units, hours, downtime)
    members_map: {group_name: [wc_name, ...]}"""
    from zira_dashboard import production_history, work_centers_store

    class _FakeLoc:
        def __init__(self, name): self.name = name

    monkeypatch.setattr(
        production_history,
        "daily_records",
        lambda s, e, c=None: [r for r in records if s <= r["day"] <= e],
    )
    monkeypatch.setattr(
        work_centers_store,
        "members",
        lambda kind, name: [_FakeLoc(n) for n in members_map.get(name, [])],
    )


def test_person_days_in_group_sums_units_and_hours_per_day(monkeypatch):
    """Per-(person, day), units and hours sum across the group's WCs."""
    _stub_data(
        monkeypatch,
        records=[
            {"day": date(2026, 4, 1), "person": "Alice", "wc": "Repair 1",
             "units": 60.0, "hours": 4.0, "downtime": 0.0},
            {"day": date(2026, 4, 1), "person": "Alice", "wc": "Repair 2",
             "units": 40.0, "hours": 4.0, "downtime": 0.0},
            {"day": date(2026, 4, 1), "person": "Bob", "wc": "Repair 1",
             "units": 50.0, "hours": 8.0, "downtime": 0.0},
        ],
        members_map={"Repairs": ["Repair 1", "Repair 2"]},
    )
    from zira_dashboard import awards
    rows = awards.person_days_in_group("Repairs", date(2026, 4, 1), date(2026, 4, 1))
    by_person = {r["name"]: r for r in rows}
    assert by_person["Alice"]["units"] == 100.0
    assert by_person["Alice"]["hours"] == 8.0
    assert by_person["Bob"]["units"] == 50.0


def test_person_days_in_group_excludes_zero_unit_days(monkeypatch):
    _stub_data(
        monkeypatch,
        records=[
            {"day": date(2026, 4, 1), "person": "Alice", "wc": "Repair 1",
             "units": 0.0, "hours": 8.0, "downtime": 0.0},
        ],
        members_map={"Repairs": ["Repair 1"]},
    )
    from zira_dashboard import awards
    rows = awards.person_days_in_group("Repairs", date(2026, 4, 1), date(2026, 4, 1))
    assert rows == []


def test_monthly_badges_top_3_by_units(monkeypatch):
    _stub_data(
        monkeypatch,
        records=[
            {"day": date(2026, 4, 1), "person": "A", "wc": "Repair 1",
             "units": 100.0, "hours": 8.0, "downtime": 0.0},
            {"day": date(2026, 4, 5), "person": "B", "wc": "Repair 1",
             "units": 90.0, "hours": 8.0, "downtime": 0.0},
            {"day": date(2026, 4, 7), "person": "C", "wc": "Repair 1",
             "units": 80.0, "hours": 8.0, "downtime": 0.0},
            {"day": date(2026, 4, 12), "person": "D", "wc": "Repair 1",
             "units": 70.0, "hours": 8.0, "downtime": 0.0},
            {"day": date(2026, 4, 15), "person": "E", "wc": "Repair 1",
             "units": 60.0, "hours": 8.0, "downtime": 0.0},
        ],
        members_map={"Repairs": ["Repair 1"]},
    )
    from zira_dashboard import awards
    badges = awards.monthly_badges("Repairs", 2026, 4)
    assert [b["position"] for b in badges] == [1, 2, 3]
    assert [b["name"] for b in badges] == ["A", "B", "C"]
    assert badges[0]["units"] == 100.0


def test_monthly_badges_tiebreak_by_pph(monkeypatch):
    """Equal units — fewer hours (higher pph) ranks ahead."""
    _stub_data(
        monkeypatch,
        records=[
            {"day": date(2026, 4, 1), "person": "Slow", "wc": "Repair 1",
             "units": 100.0, "hours": 10.0, "downtime": 0.0},
            {"day": date(2026, 4, 2), "person": "Fast", "wc": "Repair 1",
             "units": 100.0, "hours": 5.0, "downtime": 0.0},
        ],
        members_map={"Repairs": ["Repair 1"]},
    )
    from zira_dashboard import awards
    badges = awards.monthly_badges("Repairs", 2026, 4)
    assert badges[0]["name"] == "Fast"
    assert badges[1]["name"] == "Slow"


def test_monthly_badges_tiebreak_by_name_when_pph_equal(monkeypatch):
    _stub_data(
        monkeypatch,
        records=[
            {"day": date(2026, 4, 1), "person": "Bob", "wc": "Repair 1",
             "units": 100.0, "hours": 5.0, "downtime": 0.0},
            {"day": date(2026, 4, 2), "person": "Anne", "wc": "Repair 1",
             "units": 100.0, "hours": 5.0, "downtime": 0.0},
        ],
        members_map={"Repairs": ["Repair 1"]},
    )
    from zira_dashboard import awards
    badges = awards.monthly_badges("Repairs", 2026, 4)
    assert badges[0]["name"] == "Anne"


def test_monthly_badges_only_within_month(monkeypatch):
    _stub_data(
        monkeypatch,
        records=[
            {"day": date(2026, 3, 31), "person": "X", "wc": "Repair 1",
             "units": 999.0, "hours": 8.0, "downtime": 0.0},
            {"day": date(2026, 4, 1), "person": "Y", "wc": "Repair 1",
             "units": 50.0, "hours": 8.0, "downtime": 0.0},
            {"day": date(2026, 5, 1), "person": "Z", "wc": "Repair 1",
             "units": 999.0, "hours": 8.0, "downtime": 0.0},
        ],
        members_map={"Repairs": ["Repair 1"]},
    )
    from zira_dashboard import awards
    badges = awards.monthly_badges("Repairs", 2026, 4)
    assert [b["name"] for b in badges] == ["Y"]


def test_annual_top_days_top_3_by_units(monkeypatch):
    _stub_data(
        monkeypatch,
        records=[
            {"day": date(2026, 1, 5), "person": "A", "wc": "Repair 1",
             "units": 100.0, "hours": 8.0, "downtime": 0.0},
            {"day": date(2026, 6, 1), "person": "B", "wc": "Repair 1",
             "units": 200.0, "hours": 8.0, "downtime": 0.0},
            {"day": date(2026, 12, 31), "person": "C", "wc": "Repair 1",
             "units": 150.0, "hours": 8.0, "downtime": 0.0},
            {"day": date(2026, 7, 7), "person": "D", "wc": "Repair 1",
             "units": 50.0, "hours": 8.0, "downtime": 0.0},
        ],
        members_map={"Repairs": ["Repair 1"]},
    )
    from zira_dashboard import awards
    top = awards.annual_top_days("Repairs", 2026)
    assert [t["name"] for t in top] == ["B", "C", "A"]


def test_goat_returns_max_single_day(monkeypatch):
    _stub_data(
        monkeypatch,
        records=[
            {"day": date(2025, 1, 1), "person": "Old", "wc": "Repair 1",
             "units": 200.0, "hours": 8.0, "downtime": 0.0},
            {"day": date(2026, 4, 1), "person": "New", "wc": "Repair 1",
             "units": 250.0, "hours": 8.0, "downtime": 0.0},
        ],
        members_map={"Repairs": ["Repair 1"]},
    )
    from zira_dashboard import awards
    monkeypatch.setattr(awards, "_all_time_range", lambda: (date(2025, 1, 1), date(2026, 4, 1)))
    g = awards.goat("Repairs")
    assert g["name"] == "New"
    assert g["units"] == 250.0


def test_goat_first_to_set_on_tie(monkeypatch):
    _stub_data(
        monkeypatch,
        records=[
            {"day": date(2025, 6, 1), "person": "First", "wc": "Repair 1",
             "units": 200.0, "hours": 8.0, "downtime": 0.0},
            {"day": date(2026, 4, 1), "person": "Tied", "wc": "Repair 1",
             "units": 200.0, "hours": 8.0, "downtime": 0.0},
        ],
        members_map={"Repairs": ["Repair 1"]},
    )
    from zira_dashboard import awards
    monkeypatch.setattr(awards, "_all_time_range", lambda: (date(2025, 1, 1), date(2026, 12, 31)))
    g = awards.goat("Repairs")
    assert g["name"] == "First"


def test_goat_returns_none_when_no_data(monkeypatch):
    _stub_data(monkeypatch, records=[], members_map={"Repairs": ["Repair 1"]})
    from zira_dashboard import awards
    monkeypatch.setattr(awards, "_all_time_range", lambda: (date(2026, 1, 1), date(2026, 1, 1)))
    assert awards.goat("Repairs") is None


def test_annual_best_avg_group_requires_30_days(monkeypatch):
    """Person with 29 days at 20pph is excluded; person with 30 days
    at 15pph wins."""
    high_pph_29 = [
        {"day": date(2026, 1, d), "person": "Sprinter", "wc": "Repair 1",
         "units": 200.0, "hours": 10.0, "downtime": 0.0}
        for d in range(1, 30)  # 29 days
    ]
    consistent_30 = [
        {"day": date(2026, 4, d), "person": "Plodder", "wc": "Repair 1",
         "units": 150.0, "hours": 10.0, "downtime": 0.0}
        for d in range(1, 31)  # 30 days (April has 30)
    ]
    _stub_data(
        monkeypatch,
        records=high_pph_29 + consistent_30,
        members_map={"Repairs": ["Repair 1"]},
    )
    from zira_dashboard import awards
    winner = awards.annual_best_avg_group("Repairs", 2026)
    assert winner["name"] == "Plodder"
    assert winner["days"] == 30
    assert winner["pph"] == 15.0


def test_annual_best_avg_group_returns_none_when_no_qualifier(monkeypatch):
    """Nobody hits 30 days → None."""
    _stub_data(
        monkeypatch,
        records=[
            {"day": date(2026, 1, d), "person": "P", "wc": "Repair 1",
             "units": 200.0, "hours": 10.0, "downtime": 0.0}
            for d in range(1, 20)
        ],
        members_map={"Repairs": ["Repair 1"]},
    )
    from zira_dashboard import awards
    assert awards.annual_best_avg_group("Repairs", 2026) is None


def test_annual_best_avg_wc_filters_to_single_wc(monkeypatch):
    """Days in other WCs don't count toward the 30-day floor or pph."""
    repair1_30 = [
        {"day": date(2026, 1, d), "person": "P", "wc": "Repair 1",
         "units": 100.0, "hours": 10.0, "downtime": 0.0}
        for d in range(1, 31)
    ]
    repair2_30 = [
        {"day": date(2026, 1, d), "person": "P", "wc": "Repair 2",
         "units": 50.0, "hours": 10.0, "downtime": 0.0}
        for d in range(1, 31)
    ]
    _stub_data(
        monkeypatch,
        records=repair1_30 + repair2_30,
        members_map={"Repairs": ["Repair 1", "Repair 2"]},
    )
    from zira_dashboard import awards
    w = awards.annual_best_avg_wc("Repair 1", 2026)
    assert w["pph"] == 10.0  # 100u / 10h, only Repair 1 counts
    assert w["days"] == 30


def test_apply_overrides_replace(monkeypatch):
    """A 'replace' override swaps the name in the matching slot."""
    from zira_dashboard import awards
    slots = [
        {"position": 1, "name": "A", "day": date(2026, 4, 1), "units": 100.0, "pph": 12.5},
        {"position": 2, "name": "B", "day": date(2026, 4, 5), "units": 90.0, "pph": 11.2},
        {"position": 3, "name": "C", "day": date(2026, 4, 7), "units": 80.0, "pph": 10.0},
    ]
    overrides = [
        {"scope": "badge", "group_name": "Repairs", "wc_name": None,
         "year": 2026, "month": 4, "position": 2,
         "action": "replace", "name": "Replacement"},
    ]
    out = awards.apply_overrides(
        slots, scope="badge", group_name="Repairs", year=2026, month=4,
        overrides=overrides,
    )
    assert [s["name"] for s in out] == ["A", "Replacement", "C"]


def test_apply_overrides_delete(monkeypatch):
    from zira_dashboard import awards
    slots = [
        {"position": 1, "name": "A", "day": date(2026, 4, 1), "units": 100.0, "pph": 12.5},
        {"position": 2, "name": "B", "day": date(2026, 4, 5), "units": 90.0, "pph": 11.2},
        {"position": 3, "name": "C", "day": date(2026, 4, 7), "units": 80.0, "pph": 10.0},
    ]
    overrides = [
        {"scope": "badge", "group_name": "Repairs", "wc_name": None,
         "year": 2026, "month": 4, "position": 3,
         "action": "delete", "name": None},
    ]
    out = awards.apply_overrides(
        slots, scope="badge", group_name="Repairs", year=2026, month=4,
        overrides=overrides,
    )
    assert [s["position"] for s in out] == [1, 2]


def test_apply_overrides_passthrough_when_no_match(monkeypatch):
    from zira_dashboard import awards
    slots = [{"position": 1, "name": "A", "day": date(2026, 4, 1), "units": 100.0, "pph": 12.5}]
    overrides = [
        {"scope": "badge", "group_name": "Repairs", "wc_name": None,
         "year": 2026, "month": 5, "position": 1,
         "action": "replace", "name": "Other"},
    ]
    out = awards.apply_overrides(
        slots, scope="badge", group_name="Repairs", year=2026, month=4,
        overrides=overrides,
    )
    assert out == slots


def test_apply_overrides_handles_single_winner_scope(monkeypatch):
    """Single-value (not list) override application — for goat, best-avg trophies."""
    from zira_dashboard import awards
    slot = {"name": "A", "day": date(2026, 4, 1), "units": 200.0, "pph": 25.0}
    overrides = [
        {"scope": "award_goat", "group_name": "Repairs", "wc_name": None,
         "year": None, "month": None, "position": 1,
         "action": "replace", "name": "True GOAT"},
    ]
    out = awards.apply_overrides_single(
        slot, scope="award_goat", group_name="Repairs",
        overrides=overrides,
    )
    assert out["name"] == "True GOAT"


def test_apply_overrides_single_delete_returns_none(monkeypatch):
    from zira_dashboard import awards
    slot = {"name": "A", "day": date(2026, 4, 1), "units": 200.0, "pph": 25.0}
    overrides = [
        {"scope": "award_goat", "group_name": "Repairs", "wc_name": None,
         "year": None, "month": None, "position": 1,
         "action": "delete", "name": None},
    ]
    out = awards.apply_overrides_single(
        slot, scope="award_goat", group_name="Repairs",
        overrides=overrides,
    )
    assert out is None


def test_awards_earned_by_aggregates_across_types(monkeypatch):
    """Given fixtures producing GOAT + monthly badge for one person,
    earned_by returns entries with type/period info."""
    _stub_data(
        monkeypatch,
        records=[
            {"day": date(2026, 4, 1), "person": "Hero", "wc": "Repair 1",
             "units": 250.0, "hours": 8.0, "downtime": 0.0},
            {"day": date(2026, 4, 5), "person": "Other", "wc": "Repair 1",
             "units": 100.0, "hours": 8.0, "downtime": 0.0},
        ],
        members_map={"Repairs": ["Repair 1"]},
    )
    from zira_dashboard import awards, work_centers_store
    monkeypatch.setattr(awards, "_all_time_range", lambda: (date(2026, 1, 1), date(2026, 4, 30)))
    monkeypatch.setattr(work_centers_store, "registered_groups", lambda: ["Repairs"])
    monkeypatch.setattr(awards, "_load_overrides", lambda: [])

    earned = awards.awards_earned_by("Hero", today=date(2026, 4, 30))
    types = {e["type"] for e in earned}
    assert "goat" in types
    assert any(e["type"] == "badge" and e["position"] == 1 and e["group"] == "Repairs"
               for e in earned)
