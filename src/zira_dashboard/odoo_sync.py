"""Odoo → Postgres sync with TTL cache.

Single public entrypoint: sync(force=False). Returns SyncResult.
On TTL hit (default 1 hour), no Odoo call is made. On force or stale,
fetches employees + skills from Odoo and upserts into the `people`,
`skills`, `person_skills` tables. The local `reserve` flag is preserved
because we never write to it from sync.

Last-sync timestamp is stored in app_settings under key 'odoo_last_sync'.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from . import odoo_client

TTL = timedelta(hours=1)


@dataclass(frozen=True)
class SyncResult:
    ok: bool
    refreshed: bool
    employee_count: int
    skill_column_count: int
    last_sync_at: datetime | None
    error: str | None = None


def _read_last_sync() -> datetime | None:
    from . import db
    rows = db.query("SELECT value FROM app_settings WHERE key = 'odoo_last_sync'")
    if not rows:
        return None
    raw = rows[0]["value"]
    if isinstance(raw, str):
        try:
            return datetime.fromisoformat(raw.strip('"'))
        except ValueError:
            return None
    if isinstance(raw, (int, float)):
        return None
    # JSONB-decoded as Python obj — unwrap if it's a JSON string
    try:
        s = json.loads(raw) if isinstance(raw, str) else raw
        if isinstance(s, str):
            return datetime.fromisoformat(s)
    except (ValueError, TypeError):
        pass
    return None


def _write_last_sync(now: datetime) -> None:
    from . import db
    db.execute(
        "INSERT INTO app_settings (key, value, updated_at) "
        "VALUES ('odoo_last_sync', %s::jsonb, now()) "
        "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()",
        (json.dumps(now.isoformat()),),
    )


def sync(force: bool = False) -> SyncResult:
    last = _read_last_sync()
    now = datetime.now(timezone.utc)
    if not force and last is not None and (now - last) < TTL:
        return SyncResult(
            ok=True, refreshed=False, employee_count=0,
            skill_column_count=0, last_sync_at=last,
        )

    try:
        employees = odoo_client.fetch_employees()
        emp_ids = [e["id"] for e in employees]
        emp_skills = odoo_client.fetch_skills_for(emp_ids)
        spanish_ids = odoo_client.fetch_spanish_speaker_ids()
        columns_meta = odoo_client.fetch_skill_columns_with_types()
        buckets = odoo_client.fetch_skill_level_buckets()
        departments = odoo_client.fetch_departments()
    except Exception as e:
        return SyncResult(
            ok=False, refreshed=False, employee_count=0,
            skill_column_count=0, last_sync_at=last, error=str(e),
        )

    from . import db
    columns = [c["name"] for c in columns_meta]
    type_by_skill = {c["name"]: c.get("type", "") for c in columns_meta}
    pulled_at = now

    def _short_name(full: str) -> str:
        """Take first two whitespace-delimited tokens. Odoo employee
        cards often have 3–5 word full names (e.g. "Adrian Aragon
        Olivera"); the dashboard displays the first two ("Adrian Aragon")
        for compact matrix rows."""
        parts = (full or "").strip().split()
        return " ".join(parts[:2]) if parts else (full or "")
    with db.cursor() as cur:
        # Skills first (employees + person_skills FK them).
        for i, m in enumerate(columns_meta):
            cur.execute(
                "INSERT INTO skills (name, skill_type, sort_order, last_pulled_at) "
                "VALUES (%s, %s, %s, %s) "
                "ON CONFLICT (name) DO UPDATE SET skill_type = EXCLUDED.skill_type, "
                "sort_order = EXCLUDED.sort_order, last_pulled_at = EXCLUDED.last_pulled_at",
                (m["name"], m.get("type", ""), i, pulled_at),
            )
        # Employees: upsert by odoo_id (stable across renames).
        seen_employee_ids = set()
        for emp in employees:
            seen_employee_ids.add(emp["id"])
            # Odoo selection fields return False when unset; normalize to None.
            wage_type = emp.get("wage_type") or None
            spanish_speaker = emp["id"] in spanish_ids
            cur.execute(
                "INSERT INTO people (odoo_id, name, active, wage_type, spanish_speaker, last_pulled_at) "
                "VALUES (%s, %s, %s, %s, %s, %s) "
                "ON CONFLICT (odoo_id) DO UPDATE SET name = EXCLUDED.name, "
                "active = EXCLUDED.active, wage_type = EXCLUDED.wage_type, "
                "spanish_speaker = EXCLUDED.spanish_speaker, "
                "last_pulled_at = EXCLUDED.last_pulled_at",
                (emp["id"], _short_name(emp["name"]), bool(emp.get("active", True)),
                 wage_type, spanish_speaker, pulled_at),
            )
        # Deactivate Odoo-mapped people who disappeared from the response —
        # i.e., archived (or deleted) in Odoo. fetch_employees() searches
        # with active=True so this set covers both cases. Guard against
        # an unexpectedly empty response (we'd never want to deactivate
        # everyone) by skipping when no employees came back at all.
        if seen_employee_ids:
            cur.execute(
                "UPDATE people SET active = FALSE, last_pulled_at = %s "
                "WHERE odoo_id IS NOT NULL "
                "AND odoo_id != ALL(%s) "
                "AND active = TRUE",
                (pulled_at, list(seen_employee_ids)),
            )
        # Refresh person_skills: for each employee, replace their skill levels
        # with the Odoo set. We use DELETE + INSERT inside one transaction so
        # a person who lost a skill in Odoo also drops it locally.
        for emp in employees:
            cur.execute(
                "DELETE FROM person_skills WHERE person_id = "
                "(SELECT id FROM people WHERE odoo_id = %s) "
                "AND local_dirty = FALSE",
                (emp["id"],),
            )
            for s in emp_skills.get(emp["id"], []):
                if s["skill_name"] not in columns:
                    continue
                if type_by_skill.get(s["skill_name"]) == "Certifications":
                    # Binary semantics: any synced cert link counts as having it.
                    # cert_lookup ignores level; staffing color uses 3 = green.
                    level = 3
                else:
                    level = buckets.get(s["level_id"], 0)
                    if level <= 0:
                        continue
                cur.execute(
                    "INSERT INTO person_skills (person_id, skill_id, level, last_pulled_at) "
                    "SELECT pe.id, sk.id, %s, %s FROM people pe, skills sk "
                    "WHERE pe.odoo_id = %s AND sk.name = %s "
                    "ON CONFLICT (person_id, skill_id) DO UPDATE SET "
                    "  level = EXCLUDED.level, last_pulled_at = EXCLUDED.last_pulled_at",
                    (level, pulled_at, emp["id"], s["skill_name"]),
                )

    # Departments: upsert into the departments registry table (kept
    # under that name internally for backward compat — UI calls it
    # "Department"). Additive only — never delete existing rows so any
    # WC.department column value that references a now-archived Odoo
    # department still resolves in the dropdown. Goal-override column
    # is preserved by the ON CONFLICT DO NOTHING.
    if departments:
        with db.cursor() as cur:
            for name in departments:
                cur.execute(
                    "INSERT INTO departments (name) VALUES (%s) "
                    "ON CONFLICT (name) DO NOTHING",
                    (name,),
                )

    _write_last_sync(pulled_at)

    # Bust caches that depend on the freshly-synced data.
    from . import cert_lookup, staffing
    cert_lookup.invalidate_cache()
    staffing._invalidate_roster_cache()

    return SyncResult(
        ok=True, refreshed=True, employee_count=len(employees),
        skill_column_count=len(columns), last_sync_at=pulled_at,
    )
