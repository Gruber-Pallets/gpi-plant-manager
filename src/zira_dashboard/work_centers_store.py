"""Per-work-center configuration backed by Postgres.

Reads/writes the `work_centers`, `work_center_required_skills`,
`work_center_default_people`, `groups`, and `value_streams` tables.

Public API is unchanged from the JSON-file era so route handlers don't
need to migrate. All functions are pure pass-throughs to SQL.
"""

from __future__ import annotations

from .shift_config import TARGET_PER_DAY, productive_minutes_per_day
from .staffing import LOCATIONS, Location, required_skills_for

GROUP_KINDS = ("group", "value_stream")

# Bootstrap fallback for the value_streams (a.k.a. "departments" in the
# UI) registry. Used on a fresh deploy before Odoo sync has populated
# the `value_streams` table for the first time. After sync, the live
# list comes from the DB via `synced_value_streams()`.
VALUE_STREAMS: tuple[str, ...] = ("New", "Recycled", "Transportation")


def synced_value_streams() -> list[str]:
    """Live list of registered value streams (UI: "Departments"). Reads
    from the `value_streams` table, which `odoo_sync.sync()` populates
    from `hr.department` (numeric prefixes stripped). Falls back to the
    hardcoded `VALUE_STREAMS` bootstrap on a fresh DB. Sorted
    case-insensitive."""
    from . import db
    rows = db.query("SELECT name FROM value_streams ORDER BY lower(name)")
    if not rows:
        return list(VALUE_STREAMS)
    return [r["name"] for r in rows]

from ._cache import TTLCache

# Per-location WC config and per-group goal overrides change rarely
# (only on Settings save). Cache for 60s and invalidate on writes.
_EFFECTIVE_CACHE = TTLCache(ttl_seconds=60.0, max_entries=128)
_GOAL_OVERRIDE_CACHE = TTLCache(ttl_seconds=60.0, max_entries=64)
_GROUP_NAMES_CACHE = TTLCache(ttl_seconds=60.0, max_entries=4)


def _invalidate_caches() -> None:
    """Clear all in-process caches. Called by every write path so the
    next read sees the freshly-saved state."""
    _EFFECTIVE_CACHE.invalidate()
    _GOAL_OVERRIDE_CACHE.invalidate()
    _GROUP_NAMES_CACHE.invalidate()


def _default_goal_for(loc: Location) -> int:
    category = {"Dismantler": "Dismantler", "Repair": "Repair"}.get(loc.skill, "Other")
    return int(TARGET_PER_DAY.get(category, 0))


# ---------- effective per-work-center ----------

def effective(loc: Location) -> dict:
    return _EFFECTIVE_CACHE.get_or_compute(loc.name, lambda: _effective_uncached(loc))


def _effective_uncached(loc: Location) -> dict:
    from . import db
    rows = db.query(
        "SELECT goal_per_day_override, min_ops, max_ops, value_stream, "
        "       group_name, note "
        "FROM work_centers WHERE name = %s",
        (loc.name,),
    )
    rec = rows[0] if rows else {}
    req_rows = db.query(
        "SELECT s.name FROM work_center_required_skills wrs "
        "JOIN work_centers wc ON wc.id = wrs.wc_id "
        "JOIN skills s ON s.id = wrs.skill_id "
        "WHERE wc.name = %s",
        (loc.name,),
    )
    if req_rows:
        req = [r["name"] for r in req_rows]
    else:
        req = list(required_skills_for(loc))
    def_rows = db.query(
        "SELECT pe.name FROM work_center_default_people wcdp "
        "JOIN work_centers wc ON wc.id = wcdp.wc_id "
        "JOIN people pe ON pe.id = wcdp.person_id "
        "WHERE wc.name = %s ORDER BY wcdp.sort_order",
        (loc.name,),
    )
    defaults = [r["name"] for r in def_rows]
    goal = rec.get("goal_per_day_override")
    return {
        "goal_per_day": int(goal) if goal is not None else _default_goal_for(loc),
        "min_ops": int(rec.get("min_ops") or loc.min_ops),
        "max_ops": rec.get("max_ops") if rec.get("max_ops") is not None else loc.max_ops,
        "required_skills": req,
        "note": rec.get("note") or "",
        "groups": [rec.get("group_name")] if rec.get("group_name") else [],
        "value_stream": rec.get("value_stream") or "",
        "default_people": defaults,
    }


def goal_per_day(loc: Location) -> int:        return effective(loc)["goal_per_day"]
def min_ops(loc: Location) -> int:              return int(effective(loc)["min_ops"])
def max_ops(loc: Location):                     return effective(loc)["max_ops"]
def required_skills(loc: Location):             return list(effective(loc)["required_skills"])
def note(loc: Location) -> str:                 return effective(loc)["note"]
def groups(loc: Location) -> list[str]:         return list(effective(loc)["groups"])
def value_stream(loc: Location) -> str:         return effective(loc)["value_stream"]
def default_people(loc: Location) -> list[str]: return list(effective(loc)["default_people"])


