#!/usr/bin/env python3
"""One-shot migration: read existing JSON state files, INSERT into
Postgres. Idempotent — safe to re-run; uses INSERT ... ON CONFLICT.

Run from the project root with DATABASE_URL set:
  python -m scripts.migrate_json_to_postgres
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from zira_dashboard import db  # noqa: E402


def _load_json(path: Path) -> dict | list | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        print(f"  WARN: could not read {path}: {e}")
        return None


def migrate_skills(meta: list[dict] | None) -> None:
    if not meta:
        print("skills: no skill_columns_meta.json — skipped")
        return
    for i, m in enumerate(meta):
        db.execute(
            "INSERT INTO skills (name, skill_type, sort_order) VALUES (%s, %s, %s) "
            "ON CONFLICT (name) DO UPDATE SET skill_type = EXCLUDED.skill_type, sort_order = EXCLUDED.sort_order",
            (m["name"], m.get("type", ""), i),
        )
    print(f"skills: {len(meta)} rows")


def migrate_people(roster: list[dict] | None) -> None:
    if not roster:
        print("people: no roster.json — skipped")
        return
    for p in roster:
        db.execute(
            "INSERT INTO people (odoo_id, name, active, reserve) VALUES (%s, %s, %s, %s) "
            "ON CONFLICT (name) DO UPDATE SET odoo_id = EXCLUDED.odoo_id, active = EXCLUDED.active, reserve = EXCLUDED.reserve",
            (p.get("employee_id"), p["name"], p.get("active", True), p.get("reserve", False)),
        )
    print(f"people: {len(roster)} rows")
    for p in roster:
        for skill_name, level in (p.get("skills") or {}).items():
            if not isinstance(level, (int, float)) or level <= 0:
                continue
            db.execute(
                "INSERT INTO person_skills (person_id, skill_id, level) "
                "SELECT pe.id, sk.id, %s FROM people pe, skills sk "
                "WHERE pe.name = %s AND sk.name = %s "
                "ON CONFLICT (person_id, skill_id) DO UPDATE SET level = EXCLUDED.level",
                (int(level), p["name"], skill_name),
            )


def migrate_work_centers(wc_data: dict | None) -> None:
    from zira_dashboard import staffing
    # work_centers.json shape: {"work_centers": {key: {...}}, "groups": [names], "group_overrides": {...}}
    wc_overrides = (wc_data or {}).get("work_centers", {}) if isinstance(wc_data, dict) else {}
    for loc in staffing.LOCATIONS:
        key = loc.meter_id if loc.meter_id else f"name:{loc.name}"
        eff = wc_overrides.get(key, {}) if isinstance(wc_overrides, dict) else {}
        groups_list = eff.get("groups") or []
        first_group = groups_list[0] if groups_list else None
        db.execute(
            "INSERT INTO work_centers (name, meter_id, category, cell, value_stream, "
            "min_ops, max_ops, goal_per_day_override, group_name, note) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
            "ON CONFLICT (name) DO UPDATE SET "
            "meter_id = EXCLUDED.meter_id, category = EXCLUDED.category, cell = EXCLUDED.cell, "
            "value_stream = EXCLUDED.value_stream, min_ops = EXCLUDED.min_ops, "
            "max_ops = EXCLUDED.max_ops, goal_per_day_override = EXCLUDED.goal_per_day_override, "
            "group_name = EXCLUDED.group_name, note = EXCLUDED.note",
            (
                loc.name, loc.meter_id, loc.skill, loc.bay,
                eff.get("value_stream", "Recycled"),
                eff.get("min_ops", loc.min_ops),
                eff.get("max_ops", loc.max_ops),
                eff.get("goal_per_day"),
                first_group,
                eff.get("note", ""),
            ),
        )
    print(f"work_centers: {len(staffing.LOCATIONS)} rows")
    for loc in staffing.LOCATIONS:
        key = loc.meter_id if loc.meter_id else f"name:{loc.name}"
        eff = wc_overrides.get(key, {}) if isinstance(wc_overrides, dict) else {}
        for s in (eff.get("required_skills") or []):
            db.execute(
                "INSERT INTO work_center_required_skills (wc_id, skill_id) "
                "SELECT wc.id, sk.id FROM work_centers wc, skills sk "
                "WHERE wc.name = %s AND sk.name = %s "
                "ON CONFLICT DO NOTHING",
                (loc.name, s),
            )
        for i, person_name in enumerate(eff.get("default_people") or []):
            db.execute(
                "INSERT INTO work_center_default_people (wc_id, person_id, sort_order) "
                "SELECT wc.id, pe.id, %s FROM work_centers wc, people pe "
                "WHERE wc.name = %s AND pe.name = %s "
                "ON CONFLICT (wc_id, person_id) DO UPDATE SET sort_order = EXCLUDED.sort_order",
                (i, loc.name, person_name),
            )


def migrate_groups_and_value_streams(wc_data: dict | None) -> None:
    if not isinstance(wc_data, dict):
        return
    # `groups` is a flat list of names; `group_overrides` may carry per-name goal overrides.
    group_names = wc_data.get("groups") or []
    group_overrides = ((wc_data.get("group_overrides") or {}).get("group") or {}) if isinstance(wc_data.get("group_overrides"), dict) else {}
    for name in group_names:
        if not isinstance(name, str):
            continue
        override = group_overrides.get(name)
        db.execute(
            "INSERT INTO groups (name, goal_per_day_override) VALUES (%s, %s) "
            "ON CONFLICT (name) DO UPDATE SET goal_per_day_override = EXCLUDED.goal_per_day_override",
            (name, override if isinstance(override, int) else None),
        )
    print(f"groups: {len(group_names)} rows")
    # Value streams: from VALUE_STREAMS constant + any overrides.
    from zira_dashboard import work_centers_store
    vs_overrides = ((wc_data.get("group_overrides") or {}).get("value_stream") or {}) if isinstance(wc_data.get("group_overrides"), dict) else {}
    for vs in work_centers_store.VALUE_STREAMS:
        override = vs_overrides.get(vs)
        db.execute(
            "INSERT INTO value_streams (name, goal_per_day_override) VALUES (%s, %s) "
            "ON CONFLICT (name) DO UPDATE SET goal_per_day_override = EXCLUDED.goal_per_day_override",
            (vs, override if isinstance(override, int) else None),
        )
    print(f"value_streams: {len(work_centers_store.VALUE_STREAMS)} rows")


def migrate_schedules() -> None:
    sched_dir = ROOT / "schedules"
    if not sched_dir.exists():
        print("schedules: no schedules/ — skipped")
        return
    n = 0
    for path in sorted(sched_dir.glob("*.json")):
        d_iso = path.stem
        data = _load_json(path)
        if not isinstance(data, dict):
            continue
        db.execute(
            "INSERT INTO schedules (day, published, testing_day, notes, custom_hours, published_snapshot) "
            "VALUES (%s, %s, %s, %s, %s::jsonb, %s::jsonb) "
            "ON CONFLICT (day) DO UPDATE SET published = EXCLUDED.published, "
            "testing_day = EXCLUDED.testing_day, notes = EXCLUDED.notes, "
            "custom_hours = EXCLUDED.custom_hours, published_snapshot = EXCLUDED.published_snapshot",
            (
                d_iso,
                bool(data.get("published", False)),
                bool(data.get("testing_day", False)),
                str(data.get("notes") or ""),
                json.dumps(data.get("custom_hours")) if data.get("custom_hours") else None,
                json.dumps(data.get("published_snapshot")) if data.get("published_snapshot") else None,
            ),
        )
        assignments = data.get("assignments") or {}
        time_off = assignments.get("__time_off") or []
        for person_name in time_off:
            db.execute(
                "INSERT INTO schedule_time_off (day, person_id) "
                "SELECT %s, pe.id FROM people pe WHERE pe.name = %s "
                "ON CONFLICT DO NOTHING",
                (d_iso, person_name),
            )
        for wc_name, names in assignments.items():
            if wc_name == "__time_off":
                continue
            for i, person_name in enumerate(names or []):
                db.execute(
                    "INSERT INTO schedule_assignments (day, wc_id, person_id, sort_order) "
                    "SELECT %s, wc.id, pe.id, %s FROM work_centers wc, people pe "
                    "WHERE wc.name = %s AND pe.name = %s "
                    "ON CONFLICT (day, wc_id, person_id) DO UPDATE SET sort_order = EXCLUDED.sort_order",
                    (d_iso, i, wc_name, person_name),
                )
        for wc_name, note in (data.get("wc_notes") or {}).items():
            if not note:
                continue
            db.execute(
                "INSERT INTO schedule_wc_notes (day, wc_id, note) "
                "SELECT %s, wc.id, %s FROM work_centers wc WHERE wc.name = %s "
                "ON CONFLICT (day, wc_id) DO UPDATE SET note = EXCLUDED.note",
                (d_iso, note, wc_name),
            )
        n += 1
    print(f"schedules: {n} files")


def migrate_global_schedule(data: dict | None) -> None:
    if not data:
        print("global_schedule: no schedule.json — skipped")
        return
    db.execute(
        "INSERT INTO global_schedule (id, shift_start, shift_end, work_weekdays, breaks) "
        "VALUES (1, %s, %s, %s, %s::jsonb) "
        "ON CONFLICT (id) DO UPDATE SET shift_start = EXCLUDED.shift_start, "
        "shift_end = EXCLUDED.shift_end, work_weekdays = EXCLUDED.work_weekdays, "
        "breaks = EXCLUDED.breaks",
        (
            data.get("shift_start", "07:00"),
            data.get("shift_end", "15:00"),
            list(data.get("work_weekdays", [0, 1, 2, 3, 4])),
            json.dumps(data.get("breaks") or []),
        ),
    )
    print("global_schedule: 1 row")


def migrate_simple_kv(label: str, key: str, path_name: str) -> None:
    data = _load_json(ROOT / path_name)
    if data is None:
        print(f"{label}: no {path_name} — skipped")
        return
    db.execute(
        "INSERT INTO app_settings (key, value, updated_at) VALUES (%s, %s::jsonb, now()) "
        "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()",
        (key, json.dumps(data)),
    )
    print(f"{label}: 1 row")


def main() -> int:
    print("Bootstrapping schema...")
    db.init_pool()
    db.bootstrap_schema()

    print("\n=== Migrating JSON files into Postgres ===")
    migrate_skills(_load_json(ROOT / "skill_columns_meta.json"))
    migrate_people(_load_json(ROOT / "roster.json"))
    wc_data = _load_json(ROOT / "work_centers.json")
    migrate_work_centers(wc_data)
    migrate_groups_and_value_streams(wc_data)
    migrate_schedules()
    migrate_global_schedule(_load_json(ROOT / "schedule.json"))
    migrate_simple_kv("settings", "settings", "settings.json")
    migrate_simple_kv("layouts", "layouts", "layouts.json")
    migrate_simple_kv("widget_customizations", "widget_customizations", "widget_customizations.json")
    migrate_simple_kv("skill_filter", "skill_filter", "skill_filter.json")

    last_sync_path = ROOT / ".odoo_last_sync"
    if last_sync_path.exists():
        ts = last_sync_path.read_text().strip()
        db.execute(
            "INSERT INTO app_settings (key, value, updated_at) VALUES ('odoo_last_sync', %s::jsonb, now()) "
            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()",
            (json.dumps(ts),),
        )
        print("odoo_last_sync: 1 row")

    print("\nDone.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
