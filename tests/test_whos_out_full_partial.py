"""Who's Out calendar — full-vs-partial refinement.

`_build_calendar_context` re-derives each leave's `full` against the company
shift length so an unpaid full day (synced from Odoo tagged `midday_gap` with
full-shift hour bounds) renders as a full day (blue, name only) instead of a
partial (amber) showing a bogus time range. Mirrors the staffing Time Off
calendar. Stubs the fan-out + shift length so no DB/Odoo is touched.
"""

from __future__ import annotations

import types
from datetime import date

import zira_dashboard.routes.timeclock_time_off as tto


def _cells(grid):
    return [cell for wk in grid["weeks"] for cell in wk]


def _entries_on(grid, day_num):
    for cell in _cells(grid):
        if not cell["outside"] and cell["num"] == day_num:
            return cell["names"]
    return []


def _patch(monkeypatch, raw):
    monkeypatch.setattr(tto, "_approved_by_day", lambda s, e: raw)
    monkeypatch.setattr(
        tto.schedule_store, "current",
        lambda: types.SimpleNamespace(shift_len=8.5),
    )


def test_full_day_unpaid_synced_as_midday_gap_renders_full_no_time(monkeypatch):
    raw = {date(2026, 6, 2): [{
        "name": "Alice", "label": "7:00am–3:30pm", "full": False,
        "shape": "midday_gap", "hour_from": 7.0, "hour_to": 15.5,
    }]}
    _patch(monkeypatch, raw)
    (entry,) = _entries_on(tto._build_calendar_context("2026-06"), 2)
    assert entry["full"] is True
    assert entry["label"] == ""    # name alone — no bogus time range


def test_genuine_partial_keeps_amber_and_time(monkeypatch):
    raw = {date(2026, 6, 2): [{
        "name": "Bob", "label": "leaves 2:00pm", "full": False,
        "shape": "early_leave", "hour_from": 14.0, "hour_to": 15.5,
    }]}
    _patch(monkeypatch, raw)
    (entry,) = _entries_on(tto._build_calendar_context("2026-06"), 2)
    assert entry["full"] is False
    assert entry["label"] == "leaves 2:00pm"


def test_holiday_entry_untouched(monkeypatch):
    raw = {date(2026, 6, 2): [{
        "name": "Plant Closed", "label": "Plant Closed", "source": "holiday",
    }]}
    _patch(monkeypatch, raw)
    (entry,) = _entries_on(tto._build_calendar_context("2026-06"), 2)
    assert entry["source"] == "holiday"
    assert entry["label"] == "Plant Closed"