def goal_per_hour(loc: Location) -> float:
    hrs = productive_minutes_per_day() / 60.0
    return (goal_per_day(loc) / hrs) if hrs else 0.0


# ---------- groups (registry + per-group goals) ----------

def members(kind: str, name: str) -> list[Location]:
    if kind not in GROUP_KINDS or not name:
        return []
    if kind == "group":
        return [loc for loc in LOCATIONS if name in groups(loc)]
    return [loc for loc in LOCATIONS if value_stream(loc) == name]


def group_goal_auto(kind: str, name: str) -> int:
    return sum(goal_per_day(loc) for loc in members(kind, name))


def group_goal_override(kind: str, name: str):
    if kind not in GROUP_KINDS or not name:
        return None
    return _GOAL_OVERRIDE_CACHE.get_or_compute(
        (kind, name),
        lambda: _group_goal_override_uncached(kind, name),
    )


def _group_goal_override_uncached(kind: str, name: str):
    from . import db
    table = "groups" if kind == "group" else "value_streams"
    rows = db.query(
        f"SELECT goal_per_day_override FROM {table} WHERE name = %s",
        (name,),
    )
    if not rows:
        return None
    v = rows[0]["goal_per_day_override"]
    return int(v) if v is not None else None


def group_goal(kind: str, name: str) -> int:
    o = group_goal_override(kind, name)
    return int(o) if o is not None else group_goal_auto(kind, name)


def all_group_names(kind: str) -> list[str]:
    return list(_GROUP_NAMES_CACHE.get_or_compute(
        kind,
        lambda: tuple(_all_group_names_uncached(kind)),
    ))


def _all_group_names_uncached(kind: str) -> list[str]:
    from . import db
    seen, out = set(), []
    if kind == "group":
        for loc in LOCATIONS:
            for g in groups(loc):
                if g not in seen:
                    seen.add(g); out.append(g)
        rows = db.query("SELECT name FROM groups ORDER BY name")
        for r in rows:
            if r["name"] not in seen:
                seen.add(r["name"]); out.append(r["name"])
    else:
        for loc in LOCATIONS:
            v = value_stream(loc)
            if v and v not in seen:
                seen.add(v); out.append(v)
        for v in VALUE_STREAMS:
            if v not in seen:
                seen.add(v); out.append(v)
        rows = db.query("SELECT name FROM value_streams ORDER BY name")
        for r in rows:
            if r["name"] not in seen:
                seen.add(r["name"]); out.append(r["name"])
    return sorted(out, key=str.lower)


# ---------- write ----------

def save_one(loc: Location, updates: dict) -> dict:
    """Upsert one work_center row + replace its required_skills and
    default_people lists. Only fields present in `updates` are touched."""
    from . import db

    # Whitelist + coerce updates.
    direct: dict = {}  # column → new value
    if "goal_per_day" in updates:
        v = updates["goal_per_day"]
        if isinstance(v, str): v = v.strip()
        try:
            direct["goal_per_day_override"] = max(0, int(v))
        except (TypeError, ValueError):
            direct["goal_per_day_override"] = None
    if "min_ops" in updates:
        try:
            direct["min_ops"] = max(0, int(updates["min_ops"]))
        except (TypeError, ValueError):
            pass
    if "max_ops" in updates:
        v = updates["max_ops"]
        if v in (None, ""):
            direct["max_ops"] = None
        else:
            try:
                direct["max_ops"] = max(0, int(v))
            except (TypeError, ValueError):
                pass
    if "note" in updates and isinstance(updates["note"], str):
        direct["note"] = updates["note"].strip()[:200]
    if "value_stream" in updates and isinstance(updates["value_stream"], str):
        v = updates["value_stream"].strip()
        if v == "" or v in synced_value_streams():
            direct["value_stream"] = v or None
    if "groups" in updates and isinstance(updates["groups"], list):
        direct["group_name"] = next(
            (g for g in updates["groups"] if isinstance(g, str) and g),
            None,
        )

    with db.cursor() as cur:
        # Ensure the work_center row exists.
        cur.execute(
            "INSERT INTO work_centers (name, category, cell, meter_id, min_ops, max_ops) "
            "VALUES (%s, %s, %s, %s, %s, %s) "
            "ON CONFLICT (name) DO NOTHING",
            (loc.name, loc.skill, loc.bay, loc.meter_id, loc.min_ops, loc.max_ops),
        )
        if direct:
            sets = ", ".join(f"{col} = %s" for col in direct.keys())
            cur.execute(
                f"UPDATE work_centers SET {sets} WHERE name = %s",
                (*direct.values(), loc.name),
            )
        # Replace required_skills if provided.
        if "required_skills" in updates and isinstance(updates["required_skills"], list):
            cur.execute(
                "DELETE FROM work_center_required_skills WHERE wc_id = "
                "(SELECT id FROM work_centers WHERE name = %s)",
                (loc.name,),
            )
            for s in updates["required_skills"]:
                if not isinstance(s, str):
                    continue
                cur.execute(
                    "INSERT INTO work_center_required_skills (wc_id, skill_id) "
                    "SELECT wc.id, sk.id FROM work_centers wc, skills sk "
                    "WHERE wc.name = %s AND sk.name = %s ON CONFLICT DO NOTHING",
                    (loc.name, s),
                )
        # Replace default_people if provided.
        if "default_people" in updates and isinstance(updates["default_people"], list):
            cur.execute(
                "DELETE FROM work_center_default_people WHERE wc_id = "
                "(SELECT id FROM work_centers WHERE name = %s)",
                (loc.name,),
            )
            for i, person_name in enumerate(updates["default_people"]):
                if not isinstance(person_name, str) or not person_name.strip():
                    continue
                cur.execute(
                    "INSERT INTO work_center_default_people (wc_id, person_id, sort_order) "
                    "SELECT wc.id, pe.id, %s FROM work_centers wc, people pe "
                    "WHERE wc.name = %s AND pe.name = %s",
                    (i, loc.name, person_name.strip()),
                )

    _invalidate_caches()
    return effective(loc)


