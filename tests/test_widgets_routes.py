"""Integration tests for the Widget Workshop routes."""
from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

from zira_dashboard.app import app

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="widget routes need Postgres",
)


@pytest.fixture(autouse=True)
def _clean():
    from zira_dashboard import db
    db.init_pool()
    db.bootstrap_schema()
    db.execute("DELETE FROM widget_definitions WHERE name LIKE 'wr-%'")
    yield
    db.execute("DELETE FROM widget_definitions WHERE name LIKE 'wr-%'")


def test_get_widgets_types_returns_registry():
    c = TestClient(app)
    r = c.get("/api/widgets/types")
    assert r.status_code == 200
    body = r.json()
    type_ids = {t["type"] for t in body["types"]}
    assert {"pallets_by_wc", "goat_race", "ribbons"}.issubset(type_ids)


def test_get_widgets_options_groups():
    c = TestClient(app)
    r = c.get("/api/widgets/options/groups")
    assert r.status_code == 200
    body = r.json()
    assert isinstance(body["options"], list)


def test_get_widgets_options_unknown_kind_returns_400():
    c = TestClient(app)
    r = c.get("/api/widgets/options/garbage")
    assert r.status_code == 400


def test_post_widget_def_creates():
    c = TestClient(app)
    r = c.post("/api/widget-defs", json={
        "name": "wr-create",
        "type": "ribbons",
        "visual": {},
        "default_data": {"group": "Repairs"},
    })
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["definition"]["name"] == "wr-create"


def test_post_widget_def_rejects_unknown_type():
    c = TestClient(app)
    r = c.post("/api/widget-defs", json={
        "name": "wr-bad-type", "type": "nope", "visual": {}, "default_data": {},
    })
    assert r.status_code == 400


def test_get_widget_defs_lists_them():
    c = TestClient(app)
    c.post("/api/widget-defs", json={
        "name": "wr-list", "type": "ribbons", "visual": {}, "default_data": {"group": "Repairs"},
    })
    r = c.get("/api/widget-defs")
    assert r.status_code == 200
    names = [d["name"] for d in r.json()["definitions"]]
    assert "wr-list" in names


def test_delete_widget_def_removes():
    c = TestClient(app)
    add = c.post("/api/widget-defs", json={
        "name": "wr-del", "type": "ribbons", "visual": {}, "default_data": {"group": "Repairs"},
    }).json()
    r = c.delete(f"/api/widget-defs/{add['definition']['id']}")
    assert r.status_code == 200
    assert r.json()["ok"] is True


def test_delete_widget_def_409_when_in_use():
    """Deletion blocked while any placement references the def."""
    from zira_dashboard import custom_dashboards_store, db
    c = TestClient(app)
    add = c.post("/api/widget-defs", json={
        "name": "wr-inuse", "type": "ribbons", "visual": {}, "default_data": {"group": "Repairs"},
    }).json()
    dash = custom_dashboards_store.save_dashboard(
        name="wr-host", scope_kind="group", scope_value="Repairs", theme="dark",
    )
    custom_dashboards_store.add_placement(
        dashboard_id=dash["id"], widget_def_id=add["definition"]["id"],
        x=0, y=0, w=4, h=4, data_overrides={},
    )
    r = c.delete(f"/api/widget-defs/{add['definition']['id']}")
    assert r.status_code == 409
    assert "in use" in r.text.lower() or "referenced" in r.text.lower()
    db.execute("DELETE FROM custom_dashboards WHERE slug LIKE 'wr-%'")


def test_get_widgets_page_renders():
    c = TestClient(app)
    r = c.get("/widgets")
    assert r.status_code == 200
    assert "Workshop" in r.text or "Widgets" in r.text


def test_post_duplicate_creates_copy():
    c = TestClient(app)
    orig = c.post("/api/widget-defs", json={
        "name": "wr-dup-source", "type": "ribbons",
        "visual": {}, "default_data": {"group": "Repairs"},
    }).json()
    r = c.post(f"/api/widget-defs/{orig['definition']['id']}/duplicate")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["definition"]["name"] == "wr-dup-source (copy)"
    assert body["definition"]["id"] != orig["definition"]["id"]


def test_post_duplicate_unknown_id_returns_404():
    c = TestClient(app)
    r = c.post("/api/widget-defs/999999999/duplicate")
    assert r.status_code == 404
