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
    """Stub every data source the operator-dashboard render touches so the
    page renders without live Zira/Odoo, for slug 'repair-1'.

    Keep this in sync with `_render_wc_dashboard`'s ``wc_dashboard_data.*``
    calls: the render consumes ``fifteen_min_progress_buckets`` and
    ``kpi_tiles`` (the ``daily_progress``/``fifteen_min_increments`` helpers
    they replaced are no longer called by the render)."""
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
    monkeypatch.setattr(wc_dashboard_data, "kpi_tiles",
                        lambda nm, d: {"units_today": 87, "downtime_minutes": 12,
                                       "hours_elapsed": 4.0, "up_time_pct": 95.0,
                                       "pallets_per_hour": 21.7})
    monkeypatch.setattr(wc_dashboard_data, "goat_race",
                        lambda nm, d: {"group": "Repairs", "goat": None, "units_today": 87,
                                       "goat_pace_today": 0, "status": None})
    monkeypatch.setattr(wc_dashboard_data, "monthly_ribbons",
                        lambda nm, y, m: {"group": "Repairs", "entries": []})
    monkeypatch.setattr(wc_dashboard_data, "fifteen_min_progress_buckets",
                        lambda nm, d: {"buckets": [], "bucket_target": 0})
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
    # All operator-dashboard widgets present (shared /recycling-style layout;
    # the KPI row was consolidated to a single Pallets/hr tile).
    for wid in ("kpi-pph", "pallets-banner", "progress-15min", "cumulative-daily",
                "downtime-row", "goat-race", "monthly-ribbons"):
        assert f'gs-id="{wid}"' in r.text, f"missing widget {wid}"


def test_tv_route_renders_with_dark_theme_and_no_chrome(monkeypatch):
    _stub_wc(monkeypatch)
    c = TestClient(app)
    r = c.get("/tv/wc/repair-1")
    assert r.status_code == 200
    assert 'data-tv-theme="dark"' in r.text
    assert "/static/tv-mode.css" in r.text
    # Resilient auto-refresh replaces the hard meta-refresh: no more black
    # "upstream error" screen on a deploy/blip (see static/tv-refresh.js).
    assert 'http-equiv="refresh"' not in r.text
    assert "tv-refresh.js" in r.text
    # Same widgets present.
    assert 'gs-id="pallets-banner"' in r.text


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


def test_operator_route_uses_shared_layout_key(monkeypatch):
    """Both /wc/repair-1 and /wc/dismantler-2 read layout from page='operator'."""
    _stub_wc(monkeypatch)
    calls = []
    from zira_dashboard import layout_store
    monkeypatch.setattr(layout_store, "layout_map", lambda page: (calls.append(page) or {}))
    # Make wc_by_slug resolve both slugs to fake Locations.
    from zira_dashboard import wc_dashboard_data

    class _Loc:
        def __init__(self, n): self.name = n; self.meter_id = "m"; self.skill = "Repair"; self.bay = "Bay 1"

    monkeypatch.setattr(
        wc_dashboard_data, "wc_by_slug",
        lambda s: _Loc("Repair 1") if s == "repair-1"
              else _Loc("Dismantler 2") if s == "dismantler-2"
              else None,
    )
    c = TestClient(app)
    c.get("/wc/repair-1")
    c.get("/wc/dismantler-2")
    assert "operator" in calls, f"layout_map never called with 'operator'; got {calls}"
    assert all(p == "operator" for p in calls), f"unexpected layout keys: {calls}"


def test_operator_route_loads_widget_customizations(monkeypatch):
    """The render context loads widget customizations from page='operator'."""
    _stub_wc(monkeypatch)
    seen = {}
    from zira_dashboard import widget_customizer

    def _record(page):
        # Record the page key, but always return a dict — the template does
        # ``customs.get(...)`` on the result, so a truthy non-dict (e.g. the
        # page string) would raise UndefinedError.
        seen["page"] = page
        return {}

    monkeypatch.setattr(widget_customizer, "load_all", _record)
    c = TestClient(app)
    r = c.get("/wc/repair-1")
    assert r.status_code == 200
    assert seen.get("page") == "operator", f"expected widget_customizer.load_all('operator'); got {seen}"


def test_operator_route_renders_without_500_after_context_changes(monkeypatch):
    """Smoke: the route still 200s with the new context vars (customs,
    shift_start_label, now_label, banner_now_pct)."""
    _stub_wc(monkeypatch)
    c = TestClient(app)
    r = c.get("/wc/repair-1")
    assert r.status_code == 200


def test_operator_dashboard_has_single_pallets_per_hour_kpi(monkeypatch):
    """Only the Pallets/hr KPI remains a standalone widget — Units / Up Time /
    Downtime were folded into the Pallets banner + Downtime widget."""
    _stub_wc(monkeypatch)
    c = TestClient(app)
    r = c.get("/wc/repair-1")
    assert r.status_code == 200
    assert 'gs-id="kpi-pph"' in r.text
    # The old four-way KPI split is gone.
    for old in ("kpi-units", "kpi-uptime", "kpi-downtime"):
        assert f'gs-id="{old}"' not in r.text, f"stale KPI widget still present: {old}"


def test_operator_dashboard_renders_operator_strip(monkeypatch):
    """The unified chrome strip shows operator names from the Plant Scheduler."""
    _stub_wc(monkeypatch)  # _stub_wc sets operators to ["Christian", "Jose L"]
    c = TestClient(app)
    r = c.get("/wc/repair-1")
    assert r.status_code == 200
    assert "operator-strip" in r.text
    assert "Christian" in r.text and "Jose L" in r.text


