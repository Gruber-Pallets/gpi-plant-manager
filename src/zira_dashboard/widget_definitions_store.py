"""Persistence layer for widget definitions (workshop presets).

Each definition has a type (one of the registry slugs), a visual config
JSON, and a default data scope JSON. Deletion is blocked while any
`dashboard_widgets` row references the row — caller should check
`usage_count` first and ask the user to remove placements.
"""
from __future__ import annotations

import json
import logging
from typing import Optional

_log = logging.getLogger(__name__)


def save(
    *,
    name: str,
    type: str,
    visual: dict,
    default_data: dict,
    id: Optional[int] = None,
) -> dict:
    """Insert or update a definition. Returns the saved row as a dict."""
    from . import db
    if not isinstance(name, str) or not name.strip():
        raise ValueError("name required")
    if not isinstance(type, str) or not type.strip():
        raise ValueError("type required")
    visual = visual or {}
    default_data = default_data or {}
    if id is None:
        rows = db.query(
            "INSERT INTO widget_definitions (name, type, visual_json, default_data_json) "
            "VALUES (%s, %s, %s::jsonb, %s::jsonb) "
            "RETURNING id, name, type, visual_json, default_data_json",
            (name.strip(), type.strip(), json.dumps(visual), json.dumps(default_data)),
        )
    else:
        rows = db.query(
            "UPDATE widget_definitions SET "
            "  name = %s, type = %s, visual_json = %s::jsonb, "
            "  default_data_json = %s::jsonb, updated_at = now() "
            "WHERE id = %s "
            "RETURNING id, name, type, visual_json, default_data_json",
            (name.strip(), type.strip(), json.dumps(visual), json.dumps(default_data), id),
        )
    if not rows:
        raise LookupError(f"no widget_definitions row with id={id}")
    return _hydrate(rows[0])


def get(id: int) -> Optional[dict]:
    from . import db
    rows = db.query(
        "SELECT id, name, type, visual_json, default_data_json "
        "FROM widget_definitions WHERE id = %s",
        (id,),
    )
    return _hydrate(rows[0]) if rows else None


def list_definitions() -> list[dict]:
    """All definitions with `usage_count` precomputed via subquery."""
    from . import db
    rows = db.query(
        "SELECT wd.id, wd.name, wd.type, wd.visual_json, wd.default_data_json, "
        "  COALESCE(c.n, 0) AS usage_count "
        "FROM widget_definitions wd "
        "LEFT JOIN ("
        "  SELECT widget_def_id, COUNT(*) AS n "
        "  FROM dashboard_widgets GROUP BY widget_def_id"
        ") c ON c.widget_def_id = wd.id "
        "ORDER BY wd.type, lower(wd.name)"
    )
    out = []
    for r in rows:
        d = _hydrate(r)
        d["usage_count"] = int(r["usage_count"])
        out.append(d)
    return out


def delete(id: int) -> None:
    """Hard-delete a definition. Postgres FK ON DELETE RESTRICT raises if
    any dashboard_widgets row references it — caller is expected to have
    called `usage_count` first."""
    from . import db
    db.execute("DELETE FROM widget_definitions WHERE id = %s", (id,))


def usage_count(id: int) -> int:
    from . import db
    rows = db.query(
        "SELECT COUNT(*) AS n FROM dashboard_widgets WHERE widget_def_id = %s",
        (id,),
    )
    return int(rows[0]["n"]) if rows else 0


def _hydrate(row: dict) -> dict:
    return {
        "id": int(row["id"]),
        "name": row["name"],
        "type": row["type"],
        "visual": _decode(row["visual_json"]),
        "default_data": _decode(row["default_data_json"]),
    }


def _decode(raw) -> dict:
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return {}
    return raw if isinstance(raw, dict) else {}


