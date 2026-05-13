"""Unit tests for dashboard_catalog — uses monkeypatch, no Postgres."""
from __future__ import annotations


def test_all_dashboards_lists_vs_then_wcs_then_custom(monkeypatch):
    from zira_dashboard import dashboard_catalog, pinned_dashboards_store, staffing, custom_dashboards_store

    class _Loc:
        def __init__(self, name): self.name = name

    monkeypatch.setattr(staffing, "LOCATIONS", [_Loc("Repair 1"), _Loc("Junior 2")])
    monkeypatch.setattr(custom_dashboards_store, "list_dashboards", lambda: [
        {"id": 7, "name": "Floor Hub", "slug": "floor-hub",
         "scope_kind": "wc", "scope_value": "Repair 1", "theme": "dark",
         "sort_order": 0, "widget_count": 3},
    ])
    monkeypatch.setattr(pinned_dashboards_store, "list_pins", lambda: [
        {"kind": "vs_recycling", "ref": "", "sort_order": 0},
        {"kind": "custom", "ref": "floor-hub", "sort_order": 1},
    ])

    out = dashboard_catalog.all_dashboards()
    kinds = [d["kind"] for d in out]
    assert kinds == ["vs_recycling", "vs_new", "vs_work_centers", "wc", "wc", "custom"]
    by_key = {(d["kind"], d["ref"]): d for d in out}
    assert by_key[("vs_recycling", "")]["pinned"] is True
    assert by_key[("vs_new", "")]["pinned"] is False
    assert by_key[("vs_work_centers", "")]["pinned"] is False
    assert by_key[("wc", "Repair 1")]["pinned"] is False
    assert by_key[("custom", "floor-hub")]["pinned"] is True


def test_all_dashboards_urls_are_correct(monkeypatch):
    from zira_dashboard import dashboard_catalog, pinned_dashboards_store, staffing, custom_dashboards_store

    class _Loc:
        def __init__(self, name): self.name = name

    monkeypatch.setattr(staffing, "LOCATIONS", [_Loc("Repair 1")])
    monkeypatch.setattr(custom_dashboards_store, "list_dashboards", lambda: [
        {"id": 3, "name": "X", "slug": "x", "scope_kind": "wc",
         "scope_value": "Repair 1", "theme": "dark", "sort_order": 0, "widget_count": 0},
    ])
    monkeypatch.setattr(pinned_dashboards_store, "list_pins", lambda: [])

    out = dashboard_catalog.all_dashboards()
    urls = {(d["kind"], d["ref"]): (d["open_url"], d["tv_url"]) for d in out}
    assert urls[("vs_recycling", "")] == ("/recycling", "/tv/recycling")
    assert urls[("vs_new", "")] == ("/new-vs", "/tv/new-vs")
    assert urls[("vs_work_centers", "")] == ("/work-centers", None)
    assert urls[("wc", "Repair 1")] == ("/wc/repair-1", "/tv/wc/repair-1")
    assert urls[("custom", "x")] == ("/dashboards/x", "/tv/dashboards/x")


def test_pinned_dashboards_for_subnav_filters_unpinned(monkeypatch):
    from zira_dashboard import dashboard_catalog, pinned_dashboards_store, staffing, custom_dashboards_store

    class _Loc:
        def __init__(self, name): self.name = name

    monkeypatch.setattr(staffing, "LOCATIONS", [_Loc("Repair 1")])
    monkeypatch.setattr(custom_dashboards_store, "list_dashboards", lambda: [])
    monkeypatch.setattr(pinned_dashboards_store, "list_pins", lambda: [
        {"kind": "vs_recycling", "ref": "", "sort_order": 0},
        {"kind": "wc", "ref": "Repair 1", "sort_order": 1},
    ])

    out = dashboard_catalog.pinned_dashboards_for_subnav()
    keys = [d["key"] for d in out]
    assert keys == ["vs_recycling:", "wc:Repair 1"]
    names = [d["name"] for d in out]
    assert names == ["Recycling VS", "Repair 1"]


def test_pinned_subnav_filters_orphaned_pins(monkeypatch):
    """A pin pointing at a deleted custom dashboard or removed WC is skipped."""
    from zira_dashboard import dashboard_catalog, pinned_dashboards_store, staffing, custom_dashboards_store

    monkeypatch.setattr(staffing, "LOCATIONS", [])
    monkeypatch.setattr(custom_dashboards_store, "list_dashboards", lambda: [])
    monkeypatch.setattr(pinned_dashboards_store, "list_pins", lambda: [
        {"kind": "vs_recycling", "ref": "", "sort_order": 0},
        {"kind": "wc", "ref": "Repair 1", "sort_order": 1},
        {"kind": "custom", "ref": "deleted-slug", "sort_order": 2},
    ])

    out = dashboard_catalog.pinned_dashboards_for_subnav()
    keys = [d["key"] for d in out]
    assert keys == ["vs_recycling:"]
