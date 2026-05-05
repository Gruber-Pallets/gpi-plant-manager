from datetime import date

from zira_dashboard.deps import _window_dates


def test_last_week_returns_prev_mon_to_prev_sun():
    # 2026-05-05 is a Tuesday. Previous calendar week is Mon 2026-04-27 → Sun 2026-05-03.
    start, end = _window_dates("last_week", date(2026, 5, 5))
    assert start == date(2026, 4, 27)
    assert end == date(2026, 5, 3)


def test_last_week_when_today_is_monday():
    # On a Monday, "last week" still resolves to the prior Mon-Sun.
    start, end = _window_dates("last_week", date(2026, 5, 4))
    assert start == date(2026, 4, 27)
    assert end == date(2026, 5, 3)


def test_last_month_returns_full_prev_calendar_month():
    start, end = _window_dates("last_month", date(2026, 5, 5))
    assert start == date(2026, 4, 1)
    assert end == date(2026, 4, 30)


def test_last_month_first_of_month_edge():
    # Even on the 1st, "last month" still means the previous full calendar month.
    start, end = _window_dates("last_month", date(2026, 5, 1))
    assert start == date(2026, 4, 1)
    assert end == date(2026, 4, 30)


def test_last_month_january_crosses_year():
    start, end = _window_dates("last_month", date(2026, 1, 15))
    assert start == date(2025, 12, 1)
    assert end == date(2025, 12, 31)
