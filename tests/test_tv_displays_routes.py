"""Integration tests for the tv-displays routes."""
from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

from zira_dashboard.app import app

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="tv-displays route tests need Postgres",
)


@pytest.fixture(autouse=True)
def _clean_displays():
    from zira_dashboard import db
    db.init_pool()
    db.bootstrap_schema()
    db.execute("DELETE FROM tv_displays WHERE slug LIKE 'rt-%'")
    yield
    db.execute("DELETE FROM tv_displays WHERE slug LIKE 'rt-%'")


def test_post_add_display_returns_url():
    c = TestClient(app)
    r = c.post("/api/tv-displays", json={
        "name": "rt-recycling-tv",
        "kind": "vs_recycling",
        "theme": "dark",
    })
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["slug"] == "rt-recycling-tv"
    assert body["url"] == "/tv/rt-recycling-tv"
    assert isinstance(body["id"], int)


def test_post_add_rejects_missing_name():
    c = TestClient(app)
    r = c.post("/api/tv-displays", json={"kind": "vs_recycling", "theme": "dark"})
    assert r.status_code == 400


def test_post_add_rejects_bad_kind():
    c = TestClient(app)
    r = c.post("/api/tv-displays", json={"name": "rt-bad", "kind": "garbage", "theme": "dark"})
    assert r.status_code == 400


def test_post_add_wc_requires_wc_name():
    c = TestClient(app)
    r = c.post("/api/tv-displays", json={"name": "rt-wcnone", "kind": "wc", "theme": "dark"})
    assert r.status_code == 400


def test_post_theme_toggle():
    c = TestClient(app)
    add = c.post("/api/tv-displays", json={
        "name": "rt-theme-toggle",
        "kind": "vs_recycling",
        "theme": "dark",
    }).json()
    r = c.post(f"/api/tv-displays/{add['id']}/theme", json={"theme": "light"})
    assert r.status_code == 200
    assert r.json()["ok"] is True


def test_delete_display():
    c = TestClient(app)
    add = c.post("/api/tv-displays", json={
        "name": "rt-deleteme",
        "kind": "vs_recycling",
        "theme": "dark",
    }).json()
    r = c.delete(f"/api/tv-displays/{add['id']}")
    assert r.status_code == 200
    assert r.json()["ok"] is True


def test_get_tv_unknown_slug_returns_404():
    c = TestClient(app)
    r = c.get("/tv/rt-not-a-real-display")
    assert r.status_code == 404
    assert "settings" in r.text.lower() or "tvs" in r.text.lower()


def test_get_tv_vs_recycling_dispatches():
    c = TestClient(app)
    c.post("/api/tv-displays", json={
        "name": "rt-recyc-light",
        "kind": "vs_recycling",
        "theme": "light",
    })
    r = c.get("/tv/rt-recyc-light")
    assert r.status_code == 200
    assert 'data-tv-theme="light"' in r.text


def test_get_tv_recycling_leaderboard_dispatches(monkeypatch):
    from zira_dashboard.routes import recycling_leaderboard

    def _fake_render(request, *, tv_theme="dark"):
        from fastapi.responses import HTMLResponse
        return HTMLResponse(
            f'<html data-tv-theme="{tv_theme}">Recycling-leaderboard</html>'
        )

    monkeypatch.setattr(
        recycling_leaderboard,
        "render_recycling_leaderboard_tv",
        _fake_render,
    )
    c = TestClient(app)
    c.post("/api/tv-displays", json={
        "name": "rt-recycling-leaderboard",
        "kind": "vs_recycling_leaderboard",
        "theme": "light",
    })
    r = c.get("/tv/rt-recycling-leaderboard")
    assert r.status_code == 200
    assert 'data-tv-theme="light"' in r.text
    assert "Recycling-leaderboard" in r.text


def test_get_tv_with_query_theme_overrides_stored():
    c = TestClient(app)
    c.post("/api/tv-displays", json={
        "name": "rt-recyc-dark",
        "kind": "vs_recycling",
        "theme": "dark",
    })
    r = c.get("/tv/rt-recyc-dark?theme=light")
    assert r.status_code == 200
    assert 'data-tv-theme="light"' in r.text


