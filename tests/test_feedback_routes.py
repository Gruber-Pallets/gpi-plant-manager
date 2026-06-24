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


def test_post_feedback_trims_optional_fields_and_drops_blanks(monkeypatch):
    captured = {}

    def fake_insert(**kwargs):
        captured.update(kwargs)
        return 124

    monkeypatch.setattr(feedback_store, "insert", fake_insert)

    resp = client.post("/feedback", json={
        "message": "  Needs cleanup  ",
        "category": "   ",
        "page_url": "  /staffing?day=2026-06-24  ",
    })

    assert resp.status_code == 200
    assert captured["message"] == "Needs cleanup"
    assert captured["category"] is None
    assert captured["page_url"] == "/staffing?day=2026-06-24"


def test_post_feedback_drops_unsafe_page_url(monkeypatch):
    captured = {}

    def fake_insert(**kwargs):
        captured.update(kwargs)
        return 125

    monkeypatch.setattr(feedback_store, "insert", fake_insert)

    resp = client.post("/feedback", json={
        "message": "Look at this",
        "page_url": "javascript:alert(1)",
    })

    assert resp.status_code == 200
    assert captured["page_url"] is None
