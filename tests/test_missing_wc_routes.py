"""Missing-WC routes: GET shape, assign (mocked Odoo) + dismiss record suppression."""

import os
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from zira_dashboard.app import app
from zira_dashboard import db, missing_wc, odoo_client

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="needs Postgres",
)

client = TestClient(app)
ATT = 999100


@pytest.fixture(autouse=True)
def _clean():
    db.execute("DELETE FROM missing_wc_resolved WHERE attendance_id = %s", (ATT,))
    yield
    db.execute("DELETE FROM missing_wc_resolved WHERE attendance_id = %s", (ATT,))


def test_get_returns_count_rows_and_work_centers():
    r = client.get("/api/missing-wc")
    assert r.status_code == 200
    body = r.json()
    assert set(["count", "rows", "work_centers"]) <= set(body.keys())
    assert "Dismantler 1" in body["work_centers"]


def test_assign_writes_wc_and_records_resolved(monkeypatch):
    calls = {}
    monkeypatch.setattr(odoo_client, "set_attendance_wc",
                        lambda att_id, wc: calls.update(att_id=att_id, wc=wc) or True)
    r = client.post("/missing-wc/assign",
                    json={"attendance_id": ATT, "wc_name": "Dismantler 1", "name": "Maria"})
    assert r.status_code == 200 and r.json()["ok"] is True
    assert calls == {"att_id": ATT, "wc": "Dismantler 1"}
    assert ATT in missing_wc.resolved_ids()


def test_assign_rejects_work_center_without_active_odoo_mapping(monkeypatch):
    monkeypatch.setattr(odoo_client, "set_attendance_wc", lambda *_args: False)
    resolve = MagicMock()
    log_event = MagicMock()
    monkeypatch.setattr(missing_wc, "resolve", resolve)
    monkeypatch.setattr("zira_dashboard.inbox_log.log_event_safe", log_event)

    r = client.post(
        "/missing-wc/assign",
        json={"attendance_id": ATT, "wc_name": "Dismantler 1", "name": "Maria"},
    )

    assert r.status_code == 409
    assert r.json() == {
        "ok": False,
        "error": "That work center has no active Odoo mapping yet.",
    }
    resolve.assert_not_called()
    log_event.assert_not_called()
    assert ATT not in missing_wc.resolved_ids()


def test_assign_rejects_unknown_wc():
    r = client.post("/missing-wc/assign",
                    json={"attendance_id": ATT, "wc_name": "Not A WC"})
    assert r.status_code == 400
    assert ATT not in missing_wc.resolved_ids()


def test_assign_rejects_bad_id():
    r = client.post("/missing-wc/assign", json={"attendance_id": "x", "wc_name": "Dismantler 1"})
    assert r.status_code == 400


def test_dismiss_records_resolved():
    r = client.post("/missing-wc/dismiss", json={"attendance_id": ATT, "name": "Maria"})
    assert r.status_code == 200 and r.json()["ok"] is True
    assert ATT in missing_wc.resolved_ids()
