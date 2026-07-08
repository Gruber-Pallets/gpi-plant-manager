"""Pure-logic tests for the excluded_minutes extension to attribute_for_day,
plus tests for attribution_for's breakdown-exclusion wiring."""
from datetime import date, datetime, timezone

from zira_dashboard.production_history import attribute_for_day
from zira_dashboard import production_history


def test_attribute_for_day_carries_excluded_minutes():
    assignments = {"Dismantler 2": ["Juan", "Benjamin"]}
    wc_totals = {"Dismantler 2": (100, 20)}
    excluded = {"Juan": {"Dismantler 2": 30.0}}
    out = attribute_for_day(assignments, wc_totals, 480, excluded_minutes=excluded)
    assert out["Juan"]["Dismantler 2"]["excluded_minutes"] == 30.0
    assert out["Benjamin"]["Dismantler 2"]["excluded_minutes"] == 0.0
    # Units/downtime unaffected -- breakdown never touches units.
    assert out["Juan"]["Dismantler 2"]["units"] == 50.0


def test_attribute_for_day_no_excluded_minutes_argument_defaults_zero():
    assignments = {"Forklift": ["Lauro"]}
    wc_totals = {"Forklift": (8, 0)}
    out = attribute_for_day(assignments, wc_totals, 480)
    assert out["Lauro"]["Forklift"]["excluded_minutes"] == 0.0


def test_excluded_minutes_by_person_wc_sums_closed_and_caps_open(monkeypatch):
    from zira_dashboard import wc_attributions
    day = date(2026, 7, 8)
    # Times are picked to fall strictly before the plant's default 9:00-9:15
    # America/Chicago "Morning break" (13:00/13:30 UTC = 8:00/8:30 CDT;
    # 12:30/12:50 UTC = 7:30/7:50 CDT) so `productive_minutes_in_window`'s
    # real break-subtraction logic doesn't eat into either window -- this
    # test is about the closed/open-window handling, not break math.
    s = datetime(2026, 7, 8, 13, 0, tzinfo=timezone.utc)
    e = datetime(2026, 7, 8, 13, 30, tzinfo=timezone.utc)
    open_s = datetime(2026, 7, 8, 12, 30, tzinfo=timezone.utc)
    monkeypatch.setattr(
        wc_attributions, "breakdown_windows_for_day",
        lambda d: {("Juan", "Dismantler 2"): [(s, e)], ("Benjamin", "Dismantler 2"): [(open_s, None)]},
    )
    now = datetime(2026, 7, 8, 12, 50, tzinfo=timezone.utc)
    out = production_history._excluded_minutes_by_person_wc(day, now)
    assert out["Juan"]["Dismantler 2"] == 30.0
    assert out["Benjamin"]["Dismantler 2"] == 20.0  # open window capped at `now`


def test_effective_now_clamps_to_shift_end(monkeypatch):
    from zira_dashboard import shift_config
    day = date(2026, 7, 8)
    monkeypatch.setattr(shift_config, "shift_end_for", lambda d: __import__("datetime").time(15, 30))
    late_now = datetime(2026, 7, 9, 2, 0, tzinfo=timezone.utc)  # well past shift end
    effective = production_history._effective_now(day, late_now)
    assert effective < late_now
