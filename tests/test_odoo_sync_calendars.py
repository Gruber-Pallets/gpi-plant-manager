"""Tests for sync helpers: many2one id extraction (pure) + schedule-hours
refresh (Postgres-backed, Odoo monkeypatched)."""

import os

import pytest

from zira_dashboard import odoo_sync


def test_m2o_id_extracts_id():
    assert odoo_sync._m2o_id([7, "Drivers"]) == 7
    assert odoo_sync._m2o_id(False) is None
    assert odoo_sync._m2o_id(None) is None
    assert odoo_sync._m2o_id([]) is None


@pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="needs Postgres")
def test_refresh_only_touches_configured_overrides(monkeypatch):
    from zira_dashboard import db, work_schedule_store, odoo_client
    from zira_dashboard.rounding import RoundingSettings

    cal_id = 990002
    other_id = 990099  # an Odoo schedule that is NOT configured as an override
    db.execute("DELETE FROM work_schedules WHERE resource_calendar_id = %s", (cal_id,))
    db.execute("DELETE FROM work_schedules WHERE resource_calendar_id = %s", (other_id,))
    work_schedule_store.reload()
    try:
        work_schedule_store.create(cal_id, "Drivers")
        work_schedule_store.save_rounding(cal_id, RoundingSettings(20, 0, 0, 0))

        monkeypatch.setattr(odoo_client, "fetch_work_schedules",
                            lambda: [{"id": cal_id, "name": "Drivers 5:45"},
                                     {"id": other_id, "name": "Some Other Schedule"}])
        monkeypatch.setattr(odoo_client, "fetch_calendar_hours",
                            lambda ids: {cal_id: {"0": ["05:45", "14:30"]}})

        odoo_sync.refresh_work_schedule_hours()

        ws = work_schedule_store.get(cal_id)
        assert ws.name == "Drivers 5:45"
        assert ws.work_hours[0][0].hour == 5 and ws.work_hours[0][0].minute == 45
        assert ws.rounding == RoundingSettings(20, 0, 0, 0)
        # The unconfigured Odoo schedule must NOT become an override row.
        assert work_schedule_store.get(other_id) is None
    finally:
        db.execute("DELETE FROM work_schedules WHERE resource_calendar_id = %s", (cal_id,))
        db.execute("DELETE FROM work_schedules WHERE resource_calendar_id = %s", (other_id,))
        work_schedule_store.reload()
