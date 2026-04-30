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


def test_averages_zero_target_yields_zero_pct():
    records = [_rec(date(2026, 4, 27), "Alice", "WC1", 200)]
    rows = averages_for_wc(records, 0.0, _const_productive, "pct")
    assert rows[0]["avg_pct"] == 0.0
    assert rows[0]["avg_units"] == 200.0  # units math still works
