"""Settings routes for rounding systems + department mapping. Postgres-backed."""

import os

import pytest
from fastapi.testclient import TestClient

from zira_dashboard.app import app
from zira_dashboard import db, rounding_system_store
from zira_dashboard.rounding import RoundingSettings

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="needs Postgres",
)

client = TestClient(app)
SYS_NAME = "ZZ Route System"
DEPT = "ZZ Route Dept"


@pytest.fixture(autouse=True)
def _clean():
    db.execute("DELETE FROM department_rounding WHERE department = %s", (DEPT,))
    db.execute("DELETE FROM rounding_systems WHERE name LIKE 'ZZ Route%'")
    rounding_system_store.reload()
    yield
    db.execute("DELETE FROM department_rounding WHERE department = %s", (DEPT,))
    db.execute("DELETE FROM rounding_systems WHERE name LIKE 'ZZ Route%'")
    rounding_system_store.reload()


def _sid():
    return next(s.id for s in rounding_system_store.all_systems() if s.name == SYS_NAME)


def test_add_then_save_windows_clamps():
    r = client.post("/settings/rounding_system/add", data={"name": SYS_NAME}, follow_redirects=False)
    assert r.status_code == 303
    rounding_system_store.reload()
    sid = _sid()
    r = client.post(
        "/settings/rounding_system",
        data={"system_id": str(sid), "in_before_min": "20", "in_after_min": "0",
              "out_before_min": "0", "out_after_min": "999"},  # clamps to 60
        headers={"accept": "application/json"},
    )
    assert r.status_code == 200
    rounding_system_store.reload()
    sysrec = next(s for s in rounding_system_store.all_systems() if s.id == sid)
    assert sysrec.rounding == RoundingSettings(20, 0, 0, 60)


def test_set_department_map():
    client.post("/settings/rounding_system/add", data={"name": SYS_NAME}, follow_redirects=False)
    rounding_system_store.reload()
    sid = _sid()
    rounding_system_store.save_system_windows(sid, RoundingSettings(20, 0, 0, 0))
    r = client.post(
        "/settings/department_rounding",
        data={"department": DEPT, "system_id": str(sid)},
        headers={"accept": "application/json"},
    )
    assert r.status_code == 200
    rounding_system_store.reload()
    assert rounding_system_store.windows_for_department(DEPT) == RoundingSettings(20, 0, 0, 0)


def test_set_department_map_to_plant_default():
    client.post("/settings/rounding_system/add", data={"name": SYS_NAME}, follow_redirects=False)
    rounding_system_store.reload()
    sid = _sid()
    rounding_system_store.set_department_system(DEPT, sid)
    r = client.post(
        "/settings/department_rounding",
        data={"department": DEPT, "system_id": "none"},
        headers={"accept": "application/json"},
    )
    assert r.status_code == 200
    rounding_system_store.reload()
    assert rounding_system_store.windows_for_department(DEPT) is None


def test_remove_system():
    client.post("/settings/rounding_system/add", data={"name": SYS_NAME}, follow_redirects=False)
    rounding_system_store.reload()
    sid = _sid()
    r = client.post("/settings/rounding_system/remove", data={"system_id": str(sid)}, follow_redirects=False)
    assert r.status_code == 303
    rounding_system_store.reload()
    assert all(s.id != sid for s in rounding_system_store.all_systems())


def test_save_bad_id_returns_400():
    r = client.post(
        "/settings/rounding_system",
        data={"system_id": "notanint", "in_before_min": "5"},
        headers={"accept": "application/json"},
    )
    assert r.status_code == 400


def test_settings_page_shows_systems_section():
    r = client.get("/settings?section=timeclock")
    assert r.status_code == 200
    assert "Rounding systems" in r.text
    assert "Department rounding" in r.text
