"""Unit tests for the pure averages helpers in routes/leaderboards.py.

These tests don't need Postgres — the helpers are dependency-injected
with a fake `productive_minutes_for` callable and explicit targets.
"""
from datetime import date

from zira_dashboard.routes.leaderboards import averages_for_wc


# A 7h productive day at every date — keeps math simple in tests.
def _const_productive(_day):
    return 7 * 60  # 420 min = 7h


def _rec(d, person, wc, units):
    return {"day": d, "person": person, "wc": wc, "units": units,
            "downtime": 0.0, "hours": 7.0}


def test_averages_single_person_multiple_days():
    target_per_hour = 30.0  # 7h * 30 = 210 expected per day
    records = [
        _rec(date(2026, 4, 27), "Alice", "WC1", 200),
        _rec(date(2026, 4, 28), "Alice", "WC1", 220),
        _rec(date(2026, 4, 29), "Alice", "WC1", 210),
    ]
    rows = averages_for_wc(records, target_per_hour, _const_productive, "units")
    assert len(rows) == 1
    r = rows[0]
    assert r["rank"] == 1
    assert r["name"] == "Alice"
    assert r["name_count"] == 3
    assert r["avg_units"] == 210.0
    # avg_pct = mean of (200/210, 220/210, 210/210)
    assert abs(r["avg_pct"] - (200/210 + 220/210 + 210/210) / 3) < 1e-9


def test_averages_sort_by_units_desc():
    records = [
        _rec(date(2026, 4, 27), "Alice", "WC1", 100),
        _rec(date(2026, 4, 27), "Bob",   "WC1", 300),
        _rec(date(2026, 4, 28), "Bob",   "WC1", 300),
    ]
    rows = averages_for_wc(records, 30.0, _const_productive, "units")
    assert [r["name"] for r in rows] == ["Bob", "Alice"]
    assert rows[0]["rank"] == 1
    assert rows[1]["rank"] == 2


def test_averages_sort_by_pct_desc():
    # Alice: avg 100 units/day, pct = 100/210 ≈ 0.476
    # Bob:   avg 200 units/day, pct = 200/210 ≈ 0.952
    records = [
        _rec(date(2026, 4, 27), "Alice", "WC1", 100),
        _rec(date(2026, 4, 27), "Bob",   "WC1", 200),
    ]
    rows = averages_for_wc(records, 30.0, _const_productive, "pct")
    assert [r["name"] for r in rows] == ["Bob", "Alice"]


def test_averages_tiebreak_more_days_ranks_higher():
    # Both average 200 units/day. Alice worked more days → ranks higher.
    records = [
        _rec(date(2026, 4, 27), "Alice", "WC1", 200),
        _rec(date(2026, 4, 28), "Alice", "WC1", 200),
        _rec(date(2026, 4, 29), "Alice", "WC1", 200),
        _rec(date(2026, 4, 27), "Bob",   "WC1", 200),
    ]
    rows = averages_for_wc(records, 30.0, _const_productive, "units")
    assert [r["name"] for r in rows] == ["Alice", "Bob"]


def test_averages_zero_unit_records_filtered():
    # Days where units=0 (e.g., time off) should NOT drag down the average.
    records = [
        _rec(date(2026, 4, 27), "Alice", "WC1", 200),
        _rec(date(2026, 4, 28), "Alice", "WC1", 0),
    ]
    rows = averages_for_wc(records, 30.0, _const_productive, "units")
    assert rows[0]["avg_units"] == 200.0
    assert rows[0]["name_count"] == 1


def test_averages_custom_hours_shrinks_expected():
    # Day 1 is a 4h day, day 2 is the standard 7h day.
    def productive_per_day(d):
        if d == date(2026, 4, 27):
            return 4 * 60
        return 7 * 60

    target_per_hour = 30.0
    # Alice did 120 on a 4h day → pct = 120 / (30*4) = 1.0
    # Alice did 210 on a 7h day → pct = 210 / (30*7) = 1.0
    records = [
        _rec(date(2026, 4, 27), "Alice", "WC1", 120),
        _rec(date(2026, 4, 28), "Alice", "WC1", 210),
    ]
    rows = averages_for_wc(records, target_per_hour, productive_per_day, "pct")
    assert abs(rows[0]["avg_pct"] - 1.0) < 1e-9


def test_averages_empty_records_returns_empty_list():
    assert averages_for_wc([], 30.0, _const_productive, "units") == []


def test_averages_zero_target_yields_no_pct():
    # No goal configured → avg_pct is None (renders "—"), never a bogus 0%.
    records = [_rec(date(2026, 4, 27), "Alice", "WC1", 200)]
    rows = averages_for_wc(records, 0.0, _const_productive, "pct")
    assert rows[0]["avg_pct"] is None
    assert rows[0]["avg_units"] == 200.0  # units math still works


from zira_dashboard.routes.leaderboards import averages_for_group


