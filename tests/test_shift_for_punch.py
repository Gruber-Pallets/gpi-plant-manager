"""Resolution of (shift_start, shift_end, rounding) per employee schedule.
Postgres-backed (needs people + work_schedules)."""

import os
from datetime import date, time

import pytest

from zira_dashboard import db, work_schedule_store, shift_config
from zira_dashboard.rounding import RoundingSettings
from zira_dashboard.routes.timeclock import _shift_for_punch

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="needs Postgres",
)

CAL_ID = 990003
ODOO_ID = 990103  # test employee odoo_id
MONDAY = date(2026, 6, 1)   # 2026-06-01 is a Monday (weekday 0)


@pytest.fixture(autouse=True)
def _seed():
    db.execute("DELETE FROM work_schedules WHERE resource_calendar_id = %s", (CAL_ID,))
    db.execute(
        "INSERT INTO people (odoo_id, name, active, resource_calendar_id) "
        "VALUES (%s, %s, TRUE, %s) "
        "ON CONFLICT (odoo_id) DO UPDATE SET resource_calendar_id = EXCLUDED.resource_calendar_id, "
        "active = TRUE",
        (ODOO_ID, "Test Driver", CAL_ID),
    )
    work_schedule_store.reload()
    yield
    db.execute("DELETE FROM people WHERE odoo_id = %s", (ODOO_ID,))
    db.execute("DELETE FROM work_schedules WHERE resource_calendar_id = %s", (CAL_ID,))
    work_schedule_store.reload()


def test_driver_resolves_to_override_hours_and_windows():
    work_schedule_store.create(CAL_ID, "Drivers")
    work_schedule_store.refresh_synced(CAL_ID, "Drivers", {"0": ["05:45", "14:30"]})
    work_schedule_store.save_rounding(CAL_ID, RoundingSettings(20, 0, 0, 0))

    start, end, windows = _shift_for_punch(ODOO_ID, MONDAY)
    assert start == time(5, 45)
    assert end == time(14, 30)
    assert windows == RoundingSettings(20, 0, 0, 0)


def test_weekday_without_hours_falls_back_to_plant_default():
    work_schedule_store.create(CAL_ID, "Drivers")
    # Only Monday configured; ask for a Saturday punch (weekday 5).
    work_schedule_store.refresh_synced(CAL_ID, "Drivers", {"0": ["05:45", "14:30"]})
    work_schedule_store.save_rounding(CAL_ID, RoundingSettings(20, 0, 0, 0))

    saturday = date(2026, 6, 6)
    start, end, windows = _shift_for_punch(ODOO_ID, saturday)
    assert start == shift_config.shift_start_for(saturday)
    assert end == shift_config.shift_end_for(saturday)


def test_employee_without_override_uses_plant_default():
    # No work_schedules row for CAL_ID -> plant default.
    start, end, windows = _shift_for_punch(ODOO_ID, MONDAY)
    assert start == shift_config.shift_start_for(MONDAY)
    assert end == shift_config.shift_end_for(MONDAY)
