"""Tests for GET /api/feedback/mine (store + Odoo monkeypatched)."""

from fastapi.testclient import TestClient

from zira_dashboard import feedback_store, odoo_client
from zira_dashboard.app import app

client = TestClient(app)


def _legacy_rows():
    return [
        {
            "id": 2,
            "created_at": "2026-06-24 10:00",
            "submitter": None,
            "page_url": "/p",
            "task_type": "bug",
            "odoo_task_id": 901,
            "message": "Totals wrong\nmore detail",
            "status": None,
        },
        {
            "id": 1,
            "created_at": "2026-06-23 09:00",
            "submitter": None,
            "page_url": None,
            "task_type": "feature",
            "odoo_task_id": 902,
            "message": "Add export",
            "status": None,
        },
    ]


def test_mine_maps_batched_legacy_odoo_statuses_to_local_names(monkeypatch):
    monkeypatch.setattr(
        feedback_store, "for_submitter", lambda upn, limit=100: _legacy_rows()
    )
    seen = []
    monkeypatch.setattr(
        odoo_client,
        "fetch_task_stage_names",
        lambda ids: seen.append(ids) or {901: "Done", 902: "Rejected"},
    )

    response = client.get("/api/feedback/mine")

    assert response.status_code == 200
    body = response.json()
    assert seen == [[901, 902]]
    assert body["status_available"] is True
    assert body["items"][0] == {
        "type": "bug",
        "title": "Totals wrong",
        "created_at": "2026-06-24 10:00",
        "page_url": "/p",
        "status": "completed",
    }
    assert body["items"][1]["status"] == "declined"


def test_mine_uses_local_status_without_odoo_for_migrated_rows(monkeypatch):
    monkeypatch.setattr(
        feedback_store,
        "for_submitter",
        lambda upn, limit=100: [
            {
                "id": 1,
                "message": "Safe",
                "task_type": "bug",
                "created_at": "2026-08-20",
                "page_url": None,
                "status": "completed",
                "odoo_task_id": 999,
            }
        ],
    )
    monkeypatch.setattr(
        odoo_client,
        "fetch_task_stage_names",
        lambda ids: (_ for _ in ()).throw(AssertionError(ids)),
    )

    response = client.get("/api/feedback/mine")

    assert response.status_code == 200
    assert response.json()["items"][0]["status"] == "completed"
    assert response.json()["status_available"] is True


def test_mine_only_fetches_status_null_legacy_task_ids(monkeypatch):
    rows = _legacy_rows()[:1]
    rows.insert(
        0,
        {
            "id": 3,
            "created_at": "2026-08-20",
            "page_url": None,
            "task_type": "feature",
            "odoo_task_id": 777,
            "message": "Already local",
            "status": "in_progress",
        },
    )
    monkeypatch.setattr(feedback_store, "for_submitter", lambda upn, limit=100: rows)
    seen = []
    monkeypatch.setattr(
        odoo_client,
        "fetch_task_stage_names",
        lambda ids: seen.extend(ids) or {901: "New"},
    )

    response = client.get("/api/feedback/mine")

    assert response.status_code == 200
    assert seen == [901]
    assert [item["status"] for item in response.json()["items"]] == [
        "in_progress",
        "requested",
    ]


def test_mine_defaults_legacy_status_to_requested_when_odoo_unavailable(monkeypatch):
    monkeypatch.setattr(
        feedback_store, "for_submitter", lambda upn, limit=100: _legacy_rows()
    )

    def unavailable(ids):
        raise RuntimeError("odoo down")

    monkeypatch.setattr(odoo_client, "fetch_task_stage_names", unavailable)

    response = client.get("/api/feedback/mine")

    assert response.status_code == 200
    body = response.json()
    assert all(item["status"] == "requested" for item in body["items"])
    assert body["status_available"] is False
