"""Testing-window carve-out in the crediting path. Pure helper tests plus an
attribution_for integration test with leaderboard + DB accessors stubbed."""
from __future__ import annotations

from datetime import date, datetime, timezone

from zira_dashboard import production_history


def _dt(h, m=0):
    return datetime(2026, 6, 2, h, m, tzinfo=timezone.utc)


def test_apply_testing_offsets_subtracts_in_window():
    wc_totals = {"Junior #2": (40, 5)}
    samples_by_wc = {"Junior #2": [(_dt(13, 10), 10), (_dt(13, 50), 5), (_dt(15, 0), 25)]}
    testing = {"Junior #2": [(_dt(13, 0), _dt(14, 0))]}
    out = production_history._apply_testing_offsets(wc_totals, samples_by_wc, testing)
    # 10 + 5 units fell inside 13:00-14:00; subtracted. Downtime untouched.
    assert out["Junior #2"] == (25, 5)


def test_apply_testing_offsets_floors_at_zero():
    wc_totals = {"Junior #2": (8, 0)}
    samples_by_wc = {"Junior #2": [(_dt(13, 10), 10)]}
    testing = {"Junior #2": [(_dt(13, 0), _dt(14, 0))]}
    out = production_history._apply_testing_offsets(wc_totals, samples_by_wc, testing)
    assert out["Junior #2"] == (0, 0)


def test_apply_testing_offsets_no_testing_returns_input():
    wc_totals = {"Junior #2": (40, 5)}
    assert production_history._apply_testing_offsets(wc_totals, {}, {}) == wc_totals


def test_apply_testing_offsets_multiple_windows_same_wc():
    wc_totals = {"Junior #2": (50, 0)}
    samples_by_wc = {"Junior #2": [
        (_dt(10, 30), 8),   # in window 1
        (_dt(12, 0), 12),   # between windows -> kept
        (_dt(14, 30), 6),   # in window 2
    ]}
    testing = {"Junior #2": [(_dt(10, 0), _dt(11, 0)), (_dt(14, 0), _dt(15, 0))]}
    out = production_history._apply_testing_offsets(wc_totals, samples_by_wc, testing)
    assert out["Junior #2"] == (36, 0)  # 50 - (8 + 6)


def test_attribution_for_excludes_testing_units(monkeypatch):
    from zira_dashboard import staffing, wc_attributions

    sched = staffing.Schedule(day=date(2026, 6, 2), published=True, assignments={})
    monkeypatch.setattr(staffing, "load_schedule", lambda d: sched)
    monkeypatch.setattr(production_history, "_fetch_wc_totals",
                        lambda client, day: {"Junior #2": (40, 0)})
    monkeypatch.setattr(production_history, "_fetch_wc_samples",
                        lambda client, day: {"Junior #2": [(_dt(13, 10), 15), (_dt(15, 0), 25)]})
    monkeypatch.setattr(production_history, "_elapsed_minutes_for", lambda d: 480)
    # Lauro is the remainder operator; Testing window 13:00-14:00 (15 units).
    monkeypatch.setattr(wc_attributions, "people_by_wc",
                        lambda d: {"Junior #2": ["Lauro"]})
    monkeypatch.setattr(wc_attributions, "testing_windows_for_day",
                        lambda d: {"Junior #2": [(_dt(13, 0), _dt(14, 0))]})

    out = production_history.attribution_for(date(2026, 6, 2), client=object())
    assert out["Lauro"]["Junior #2"]["units"] == 25.0  # 40 - 15 testing
    assert "Testing" not in out