def test_operator_dashboard_unassigned_strip(monkeypatch):
    """With no operators assigned, the strip shows '(unassigned)'."""
    _stub_wc(monkeypatch)
    from zira_dashboard import wc_dashboard_data
    monkeypatch.setattr(wc_dashboard_data, "assigned_operators_for_wc",
                        lambda nm, d: [])
    c = TestClient(app)
    r = c.get("/wc/repair-1")
    assert r.status_code == 200
    assert "(unassigned)" in r.text


def test_operator_dashboard_renames_remaining_widget_ids(monkeypatch):
    """Non-KPI widgets use the new shared IDs (no 'wc-' prefix)."""
    _stub_wc(monkeypatch)
    c = TestClient(app)
    r = c.get("/wc/repair-1")
    for wid in ("pallets-banner", "progress-15min", "cumulative-daily",
                "downtime-row", "goat-race", "monthly-ribbons"):
        assert f'gs-id="{wid}"' in r.text, f"missing widget {wid}"
    for old in ("wc-kpi-row", "wc-pallets-banner", "wc-15min-progress",
                "wc-cumulative", "wc-downtime", "wc-goat-race",
                "wc-monthly-ribbons"):
        assert f'gs-id="{old}"' not in r.text, f"stale widget id still present: {old}"


def test_operator_dashboard_body_has_wc_dashboard_class(monkeypatch):
    _stub_wc(monkeypatch)
    c = TestClient(app)
    r = c.get("/wc/repair-1")
    assert 'class="wc-dashboard"' in r.text


def test_operator_dashboard_has_edit_chrome_in_strip(monkeypatch):
    """The unified strip includes the save-indicator + Reset Layout button."""
    _stub_wc(monkeypatch)
    c = TestClient(app)
    r = c.get("/wc/repair-1")
    assert r.status_code == 200
    assert 'operator-strip-right' in r.text
    assert 'id="save-indicator"' in r.text
    assert 'id="reset-layout"' in r.text
    assert "Drag / resize" in r.text


def test_tv_wc_dashboard_omits_edit_bar(monkeypatch):
    """TV view skips the chrome strip (read-only)."""
    _stub_wc(monkeypatch)
    c = TestClient(app)
    r = c.get("/tv/wc/repair-1")
    assert r.status_code == 200
    assert 'id="save-indicator"' not in r.text
    assert 'id="reset-layout"' not in r.text


def test_operator_dashboard_persists_to_operator_layout_endpoint(monkeypatch):
    """Persistence targets the shared "operator" page key (not the old
    per-WC /api/layout/wc:... keys). The endpoint URLs are built by
    dashboard-grid.js from the grid's data-layout-page attribute."""
    _stub_wc(monkeypatch)
    c = TestClient(app)
    r = c.get("/wc/repair-1")
    assert 'data-layout-page="operator"' in r.text
    assert "/static/dashboard-grid.js" in r.text
    assert "/api/layout/wc:" not in r.text
    assert 'data-layout-page="wc:' not in r.text


def test_operator_dashboard_has_widget_edit_buttons(monkeypatch):
    """Every editable widget has a ⋮ button + a hidden edit panel."""
    _stub_wc(monkeypatch)
    c = TestClient(app)
    r = c.get("/wc/repair-1")
    assert r.status_code == 200
    for wid in ("kpi-pph", "pallets-banner", "progress-15min", "cumulative-daily",
                "downtime-row", "goat-race", "monthly-ribbons"):
        assert f'data-widget="{wid}"' in r.text, f"missing edit btn for {wid}"


def test_tv_wc_dashboard_omits_edit_buttons(monkeypatch):
    """TV view skips edit chrome (no ⋮ buttons, no edit panels)."""
    _stub_wc(monkeypatch)
    c = TestClient(app)
    r = c.get("/tv/wc/repair-1")
    assert r.status_code == 200
    assert 'widget-edit-btn' not in r.text
    assert 'class="widget-edit"' not in r.text


def test_operator_dashboard_applies_custom_titles(monkeypatch):
    """A title saved via widget_customizer flows into the rendered HTML."""
    _stub_wc(monkeypatch)
    from zira_dashboard import widget_customizer
    monkeypatch.setattr(
        widget_customizer, "load_all",
        lambda page: {"kpi-pph": {"title": "Pallets Done"}} if page == "operator" else {},
    )
    c = TestClient(app)
    r = c.get("/wc/repair-1")
    assert "Pallets Done" in r.text


def test_operator_dashboard_posts_widget_edits_to_operator_endpoint(monkeypatch):
    """Widget edits target /api/widget/operator/{id} — built by
    dashboard-grid.js from the same data-layout-page="operator" attribute
    (one segment drives both the layout and widget endpoints)."""
    _stub_wc(monkeypatch)
    c = TestClient(app)
    r = c.get("/wc/repair-1")
    assert 'data-layout-page="operator"' in r.text
    assert "/static/dashboard-grid.js" in r.text


def test_pallets_banner_renders_start_and_now_ticks(monkeypatch):
    """Banner shows `start · HH:MM` and `now · HH:MM` axis labels."""
    _stub_wc(monkeypatch)
    c = TestClient(app)
    r = c.get("/wc/repair-1")
    assert r.status_code == 200
    assert "start ·" in r.text
    # `now ·` only renders on today's date; the test runs today by default.
    assert "now ·" in r.text


def test_tv_route_has_no_desktop_chrome(monkeypatch):
    """Chrome-consolidation guard: the TV operator view must render no
    topnav, no footer, and exactly one document shell."""
    _stub_wc(monkeypatch)
    c = TestClient(app)
    r = c.get("/tv/wc/repair-1")
    assert r.status_code == 200
    assert 'class="brand-row"' not in r.text, "TV page must not render the topnav"
    assert "changelog-modal" not in r.text, "TV page must not render the footer"
    assert r.text.lower().count("<!doctype") == 1