def test_group_averages_basic_two_wcs():
    target_by_wc = {"Repair-1": 30.0, "Repair-2": 25.0}
    # Alice: 2 days at Repair-1 (210 each), 1 day at Repair-2 (175).
    # Repair-1 expected = 210, Repair-2 expected = 175.
    records = [
        _rec(date(2026, 4, 27), "Alice", "Repair-1", 210),
        _rec(date(2026, 4, 28), "Alice", "Repair-1", 210),
        _rec(date(2026, 4, 29), "Alice", "Repair-2", 175),
    ]
    rows = averages_for_group(records, target_by_wc, _const_productive, "units")
    assert len(rows) == 1
    r = rows[0]
    assert r["name"] == "Alice"
    assert r["name_count"] == 3  # total person-days across the group
    assert r["avg_units"] == (210 + 210 + 175) / 3
    # All three days were exactly at goal → pct = 1.0
    assert abs(r["avg_pct"] - 1.0) < 1e-9
    assert r["top_wc"] == "Repair-1"  # 2 days vs 1


def test_group_averages_top_wc_alphabetical_tiebreak():
    # Alice worked Repair-1 once and Repair-2 once → tie, alphabetical first wins.
    target_by_wc = {"Repair-1": 30.0, "Repair-2": 30.0}
    records = [
        _rec(date(2026, 4, 27), "Alice", "Repair-2", 100),
        _rec(date(2026, 4, 28), "Alice", "Repair-1", 100),
    ]
    rows = averages_for_group(records, target_by_wc, _const_productive, "units")
    assert rows[0]["top_wc"] == "Repair-1"


def test_group_averages_sort_and_rank():
    target_by_wc = {"WC1": 30.0}
    records = [
        _rec(date(2026, 4, 27), "Alice", "WC1", 100),
        _rec(date(2026, 4, 27), "Bob",   "WC1", 300),
    ]
    rows = averages_for_group(records, target_by_wc, _const_productive, "units")
    assert [r["name"] for r in rows] == ["Bob", "Alice"]
    assert rows[0]["rank"] == 1


def test_group_averages_unknown_wc_target_excluded_from_pct():
    # If a record's WC isn't in target_by_wc there is no goal to measure
    # against — the day contributes no pct sample (it must not read as a
    # 0% day and drag the average down). Units math is unaffected.
    target_by_wc = {"WC1": 30.0}
    records = [
        _rec(date(2026, 4, 27), "Alice", "WC1",      210),  # pct = 1.0
        _rec(date(2026, 4, 28), "Alice", "WC-Other", 100),  # no goal — excluded
    ]
    rows = averages_for_group(records, target_by_wc, _const_productive, "pct")
    assert abs(rows[0]["avg_pct"] - 1.0) < 1e-9
    assert rows[0]["avg_units"] == 155.0


def test_group_averages_filters_zero_unit_records():
    target_by_wc = {"WC1": 30.0}
    records = [
        _rec(date(2026, 4, 27), "Alice", "WC1", 200),
        _rec(date(2026, 4, 28), "Alice", "WC1", 0),
    ]
    rows = averages_for_group(records, target_by_wc, _const_productive, "units")
    assert rows[0]["name_count"] == 1
    assert rows[0]["avg_units"] == 200.0


def _rec_excl(d, person, wc, units, excluded_minutes=0.0):
    return {"day": d, "person": person, "wc": wc, "units": units,
            "downtime": 0.0, "hours": 7.0, "excluded_minutes": excluded_minutes}


def test_averages_for_wc_shrinks_expected_by_excluded_minutes():
    # Expected without exclusion: 7h * 30/h = 210. With 60 excluded minutes,
    # productive hours drop to 6h -> expected 180. units 180 -> pct == 1.0.
    records = [_rec_excl(date(2026, 4, 27), "Alice", "WC1", 180, excluded_minutes=60.0)]
    rows = averages_for_wc(records, 30.0, _const_productive, "pct")
    assert abs(rows[0]["avg_pct"] - 1.0) < 1e-9


def test_averages_for_wc_zero_exclusion_matches_pre_existing_behavior():
    """Regression guard: with excluded_minutes == 0 for every record, the
    result is bit-for-bit identical to before this feature existed."""
    records = [
        _rec_excl(date(2026, 4, 27), "Alice", "WC1", 200, excluded_minutes=0.0),
        _rec_excl(date(2026, 4, 28), "Alice", "WC1", 220, excluded_minutes=0.0),
    ]
    rows = averages_for_wc(records, 30.0, _const_productive, "pct")
    assert rows[0]["avg_units"] == 210.0
    assert abs(rows[0]["avg_pct"] - (200/210 + 220/210) / 2) < 1e-9


def test_averages_for_wc_missing_excluded_minutes_key_defaults_zero():
    """Records without an excluded_minutes key (e.g. old cached data) behave
    exactly like excluded_minutes=0 -- .get() with a default, never a KeyError."""
    records = [_rec(date(2026, 4, 27), "Alice", "WC1", 200)]  # no excluded_minutes key
    rows = averages_for_wc(records, 30.0, _const_productive, "pct")
    assert abs(rows[0]["avg_pct"] - 200 / 210) < 1e-9


def test_averages_for_group_shrinks_expected_by_excluded_minutes():
    records = [_rec_excl(date(2026, 4, 27), "Alice", "WC1", 180, excluded_minutes=60.0)]
    rows = averages_for_group(records, {"WC1": 30.0}, _const_productive, "pct")
    assert abs(rows[0]["avg_pct"] - 1.0) < 1e-9
