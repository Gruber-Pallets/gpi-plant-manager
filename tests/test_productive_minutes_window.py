"""shift_config.productive_minutes_in_window — breaks-only, person-independent
proration used for per-WC pace goals. Pure: breaks_for is monkeypatched, no DB.

America/Chicago in June is CDT (UTC-5), so local 09:00 == 14:00 UTC.
"""
from datetime import date, datetime, timezone, time as _t

from zira_dashboard import shift_config
from zira_dashboard.schedule_store import Break

UTC = timezone.utc
DAY = date(2026, 6, 2)


def _u(h, m=0):
    return datetime(2026, 6, 2, h, m, tzinfo=UTC)


def test_subtracts_break_inside_window(monkeypatch):
    # 09:00-09:30 CDT == 14:00-14:30 UTC, inside [07:00, 14:00] CDT window.
    monkeypatch.setattr(shift_config, "breaks_for",
                        lambda d: (Break(_t(9, 0), _t(9, 30), "Morning"),))
    assert shift_config.productive_minutes_in_window(DAY, _u(12, 0), _u(19, 0)) == 390  # 420 - 30


def test_break_outside_window_not_subtracted(monkeypatch):
    # 06:00-06:30 CDT == 11:00-11:30 UTC, before the 12:00 UTC window start.
    monkeypatch.setattr(shift_config, "breaks_for",
                        lambda d: (Break(_t(6, 0), _t(6, 30), "Pre-shift"),))
    assert shift_config.productive_minutes_in_window(DAY, _u(12, 0), _u(19, 0)) == 420


def test_partial_window_for_midday_segment(monkeypatch):
    # Mid-day assignment [13:00, 14:40] CDT == [18:00, 19:40] UTC = 100 min,
    # with the 13:30-13:45 CDT afternoon break (18:30-18:45 UTC) inside → 85.
    monkeypatch.setattr(shift_config, "breaks_for",
                        lambda d: (Break(_t(13, 30), _t(13, 45), "Afternoon"),))
    assert shift_config.productive_minutes_in_window(DAY, _u(18, 0), _u(19, 40)) == 85


def test_empty_or_inverted_window_is_zero(monkeypatch):
    monkeypatch.setattr(shift_config, "breaks_for", lambda d: ())
    assert shift_config.productive_minutes_in_window(DAY, _u(14, 0), _u(14, 0)) == 0
    assert shift_config.productive_minutes_in_window(DAY, _u(15, 0), _u(14, 0)) == 0


def test_no_breaks_is_full_span(monkeypatch):
    monkeypatch.setattr(shift_config, "breaks_for", lambda d: ())
    assert shift_config.productive_minutes_in_window(DAY, _u(12, 0), _u(19, 0)) == 420
