"""Staffing Time Off calendar — full-vs-partial pill shaping.

`_odoo_time_off_by_day` re-derives `full` against the company shift length so
an unpaid full day (which syncs in from Odoo tagged `midday_gap` with
full-shift hour bounds) reads as a full day, not a partial: full days show the
name alone (green); only genuine partials keep a timing label (gold). Stubs the
fan-out + shift length so no DB/Odoo is touched.
"""

from __future__ import annotations

from datetime import date

import zira_dashboard.routes.time_off as to


def _patch(monkeypatch, raw):
    monkeypatch.setattr(to, "_approved_by_day", lambda s, e: raw)
    monkeypatch.setattr(to, "_company_shift_len", lambda: 8.5)


def test_full_day_unpaid_synced_as_midday_gap_reads_full(monkeypatch):
    # 7:00–15:30 hour bounds == the whole shift → effectively a full day.
    raw = {date(2026, 6, 2): [{
        "name": "Alice", "label": "7:00am–3:30pm", "full": False,
        "shape": "midday_gap", "hour_from": 7.0, "hour_to": 15.5,
    }]}
    _patch(monkeypatch, raw)
    (entry,) = to._odoo_time_off_by_day(date(2026, 6, 2), date(2026, 6, 2))[date(2026, 6, 2)]
    assert entry["full"] is True
    assert entry["label"] == ""        # no time next to the name
    assert entry["source"] == "odoo"


def test_genuine_partial_keeps_time_and_reads_partial(monkeypatch):
    raw = {date(2026, 6, 2): [{
        "name": "Bob", "label": "leaves 2:00pm", "full": False,
        "shape": "early_leave", "hour_from": 14.0, "hour_to": 15.5,
    }]}
    _patch(monkeypatch, raw)
    (entry,) = to._odoo_time_off_by_day(date(2026, 6, 2), date(2026, 6, 2))[date(2026, 6, 2)]
    assert entry["full"] is False
    assert entry["label"] == "leaves 2:00pm"


def test_full_day_shape_blanks_label(monkeypatch):
    raw = {date(2026, 6, 2): [{
        "name": "Carol", "label": "full day", "full": True,
        "shape": "full_day", "hour_from": None, "hour_to": None,
    }]}
    _patch(monkeypatch, raw)
    (entry,) = to._odoo_time_off_by_day(date(2026, 6, 2), date(2026, 6, 2))[date(2026, 6, 2)]
    assert entry["full"] is True
    assert entry["label"] == ""


def test_holiday_entry_preserved(monkeypatch):
    raw = {date(2026, 7, 4): [{
        "name": "Independence Day", "label": "Plant Closed", "source": "holiday",
    }]}
    _patch(monkeypatch, raw)
    (entry,) = to._odoo_time_off_by_day(date(2026, 7, 4), date(2026, 7, 4))[date(2026, 7, 4)]
    assert entry["source"] == "holiday"
    assert entry["label"] == "Plant Closed"
    assert entry["full"] is True
