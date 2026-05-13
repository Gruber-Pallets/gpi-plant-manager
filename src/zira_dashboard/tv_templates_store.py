"""Persistence + apply layer for TV-dashboard layout templates.

A template is a named snapshot of a widget-layout JSON. The /wc/{slug}
editor can save its current arrangement as a template, then apply that
template to one WC, every WC in a group, or every WC at once.

Theme is stored per template (column on the table), but theme
propagation to target WCs waits for sub-project 4, which adds a
`tv_displays.theme` column. For now, apply_to_targets only writes
layouts.
"""
from __future__ import annotations

import json

from . import layout_store


def save(name: str, layout: list[dict], theme: str = "dark") -> None:
    """UPSERT a named template by `name`.

    `layout` is the list of {id, x, y, w, h} gridstack items. The same
    normalization the layout API uses applies — items without an id
    are dropped, numbers are coerced to int.
    """
    from . import db
    items = [layout_store._normalize(i) for i in (layout or []) if isinstance(i, dict) and i.get("id")]
    if theme not in ("light", "dark"):
        theme = "dark"
    db.execute(
        "INSERT INTO tv_dashboard_templates (name, layout_json, theme, updated_at) "
        "VALUES (%s, %s::jsonb, %s, now()) "
        "ON CONFLICT (name) DO UPDATE SET "
        "  layout_json = EXCLUDED.layout_json, "
        "  theme = EXCLUDED.theme, "
        "  updated_at = now()",
        (name, json.dumps(items), theme),
    )


def list_templates() -> list[dict]:
    """All templates as {id, name, theme, updated_at} dicts. Newest first."""
    from . import db
    rows = db.query(
        "SELECT id, name, theme, updated_at FROM tv_dashboard_templates "
        "ORDER BY updated_at DESC"
    )
    return [
        {
            "id": int(r["id"]),
            "name": r["name"],
            "theme": r["theme"],
            "updated_at": r["updated_at"],
        }
        for r in rows
    ]


def load(template_id: int) -> dict | None:
    """Full template by id: {id, name, layout_json, theme}.

    Returns None if no row with that id."""
    from . import db
    rows = db.query(
        "SELECT id, name, layout_json, theme FROM tv_dashboard_templates "
        "WHERE id = %s",
        (template_id,),
    )
    if not rows:
        return None
    r = rows[0]
    raw = r["layout_json"]
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            raw = []
    return {
        "id": int(r["id"]),
        "name": r["name"],
        "layout_json": raw or [],
        "theme": r["theme"],
    }


def delete(template_id: int) -> None:
    from . import db
    db.execute("DELETE FROM tv_dashboard_templates WHERE id = %s", (template_id,))


def resolve_targets(targets) -> list[str]:
    """Normalize the various target shapes into a flat list of page keys.

    Accepts:
      - list[str] of page keys: ["wc:repair-1", "wc:repair-2"] — passed through
      - "group:<group_name>": expand to every WC in that group
      - "all": every Location.name in staffing.LOCATIONS
    """
    from . import staffing, work_centers_store
    from .wc_dashboard_data import slug_for_wc

    if isinstance(targets, list):
        return [t for t in targets if isinstance(t, str) and t]

    if isinstance(targets, str):
        if targets == "all":
            return [f"wc:{slug_for_wc(loc.name)}" for loc in staffing.LOCATIONS]
        if targets.startswith("group:"):
            group_name = targets[len("group:"):]
            members = work_centers_store.members("group", group_name) or []
            return [f"wc:{slug_for_wc(loc.name)}" for loc in members]
    return []


def apply_to_targets(template_id: int, targets) -> dict:
    """Apply the template's layout to each target page.

    Targets can be a list of page keys, "group:<name>", or "all".
    Each target gets its widget_layouts row upserted via layout_store.
    Theme propagation deferred to sub-project 4.
    """
    tmpl = load(template_id)
    if tmpl is None:
        return {"applied_count": 0, "applied_pages": [], "error": "template not found"}
    pages = resolve_targets(targets)
    for page in pages:
        layout_store.save(page, tmpl["layout_json"])
    return {
        "applied_count": len(pages),
        "applied_pages": pages,
    }
