"""Pure-logic tests for machine_breakdown.detect() and departed_at()."""
from datetime import datetime, timedelta, timezone

from zira_dashboard.machine_breakdown import StationSignal, BreakdownCandidate, detect, departed_at

SHIFT_START = datetime(2026, 7, 8, 12, 0, tzinfo=timezone.utc)   # 7:00 AM Central
SHIFT_END = datetime(2026, 7, 8, 20, 30, tzinfo=timezone.utc)    # 3:30 PM Central


def test_detect_flags_station_with_no_output_past_threshold():
    last_output = SHIFT_START + timedelta(hours=2)
    now = last_output + timedelta(minutes=60)
    signals = [StationSignal(wc_name="Dismantler 2", last_output_utc=last_output, has_operator=True)]
    out = detect(signals, now, SHIFT_START, SHIFT_END)
    assert out == [BreakdownCandidate(wc_name="Dismantler 2", stop_utc=last_output)]


def test_detect_ignores_station_under_threshold():
    last_output = SHIFT_START + timedelta(hours=2)
    now = last_output + timedelta(minutes=59)
    signals = [StationSignal(wc_name="Dismantler 2", last_output_utc=last_output, has_operator=True)]
    assert detect(signals, now, SHIFT_START, SHIFT_END) == []


def test_detect_ignores_station_with_no_operator():
    last_output = SHIFT_START + timedelta(hours=2)
    now = last_output + timedelta(minutes=30)
    signals = [StationSignal(wc_name="Dismantler 2", last_output_utc=last_output, has_operator=False)]
    assert detect(signals, now, SHIFT_START, SHIFT_END) == []


def test_detect_treats_never_produced_as_stopped_since_shift_start():
    now = SHIFT_START + timedelta(minutes=60)
    signals = [StationSignal(wc_name="Dismantler 2", last_output_utc=None, has_operator=True)]
    out = detect(signals, now, SHIFT_START, SHIFT_END)
    assert out == [BreakdownCandidate(wc_name="Dismantler 2", stop_utc=SHIFT_START)]


def test_detect_returns_nothing_outside_shift_hours():
    last_output = SHIFT_START + timedelta(hours=2)
    now = SHIFT_END + timedelta(minutes=30)
    signals = [StationSignal(wc_name="Dismantler 2", last_output_utc=last_output, has_operator=True)]
    assert detect(signals, now, SHIFT_START, SHIFT_END) == []


def test_detect_respects_custom_threshold():
    last_output = SHIFT_START + timedelta(hours=2)
    now = last_output + timedelta(minutes=6)
    signals = [StationSignal(wc_name="Dismantler 2", last_output_utc=last_output, has_operator=True)]
    assert detect(signals, now, SHIFT_START, SHIFT_END, no_output_minutes=5) == [
        BreakdownCandidate(wc_name="Dismantler 2", stop_utc=last_output)
    ]


def test_detect_uses_productive_elapsed_minutes_when_provided():
    last_output = SHIFT_START + timedelta(hours=2)
    now = last_output + timedelta(minutes=75)
    signals = [StationSignal(wc_name="Dismantler 2", last_output_utc=last_output, has_operator=True)]

    assert detect(
        signals,
        now,
        SHIFT_START,
        SHIFT_END,
        elapsed_minutes=lambda start, end: 45,
    ) == []

    assert detect(
        signals,
        now,
        SHIFT_START,
        SHIFT_END,
        elapsed_minutes=lambda start, end: 60,
    ) == [BreakdownCandidate(wc_name="Dismantler 2", stop_utc=last_output)]


def test_detect_suppresses_when_current_time_is_not_productive():
    last_output = SHIFT_START + timedelta(hours=2)
    now = last_output + timedelta(minutes=90)
    signals = [StationSignal(wc_name="Dismantler 2", last_output_utc=last_output, has_operator=True)]

    assert detect(signals, now, SHIFT_START, SHIFT_END, now_is_productive=False) == []


def test_departed_at_returns_none_when_open_punch_exists():
    stop = datetime(2026, 7, 8, 18, 0, tzinfo=timezone.utc)
    punch_windows = {"Juan": [("Dismantler 2", SHIFT_START, None)]}
    assert departed_at("Juan", "Dismantler 2", punch_windows, stop) is None


def test_departed_at_returns_close_time_when_all_relevant_windows_closed():
    stop = datetime(2026, 7, 8, 18, 0, tzinfo=timezone.utc)
    end = datetime(2026, 7, 8, 18, 5, tzinfo=timezone.utc)
    punch_windows = {"Juan": [("Dismantler 2", SHIFT_START, end)]}
    assert departed_at("Juan", "Dismantler 2", punch_windows, stop) == end


def test_departed_at_ignores_windows_that_closed_before_stop():
    stop = datetime(2026, 7, 8, 18, 0, tzinfo=timezone.utc)
    old_end = datetime(2026, 7, 8, 10, 0, tzinfo=timezone.utc)  # closed long before the breakdown
    punch_windows = {"Juan": [("Dismantler 2", SHIFT_START, old_end)]}
    assert departed_at("Juan", "Dismantler 2", punch_windows, stop) is None


def test_departed_at_none_when_no_windows_for_wc():
    stop = datetime(2026, 7, 8, 18, 0, tzinfo=timezone.utc)
    punch_windows = {"Juan": [("Repair 1", SHIFT_START, None)]}
    assert departed_at("Juan", "Dismantler 2", punch_windows, stop) is None
