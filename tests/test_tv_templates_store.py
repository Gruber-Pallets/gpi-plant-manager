"""Postgres-gated tests for tv_templates_store.

Each test resets the templates table for a 'test-' prefix so they
don't collide with real templates Dale has saved.
"""
from __future__ import annotations

import os

import pytest

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="tv_templates_store tests need Postgres",
)


@pytest.fixture(autouse=True)
def _clean_templates():
    """Drop every 'test-' prefixed template + layout before/after each test."""
    from zira_dashboard import db
    db.init_pool()
    db.bootstrap_schema()
    db.execute("DELETE FROM tv_dashboard_templates WHERE name LIKE 'test-%'")
    db.execute("DELETE FROM widget_layouts WHERE page LIKE 'test-wc:%'")
    yield
    db.execute("DELETE FROM tv_dashboard_templates WHERE name LIKE 'test-%'")
    db.execute("DELETE FROM widget_layouts WHERE page LIKE 'test-wc:%'")


def test_save_creates_template():
    from zira_dashboard import tv_templates_store
    tv_templates_store.save("test-repairs", [{"id": "wc-pallets-banner", "x": 0, "y": 0, "w": 12, "h": 2}], theme="dark")
    rows = tv_templates_store.list_templates()
    names = [r["name"] for r in rows]
    assert "test-repairs" in names


def test_save_upserts_by_name():
    """Saving twice with the same name updates the layout, not duplicates."""
    from zira_dashboard import tv_templates_store
    tv_templates_store.save("test-A", [{"id": "wc-pallets-banner", "x": 0, "y": 0, "w": 12, "h": 2}], theme="dark")
    tv_templates_store.save("test-A", [{"id": "wc-pallets-banner", "x": 1, "y": 1, "w": 6, "h": 4}], theme="light")
    rows = tv_templates_store.list_templates()
    matching = [r for r in rows if r["name"] == "test-A"]
    assert len(matching) == 1
    loaded = tv_templates_store.load(matching[0]["id"])
    assert loaded["layout_json"][0]["x"] == 1
    assert loaded["theme"] == "light"


def test_delete_removes_template():
    from zira_dashboard import tv_templates_store
    tv_templates_store.save("test-delete-me", [{"id": "wc-pallets-banner", "x": 0, "y": 0, "w": 12, "h": 2}])
    rows = tv_templates_store.list_templates()
    target = next(r for r in rows if r["name"] == "test-delete-me")
    tv_templates_store.delete(target["id"])
    rows_after = tv_templates_store.list_templates()
    assert all(r["name"] != "test-delete-me" for r in rows_after)


def test_apply_to_explicit_targets_writes_layout():
    """apply_to_targets with explicit page list writes each one."""
    from zira_dashboard import tv_templates_store, layout_store
    layout = [{"id": "wc-pallets-banner", "x": 0, "y": 0, "w": 12, "h": 2}]
    tv_templates_store.save("test-explicit", layout, theme="dark")
    rows = tv_templates_store.list_templates()
    tid = next(r for r in rows if r["name"] == "test-explicit")["id"]

    result = tv_templates_store.apply_to_targets(tid, ["test-wc:a", "test-wc:b"])
    assert sorted(result["applied_pages"]) == ["test-wc:a", "test-wc:b"]
    assert result["applied_count"] == 2
    assert layout_store.load("test-wc:a")[0]["id"] == "wc-pallets-banner"
    assert layout_store.load("test-wc:b")[0]["id"] == "wc-pallets-banner"


def test_apply_returns_zero_when_template_missing():
    from zira_dashboard import tv_templates_store
    result = tv_templates_store.apply_to_targets(999_999_999, ["test-wc:a"])
    assert result == {"applied_count": 0, "applied_pages": [], "error": "template not found"}


def test_resolve_targets_explicit_list_passes_through():
    """resolve_targets normalizes the various input shapes to a flat list of page keys."""
    from zira_dashboard import tv_templates_store
    out = tv_templates_store.resolve_targets(["wc:repair-1", "wc:repair-2"])
    assert sorted(out) == ["wc:repair-1", "wc:repair-2"]


def test_resolve_targets_group_expands(monkeypatch):
    """resolve_targets('group:Repairs') expands to every WC slug in that group."""
    from zira_dashboard import tv_templates_store, work_centers_store

    class _Loc:
        def __init__(self, name): self.name = name

    monkeypatch.setattr(
        work_centers_store, "members",
        lambda kind, name: [_Loc("Repair 1"), _Loc("Repair 2")] if (kind, name) == ("group", "Repairs") else [],
    )
    out = tv_templates_store.resolve_targets("group:Repairs")
    assert sorted(out) == ["wc:repair-1", "wc:repair-2"]


def test_resolve_targets_all_expands_to_every_wc(monkeypatch):
    """resolve_targets('all') expands to every Location.name in staffing.LOCATIONS."""
    from zira_dashboard import tv_templates_store, staffing

    class _Loc:
        def __init__(self, name): self.name = name

    monkeypatch.setattr(staffing, "LOCATIONS", [_Loc("Repair 1"), _Loc("Hand Build #1")])
    out = tv_templates_store.resolve_targets("all")
    assert sorted(out) == ["wc:hand-build-1", "wc:repair-1"]
