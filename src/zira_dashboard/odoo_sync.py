"""Odoo to roster.json sync with TTL cache.

Single public entrypoint: sync(force=False). Returns SyncResult.
On TTL hit (default 1 hour), no Odoo call is made and the existing
roster file is left alone. On force or stale, fetches employees + skills
from Odoo and atomically rewrites roster.json. The local `reserve` flag
on existing entries is preserved across syncs.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import odoo_client

ROSTER_PATH = Path("roster.json")
LAST_SYNC_PATH = Path(".odoo_last_sync")
SKILL_META_PATH = Path("skill_columns_meta.json")
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
    if not LAST_SYNC_PATH.exists():
        return None
    try:
        return datetime.fromisoformat(LAST_SYNC_PATH.read_text().strip())
    except (ValueError, OSError):
        return None


def _read_existing_reserves() -> dict[str, bool]:
    if not ROSTER_PATH.exists():
        return {}
    try:
        rows = json.loads(ROSTER_PATH.read_text())
    except (json.JSONDecodeError, OSError):
        return {}
    return {r["name"]: bool(r.get("reserve", False))
            for r in rows if isinstance(r, dict) and r.get("name")}


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
        columns_meta = odoo_client.fetch_skill_columns_with_types()
        buckets = odoo_client.fetch_skill_level_buckets()
    except Exception as e:  # OdooConfigError, OdooAuthError, network, etc.
        return SyncResult(
            ok=False, refreshed=False, employee_count=0,
            skill_column_count=0, last_sync_at=last, error=str(e),
        )

    columns = [c["name"] for c in columns_meta]
    reserves = _read_existing_reserves()
    rows = []
    for emp in employees:
        skills_for_emp = {col: 0 for col in columns}
        for s in emp_skills.get(emp["id"], []):
            if s["skill_name"] in skills_for_emp:
                skills_for_emp[s["skill_name"]] = buckets.get(s["level_id"], 0)
        rows.append({
            "name": emp["name"],
            "active": bool(emp.get("active", True)),
            "reserve": reserves.get(emp["name"], False),
            "skills": skills_for_emp,
            "employee_id": emp["id"],
        })
    rows.sort(key=lambda r: r["name"].lower())

    tmp = ROSTER_PATH.with_suffix(ROSTER_PATH.suffix + ".tmp")
    tmp.write_text(json.dumps(rows, indent=2))
    os.replace(tmp, ROSTER_PATH)
    # Write the skill columns metadata so the matrix can render type groups
    # in the filter UI without re-fetching from Odoo on every page load.
    meta_tmp = SKILL_META_PATH.with_suffix(SKILL_META_PATH.suffix + ".tmp")
    meta_tmp.write_text(json.dumps(columns_meta, indent=2))
    os.replace(meta_tmp, SKILL_META_PATH)
    LAST_SYNC_PATH.write_text(now.isoformat())

    return SyncResult(
        ok=True, refreshed=True, employee_count=len(rows),
        skill_column_count=len(columns), last_sync_at=now,
    )
