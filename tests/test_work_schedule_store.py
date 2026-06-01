"""Tests for work_schedule_store load/save/refresh/cache. Postgres-backed."""

import os
from datetime import time

import pytest

from zira_dashboard import db, work_schedule_store
from zira_dashboard.rounding import RoundingSettings

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="needs Postgres",
)

CAL_ID = 990001  # a test calendar id unlikely to collide


@pytest.fixture(autouse=True)
def _clean():
    db.execute("DELETE FROM work_schedules WHERE resource_calendar_id = %s", (CAL_ID,))
    work_schedule_store.reload()
    yield
    db.execute("DELETE FROM work_schedules WHERE resource_calendar_id = %s", (CAL_ID,))
    work_schedule_store.reload()


def test_create_then_get_returns_zero_rounding():
    work_schedule_store.create(CAL_ID, "Drivers")
    ws = work_schedule_store.get(CAL_ID)
    assert ws is not None
    assert ws.name == "Drivers"
    assert ws.rounding == RoundingSettings(0, 0, 0, 0)
    assert ws.work_hours == {}


def test_save_rounding_updates_only_windows():
    work_schedule_store.create(CAL_ID, "Drivers")
    work_schedule_store.refresh_synced(CAL_ID, "Drivers 5:45", {"0": ["05:45", "14:30"]})
    work_schedule_store.save_rounding(CAL_ID, RoundingSettings(20, 0, 0, 0))
    ws = work_schedule_store.get(CAL_ID)
    assert ws.rounding == RoundingSettings(20, 0, 0, 0)
    # Hours + name (Odoo-owned) survive a rounding save.
    assert ws.work_hours == {0: (time(5, 45), time(14, 30))}
    assert ws.name == "Drivers 5:45"


def test_refresh_synced_updates_only_hours_and_name():
    work_schedule_store.create(CAL_ID, "Drivers")
    work_schedule_store.save_rounding(CAL_ID, RoundingSettings(20, 0, 0, 0))
    work_schedule_store.refresh_synced(CAL_ID, "Drivers 5:45", {"0": ["05:45", "14:30"]})
    ws = work_schedule_store.get(CAL_ID)
    # Windows (app-owned) survive a sync refresh.
    assert ws.rounding == RoundingSettings(20, 0, 0, 0)
    assert ws.work_hours == {0: (time(5, 45), time(14, 30))}


def test_get_missing_returns_none():
    assert work_schedule_store.get(CAL_ID) is None


def test_cache_invalidated_on_save():
    work_schedule_store.create(CAL_ID, "Drivers")
    work_schedule_store.get(CAL_ID)  # prime cache
    db.execute("UPDATE work_schedules SET in_before_min = 99 WHERE resource_calendar_id = %s", (CAL_ID,))
    assert work_schedule_store.get(CAL_ID).rounding.in_before_min == 0  # stale cache
    work_schedule_store.reload()
    assert work_schedule_store.get(CAL_ID).rounding.in_before_min == 99


def test_delete_removes_override():
    work_schedule_store.create(CAL_ID, "Drivers")
    work_schedule_store.delete(CAL_ID)
    assert work_schedule_store.get(CAL_ID) is None
