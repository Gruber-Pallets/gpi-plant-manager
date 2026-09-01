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
from zira_dashboard.stations import Station


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


def _iso_z(h: int, m: int = 0, day: date | None = None) -> str:
    return _utc(h, m, day).isoformat().replace("+00:00", "Z")


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


def test_fetch_station_day_ignores_stopped_status_on_productive_rows(monkeypatch):
    """A row with units is production evidence, not downtime evidence.

    Zira can return a non-working status/duration on the same reading that
    carries positive units. Counting that row as a continuous stopped interval
    makes the Downtime Report claim the station was down for the same minutes
    it was producing pallets.
    """
    day = date(2026, 7, 3)
    monkeypatch.setattr(leaderboard, "is_workday", lambda d: True)
    monkeypatch.setattr(leaderboard, "shift_start_for", lambda d: time(6, 0))
    monkeypatch.setattr(leaderboard, "shift_end_for", lambda d: time(14, 30))
    monkeypatch.setattr(leaderboard, "breaks_for", lambda d: ())

    class _Client:
        def get_readings(self, **kwargs):
            return {
                "data": [
                    {
                        "event_date": _iso_z(6, 5, day),
                        "units": 40,
                        "status": "Working",
                        "duration": 0,
                    },
                    {
                        "event_date": _iso_z(6, 30, day),
                        "units": 45,
                        "status": "Working",
                        "duration": 0,
                    },
                    {
                        "event_date": _iso_z(7, 0, day),
                        "units": 45,
                        "status": "Stopped",
                        "duration": 55,
                    },
                ],
                "lastValue": None,
            }

    station = Station(meter_id="d3", name="Dismantler 3", category="Dismantler", cell="Recycling")
    start_iso, end_iso = leaderboard.day_window_utc(day)
    total = leaderboard.fetch_station_day(
        _Client(),
        station,
        start_iso,
        end_iso,
        now_utc=_utc(7, 14, day),
    )

    assert total.units == 130
    assert total.downtime_minutes == 0


def test_fetch_station_day_total_excludes_positive_out_of_shift_readings(monkeypatch):
    day = date(2026, 7, 3)
    monkeypatch.setattr(leaderboard, "is_workday", lambda d: True)
    monkeypatch.setattr(leaderboard, "shift_start_for", lambda d: time(6, 0))
    monkeypatch.setattr(leaderboard, "shift_end_for", lambda d: time(14, 30))
    monkeypatch.setattr(leaderboard, "breaks_for", lambda d: ())

    class _Client:
        def get_readings(self, **kwargs):
            return {
                "data": [
                    {"event_date": _iso_z(5, 30, day), "units": 5},
                    {"event_date": _iso_z(6, 5, day), "units": 7},
                ],
                "lastValue": None,
            }

    station = Station("d3", "Dismantler 3", "Dismantler", "Recycling")
    start_iso, end_iso = leaderboard.day_window_utc(day)

    total = leaderboard.fetch_station_day(
        _Client(), station, start_iso, end_iso, now_utc=_utc(7, 0, day)
    )

    assert total.units == 7
    assert total.samples == ((_utc(6, 5, day), 7),)


def test_fetch_station_day_exposes_adjusted_downtime_intervals(monkeypatch):
    day = date(2026, 7, 3)
    monkeypatch.setattr(leaderboard, "is_workday", lambda d: True)
    monkeypatch.setattr(leaderboard, "shift_start_for", lambda d: time(6, 0))
    monkeypatch.setattr(leaderboard, "shift_end_for", lambda d: time(14, 30))
    monkeypatch.setattr(leaderboard, "breaks_for", lambda d: ())

    class _Client:
        def get_readings(self, **kwargs):
            return {
                "data": [
                    {
                        "event_date": _iso_z(6, 5, day),
                        "units": 1,
                        "status": "Working",
                        "duration": 0,
                    },
                    {
                        "event_date": _iso_z(6, 30, day),
                        "units": 0,
                        "status": "Stopped",
                        "duration": 10,
                    },
                ],
                "lastValue": None,
            }

    station = Station("d3", "Dismantler 3", "Dismantler", "Recycling")
    start_iso, end_iso = leaderboard.day_window_utc(day)

    total = leaderboard.fetch_station_day(
        _Client(), station, start_iso, end_iso, now_utc=_utc(7, 0, day)
    )

    assert total.downtime_minutes == 10
    assert total.downtime_intervals == ((_utc(6, 20, day), _utc(6, 30, day)),)


def test_adjusted_downtime_trims_stop_duration_that_crosses_production(_lunch_1130_to_1200):
    """A stopped-duration row cannot reach backward across a production sample.

    Live Zira data can emit a zero-unit Stopped row at the top of the hour with
    a long duration even though productive readings exist inside that duration.
    Production inside the claimed stopped window proves the machine was not
    continuously down, so downtime can only start after the latest production
    sample in that window.
    """
    day = date(2026, 7, 3)
    samples = [
        (_utc(6, 5, day), 40),
        (_utc(6, 30, day), 45),
        (_utc(6, 55, day), 63),
    ]
    downtime_rows = [(_utc(7, 0, day), 55)]  # Claims 06:05 -> 07:00, crossing production.
    end_of_day = _utc(7, 24, day)

    result = leaderboard._adjusted_downtime(downtime_rows, samples, end_of_day)
    assert result == 5


def test_adjusted_downtime_intervals_split_around_lunch(_lunch_1130_to_1200):
    samples = [
        (_utc(11, 20), 1),
        (_utc(12, 10), 1),
    ]
    rows = [(_utc(12, 5), 40)]

    intervals = leaderboard._adjusted_downtime_intervals(
        rows, samples, _utc(14, 30)
    )

    assert intervals == (
        (_utc(11, 25), _utc(11, 30)),
        (_utc(12, 0), _utc(12, 5)),
    )
    assert leaderboard._adjusted_downtime(rows, samples, _utc(14, 30)) == 10


def test_station_total_keeps_interval_contract_backward_compatible():
    station = Station("1", "Repair 1", "Repair", "Recycling")

    total = leaderboard.StationTotal(
        station, 1, 1, False, 0, 0, None, None, (), ()
    )

    assert total.downtime_intervals == ()
