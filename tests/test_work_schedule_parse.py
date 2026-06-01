"""Pure unit tests for work_schedule_store._parse_work_hours. No DB needed."""

from datetime import time

from zira_dashboard.work_schedule_store import _parse_work_hours


def test_parses_valid_weekday_entries():
    out = _parse_work_hours({"0": ["05:45", "14:30"], "4": ["07:00", "15:30"]})
    assert out == {0: (time(5, 45), time(14, 30)), 4: (time(7, 0), time(15, 30))}


def test_drops_non_int_key():
    assert _parse_work_hours({"bad": ["05:45", "14:30"]}) == {}


def test_drops_out_of_range_weekday():
    assert _parse_work_hours({"9": ["05:45", "14:30"]}) == {}


def test_drops_wrong_length_value():
    assert _parse_work_hours({"0": ["05:45"]}) == {}
    assert _parse_work_hours({"0": ["05:45", "14:30", "x"]}) == {}


def test_drops_unparseable_time():
    assert _parse_work_hours({"0": ["nope", "14:30"]}) == {}


def test_non_dict_input_returns_empty():
    assert _parse_work_hours(None) == {}
    assert _parse_work_hours([1, 2]) == {}
