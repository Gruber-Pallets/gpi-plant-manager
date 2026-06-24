"""Tests for GET /api/feedback/mine (store + Odoo monkeypatched)."""

from fastapi.testclient import TestClient

from zira_dashboard import feedback_store, odoo_client
from zira_dashboard.app import app

client = TestClient(app)


def _rows():
    return [
        {"id": 2, "created_at": "2026-06-24 10:00", "submitter": None,
         "page_url": "/p", "task_type": "bug", "odoo_task_id": 901,
         "message": "Totals wrong\nmore detail"},
        {"id": 1, "created_at": "2026-06-23 09:00", "submitter": None,
         "page_url": None, "task_type": "feature", "odoo_task_id": 902,
         "message": "Add export"},
    ]


def test_mine_merges_live_status(monkeypatch):
    monkeypatch.setattr(feedback_store, "for_submitter", lambda upn, limit=100: _rows())
    monkeypatch.setattr(
        odoo_client, "fetch_task_stage_names",
        lambda ids: {901: "Done", 902: "Rejected"},
    )

    resp = client.get("/api/feedback/mine")

    assert resp.status_code == 200
    items = resp.json()["items"]
    assert items[0]["type"] == "bug"
    assert items[0]["title"] == "Totals wrong"
    assert items[0]["status"] == "done"
    assert items[1]["status"] == "rejected"


def test_mine_defaults_open_when_odoo_unavailable(monkeypatch):
    monkeypatch.setattr(feedback_store, "for_submitter", lambda upn, limit=100: _rows())

    def boom(ids):
        raise RuntimeError("odoo down")

    monkeypatch.setattr(odoo_client, "fetch_task_stage_names", boom)

    resp = client.get("/api/feedback/mine")

    assert resp.status_code == 200
    body = resp.json()
    assert all(it["status"] == "open" for it in body["items"])
    assert body["status_available"] is False