def registered_groups() -> list[str]:
    from . import db
    rows = db.query("SELECT name FROM groups ORDER BY lower(name)")
    return [r["name"] for r in rows]


def add_group(name: str) -> None:
    name = (name or "").strip()[:80]
    if not name:
        return
    from . import db
    db.execute(
        "INSERT INTO groups (name) VALUES (%s) ON CONFLICT (name) DO NOTHING",
        (name,),
    )
    _invalidate_caches()


def rename_group(old: str, new: str) -> None:
    old = (old or "").strip()
    new = (new or "").strip()[:80]
    if not old or not new or old == new:
        return
    from . import db
    with db.cursor() as cur:
        # Delete the new name's row (if it exists) so the rename can clear FKs cleanly.
        cur.execute(
            "INSERT INTO groups (name) VALUES (%s) ON CONFLICT (name) DO NOTHING",
            (new,),
        )
        cur.execute(
            "UPDATE work_centers SET group_name = %s WHERE group_name = %s",
            (new, old),
        )
        cur.execute(
            "UPDATE groups SET goal_per_day_override = "
            "(SELECT goal_per_day_override FROM groups WHERE name = %s) "
            "WHERE name = %s AND goal_per_day_override IS NULL",
            (old, new),
        )
        cur.execute("DELETE FROM groups WHERE name = %s", (old,))
    _invalidate_caches()


def delete_group(name: str) -> None:
    name = (name or "").strip()
    if not name:
        return
    from . import db
    with db.cursor() as cur:
        cur.execute(
            "UPDATE work_centers SET group_name = NULL WHERE group_name = %s",
            (name,),
        )
        cur.execute("DELETE FROM groups WHERE name = %s", (name,))
    _invalidate_caches()


def save_group_override(kind: str, name: str, value) -> None:
    if kind not in GROUP_KINDS or not name:
        return
    table = "groups" if kind == "group" else "value_streams"
    if isinstance(value, str):
        value = value.strip()
    from . import db
    if value in (None, ""):
        db.execute(
            f"UPDATE {table} SET goal_per_day_override = NULL WHERE name = %s",
            (name,),
        )
    else:
        try:
            iv = int(value)
        except (TypeError, ValueError):
            return
        db.execute(
            f"INSERT INTO {table} (name, goal_per_day_override) VALUES (%s, %s) "
            f"ON CONFLICT (name) DO UPDATE SET goal_per_day_override = EXCLUDED.goal_per_day_override",
            (name, iv),
        )
    _invalidate_caches()


def snapshot() -> dict:
    """Compatibility shim — returns a dict that matches the old JSON
    snapshot shape. Used by code that wanted the raw config blob."""
    from . import db
    wc_rows = db.query(
        "SELECT name, meter_id, value_stream, min_ops, max_ops, "
        "       goal_per_day_override, group_name, note FROM work_centers"
    )
    wc_dict = {}
    for r in wc_rows:
        key = r["meter_id"] if r["meter_id"] else f"name:{r['name']}"
        rec: dict = {}
        if r["goal_per_day_override"] is not None:
            rec["goal_per_day"] = int(r["goal_per_day_override"])
        if r["min_ops"] is not None:
            rec["min_ops"] = int(r["min_ops"])
        if r["max_ops"] is not None:
            rec["max_ops"] = int(r["max_ops"])
        if r["value_stream"]:
            rec["value_stream"] = r["value_stream"]
        if r["group_name"]:
            rec["groups"] = [r["group_name"]]
        if r["note"]:
            rec["note"] = r["note"]
        if rec:
            wc_dict[key] = rec
    g_rows = db.query("SELECT name, goal_per_day_override FROM groups WHERE goal_per_day_override IS NOT NULL")
    vs_rows = db.query("SELECT name, goal_per_day_override FROM value_streams WHERE goal_per_day_override IS NOT NULL")
    return {
        "work_centers": wc_dict,
        "group_overrides": {
            "group": {r["name"]: int(r["goal_per_day_override"]) for r in g_rows},
            "value_stream": {r["name"]: int(r["goal_per_day_override"]) for r in vs_rows},
        },
    }
