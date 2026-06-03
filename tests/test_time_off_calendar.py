"""Characterization tests for the pure Who's-Out calendar engine.

These pin the *current* behavior of the functions extracted out of
``routes/timeclock_time_off.py`` (Task 2 of the decomposition) so the
behavior-preserving refactor stays honest. No I/O here — the route does
the DB/Odoo fetch and hands rows to ``fan_out_approved`` /
``build_calendar_grid``; everything below is pure.

Privacy is load-bearing: ``label_for`` must never leak the leave *type*,
only the timing.
"""

from __future__ import annotations

import calendar as _cal
from datetime import date

from zira_dashboard import time_off_calendar as c

# en-dash the labels use for ranges (U+2013), not a hyphen-minus.
NDASH = "–"


# ---------- label_for: privacy-safe timing labels ----------


def test_label_for_full_day():
    assert c.label_for({"shape": "full_day", "hour_from": None, "hour_to": None}) == "full day"


def test_label_for_late_arrival_uses_hour_to_no_type():
    # arrival time = hour_to; the leave-type name is deliberately absent.
    label = c.label_for({"shape": "late_arrival", "hour_from": None, "hour_to": 9.0})
    assert label == "arrives 9:00am"


def test_label_for_early_leave_uses_hour_from():
    label = c.label_for({"shape": "early_leave", "hour_from": 14.0, "hour_to": None})
    assert label == "leaves 2:00pm"


def test_label_for_midday_gap_is_hour_from_to_range():
    label = c.label_for({"shape": "midday_gap", "hour_from": 10.0, "hour_to": 12.0})
    assert label == f"10:00am{NDASH}12:00pm"


def test_label_for_none_hour_coerced_to_zero_without_crashing():
    # hour_from=None must coerce to 0 (12:00am), not raise.
    label = c.label_for({"shape": "midday_gap", "hour_from": None, "hour_to": 12.0})
    assert label == f"12:00am{NDASH}12:00pm"


# ---------- parse_holiday_date: tolerant date coercion ----------


def test_parse_holiday_date_strips_time_from_odoo_datetime_string():
    assert c.parse_holiday_date("2026-07-04 00:00:00") == date(2026, 7, 4)


def test_parse_holiday_date_passes_through_a_date():
    d = date(2026, 1, 2)
    assert c.parse_holiday_date(d) == d


def test_parse_holiday_date_none_empty_garbage_return_none():
    assert c.parse_holiday_date(None) is None
    assert c.parse_holiday_date("") is None
    assert c.parse_holiday_date("garbage") is None


# ---------- month_bounds: month parse + nav anchors + fallback ----------


def test_month_bounds_basic_prev_next_anchors():
    first, range_start, range_end, prev_m, next_m = c.month_bounds("2026-06")
    assert first == date(2026, 6, 1)
    assert prev_m == "2026-05"
    assert next_m == "2026-07"
    # range spans whole weeks (Mon..Sun) around the month.
    assert range_start.weekday() == 0  # Monday
    assert range_end.weekday() == 6  # Sunday
    assert range_start <= first
    assert range_end >= date(2026, 6, 30)


def test_month_bounds_december_rolls_year_to_january():
    first, _rs, _re, prev_m, next_m = c.month_bounds("2026-12")
    assert first == date(2026, 12, 1)
    assert next_m == "2027-01"  # Dec -> Jan year bump
    assert prev_m == "2026-11"


def test_month_bounds_malformed_falls_back_to_current_month_no_raise():
    today = date.today()
    cur_first = today.replace(day=1)
    for bad in (None, "nonsense", "2026-13"):
        first, _rs, _re, _prev, _next = c.month_bounds(bad)
        assert first == cur_first  # current-month fallback, no exception


# ---------- build_calendar_grid: month grid assembly ----------


def test_build_calendar_grid_heading_and_shape():
    grid = c.build_calendar_grid("2026-06", {})
    assert grid["heading"] == "June 2026"
    assert grid["prev_month"] == "2026-05"
    assert grid["next_month"] == "2026-07"
    # Same keys the template consumes.
    assert set(grid.keys()) == {
        "heading", "weeks", "prev_month", "next_month", "is_current_month",
    }
    # Every week has 6 cells — Sundays are dropped (plant closed).
    assert grid["weeks"]
    for week in grid["weeks"]:
        assert len(week) == 6
    # Cell shape.
    cell = grid["weeks"][0][0]
    assert set(cell.keys()) == {"num", "outside", "is_today", "weekend", "names"}


def test_build_calendar_grid_no_sunday_column():
    grid = c.build_calendar_grid("2026-06", {})
    # Reconstruct weekday from the cell positions: the grid renders Mon..Sat,
    # so no cell should ever fall on a Sunday. Cross-check against calendar.
    weeks = _cal.Calendar(firstweekday=0).monthdatescalendar(2026, 6)
    rendered = [d for wk in weeks for d in wk if d.weekday() != 6]
    flat = [cell["num"] for wk in grid["weeks"] for cell in wk]
    assert flat == [d.day for d in rendered]


