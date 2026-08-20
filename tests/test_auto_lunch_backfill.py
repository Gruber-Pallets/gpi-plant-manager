from datetime import datetime

from zira_dashboard import shift_config
from zira_dashboard.auto_lunch import Window
from zira_dashboard.auto_lunch_backfill import Repair, plan_repairs


def _dt(hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 8, 18, hour, minute, tzinfo=shift_config.SITE_TZ)


def _interval(check_in: datetime, check_out: datetime) -> dict:
    return {
        "id": 41,
        "employee_odoo_id": 7,
        "check_in": check_in,
        "check_out": check_out,
        "wc_name": "Repair 1",
    }


def test_plan_repairs_splits_interval_covering_lunch():
    window = Window(_dt(11), _dt(11, 30))

    repairs = plan_repairs([_interval(_dt(7), _dt(15, 30))], {7: window}, set())

    assert repairs == [Repair(
        attendance_id=41,
        person_odoo_id=7,
        out_at=_dt(11),
        in_at=_dt(11, 30),
        wc_name="Repair 1",
        create_return=True,
    )]


def test_plan_repairs_accepts_normalized_odoo_timestamps():
    window = Window(_dt(11), _dt(11, 30))
    interval = _interval("2026-08-18T12:00:00+00:00", "2026-08-18T20:30:00+00:00")

    repairs = plan_repairs([interval], {7: window}, set())

    assert repairs[0].out_at == _dt(11)
    assert repairs[0].create_return is True
