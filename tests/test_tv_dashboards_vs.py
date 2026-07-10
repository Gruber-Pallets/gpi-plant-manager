"""Integration tests for the TV variants of the value-stream dashboards.

Mirrors the test_dashboards_polish.py pattern: TestClient + monkeypatch
of the data-source helpers so the test doesn't need live Zira / Odoo.
"""
from __future__ import annotations

import os
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from zira_dashboard import staffing
from zira_dashboard.app import app

# The render path transitively hits the work_centers / schedule store DB
# lookups despite the monkeypatches, so we gate on DATABASE_URL the same
# way test_dashboards_polish.py does.
pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set; TV dashboard tests need Postgres",
)


def _stub_data(monkeypatch):
    """Stub the heavy external calls so the route renders quickly."""
    monkeypatch.setattr(staffing, "load_schedule", lambda d: staffing.Schedule(
        day=d, published=True, assignments={"Repair-1": ["Alice"]},
    ))


def _empty_new_day():
    return {
        "total_units": 0,
        "total_downtime": 0,
        "elapsed": 0,
        "available": 0,
        "uptime_minutes": 0,
        "total_man_hours": 0.0,
        "total_recycling_people": 0,
        "per_wc_units": {},
        "per_wc_downtime": {},
        "per_wc_expected": {},
        "per_wc_who": {},
        "per_wc_state": {},
        "per_wc_category": {},
        "per_wc_station_obj": {},
        "active_wc_names": set(),
        "schedule_assignments": {},
        "group_buckets": {"New": []},
        "shift_start_label": "07:00",
    }


def test_tv_recycling_renders_with_default_dark_theme(monkeypatch):
    _stub_data(monkeypatch)
    with patch("zira_dashboard.routes.departments.leaderboard", return_value=[]), \
         patch("zira_dashboard.routes.departments.shift_elapsed_minutes", return_value=60):
        c = TestClient(app)
        r = c.get("/tv/recycling")
    assert r.status_code == 200
    assert 'data-tv-theme="dark"' in r.text
    # Chrome hidden via CSS — but the TV stylesheet must be linked.
    assert "/static/tv-mode.css" in r.text
    # TV header rendered with the dashboard title.
    assert 'class="tv-header"' in r.text
    assert "Recycling VS" in r.text
    # Resilient auto-refresh: the hard meta-refresh (which paints the edge's
    # "upstream error" page on any blip) is gone, replaced by the guarded
    # tv-refresh.js that reloads only when the backend answers OK.
    assert 'http-equiv="refresh"' not in r.text
    assert "tv-refresh.js" in r.text


def test_tv_recycling_supports_light_theme_via_query(monkeypatch):
    _stub_data(monkeypatch)
    with patch("zira_dashboard.routes.departments.leaderboard", return_value=[]), \
         patch("zira_dashboard.routes.departments.shift_elapsed_minutes", return_value=60):
        c = TestClient(app)
        r = c.get("/tv/recycling?theme=light")
    assert r.status_code == 200
    assert 'data-tv-theme="light"' in r.text


def test_tv_new_vs_renders_with_default_dark_theme(monkeypatch):
    _stub_data(monkeypatch)
    with patch("zira_dashboard.routes.departments.leaderboard", return_value=[]), \
         patch("zira_dashboard.routes.departments.shift_elapsed_minutes", return_value=60):
        c = TestClient(app)
        # /tv/new-vs is a legacy URL that 301s to /tv/new; TestClient
        # follows redirects by default, so this still tests the final page.
        r = c.get("/tv/new-vs")
    assert r.status_code == 200
    assert 'data-tv-theme="dark"' in r.text
    assert "/static/tv-mode.css" in r.text
    assert ">New<" in r.text or "TV · Departments — New" in r.text


def test_tv_new_uses_static_new_grid(monkeypatch):
    _stub_data(monkeypatch)
    with patch("zira_dashboard.routes.departments._new_day_data", return_value=_empty_new_day()):
        response = TestClient(app).get("/tv/new")
    assert response.status_code == 200
    assert 'data-layout-page="new"' in response.text
    assert 'data-tv-mode="1"' in response.text
    assert 'class="rc-toolbar"' not in response.text
    assert 'id="reset-layout"' not in response.text
    assert "No readings received from configured New Zira meters for this range." in response.text
    assert 'class="bar-row' not in response.text
    assert "tv-refresh.js" in response.text


def test_screen_recycling_unaffected_by_tv_changes(monkeypatch):
    """Regression guard: the screen /recycling route must NOT carry the
    TV attributes after the plumbing changes."""
    _stub_data(monkeypatch)
    with patch("zira_dashboard.routes.departments.leaderboard", return_value=[]), \
         patch("zira_dashboard.routes.departments.shift_elapsed_minutes", return_value=60):
        c = TestClient(app)
        r = c.get("/recycling")
    assert r.status_code == 200
    assert "data-tv-theme" not in r.text, "screen page must not set data-tv-theme"
    assert "/static/tv-mode.css" not in r.text, "screen page must not link tv-mode.css"
    assert 'class="tv-header"' not in r.text, "screen page must not render TV header"
