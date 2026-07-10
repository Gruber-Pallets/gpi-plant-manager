"""Pure tests for the per-weekday hours derivation. No Odoo/DB needed."""

import importlib
import importlib.util

from zira_dashboard import odoo_client
from zira_dashboard.odoo_client import (
    _float_to_hhmm,
    _calendar_hours_from_lines,
    _calendar_lunch_windows_from_lines,
)


def test_calendar_reducers_are_extracted_behind_facade_aliases():
    module_name = "zira_dashboard._odoo_calendars"
    assert importlib.util.find_spec(module_name) is not None
    private = importlib.import_module(module_name)

    assert odoo_client._float_to_hhmm is private.float_to_hhmm
    assert odoo_client._calendar_hours_from_lines is private.calendar_hours_from_lines
    assert (
        odoo_client._calendar_lunch_windows_from_lines
        is private.calendar_lunch_windows_from_lines
    )
    assert odoo_client._is_flexible is private.is_flexible


def test_float_to_hhmm_basic():
    assert _float_to_hhmm(5.75) == "05:45"
    assert _float_to_hhmm(14.5) == "14:30"
    assert _float_to_hhmm(0.0) == "00:00"


def test_float_to_hhmm_rounds_to_nearest_minute_with_carry():
    # 5.7584h = 5h 45.504m -> rounds to 5:46
    assert _float_to_hhmm(5.7584) == "05:46"
    # 13.999h rounds up to 14:00 (carry across the hour)
    assert _float_to_hhmm(13.9999) == "14:00"


def test_calendar_hours_outer_boundary_for_lunch_split():
    # Two attendance lines on Monday (dayofweek "0"): morning + afternoon
    # around lunch. We keep the OUTER boundary: 05:45 .. 14:30.
    rows = [
        {"calendar_id": [7, "Drivers"], "dayofweek": "0", "hour_from": 5.75, "hour_to": 11.0},
        {"calendar_id": [7, "Drivers"], "dayofweek": "0", "hour_from": 11.5, "hour_to": 14.5},
        {"calendar_id": [7, "Drivers"], "dayofweek": "1", "hour_from": 5.75, "hour_to": 14.5},
    ]
    out = _calendar_hours_from_lines(rows)
    assert out == {
        7: {
            "0": ["05:45", "14:30"],
            "1": ["05:45", "14:30"],
        }
    }


def test_calendar_hours_skips_malformed_rows():
    rows = [
        {"calendar_id": False, "dayofweek": "0", "hour_from": 7.0, "hour_to": 15.0},
        {"calendar_id": [7, "X"], "dayofweek": "nope", "hour_from": 7.0, "hour_to": 15.0},
        {"calendar_id": [7, "X"], "dayofweek": "2", "hour_from": 7.0, "hour_to": 15.5},
    ]
    out = _calendar_hours_from_lines(rows)
    assert out == {7: {"2": ["07:00", "15:30"]}}


def test_lunch_period_rows_are_excluded_from_boundary():
    # A (contrived) lunch row whose hours extend past the afternoon end must
    # NOT widen the boundary — mirrors fetch_resource_calendar's lunch filter.
    rows = [
        {"calendar_id": [7, "D"], "dayofweek": "0", "day_period": "morning", "hour_from": 5.75, "hour_to": 11.0},
        {"calendar_id": [7, "D"], "dayofweek": "0", "day_period": "afternoon", "hour_from": 11.5, "hour_to": 14.5},
        {"calendar_id": [7, "D"], "dayofweek": "0", "day_period": "lunch", "hour_from": 11.0, "hour_to": 15.0},
    ]
    out = _calendar_hours_from_lines(rows)
    assert out == {7: {"0": ["05:45", "14:30"]}}  # 15.0 lunch end excluded


def test_bool_calendar_id_is_skipped():
    rows = [
        {"calendar_id": True, "dayofweek": "0", "hour_from": 7.0, "hour_to": 15.0},
        {"calendar_id": [7, "D"], "dayofweek": "0", "hour_from": 7.0, "hour_to": 15.0},
    ]
    assert _calendar_hours_from_lines(rows) == {7: {"0": ["07:00", "15:00"]}}


def test_calendar_lunch_windows_extracts_lunch_periods_only():
    rows = [
        {"calendar_id": [7, "Plant"], "dayofweek": "4", "day_period": "morning", "hour_from": 6.0, "hour_to": 10.0},
        {"calendar_id": [7, "Plant"], "dayofweek": "4", "day_period": "lunch", "hour_from": 11.0, "hour_to": 11.5},
        {"calendar_id": [8, "Drivers"], "dayofweek": "4", "day_period": "lunch", "hour_from": 10.25, "hour_to": 10.75},
        {"calendar_id": [9, "Bad"], "dayofweek": "bad", "day_period": "lunch", "hour_from": 11.0, "hour_to": 11.5},
    ]

    assert _calendar_lunch_windows_from_lines(rows) == {
        7: {"4": ["11:00", "11:30"]},
        8: {"4": ["10:15", "10:45"]},
    }
