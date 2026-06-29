from datetime import date

from scripts import repair_time_off_full_day_hours as repair


def _leave(leave_id, *, number_of_days, request_hour_from=False, request_hour_to=3.5):
    return {
        "id": leave_id,
        "employee_id": [5, "Bob"],
        "holiday_status_id": [1, "PTO"],
        "state": "validate",
        "request_date_from": "2026-06-01",
        "request_date_to": "2026-06-01",
        "request_hour_from": request_hour_from,
        "request_hour_to": request_hour_to,
        "request_unit_hours": True,
        "number_of_days": number_of_days,
        "number_of_hours": 8.0,
        "name": "PTO",
    }


def _row(leave_id, *, shape="midday_gap", hour_from=None, hour_to=3.5):
    return {
        "id": 10 + leave_id,
        "odoo_leave_id": leave_id,
        "person_name": "Bob",
        "shape": shape,
        "date_from": date(2026, 6, 1),
        "date_to": date(2026, 6, 1),
        "hour_from": hour_from,
        "hour_to": hour_to,
        "working_hours_json": None,
    }


def test_corrections_select_only_rows_odoo_normalizes_to_full_day():
    corrections = repair.corrections_for(
        [
            _leave(1, number_of_days=1.0),  # incomplete 3.5h row, full-day in Odoo
            _leave(2, number_of_days=1.0, request_hour_from=6.0, request_hour_to=14.5),
            _leave(3, number_of_days=0.44, request_hour_from=6.0, request_hour_to=9.5),
            _leave(4, number_of_days=1.0),
        ],
        {
            1: _row(1),
            2: _row(2, hour_from=6.0, hour_to=14.5),
            3: _row(3, hour_from=6.0, hour_to=9.5),
            4: _row(4, shape="full_day", hour_from=None, hour_to=None),
        },
    )

    assert [c["id"] for c in corrections] == [11, 12]
    assert corrections[0]["odoo_leave_id"] == 1
    assert corrections[0]["from_shape"] == "midday_gap"
    assert corrections[0]["from_hours"] == "None-3.5"
