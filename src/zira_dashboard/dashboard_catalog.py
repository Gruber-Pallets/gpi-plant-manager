"""Enumerates every renderable dashboard.

Single source of truth for the unified /dashboards index, the dashboards
sub-nav partial, and the TVs settings flat picker.

Order is stable: vs_recycling, vs_new, vs_work_centers, then WCs in
staffing.LOCATIONS order, then custom dashboards in
custom_dashboards_store.list_dashboards() order.
"""
from __future__ import annotations

from .wc_dashboard_data import slug_for_wc


def all_dashboards() -> list[dict]:
    """Returns every renderable dashboard:
      [{kind, ref, name, open_url, tv_url, pinned, ...}, ...]

    Custom-kind entries also carry `id` (so the TVs picker can store
    custom_dashboard_id).
    """
    from . import pinned_dashboards_store, staffing, custom_dashboards_store

    pinned_set = {(p["kind"], p["ref"]) for p in pinned_dashboards_store.list_pins()}
    out: list[dict] = []

    out.append({
        "kind": "vs_recycling", "ref": "",
        "name": "Recycling VS",
        "open_url": "/recycling", "tv_url": "/tv/recycling",
        "pinned": ("vs_recycling", "") in pinned_set,
    })
    out.append({
        "kind": "vs_new", "ref": "",
        "name": "New VS",
        "open_url": "/new-vs", "tv_url": "/tv/new-vs",
        "pinned": ("vs_new", "") in pinned_set,
    })
    # Work Centers — status board, no TV variant.
    out.append({
        "kind": "vs_work_centers", "ref": "",
        "name": "Work Centers",
        "open_url": "/work-centers", "tv_url": None,
        "pinned": ("vs_work_centers", "") in pinned_set,
    })

    for loc in staffing.LOCATIONS:
        slug = slug_for_wc(loc.name)
        out.append({
            "kind": "wc", "ref": loc.name,
            "name": loc.name,
            "open_url": f"/wc/{slug}", "tv_url": f"/tv/wc/{slug}",
            "pinned": ("wc", loc.name) in pinned_set,
        })

    for d in custom_dashboards_store.list_dashboards():
        out.append({
            "kind": "custom", "ref": d["slug"],
            "id": d["id"],
            "name": d["name"],
            "open_url": f"/dashboards/{d['slug']}",
            "tv_url": f"/tv/dashboards/{d['slug']}",
            "pinned": ("custom", d["slug"]) in pinned_set,
            "scope_kind": d.get("scope_kind"),
            "scope_value": d.get("scope_value"),
            "widget_count": d.get("widget_count", 0),
        })

    return out


def pinned_dashboards_for_subnav() -> list[dict]:
    """The pinned subset of all_dashboards(), in pin order, with a `key`
    field for the templates to mark the active tab.

    Pins referencing a removed WC or deleted custom dashboard are
    silently dropped — the underlying row stays in pinned_dashboards
    (we don't side-effect-prune at read time) but doesn't render.
    """
    from . import pinned_dashboards_store
    catalog = {(d["kind"], d["ref"]): d for d in all_dashboards()}
    out: list[dict] = []
    for pin in pinned_dashboards_store.list_pins():
        key = (pin["kind"], pin["ref"])
        item = catalog.get(key)
        if item is None:
            continue
        out.append({
            "kind": pin["kind"],
            "ref": pin["ref"],
            "name": item["name"],
            "open_url": item["open_url"],
            "key": f"{pin['kind']}:{pin['ref']}",
        })
    return out