# Seed list — 10 starter widgets that mirror the hardcoded widgets on
# /recycling and /wc/{slug}. Inserted once on first boot when the table
# is empty. Group-scoped seeds skip if the group doesn't exist; WC-scoped
# seeds skip if the WC isn't in staffing.LOCATIONS.
_SEED_LIST = [
    {"name": "Pallets by WC — Dismantlers", "type": "pallets_by_wc",
     "visual": {"color": "#22c55e", "sort": "desc"},
     "default_data": {"group": "Dismantler"},
     "needs_group": "Dismantler"},
    {"name": "Pallets by WC — Repairs", "type": "pallets_by_wc",
     "visual": {"color": "#22c55e", "sort": "desc"},
     "default_data": {"group": "Repair"},
     "needs_group": "Repair"},
    {"name": "Total Pallets — Dismantlers", "type": "kpi",
     "visual": {"color": "#22c55e"},
     "default_data": {"metric": "units_today_group", "group": "Dismantler"},
     "needs_group": "Dismantler"},
    {"name": "Total Pallets — Repairs", "type": "kpi",
     "visual": {"color": "#22c55e"},
     "default_data": {"metric": "units_today_group", "group": "Repair"},
     "needs_group": "Repair"},
    {"name": "Pallets Banner — Repair 1", "type": "pallets_banner",
     "visual": {"color": "#22c55e"},
     "default_data": {"wc_name": "Repair 1"},
     "needs_wc": "Repair 1"},
    {"name": "Daily Progress — Repair 1", "type": "daily_progress",
     "visual": {},
     "default_data": {"wc_name": "Repair 1"},
     "needs_wc": "Repair 1"},
    {"name": "Cumulative Progress — Repair 1", "type": "cumulative",
     "visual": {"color": "#22c55e", "show_target": "true"},
     "default_data": {"wc_name": "Repair 1"},
     "needs_wc": "Repair 1"},
    {"name": "Downtime Report — Repair 1", "type": "downtime",
     "visual": {},
     "default_data": {"wc_name": "Repair 1"},
     "needs_wc": "Repair 1"},
    {"name": "GOAT Race — Repairs", "type": "goat_race",
     "visual": {"color": "#22c55e"},
     "default_data": {"group": "Repair"},
     "needs_group": "Repair"},
    {"name": "Monthly Ribbons — Repairs", "type": "ribbons",
     "visual": {},
     "default_data": {"group": "Repair"},
     "needs_group": "Repair"},
]


def seed_defaults_if_empty() -> None:
    """Insert the 10-row seed list if `widget_definitions` is empty.

    Seeds whose referenced group isn't in `work_centers_store.all_group_names('group')`
    or whose WC isn't in `staffing.LOCATIONS` are skipped with a warning log so
    a partial plant config doesn't fail boot. Re-running on a non-empty table
    is a no-op — deleted seeds stay deleted across redeploys.
    """
    from . import db, staffing, work_centers_store
    existing = db.query("SELECT 1 FROM widget_definitions LIMIT 1")
    if existing:
        return
    valid_groups = set(work_centers_store.all_group_names("group"))
    valid_wcs = {loc.name for loc in staffing.LOCATIONS}
    inserted = 0
    for entry in _SEED_LIST:
        if "needs_group" in entry and entry["needs_group"] not in valid_groups:
            _log.warning(
                "widget_definitions seed skipping %s — group %r not in registered groups",
                entry["name"], entry["needs_group"],
            )
            continue
        if "needs_wc" in entry and entry["needs_wc"] not in valid_wcs:
            _log.warning(
                "widget_definitions seed skipping %s — WC %r not in staffing.LOCATIONS",
                entry["name"], entry["needs_wc"],
            )
            continue
        save(
            name=entry["name"], type=entry["type"],
            visual=entry["visual"], default_data=entry["default_data"],
        )
        inserted += 1
    _log.info("widget_definitions seeded %d starter rows", inserted)


def duplicate(id: int) -> dict:
    """Clone a definition, appending '(copy)' / '(copy 2)' / ... to the name.

    Raises LookupError if the source id doesn't exist.
    """
    from . import db
    source = get(id)
    if source is None:
        raise LookupError(f"no widget_definitions row with id={id}")
    base = source["name"]
    candidate = f"{base} (copy)"
    n = 2
    while True:
        rows = db.query(
            "SELECT id FROM widget_definitions WHERE name = %s",
            (candidate,),
        )
        if not rows:
            break
        candidate = f"{base} (copy {n})"
        n += 1
    return save(
        name=candidate, type=source["type"],
        visual=source["visual"], default_data=source["default_data"],
    )
