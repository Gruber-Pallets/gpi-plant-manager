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
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, UTC

from . import odoo_client

log = logging.getLogger(__name__)

TTL = timedelta(hours=1)


def _m2o_id(val):
    """Odoo many2one fields come back as [id, name] or False. Return the
    id, or None."""
    if isinstance(val, (list, tuple)) and val:
        return val[0]
    return None


def refresh_work_schedule_hours(only_ids=None) -> None:
    """Refresh the Odoo-owned name + per-weekday hours for the configured
    work_schedules overrides. Leaves the app-owned rounding windows alone.
    Best-effort: callers wrap in try/except so an Odoo hiccup never breaks
    the rest of the sync."""
    from . import work_schedule_store, odoo_client  # local import: avoids import cycle + lets tests monkeypatch odoo_client.fetch_*
    ids = [o.resource_calendar_id for o in work_schedule_store.all_overrides()]
    if only_ids is not None:
        wanted = {int(i) for i in only_ids}
        ids = [i for i in ids if i in wanted]
    if not ids:
        return
    names = {c["id"]: c.get("name") or "" for c in odoo_client.fetch_work_schedules()}
    hours = odoo_client.fetch_calendar_hours(ids)
    for cid in ids:
        work_schedule_store.refresh_synced(cid, names.get(cid, ""), hours.get(cid, {}))


@dataclass(frozen=True)
class SyncResult:
    ok: bool
    refreshed: bool
    employee_count: int
    skill_column_count: int
    last_sync_at: datetime | None
    error: str | None = None


def _read_last_sync() -> datetime | None:
    # odoo_last_sync is a scalar JSON *string* (an isoformat); app_settings.
    # get_setting can't decode that in psycopg2's "already-decoded" mode
    # (json.loads on a bare date string fails), so this keeps its own
    # dual-mode decode rather than going through the shared helper.
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
    from . import app_settings
    app_settings.set_setting("odoo_last_sync", now.isoformat())


def _merge_legacy_skill_into_stable(cur, *, stable_skill_id: int, legacy_skill_id: int) -> None:
    cur.execute(
        "WITH moved AS ("
        "DELETE FROM person_skills WHERE skill_id = %s "
        "RETURNING person_id, level, last_pulled_at, last_pushed_at, local_dirty"
        ") "
        "INSERT INTO person_skills "
        "(person_id, skill_id, level, last_pulled_at, last_pushed_at, local_dirty) "
        "SELECT person_id, %s, level, last_pulled_at, last_pushed_at, local_dirty FROM moved "
        "ON CONFLICT (person_id, skill_id) DO UPDATE SET "
        "level = GREATEST(person_skills.level, EXCLUDED.level), "
        "last_pulled_at = COALESCE(EXCLUDED.last_pulled_at, person_skills.last_pulled_at), "
        "last_pushed_at = COALESCE(EXCLUDED.last_pushed_at, person_skills.last_pushed_at), "
        "local_dirty = person_skills.local_dirty OR EXCLUDED.local_dirty",
        (legacy_skill_id, stable_skill_id),
    )
    cur.execute(
        "INSERT INTO work_center_required_skills (wc_id, skill_id) "
        "SELECT wc_id, %s FROM work_center_required_skills WHERE skill_id = %s "
        "ON CONFLICT (wc_id, skill_id) DO NOTHING",
        (stable_skill_id, legacy_skill_id),
    )
    cur.execute(
        "DELETE FROM work_center_required_skills WHERE skill_id = %s",
        (legacy_skill_id,),
    )
    cur.execute("DELETE FROM skills WHERE id = %s", (legacy_skill_id,))


def _merge_legacy_skill_name_collision(cur, *, skill_odoo_id: int, skill_name: str) -> None:
    cur.execute("SELECT id FROM skills WHERE odoo_id = %s", (skill_odoo_id,))
    stable = cur.fetchone()
    if not stable:
        return
    cur.execute(
        "SELECT id FROM skills WHERE name = %s AND odoo_id IS NULL",
        (skill_name,),
    )
    legacy = cur.fetchone()
    if legacy:
        _merge_legacy_skill_into_stable(
            cur,
            stable_skill_id=int(stable["id"]),
            legacy_skill_id=int(legacy["id"]),
        )


