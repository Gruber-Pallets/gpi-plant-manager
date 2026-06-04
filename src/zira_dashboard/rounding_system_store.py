"""Named rounding systems + the department->system map, cached in-process.

A "rounding system" is a named set of the four rounding windows (e.g. "Plant
Operator", "Supervisor", "Transportation"). Each static department
(staffing.Location.department) maps to at most one system; an employee's punches
use the system of the department they work that day. Resolution at punch time
reads the in-process cache, never the DB — same rationale as rounding_store /
work_schedule_store.

Anything that doesn't resolve to a mapped department + existing system falls
back to the plant default (rounding_settings id=1, via rounding_store).
"""

from __future__ import annotations

from dataclasses import dataclass
from threading import RLock

from .rounding import RoundingSettings


@dataclass(frozen=True)
class RoundingSystem:
    id: int
    name: str
    rounding: RoundingSettings


_lock = RLock()
# (systems_by_id, windows_by_department)
_cache: tuple[dict[int, RoundingSystem], dict[str, RoundingSettings]] | None = None


def _load_from_db() -> tuple[dict[int, RoundingSystem], dict[str, RoundingSettings]]:
    from . import db
    sys_rows = db.query(
        "SELECT id, name, in_before_min, in_after_min, out_before_min, out_after_min "
        "FROM rounding_systems"
    )
    systems: dict[int, RoundingSystem] = {}
    for r in sys_rows:
        systems[int(r["id"])] = RoundingSystem(
            id=int(r["id"]),
            name=str(r["name"]),
            rounding=RoundingSettings(
                in_before_min=int(r["in_before_min"]),
                in_after_min=int(r["in_after_min"]),
                out_before_min=int(r["out_before_min"]),
                out_after_min=int(r["out_after_min"]),
            ),
        )
    map_rows = db.query(
        "SELECT department, system_id FROM department_rounding WHERE system_id IS NOT NULL"
    )
    by_dept: dict[str, RoundingSettings] = {}
    for r in map_rows:
        sysrec = systems.get(int(r["system_id"]))
        if sysrec is not None:
            by_dept[str(r["department"])] = sysrec.rounding
    return systems, by_dept


def _cached() -> tuple[dict[int, RoundingSystem], dict[str, RoundingSettings]]:
    global _cache
    with _lock:
        if _cache is None:
            _cache = _load_from_db()
        return _cache


def windows_for_department(department: str | None) -> RoundingSettings | None:
    """Rounding windows for a static department, or None if the department is
    unmapped or its system was deleted. Cache read — safe on the punch path."""
    if not department:
        return None
    return _cached()[1].get(department)


def all_systems() -> list[RoundingSystem]:
    """All systems, sorted by name (for the settings UI)."""
    return sorted(_cached()[0].values(), key=lambda s: s.name.lower())


def department_map() -> dict[str, int | None]:
    """{department: system_id or None} for every department_rounding row.
    Settings-UI helper — reads the DB directly (infrequent, not the punch path)."""
    from . import db
    rows = db.query("SELECT department, system_id FROM department_rounding")
    return {
        str(r["department"]): (int(r["system_id"]) if r["system_id"] is not None else None)
        for r in rows
    }


def add_system(name: str) -> None:
    name = (name or "").strip()[:80]
    if not name:
        return
    from . import db
    db.execute(
        "INSERT INTO rounding_systems (name) VALUES (%s) ON CONFLICT (name) DO NOTHING",
        (name,),
    )
    reload()


def save_system_windows(system_id: int, r: RoundingSettings) -> None:
    from . import db
    db.execute(
        "UPDATE rounding_systems SET in_before_min = %s, in_after_min = %s, "
        "out_before_min = %s, out_after_min = %s, updated_at = now() WHERE id = %s",
        (r.in_before_min, r.in_after_min, r.out_before_min, r.out_after_min, int(system_id)),
    )
    reload()


def rename_system(system_id: int, new_name: str) -> None:
    new_name = (new_name or "").strip()[:80]
    if not new_name:
        return
    from . import db
    db.execute(
        "UPDATE rounding_systems SET name = %s, updated_at = now() WHERE id = %s",
        (new_name, int(system_id)),
    )
    reload()


def delete_system(system_id: int) -> None:
    from . import db
    db.execute("DELETE FROM rounding_systems WHERE id = %s", (int(system_id),))
    reload()


def set_department_system(department: str, system_id: int | None) -> None:
    department = (department or "").strip()
    if not department:
        return
    from . import db
    db.execute(
        "INSERT INTO department_rounding (department, system_id) VALUES (%s, %s) "
        "ON CONFLICT (department) DO UPDATE SET system_id = EXCLUDED.system_id",
        (department, int(system_id) if system_id is not None else None),
    )
    reload()


def reload() -> tuple[dict[int, RoundingSystem], dict[str, RoundingSettings]]:
    """Force a fresh read from Postgres, bypassing the cache."""
    global _cache
    with _lock:
        _cache = _load_from_db()
        return _cache
