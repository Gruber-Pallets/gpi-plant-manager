"""Integration tests for custom dashboard routes (CRUD + placements).

Editor and TV render tests come in Task 8 when the template exists.
"""
from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

from zira_dashboard.app import app

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="custom dashboards routes need Postgres",
)


@pytest.fixture(autouse=True)
def _clean():
    from zira_dashboard import db
    db.init_pool()
    db.bootstrap_schema()
    db.execute("DELETE FROM custom_dashboards WHERE slug LIKE 'cdr-%'")
    db.execute("DELETE FROM widget_definitions WHERE name LIKE 'cdr-%'")
    yield
    db.execute("DELETE FROM custom_dashboards WHERE slug LIKE 'cdr-%'")
    db.execute("DELETE FROM widget_definitions WHERE name LIKE 'cdr-%'")


def test_post_dashboard_creates():
    c = TestClient(app)
    r = c.post("/api/dashboards", json={
        "name": "cdr-repair-1-tv",
        "scope_kind": "wc",
        "scope_value": "Repair 1",
        "theme": "dark",
    })
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["dashboard"]["slug"] == "cdr-repair-1-tv"


def test_post_dashboard_rejects_bad_scope():
    c = TestClient(app)
    r = c.post("/api/dashboards", json={
        "name": "cdr-bad", "scope_kind": "fake", "scope_value": "x", "theme": "dark",
    })
    assert r.status_code == 400


def test_delete_dashboard():
    c = TestClient(app)
    add = c.post("/api/dashboards", json={
        "name": "cdr-deleteme", "scope_kind": "wc", "scope_value": "Repair 1", "theme": "dark",
    }).json()
    r = c.delete(f"/api/dashboards/{add['dashboard']['id']}")
    assert r.status_code == 200


def test_get_dashboards_index_page_renders():
    c = TestClient(app)
    c.post("/api/dashboards", json={
        "name": "cdr-shown", "scope_kind": "wc", "scope_value": "Repair 1", "theme": "dark",
    })
    r = c.get("/dashboards")
    assert r.status_code == 200
    assert "cdr-shown" in r.text


def test_add_placement():
    from zira_dashboard import widget_definitions_store
    c = TestClient(app)
    dash = c.post("/api/dashboards", json={
        "name": "cdr-place", "scope_kind": "wc", "scope_value": "Repair 1", "theme": "dark",
    }).json()["dashboard"]
    wd = widget_definitions_store.save(
        name="cdr-wd", type="ribbons", visual={}, default_data={"group": "Repairs"},
    )
    r = c.post(f"/api/dashboards/{dash['id']}/placements", json={
        "widget_def_id": wd["id"],
        "x": 0, "y": 0, "w": 4, "h": 4,
        "data_overrides": {"group": "Dismantlers"},
    })
    assert r.status_code == 200
    assert r.json()["placement"]["data_overrides"] == {"group": "Dismantlers"}


def test_patch_placement_updates_position():
    from zira_dashboard import widget_definitions_store
    c = TestClient(app)
    dash = c.post("/api/dashboards", json={
        "name": "cdr-patch", "scope_kind": "wc", "scope_value": "Repair 1", "theme": "dark",
    }).json()["dashboard"]
    wd = widget_definitions_store.save(
        name="cdr-patch-wd", type="ribbons", visual={}, default_data={"group": "Repairs"},
    )
    p = c.post(f"/api/dashboards/{dash['id']}/placements", json={
        "widget_def_id": wd["id"], "x": 0, "y": 0, "w": 4, "h": 4, "data_overrides": {},
    }).json()["placement"]
    r = c.patch(f"/api/placements/{p['id']}", json={"x": 6, "y": 2})
    assert r.status_code == 200


def test_delete_placement():
    from zira_dashboard import widget_definitions_store
    c = TestClient(app)
    dash = c.post("/api/dashboards", json={
        "name": "cdr-delp", "scope_kind": "wc", "scope_value": "Repair 1", "theme": "dark",
    }).json()["dashboard"]
    wd = widget_definitions_store.save(
        name="cdr-delp-wd", type="ribbons", visual={}, default_data={"group": "Repairs"},
    )
    p = c.post(f"/api/dashboards/{dash['id']}/placements", json={
        "widget_def_id": wd["id"], "x": 0, "y": 0, "w": 4, "h": 4, "data_overrides": {},
    }).json()["placement"]
    r = c.delete(f"/api/placements/{p['id']}")
    assert r.status_code == 200


def test_post_dashboard_layout_bulk_save():
    """Gridstack autosave POSTs a full layout list."""
    from zira_dashboard import widget_definitions_store
    c = TestClient(app)
    dash = c.post("/api/dashboards", json={
        "name": "cdr-bulk", "scope_kind": "wc", "scope_value": "Repair 1", "theme": "dark",
    }).json()["dashboard"]
    wd = widget_definitions_store.save(
        name="cdr-bulk-wd", type="ribbons", visual={}, default_data={"group": "Repairs"},
    )
    p1 = c.post(f"/api/dashboards/{dash['id']}/placements", json={
        "widget_def_id": wd["id"], "x": 0, "y": 0, "w": 4, "h": 4, "data_overrides": {},
    }).json()["placement"]
    p2 = c.post(f"/api/dashboards/{dash['id']}/placements", json={
        "widget_def_id": wd["id"], "x": 4, "y": 0, "w": 4, "h": 4, "data_overrides": {},
    }).json()["placement"]
    r = c.post(f"/api/dashboards/{dash['id']}/layout", json=[
        {"id": p1["id"], "x": 8, "y": 0, "w": 4, "h": 4},
        {"id": p2["id"], "x": 0, "y": 4, "w": 6, "h": 5},
    ])
    assert r.status_code == 200
    assert r.json()["ok"] is True


def test_editor_renders_with_placements():
    from zira_dashboard import widget_definitions_store
    c = TestClient(app)
    dash = c.post("/api/dashboards", json={
        "name": "cdr-edit", "scope_kind": "wc", "scope_value": "Repair 1", "theme": "dark",
    }).json()["dashboard"]
    wd = widget_definitions_store.save(
        name="cdr-edit-wd", type="ribbons", visual={}, default_data={"group": "Repairs"},
    )
    c.post(f"/api/dashboards/{dash['id']}/placements", json={
        "widget_def_id": wd["id"], "x": 0, "y": 0, "w": 4, "h": 4, "data_overrides": {},
    })
    r = c.get(f"/dashboards/{dash['slug']}")
    assert r.status_code == 200
    assert "cdr-edit-wd" in r.text or "Monthly Ribbons" in r.text


def test_tv_view_renders_with_tv_header():
    from zira_dashboard import widget_definitions_store
    c = TestClient(app)
    dash = c.post("/api/dashboards", json={
        "name": "cdr-tv", "scope_kind": "wc", "scope_value": "Repair 1", "theme": "light",
    }).json()["dashboard"]
    wd = widget_definitions_store.save(
        name="cdr-tv-wd", type="ribbons", visual={}, default_data={"group": "Repairs"},
    )
    c.post(f"/api/dashboards/{dash['id']}/placements", json={
        "widget_def_id": wd["id"], "x": 0, "y": 0, "w": 4, "h": 4, "data_overrides": {},
    })
    r = c.get(f"/tv/dashboards/{dash['slug']}")
    assert r.status_code == 200
    assert 'data-tv-theme="light"' in r.text
    assert "Repair 1" in r.text


def test_tv_view_404_for_unknown_slug():
    c = TestClient(app)
    r = c.get("/tv/dashboards/cdr-not-real")
    assert r.status_code == 404