def test_build_calendar_grid_flags_spillover_cells_outside():
    grid = c.build_calendar_grid("2026-06", {})
    # June 1 2026 is a Monday, so the first week is all in-month; spill-over
    # into late May / early July is flagged outside.
    outside_days = [
        cell["num"] for wk in grid["weeks"] for cell in wk if cell["outside"]
    ]
    in_month = [
        cell["num"] for wk in grid["weeks"] for cell in wk if not cell["outside"]
    ]
    assert outside_days, "expected at least one spill-over cell flagged outside"
    # In-month cells cover every June day EXCEPT the dropped Sundays
    # (7, 14, 21, 28 in June 2026).
    june_sundays = {d for d in range(1, 31) if date(2026, 6, d).weekday() == 6}
    assert sorted(set(in_month)) == [d for d in range(1, 31) if d not in june_sundays]


def test_build_calendar_grid_threads_off_map_into_cells():
    target = date(2026, 6, 15)
    entry = {"name": "Alice", "label": "full day", "full": True}
    grid = c.build_calendar_grid("2026-06", {target: [entry]})
    hit = [
        cell for wk in grid["weeks"] for cell in wk
        if not cell["outside"] and cell["num"] == 15
    ]
    assert len(hit) == 1
    assert hit[0]["names"] == [entry]


def test_build_calendar_grid_is_current_month_flag_matches_today():
    today = date.today()
    cur = today.strftime("%Y-%m")
    assert c.build_calendar_grid(cur, {})["is_current_month"] is True
    # A clearly-different month is not flagged current.
    other = date(2099, 1, 1).strftime("%Y-%m")
    assert c.build_calendar_grid(other, {})["is_current_month"] is False


# ---------- fan_out_approved: per-day expansion + clipping ----------


def test_fan_out_full_day_leave_fans_one_full_entry_per_in_range_day():
    leaves = [{
        "shape": "full_day", "date_from": date(2026, 6, 1), "date_to": date(2026, 6, 3),
        "hour_from": None, "hour_to": None, "person_name": "Alice",
    }]
    out = c.fan_out_approved(leaves, [], date(2026, 6, 1), date(2026, 6, 3))
    assert sorted(out.keys()) == [date(2026, 6, 1), date(2026, 6, 2), date(2026, 6, 3)]
    for d in out:
        assert out[d] == [{"name": "Alice", "label": "full day", "full": True}]


def test_fan_out_midday_gap_is_partial_with_label():
    leaves = [{
        "shape": "midday_gap", "date_from": date(2026, 6, 2), "date_to": date(2026, 6, 2),
        "hour_from": 10.0, "hour_to": 12.0, "person_name": "Bob",
    }]
    out = c.fan_out_approved(leaves, [], date(2026, 6, 1), date(2026, 6, 5))
    assert list(out.keys()) == [date(2026, 6, 2)]
    (entry,) = out[date(2026, 6, 2)]
    assert entry["name"] == "Bob"
    assert entry["full"] is False
    assert entry["label"] == f"10:00am{NDASH}12:00pm"


def test_fan_out_holiday_emits_holiday_source_entry_per_day():
    holidays = [{
        "name": "Independence Day",
        "date_from": "2026-07-03 00:00:00",
        "date_to": "2026-07-04 00:00:00",
    }]
    out = c.fan_out_approved([], holidays, date(2026, 7, 1), date(2026, 7, 31))
    assert sorted(out.keys()) == [date(2026, 7, 3), date(2026, 7, 4)]
    for d in out:
        (entry,) = out[d]
        assert entry["source"] == "holiday"
        assert entry["label"] == "Plant Closed"
        assert entry["name"] == "Independence Day"


def test_fan_out_clips_to_range_edges():
    # Leave runs 6/01..6/30 but the visible range is only 6/10..6/12.
    leaves = [{
        "shape": "full_day", "date_from": date(2026, 6, 1), "date_to": date(2026, 6, 30),
        "hour_from": None, "hour_to": None, "person_name": "Carol",
    }]
    out = c.fan_out_approved(leaves, [], date(2026, 6, 10), date(2026, 6, 12))
    assert sorted(out.keys()) == [date(2026, 6, 10), date(2026, 6, 11), date(2026, 6, 12)]


def test_fan_out_holiday_with_unparseable_date_is_skipped():
    holidays = [{"name": "Bad", "date_from": "garbage", "date_to": "garbage"}]
    out = c.fan_out_approved([], holidays, date(2026, 6, 1), date(2026, 6, 30))
    assert out == {}


def test_fan_out_leave_and_holiday_coexist_on_same_day():
    leaves = [{
        "shape": "full_day", "date_from": date(2026, 7, 4), "date_to": date(2026, 7, 4),
        "hour_from": None, "hour_to": None, "person_name": "Dana",
    }]
    holidays = [{
        "name": "Independence Day",
        "date_from": "2026-07-04 00:00:00",
        "date_to": "2026-07-04 00:00:00",
    }]
    out = c.fan_out_approved(leaves, holidays, date(2026, 7, 1), date(2026, 7, 31))
    day = out[date(2026, 7, 4)]
    # Leaves are appended before holidays (matches the original fan-out order).
    assert day[0] == {"name": "Dana", "label": "full day", "full": True}
    assert day[1]["source"] == "holiday"
