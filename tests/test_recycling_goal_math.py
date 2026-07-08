"""Characterization tests for the recycling per-WC goal math.

These compose the SAME pure pieces the /recycling route wires together
(assignment_windows.resolve_segments -> expected_by_wc with a breaks-only
productive-minutes function) and pin the exact scenarios behind the June 2026
goal regressions, so a future refactor of routes/departments.py cannot silently
change a goal number. Pure -- no DATABASE_URL needed.
"""
from datetime import date, datetime, time as _t, timezone

from zira_dashboard import assignment_windows as aw
from zira_dashboard import shift_config
from zira_dashboard.schedule_store import Break

DAY = date(2026, 6, 2)


def _utc(h, m=0):
    return datetime(2026, 6, 2, h, m, tzinfo=timezone.utc)


def _minutes(_name, _wc_name, s, e):
    """Breaks-only productive minutes stub = full window span (no breaks)."""
    return (e - s).total_seconds() / 60.0


def test_segments_full_day_across_autolunch_split():
    segs = aw.resolve_segments(
        assignments={}, attributions=[],
        punch_windows={"Jose": [("Dismantler 1", _utc(12), _utc(16)),
                                ("Dismantler 1", _utc(17), _utc(20, 30))]},
        shift_start_utc=_utc(12), cap_utc=_utc(20, 30),
    )
    d1 = sorted([s for s in segs if s.wc_name == "Dismantler 1"], key=lambda s: s.start_utc)
    assert [(s.start_utc, s.end_utc) for s in d1] == [
        (_utc(12), _utc(16)), (_utc(17), _utc(20, 30))]
    total_min = sum((s.end_utc - s.start_utc).total_seconds() / 60.0 for s in d1)
    assert total_min == 450.0


def test_segments_midday_assignment_to_unscheduled_wc_open_ended():
    segs = aw.resolve_segments(
        assignments={}, attributions=[
            {"wc_name": "Dismantler 4", "person_name": "Eulogio",
             "start_utc": _utc(15), "end_utc": None}],
        punch_windows={}, shift_start_utc=_utc(12), cap_utc=_utc(20),
    )
    d4 = [s for s in segs if s.wc_name == "Dismantler 4"]
    assert len(d4) == 1
    assert (d4[0].start_utc, d4[0].end_utc) == (_utc(15), _utc(20))


def test_segments_punch_beats_attribution_for_same_person():
    segs = aw.resolve_segments(
        assignments={}, attributions=[
            {"wc_name": "Repair 2", "person_name": "Ana", "start_utc": _utc(12), "end_utc": None}],
        punch_windows={"Ana": [("Repair 1", _utc(12), _utc(18))]},
        shift_start_utc=_utc(12), cap_utc=_utc(20),
    )
    wcs = {s.wc_name for s in segs if s.person_name == "Ana"}
    assert wcs == {"Repair 1"}


def test_expected_prorates_full_day_across_autolunch_split():
    segs = aw.resolve_segments(
        assignments={}, attributions=[],
        punch_windows={"Jose": [("Dismantler 1", _utc(12), _utc(16)),
                                ("Dismantler 1", _utc(17), _utc(20, 30))]},
        shift_start_utc=_utc(12), cap_utc=_utc(20, 30))
    exp = aw.expected_by_wc(segs, {"Dismantler 1": 6.0}, _minutes)
    assert round(exp["Dismantler 1"], 6) == round(6.0 * 450 / 60.0, 6)


def test_expected_uses_breaks_only_not_timeoff_adjusted_minutes():
    segs = aw.resolve_segments(
        assignments={"Dismantler 1": ["Maria"]}, attributions=[], punch_windows={},
        shift_start_utc=_utc(12), cap_utc=_utc(20))
    breaks_only = aw.expected_by_wc(segs, {"Dismantler 1": 6.0}, _minutes)
    timeoff_adjusted = aw.expected_by_wc(
        segs, {"Dismantler 1": 6.0}, lambda n, wc, s, e: 240.0)
    assert breaks_only["Dismantler 1"] == 48.0
    assert timeoff_adjusted["Dismantler 1"] == 24.0
    assert breaks_only["Dismantler 1"] != timeoff_adjusted["Dismantler 1"]


def test_expected_skips_zero_target_and_zero_minute_segments():
    segs = aw.resolve_segments(
        assignments={"Dismantler 1": ["A"], "Trim Saw": ["B"]}, attributions=[],
        punch_windows={}, shift_start_utc=_utc(12), cap_utc=_utc(20))
    exp = aw.expected_by_wc(segs, {"Dismantler 1": 6.0, "Trim Saw": 0.0}, _minutes)
    assert "Trim Saw" not in exp
    assert exp["Dismantler 1"] == 48.0


def test_expected_testing_window_adds_nothing():
    segs = aw.resolve_segments(
        assignments={}, attributions=[], punch_windows={},
        shift_start_utc=_utc(12), cap_utc=_utc(20))
    exp = aw.expected_by_wc(segs, {"Dismantler 1": 6.0}, _minutes)
    assert exp == {}


def test_breaks_only_minutes_subtract_breaks(monkeypatch):
    # 09:00-09:30 CDT == 14:00-14:30 UTC, inside the [12:00, 20:00] UTC window.
    # Breaks-only proration (person-INDEPENDENT) subtracts the 30-min break from
    # the 480-min span -> 450, the same denominator expected_by_wc multiplies by.
    monkeypatch.setattr(shift_config, "breaks_for",
                        lambda d: (Break(_t(9, 0), _t(9, 30), "Morning"),))
    assert shift_config.productive_minutes_in_window(
        DAY, _utc(12, 0), _utc(20, 0)) == 450  # 480 - 30
