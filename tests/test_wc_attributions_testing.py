"""Testing-segment handling in wc_attributions. for_day is monkeypatched so
no DB is needed."""
from __future__ import annotations

from datetime import datetime, timezone

from zira_dashboard import wc_attributions


def _row(wc, person, source, h_start, h_end, rid=1):
    return {
        "id": rid, "wc_name": wc, "person_name": person,
        "start_utc": datetime(2026, 6, 2, h_start, tzinfo=timezone.utc),
        "end_utc": datetime(2026, 6, 2, h_end, tzinfo=timezone.utc),
        "source": source,
    }


def test_people_by_wc_excludes_testing_rows(monkeypatch):
    rows = [
        _row("Junior #2", "Lauro", "manual", 14, 16, rid=1),
        _row("Junior #2", "Testing", "testing", 13, 14, rid=2),
    ]
    monkeypatch.setattr(wc_attributions, "for_day", lambda day: rows)
    out = wc_attributions.people_by_wc(object())
    assert out == {"Junior #2": ["Lauro"]}


def test_testing_windows_for_day_collects_only_testing(monkeypatch):
    rows = [
        _row("Junior #2", "Lauro", "manual", 14, 16, rid=1),
        _row("Junior #2", "Testing", "testing", 13, 14, rid=2),
        _row("Trim Saw 1", "Testing", "testing", 8, 9, rid=3),
    ]
    monkeypatch.setattr(wc_attributions, "for_day", lambda day: rows)
    out = wc_attributions.testing_windows_for_day(object())
    assert out == {
        "Junior #2": [(rows[1]["start_utc"], rows[1]["end_utc"])],
        "Trim Saw 1": [(rows[2]["start_utc"], rows[2]["end_utc"])],
    }


def test_testing_person_constant():
    assert wc_attributions.TESTING_PERSON == "Testing"


def test_unattributed_skips_testing_only_wc(monkeypatch):
    """A WC whose only attribution is a testing window must not appear as a
    pending to-do (it's been handled — credited to no one)."""
    from types import SimpleNamespace
    from zira_dashboard import staffing
    from zira_dashboard import leaderboard as _lb

    # One testing row for Junior #2, nothing else.
    rows = [_row("Junior #2", "Testing", "testing", 13, 14, rid=1)]
    monkeypatch.setattr(wc_attributions, "for_day", lambda day: rows)

    # Empty schedule so Junior #2 is unscheduled.
    monkeypatch.setattr(
        staffing, "load_schedule",
        lambda d: SimpleNamespace(assignments={}),
    )

    # Leaderboard reports production on Junior #2 above the fluke threshold.
    junior = next(loc for loc in staffing.LOCATIONS if loc.name == "Junior #2")
    result = SimpleNamespace(
        station=SimpleNamespace(meter_id=junior.meter_id, name="Junior #2"),
        units=40,
        active_intervals=(
            (datetime(2026, 6, 2, 13, tzinfo=timezone.utc),
             datetime(2026, 6, 2, 14, tzinfo=timezone.utc)),
        ),
    )
    monkeypatch.setattr(
        _lb, "cached_leaderboard",
        lambda client, stations, day, now_utc=None: [result],
    )

    from datetime import date
    out = wc_attributions.unattributed_for_day(date(2026, 6, 2), object())
    assert all(item["wc_name"] != "Junior #2" for item in out)
