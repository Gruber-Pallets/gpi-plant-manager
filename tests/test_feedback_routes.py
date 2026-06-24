"""Feedback route tests; store is monkeypatched so no Postgres is required."""

from fastapi.testclient import TestClient

from zira_dashboard import feedback_store
from zira_dashboard.app import app

client = TestClient(app)


def test_post_feedback_inserts_and_returns_id(monkeypatch):
    captured = {}

    def fake_insert(**kwargs):
        captured.update(kwargs)
        return 123

    monkeypatch.setattr(feedback_store, "insert", fake_insert)

    resp = client.post("/feedback", json={
        "message": "  Great app  ",
        "category": "Idea",
        "page_url": "/recycling",
    })

    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "id": 123}
    assert captured["message"] == "Great app"
    assert captured["category"] == "Idea"
    assert captured["page_url"] == "/recycling"


def test_post_feedback_rejects_empty_message(monkeypatch):
    called = {"n": 0}

    def fake_insert(**kwargs):
        called["n"] += 1
        return 1

    monkeypatch.setattr(feedback_store, "insert", fake_insert)

    resp = client.post("/feedback", json={"message": "   "})

    assert resp.status_code == 400
    assert resp.json()["ok"] is False
    assert called["n"] == 0


def test_admin_feedback_renders_rows(monkeypatch):
    monkeypatch.setattr(feedback_store, "recent", lambda limit=200: [{
        "id": 5,
        "created_at": "2026-06-24 09:00",
        "submitter": "dale@example.com",
        "page_url": "/staffing",
        "category": "Bug",
        "message": "Sticky note text here",
    }])

    resp = client.get("/admin/feedback")

    assert resp.status_code == 200
    assert "Sticky note text here" in resp.text
    assert "dale@example.com" in resp.text
