import os
from datetime import date, time

import pytest

from zira_dashboard import shift_config, staffing

# shift_config.shift_start_for() calls into the schedule_store which
# does a DB read for the global default. Tests need a live Postgres
# even when they monkeypatch staffing.load_schedule.
pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set; shift_config tests need Postgres",
)


def test_shift_start_for_default_falls_back_to_global(monkeypatch):
    monkeypatch.setattr(staffing, "load_schedule",
        lambda d: staffing.Schedule(day=d, published=False, custom_hours=None))
    assert shift_config.shift_start_for(date(2026, 4, 28)) == shift_config.shift_start()


def test_shift_start_for_uses_override(monkeypatch):
    monkeypatch.setattr(staffing, "load_schedule",
        lambda d: staffing.Schedule(
            day=d, published=False,
            custom_hours={"start": "09:30", "end": "15:00", "breaks": []},
        ))
    assert shift_config.shift_start_for(date(2026, 4, 28)) == time(9, 30)


def test_shift_end_for_default_falls_back_to_global(monkeypatch):
    monkeypatch.setattr(staffing, "load_schedule",
        lambda d: staffing.Schedule(day=d, published=False, custom_hours=None))
    assert shift_config.shift_end_for(date(2026, 4, 28)) == shift_config.shift_end()


def test_shift_end_for_uses_override(monkeypatch):
    monkeypatch.setattr(staffing, "load_schedule",
        lambda d: staffing.Schedule(
            day=d, published=False,
            custom_hours={"start": "07:00", "end": "11:30", "breaks": []},
        ))
    assert shift_config.shift_end_for(date(2026, 4, 28)) == time(11, 30)


from zira_dashboard.schedule_store import Break


def test_breaks_for_default_falls_back_to_global(monkeypatch):
    monkeypatch.setattr(staffing, "load_schedule",
        lambda d: staffing.Schedule(day=d, published=False, custom_hours=None))
    assert shift_config.breaks_for(date(2026, 4, 28)) == shift_config.breaks()


def test_breaks_for_uses_override(monkeypatch):
    monkeypatch.setattr(staffing, "load_schedule",
        lambda d: staffing.Schedule(
            day=d, published=False,
            custom_hours={
                "start": "07:00", "end": "15:00",
                "breaks": [
                    {"start": "10:00", "end": "10:30", "name": "Meeting"},
                    {"start": "12:00", "end": "12:30", "name": "Lunch"},
                ],
            },
        ))
    out = shift_config.breaks_for(date(2026, 4, 28))
    assert out == (
        Break(time(10, 0), time(10, 30), "Meeting"),
        Break(time(12, 0), time(12, 30), "Lunch"),
    )


def test_breaks_for_empty_list_means_no_breaks(monkeypatch):
    """Empty list override means 'no breaks today' — not 'fall back to global'."""
    monkeypatch.setattr(staffing, "load_schedule",
        lambda d: staffing.Schedule(
            day=d, published=False,
            custom_hours={"start": "07:00", "end": "11:00", "breaks": []},
        ))
    assert shift_config.breaks_for(date(2026, 4, 28)) == ()


def test_productive_minutes_for_default_matches_global(monkeypatch):
    monkeypatch.setattr(staffing, "load_schedule",
        lambda d: staffing.Schedule(day=d, published=False, custom_hours=None))
    assert shift_config.productive_minutes_for(date(2026, 4, 28)) == shift_config.productive_minutes_per_day()


def test_productive_minutes_for_half_day_with_one_break(monkeypatch):
    """09:00 → 13:00 = 240 min, minus a 30-min break = 210 min."""
    monkeypatch.setattr(staffing, "load_schedule",
        lambda d: staffing.Schedule(
            day=d, published=False,
            custom_hours={
                "start": "09:00", "end": "13:00",
                "breaks": [{"start": "11:00", "end": "11:30", "name": "Lunch"}],
            },
        ))
    assert shift_config.productive_minutes_for(date(2026, 4, 28)) == 210


from datetime import datetime
from zira_dashboard.shift_config import SITE_TZ


def test_in_shift_on_respects_custom_hours(monkeypatch):
    """09:30 → 13:00 override; 09:00 should be out, 10:00 should be in."""
    monkeypatch.setattr(staffing, "load_schedule",
        lambda d: staffing.Schedule(
            day=d, published=False,
            custom_hours={"start": "09:30", "end": "13:00", "breaks": []},
        ))
    monkeypatch.setattr(shift_config, "work_weekdays", lambda: frozenset(range(7)))
    early = datetime(2026, 4, 28, 9, 0, tzinfo=SITE_TZ)
    inside = datetime(2026, 4, 28, 10, 0, tzinfo=SITE_TZ)
    assert shift_config.in_shift_on(early) is False
    assert shift_config.in_shift_on(inside) is True


def test_shift_elapsed_minutes_respects_custom_hours(monkeypatch):
    """Custom 09:00 → 13:00 with a 30-min break at 11:00. As of 12:00,
    elapsed = 9-11 (120 min) + 11:30-12:00 (30 min) = 150 min."""
    monkeypatch.setattr(staffing, "load_schedule",
        lambda d: staffing.Schedule(
            day=d, published=False,
            custom_hours={
                "start": "09:00", "end": "13:00",
                "breaks": [{"start": "11:00", "end": "11:30", "name": "Lunch"}],
            },
        ))
    monkeypatch.setattr(shift_config, "work_weekdays", lambda: frozenset(range(7)))
    d = date(2026, 4, 28)
    now = datetime(2026, 4, 28, 12, 0, tzinfo=SITE_TZ)
    assert shift_config.shift_elapsed_minutes(d, now) == 150


def test_shift_elapsed_minutes_honors_published_schedule_on_saturday(monkeypatch):
    """Regression: A Saturday with a PUBLISHED schedule (with or without
    custom_hours) should report elapsed shift minutes, not zero. Previously
    the function returned 0 for any non-weekday, zeroing out goal
    denominators and pacing math on Saturday recycling views."""
    monkeypatch.setattr(staffing, "load_schedule",
        lambda d: staffing.Schedule(
            day=d, published=True,
            custom_hours={"start": "06:00", "end": "10:00", "breaks": []},
        ))
    # Default Mon-Fri work week. Saturday 2026-05-16 is weekday 5 → outside.
    monkeypatch.setattr(shift_config, "work_weekdays", lambda: frozenset({0, 1, 2, 3, 4}))
    saturday = date(2026, 5, 16)
    # End of the 4-hour custom shift, no breaks → 240 min elapsed.
    now = datetime(2026, 5, 16, 10, 0, tzinfo=SITE_TZ)
    assert shift_config.shift_elapsed_minutes(saturday, now) == 240


def test_shift_elapsed_minutes_returns_zero_on_unpublished_saturday(monkeypatch):
    """Symmetric check: an unpublished Saturday (no published schedule)
    still returns 0. Only the published-schedule signal opens the gate."""
    monkeypatch.setattr(staffing, "load_schedule",
        lambda d: staffing.Schedule(day=d, published=False, assignments={}))
    monkeypatch.setattr(shift_config, "work_weekdays", lambda: frozenset({0, 1, 2, 3, 4}))
    saturday = date(2026, 5, 16)
    now = datetime(2026, 5, 16, 10, 0, tzinfo=SITE_TZ)
    assert shift_config.shift_elapsed_minutes(saturday, now) == 0