def test_get_tv_wc_archived_returns_404(monkeypatch):
    from zira_dashboard import staffing

    class _Loc:
        def __init__(self, name): self.name = name

    c = TestClient(app)
    c.post("/api/tv-displays", json={
        "name": "rt-ghost-wc",
        "kind": "wc",
        "wc_name": "Repair 1",
        "theme": "dark",
    })
    monkeypatch.setattr(staffing, "LOCATIONS", [_Loc("Junior 2")])
    r = c.get("/tv/rt-ghost-wc")
    assert r.status_code == 404
    assert "work center" in r.text.lower() or "removed" in r.text.lower()


def test_get_tv_wc_valid_renders_dashboard(monkeypatch):
    """A configured work-center TV must render (HTTP 200), not 500.

    Regression for the plant-wide outage: the /tv/{slug} dispatcher called
    _render_wc_dashboard() without the required `day` keyword-only argument,
    so every registry-dispatched operator TV (kind='wc') 500'd. The
    /tv/wc/{slug} sibling route passed `day`, which is why that path stayed
    green in tests while the actual plant TVs went dark. This drives the real
    dispatcher end-to-end so the dispatch->render wiring is covered.
    """
    from zira_dashboard import staffing, wc_dashboard_data, work_centers_store

    class _Loc:
        name = "Repair 1"
        meter_id = "meter-1"
        skill = "Repair"
        bay = "Bay 1"

    loc = _Loc()
    # Dispatcher validates row.wc_name against staffing.LOCATIONS.
    monkeypatch.setattr(staffing, "LOCATIONS", [loc])
    # Stub the render's data sources (mirrors test_wc_dashboard._stub_wc) so
    # the page renders without live Zira/Odoo.
    monkeypatch.setattr(wc_dashboard_data, "wc_by_slug",
                        lambda s: loc if s == "repair-1" else None)
    monkeypatch.setattr(work_centers_store, "groups", lambda l: ["Repairs"])
    monkeypatch.setattr(work_centers_store, "goal_per_day", lambda l: 200)
    monkeypatch.setattr(wc_dashboard_data, "assigned_operators_for_wc",
                        lambda nm, d: ["Christian", "Jose L"])
    monkeypatch.setattr(wc_dashboard_data, "pallets_banner",
                        lambda nm, d: {"units_today": 87, "target_today": 100,
                                       "target_full_day": 200, "pct_of_target": 87.0})
    monkeypatch.setattr(wc_dashboard_data, "goat_race",
                        lambda nm, d: {"group": "Repairs", "goat": None,
                                       "units_today": 87, "goat_pace_today": 0,
                                       "status": None})
    monkeypatch.setattr(wc_dashboard_data, "monthly_ribbons",
                        lambda nm, y, m: {"group": "Repairs", "entries": []})
    monkeypatch.setattr(wc_dashboard_data, "downtime_report",
                        lambda nm, d: {"events": [], "total_minutes": 0})

    c = TestClient(app)
    add = c.post("/api/tv-displays", json={
        "name": "rt-repair1-live",
        "kind": "wc",
        "wc_name": "Repair 1",
        "theme": "dark",
    }).json()
    assert add["ok"] is True
    r = c.get(f"/tv/{add['slug']}")
    assert r.status_code == 200
    assert 'data-tv-theme="dark"' in r.text
    assert "Repair 1" in r.text


def test_legacy_tv_d_redirects_to_new_path():
    """Old /tv/d/{slug} URLs should 302 to /tv/{slug} so already-deployed
    TVs keep working without manual reconfiguration."""
    c = TestClient(app)
    r = c.get("/tv/d/anything", follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"] == "/tv/anything"


def test_legacy_tv_d_redirect_preserves_query_string():
    c = TestClient(app)
    r = c.get("/tv/d/anything?theme=light", follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"] == "/tv/anything?theme=light"


def test_post_add_custom_kind_rejected():
    """The 'custom' kind was removed when the workshop was torn out
    (2026-05-14). POSTing with kind=custom must 400."""
    c = TestClient(app)
    r = c.post("/api/tv-displays", json={
        "name": "rt-cust-gone", "kind": "custom", "theme": "dark",
    })
    assert r.status_code == 400
