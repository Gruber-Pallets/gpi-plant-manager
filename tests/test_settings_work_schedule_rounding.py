"""Settings routes for per-schedule rounding. Postgres-backed; Odoo not
required (the add route's hours-refresh is best-effort)."""

import os

import pytest
from fastapi.testclient import TestClient

from zira_dashboard.app import app
from zira_dashboard import db, work_schedule_store, odoo_client
from zira_dashboard.rounding import RoundingSettings

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="needs Postgres",
)

client = TestClient(app)
CAL_ID = 990004


@pytest.fixture(autouse=True)
def _clean():
    db.execute("DELETE FROM work_schedules WHERE resource_calendar_id = %s", (CAL_ID,))
    work_schedule_store.reload()
    yield
    db.execute("DELETE FROM work_schedules WHERE resource_calendar_id = %s", (CAL_ID,))
    work_schedule_store.reload()


def test_save_clamps_and_persists():
    work_schedule_store.create(CAL_ID, "Drivers")
    r = client.post(
        "/settings/work_schedule_rounding",
        data={
            "resource_calendar_id": str(CAL_ID),
            "in_before_min": "20", "in_after_min": "0",
            "out_before_min": "0", "out_after_min": "999",  # clamps to 60
        },
        headers={"accept": "application/json"},
    )
    assert r.status_code == 200
    work_schedule_store.reload()
    assert work_schedule_store.get(CAL_ID).rounding == RoundingSettings(20, 0, 0, 60)


def test_add_creates_override(monkeypatch):
    monkeypatch.setattr(odoo_client, "fetch_work_schedules",
                        lambda: [{"id": CAL_ID, "name": "Drivers"}])
    monkeypatch.setattr(odoo_client, "fetch_calendar_hours",
                        lambda ids: {CAL_ID: {"0": ["05:45", "14:30"]}})
    r = client.post(
        "/settings/work_schedule_rounding/add",
        data={"resource_calendar_id": str(CAL_ID)},
        follow_redirects=False,
    )
    assert r.status_code == 303
    work_schedule_store.reload()
    assert work_schedule_store.get(CAL_ID) is not None


def test_remove_deletes_override():
    work_schedule_store.create(CAL_ID, "Drivers")
    r = client.post(
        "/settings/work_schedule_rounding/remove",
        data={"resource_calendar_id": str(CAL_ID)},
        follow_redirects=False,
    )
    assert r.status_code == 303
    work_schedule_store.reload()
    assert work_schedule_store.get(CAL_ID) is None


def test_timeclock_settings_render_when_odoo_unavailable(monkeypatch):
    # Headline requirement: the Add-dropdown's fetch_work_schedules() is
    # guarded, so the settings page still renders (200) if Odoo is down.
    def _boom():
        raise RuntimeError("odoo unavailable")
    monkeypatch.setattr(odoo_client, "fetch_work_schedules", _boom)
    r = client.get("/settings?section=timeclock")
    assert r.status_code == 200


def test_save_bad_id_returns_400():
    r = client.post(
        "/settings/work_schedule_rounding",
        data={"resource_calendar_id": "notanint", "in_before_min": "5"},
        headers={"accept": "application/json"},
    )
    assert r.status_code == 400


def test_add_bad_id_returns_400():
    r = client.post(
        "/settings/work_schedule_rounding/add",
        data={"resource_calendar_id": ""},
        follow_redirects=False,
    )
    assert r.status_code == 400


def test_remove_bad_id_returns_400():
    r = client.post(
        "/settings/work_schedule_rounding/remove",
        data={"resource_calendar_id": "x"},
        follow_redirects=False,
    )
    assert r.status_code == 400
