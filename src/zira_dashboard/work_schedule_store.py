"""Per-work-schedule rounding overrides, cached in-process.

Each row mirrors one Odoo working schedule (resource.calendar) that has
been given its own punch-rounding windows. The shift boundaries
(`work_hours`) are synced FROM Odoo; the four rounding windows are owned by
the app (set on the settings page). Resolution at punch time reads the
in-process cache, so it never hits the DB on the hot path — same rationale
as rounding_store / schedule_store.

A row's existence == an active override. Employees inherit a schedule's
rounding by being assigned that resource.calendar in Odoo
(people.resource_calendar_id); anyone else falls back to the plant default
(rounding_settings + global_schedule).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import time
from threading import RLock

from .rounding import RoundingSettings


@dataclass(frozen=True)
class WorkScheduleOverride:
    resource_calendar_id: int
    name: str
    work_hours: dict[int, tuple[time, time]]   # weekday 0=Mon..6=Sun -> (start, end)
    rounding: RoundingSettings


def _parse_time(s) -> time | None:
    if isinstance(s, time):
        return s
    if not isinstance(s, str):
        return None
    try:
        hh, mm = s.split(":")[:2]
        return time(int(hh), int(mm))
    except (ValueError, AttributeError):
        return None


def _parse_work_hours(raw) -> dict[int, tuple[time, time]]:
    """Convert JSONB {"0": ["05:45","14:30"], ...} into
    {0: (time(5,45), time(14,30)), ...}. Skips malformed entries."""
    out: dict[int, tuple[time, time]] = {}
    if not isinstance(raw, dict):
        return out
    for k, v in raw.items():
        try:
            wd = int(k)
        except (TypeError, ValueError):
            continue
        if not (0 <= wd <= 6) or not isinstance(v, (list, tuple)) or len(v) != 2:
            continue
        start = _parse_time(v[0])
        end = _parse_time(v[1])
        if start is None or end is None:
            continue
        out[wd] = (start, end)
    return out


def _row_to_override(row: dict) -> WorkScheduleOverride:
    return WorkScheduleOverride(
        resource_calendar_id=int(row["resource_calendar_id"]),
        name=str(row.get("name") or ""),
        work_hours=_parse_work_hours(row.get("work_hours")),
        rounding=RoundingSettings(
            in_before_min=int(row["in_before_min"]),
            in_after_min=int(row["in_after_min"]),
            out_before_min=int(row["out_before_min"]),
            out_after_min=int(row["out_after_min"]),
        ),
    )


_lock = RLock()
_cache: dict[int, WorkScheduleOverride] | None = None


def _load_from_db() -> dict[int, WorkScheduleOverride]:
    from . import db
    rows = db.query(
        "SELECT resource_calendar_id, name, work_hours, "
        "in_before_min, in_after_min, out_before_min, out_after_min "
        "FROM work_schedules"
    )
    return {int(r["resource_calendar_id"]): _row_to_override(r) for r in rows}


def _all_cached() -> dict[int, WorkScheduleOverride]:
    global _cache
    with _lock:
        if _cache is None:
            _cache = _load_from_db()
        return _cache


def get(resource_calendar_id: int) -> WorkScheduleOverride | None:
    """Override for an Odoo calendar id, or None. Cache read — safe on the
    punch hot path."""
    if resource_calendar_id is None:
        return None
    return _all_cached().get(int(resource_calendar_id))


def all_overrides() -> list[WorkScheduleOverride]:
    """All configured overrides, sorted by name (for the settings UI)."""
    return sorted(_all_cached().values(), key=lambda o: o.name.lower())


def create(resource_calendar_id: int, name: str = "") -> None:
    """Insert an override row (rounding all-zero) if it doesn't exist. Hours
    are filled by the next sync via refresh_synced()."""
    from . import db
    db.execute(
        "INSERT INTO work_schedules (resource_calendar_id, name) "
        "VALUES (%s, %s) ON CONFLICT (resource_calendar_id) DO NOTHING",
        (int(resource_calendar_id), (name or "")[:200]),
    )
    reload()


def save_rounding(resource_calendar_id: int, r: RoundingSettings) -> None:
    """Update ONLY the four rounding windows for one schedule. Leaves the
    Odoo-owned name + work_hours untouched. Inserts the row if missing."""
    from . import db
    db.execute(
        "INSERT INTO work_schedules "
        "(resource_calendar_id, name, in_before_min, in_after_min, "
        " out_before_min, out_after_min, updated_at) "
        "VALUES (%s, '', %s, %s, %s, %s, now()) "
        "ON CONFLICT (resource_calendar_id) DO UPDATE SET "
        "in_before_min = EXCLUDED.in_before_min, "
        "in_after_min = EXCLUDED.in_after_min, "
        "out_before_min = EXCLUDED.out_before_min, "
        "out_after_min = EXCLUDED.out_after_min, "
        "updated_at = now()",
        (int(resource_calendar_id), r.in_before_min, r.in_after_min,
         r.out_before_min, r.out_after_min),
    )
    reload()


def refresh_synced(resource_calendar_id: int, name: str, work_hours: dict) -> None:
    """Update ONLY the Odoo-owned name + work_hours + last_synced_at for an
    EXISTING override row. Leaves the app-owned rounding windows untouched.
    No-op if the override row doesn't exist (we don't auto-configure every
    Odoo calendar)."""
    from . import db
    db.execute(
        "UPDATE work_schedules SET name = %s, work_hours = %s::jsonb, "
        "last_synced_at = now() WHERE resource_calendar_id = %s",
        ((name or "")[:200], json.dumps(work_hours or {}), int(resource_calendar_id)),
    )
    reload()


def delete(resource_calendar_id: int) -> None:
    from . import db
    db.execute(
        "DELETE FROM work_schedules WHERE resource_calendar_id = %s",
        (int(resource_calendar_id),),
    )
    reload()


def reload() -> dict[int, WorkScheduleOverride]:
    """Force a fresh read from Postgres, bypassing the cache."""
    global _cache
    with _lock:
        _cache = _load_from_db()
        return _cache