def sync(force: bool = False) -> SyncResult:
    last = _read_last_sync()
    now = datetime.now(UTC)
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

    # Flex detection is best-effort and isolated: a wrong SCHEDULE_TYPE_FIELD
    # (the Odoo "Schedule Type" field, confirmed at rollout) must NOT break the
    # whole employee/skill/department sync. On failure, degrade to "no flex" and
    # log loudly — auto-lunch simply won't treat anyone as flexible until fixed.
    try:
        flex_cal_ids = {
            c["id"] for c in odoo_client.fetch_work_schedules() if c.get("is_flexible")
        }
    except Exception:
        log.exception(
            "auto-lunch flex detection failed (check odoo_client.SCHEDULE_TYPE_FIELD); "
            "treating all employees as non-flex this sync"
        )
        flex_cal_ids = set()

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
            skill_odoo_id = m.get("id")
            if skill_odoo_id is not None:
                _merge_legacy_skill_name_collision(
                    cur,
                    skill_odoo_id=int(skill_odoo_id),
                    skill_name=m["name"],
                )
                # Older rows were keyed only by name. Attach those rows to the
                # Odoo id first so future Odoo renames update by stable id.
                cur.execute(
                    "UPDATE skills SET odoo_id = %s, skill_type = %s, "
                    "sort_order = %s, last_pulled_at = %s "
                    "WHERE name = %s AND odoo_id IS NULL",
                    (skill_odoo_id, m.get("type", ""), i, pulled_at, m["name"]),
                )
                cur.execute(
                    "INSERT INTO skills (odoo_id, name, skill_type, sort_order, last_pulled_at) "
                    "VALUES (%s, %s, %s, %s, %s) "
                    "ON CONFLICT (odoo_id) DO UPDATE SET name = EXCLUDED.name, "
                    "skill_type = EXCLUDED.skill_type, "
                    "sort_order = EXCLUDED.sort_order, last_pulled_at = EXCLUDED.last_pulled_at",
                    (skill_odoo_id, m["name"], m.get("type", ""), i, pulled_at),
                )
            else:
                cur.execute(
                    "INSERT INTO skills (name, skill_type, sort_order, last_pulled_at) "
                    "VALUES (%s, %s, %s, %s) "
                    "ON CONFLICT (name) DO UPDATE SET skill_type = EXCLUDED.skill_type, "
                    "sort_order = EXCLUDED.sort_order, last_pulled_at = EXCLUDED.last_pulled_at",
                    (m["name"], m.get("type", ""), i, pulled_at),
                )
        cur.execute(
            "UPDATE skills SET skill_type = 'Legacy Skills', last_pulled_at = %s "
            "WHERE odoo_id IS NULL "
            "AND skill_type IN ('Production Skills', 'Supervisor Skills') "
            "AND NOT (name = ANY(%s))",
            (pulled_at, columns),
        )
        # Employees: upsert by odoo_id (stable across renames).
        seen_employee_ids = set()
        for emp in employees:
            seen_employee_ids.add(emp["id"])
            # Odoo selection fields return False when unset; normalize to None.
            wage_type = emp.get("wage_type") or None
            spanish_speaker = emp["id"] in spanish_ids
            is_flex = _m2o_id(emp.get("resource_calendar_id")) in flex_cal_ids
            cur.execute(
                "INSERT INTO people (odoo_id, name, active, wage_type, spanish_speaker, "
                "resource_calendar_id, is_flexible, last_pulled_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) "
                "ON CONFLICT (odoo_id) DO UPDATE SET name = EXCLUDED.name, "
                "active = EXCLUDED.active, wage_type = EXCLUDED.wage_type, "
                "spanish_speaker = EXCLUDED.spanish_speaker, "
                "resource_calendar_id = EXCLUDED.resource_calendar_id, "
                "is_flexible = EXCLUDED.is_flexible, "
                "last_pulled_at = EXCLUDED.last_pulled_at",
                (emp["id"], _short_name(emp["name"]), bool(emp.get("active", True)),
                 wage_type, spanish_speaker, _m2o_id(emp.get("resource_calendar_id")),
                 is_flex, pulled_at),
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

    # Best-effort: refresh per-schedule rounding overrides' hours from Odoo.
    # A failure here must not fail the (already-committed) employee sync.
    try:
        refresh_work_schedule_hours()
    except Exception:
        log.exception("refresh_work_schedule_hours failed during sync")

    return SyncResult(
        ok=True, refreshed=True, employee_count=len(employees),
        skill_column_count=len(columns), last_sync_at=pulled_at,
    )
