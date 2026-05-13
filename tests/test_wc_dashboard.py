"""Integration tests for the per-WC dashboard routes.

Mirrors the test_dashboards_polish.py pattern: TestClient + monkeypatch
of the data-source helpers so the test doesn't need live Zira / Odoo.
"""
from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

from zira_dashboard.app import app

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set; wc-dashboard tests need Postgres",
)


def _stub_wc(monkeypatch):
    """Make `wc_by_slug` return a fake Location for slug 'repair-1'."""
    from zira_dashboard import wc_dashboard_data, work_centers_store

    class _Loc:
        name = "Repair 1"
        meter_id = "meter-1"
        skill = "Repair"
        bay = "Bay 1"

    fake = _Loc()
    monkeypatch.setattr(wc_dashboard_data, "wc_by_slug", lambda s: fake if s == "repair-1" else None)
    monkeypatch.setattr(work_centers_store, "groups", lambda loc: ["Repairs"])
    monkeypatch.setattr(work_centers_store, "goal_per_day", lambda loc: 200)
    monkeypatch.setattr(wc_dashboard_data, "assigned_operators_for_wc",
                        lambda nm, d: ["Christian", "Jose L"])
    monkeypatch.setattr(wc_dashboard_data, "pallets_banner",
                        lambda nm, d: {"units_today": 87, "target_today": 100,
                                       "target_full_day": 200, "pct_of_target": 87.0})
    monkeypatch.setattr(wc_dashboard_data, "daily_progress", lambda nm, d: [])
    monkeypatch.setattr(wc_dashboard_data, "goat_race",
                        lambda nm, d: {"group": "Repairs", "goat": None, "units_today": 87,
                                       "goat_pace_today": 0, "status": None})
    monkeypatch.setattr(wc_dashboard_data, "monthly_ribbons",
                        lambda nm, y, m: {"group": "Repairs", "entries": []})
    monkeypatch.setattr(wc_dashboard_data, "fifteen_min_increments", lambda nm, d: [])
    monkeypatch.setattr(wc_dashboard_data, "downtime_report",
                        lambda nm, d: {"events": [], "total_minutes": 0})


def test_editor_route_renders_with_drag(monkeypatch):
    _stub_wc(monkeypatch)
    c = TestClient(app)
    r = c.get("/wc/repair-1")
    assert r.status_code == 200
    # Editor: not in tv_mode, no data-tv-theme, no tv-mode.css link.
    assert "data-tv-theme" not in r.text
    assert "/static/tv-mode.css" not in r.text
    # Header renders the WC name + operator list.
    assert "Repair 1" in r.text
    assert "Christian · Jose L" in r.text
    # All 6 widget IDs present.
    for wid in ("wc-pallets-banner", "wc-daily-progress", "wc-goat-race",
                "wc-monthly-ribbons", "wc-15min-increments", "wc-downtime-report"):
        assert wid in r.text


def test_tv_route_renders_with_dark_theme_and_no_chrome(monkeypatch):
    _stub_wc(monkeypatch)
    c = TestClient(app)
    r = c.get("/tv/wc/repair-1")
    assert r.status_code == 200
    assert 'data-tv-theme="dark"' in r.text
    assert "/static/tv-mode.css" in r.text
    assert 'http-equiv="refresh"' in r.text
    # Same widgets present.
    assert "wc-pallets-banner" in r.text


def test_tv_route_supports_light_theme_via_query(monkeypatch):
    _stub_wc(monkeypatch)
    c = TestClient(app)
    r = c.get("/tv/wc/repair-1?theme=light")
    assert r.status_code == 200
    assert 'data-tv-theme="light"' in r.text


def test_unknown_slug_returns_404(monkeypatch):
    from zira_dashboard import wc_dashboard_data
    monkeypatch.setattr(wc_dashboard_data, "wc_by_slug", lambda s: None)
    c = TestClient(app)
    r = c.get("/wc/ghost")
    assert r.status_code == 404
    r2 = c.get("/tv/wc/ghost")
    assert r2.status_code == 404


def test_unassigned_wc_renders_with_placeholder(monkeypatch):
    _stub_wc(monkeypatch)
    from zira_dashboard import wc_dashboard_data
    monkeypatch.setattr(wc_dashboard_data, "assigned_operators_for_wc", lambda nm, d: [])
    c = TestClient(app)
    r = c.get("/tv/wc/repair-1")
    assert r.status_code == 200
    assert "(unassigned)" in r.text
