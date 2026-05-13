"""Integration tests for the tv-templates API endpoints."""
from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

from zira_dashboard.app import app

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="tv-templates route tests need Postgres",
)


@pytest.fixture(autouse=True)
def _clean_templates():
    from zira_dashboard import db
    db.init_pool()
    db.bootstrap_schema()
    db.execute("DELETE FROM tv_dashboard_templates WHERE name LIKE 'rt-%'")
    db.execute("DELETE FROM widget_layouts WHERE page LIKE 'rt-wc:%'")
    yield
    db.execute("DELETE FROM tv_dashboard_templates WHERE name LIKE 'rt-%'")
    db.execute("DELETE FROM widget_layouts WHERE page LIKE 'rt-wc:%'")


def test_post_save_creates_template():
    c = TestClient(app)
    r = c.post("/api/tv-templates", json={
        "name": "rt-template",
        "layout": [{"id": "wc-pallets-banner", "x": 0, "y": 0, "w": 12, "h": 2}],
        "theme": "dark",
    })
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True


def test_post_save_rejects_missing_name():
    c = TestClient(app)
    r = c.post("/api/tv-templates", json={"layout": []})
    assert r.status_code == 400


def test_get_list_returns_saved_templates():
    c = TestClient(app)
    c.post("/api/tv-templates", json={
        "name": "rt-list-test",
        "layout": [{"id": "wc-pallets-banner", "x": 0, "y": 0, "w": 12, "h": 2}],
    })
    r = c.get("/api/tv-templates")
    assert r.status_code == 200
    names = [t["name"] for t in r.json()["templates"]]
    assert "rt-list-test" in names


def test_delete_template():
    c = TestClient(app)
    c.post("/api/tv-templates", json={
        "name": "rt-to-delete",
        "layout": [{"id": "wc-pallets-banner", "x": 0, "y": 0, "w": 12, "h": 2}],
    })
    list_r = c.get("/api/tv-templates")
    tid = next(t["id"] for t in list_r.json()["templates"] if t["name"] == "rt-to-delete")
    r = c.delete(f"/api/tv-templates/{tid}")
    assert r.status_code == 200
    assert r.json()["ok"] is True


def test_post_apply_to_explicit_targets():
    from zira_dashboard import layout_store
    c = TestClient(app)
    c.post("/api/tv-templates", json={
        "name": "rt-apply",
        "layout": [{"id": "wc-pallets-banner", "x": 0, "y": 0, "w": 12, "h": 2}],
    })
    list_r = c.get("/api/tv-templates")
    tid = next(t["id"] for t in list_r.json()["templates"] if t["name"] == "rt-apply")

    r = c.post(f"/api/tv-templates/{tid}/apply", json={
        "targets": ["rt-wc:a", "rt-wc:b"],
    })
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert sorted(body["applied_pages"]) == ["rt-wc:a", "rt-wc:b"]
    assert body["applied_count"] == 2
    assert layout_store.load("rt-wc:a")[0]["id"] == "wc-pallets-banner"
    assert layout_store.load("rt-wc:b")[0]["id"] == "wc-pallets-banner"


def test_post_apply_unknown_id_returns_404():
    c = TestClient(app)
    r = c.post("/api/tv-templates/999999999/apply", json={"targets": ["wc:nowhere"]})
    assert r.status_code == 404
