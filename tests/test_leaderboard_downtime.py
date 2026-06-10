"""Unit tests for the downtime calculation in `leaderboard.py`.

Specifically the bug fix that subtracts break/lunch minutes from a
downtime event whose duration bleeds into a break window — the upstream
`in_shift_on` filter only catches events whose START is in a break, so
without `_adjusted_downtime` doing the break subtraction itself, the
lunch portion of a long downtime event would be incorrectly counted.
"""
from __future__ import annotations

from datetime import date, datetime, time, timezone

import pytest

from zira_dashboard import leaderboard
from zira_dashboard.shift_config import SITE_TZ


class _Break:
    """Minimal Break shape — matches schedule_store.Break duck-type."""

    def __init__(self, start: time, end: time, name: str = "Lunch") -> None:
        self.start = start
        self.end = end
        self.name = name


@pytest.fixture
def _lunch_1130_to_1200(monkeypatch):
    """Patch shift_config.breaks_for so every day has a single 11:30-12:00 lunch."""
    def _fake_breaks_for(d):
        return (_Break(time(11, 30), time(12, 0)),)
    # Patch where leaderboard.py imported it from.
    monkeypatch.setattr(leaderboard, "breaks_for", _fake_breaks_for)
    return _fake_breaks_for


def _local(h: int, m: int = 0, day: date | None = None) -> datetime:
    """Helper: SITE_TZ-local datetime on `day` (defaults to today)."""
    d = day or date.today()
    return datetime(d.year, d.month, d.day, h, m, tzinfo=SITE_TZ)


def _utc(h: int, m: int = 0, day: date | None = None) -> datetime:
    return _local(h, m, day).astimezone(timezone.utc)


def test_minutes_in_breaks_no_overlap(_lunch_1130_to_1200):
    """Window entirely outside the break window returns 0."""
    start = _utc(8, 0)
    end = _utc(9, 0)
    assert leaderboard._minutes_in_breaks(start, end) == 0.0


def test_minutes_in_breaks_fully_inside(_lunch_1130_to_1200):
    """Window entirely inside the break window returns its full duration."""
    start = _utc(11, 35)
    end = _utc(11, 50)
    assert leaderboard._minutes_in_breaks(start, end) == 15.0


def test_minutes_in_breaks_partial_overlap_start(_lunch_1130_to_1200):
    """Window starting before the break and ending inside returns only the in-break portion."""
    start = _utc(11, 25)
    end = _utc(11, 45)
    # In-break portion is 11:30 -> 11:45 = 15 minutes.
    assert leaderboard._minutes_in_breaks(start, end) == 15.0


def test_minutes_in_breaks_partial_overlap_end(_lunch_1130_to_1200):
    """Window starting inside the break and ending after returns only the in-break portion."""
    start = _utc(11, 50)
    end = _utc(12, 15)
    # In-break portion is 11:50 -> 12:00 = 10 minutes.
    assert leaderboard._minutes_in_breaks(start, end) == 10.0


def test_minutes_in_breaks_spans_break(_lunch_1130_to_1200):
    """Window covering the entire break returns the break's full duration."""
    start = _utc(11, 25)
    end = _utc(12, 15)
    assert leaderboard._minutes_in_breaks(start, end) == 30.0


def test_adjusted_downtime_event_during_break_excluded(_lunch_1130_to_1200):
    """The portion of a downtime event that bleeds into lunch is NOT counted.

    Setup: machine reports Stopped covering 11:25 -> 12:15 (50 min) -- the
    reading is stamped at the END (12:15) with duration 50. Samples bracket
    lunch within TRANSFER_GAP (active interval spans the break). Without the
    break-subtraction fix, all 50 minutes would count. With the fix, the 30
    min of lunch is subtracted → 20m.
    """
    samples = [
        (_utc(11, 20), 1),  # last pallet before lunch
        (_utc(12, 15), 1),  # first pallet after lunch (45 min gap, < 60min)
    ]
    downtime_rows = [(_utc(12, 15), 50)]  # Stopped 11:25 -> 12:15 (stamped at the END)
    end_of_day = _utc(15, 30)  # shift end ~3:30pm

    result = leaderboard._adjusted_downtime(downtime_rows, samples, end_of_day)
    # Overlap window inside active interval = (11:25, 12:15) = 50 min wall-clock
    # Minus lunch overlap (11:30-12:00) = 30 min
    # Net counted downtime = 20 min
    assert result == 20


def test_adjusted_downtime_event_fully_outside_break(_lunch_1130_to_1200):
    """A downtime event entirely outside lunch counts in full (no subtraction)."""
    samples = [
        (_utc(9, 0), 1),
        (_utc(10, 0), 1),
    ]
    downtime_rows = [(_utc(9, 25), 15)]  # Stopped 9:10 -> 9:25 (reading stamped at the END)
    end_of_day = _utc(15, 30)

    result = leaderboard._adjusted_downtime(downtime_rows, samples, end_of_day)
    assert result == 15


def test_adjusted_downtime_ignores_overnight_stop_stamped_at_shift_start(_lunch_1130_to_1200):
    """The overnight idle hour must not count against the morning's production.

    The meter reports an hourly Stop reading whose `duration` describes the
    interval that just ENDED. The reading stamped 07:00 with duration=60
    covers 06:00->07:00 (overnight, pre-shift) — NOT 07:00->08:00. Projecting
    it forward laid a phantom ~hour of downtime over every producing station
    at shift start (the bug behind the wildly-wrong Downtime Report).
    """
    samples = [
        (_utc(7, 8), 1),   # first pallet of the day
        (_utc(8, 0), 1),   # still producing
    ]
    downtime_rows = [(_utc(7, 0), 60)]  # overnight Stop stamped 07:00 -> covers 06:00..07:00
    end_of_day = _utc(15, 30)

    result = leaderboard._adjusted_downtime(downtime_rows, samples, end_of_day)
    assert result == 0
