"""Plant Saturday default schedule: shift hours + breaks for occasional
Saturdays. Persisted in the `saturday_schedule` table (singleton row id=1).

Mirrors schedule_store: an in-process cache of the singleton, invalidated
on save(), because shift_config's per-day resolver reads current() in hot
loops (per-sample, per-bucket) on Saturdays.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import time

from ._singleton import CachedSingleton
from .schedule_store import Break, _parse_time, _format_time


@dataclass(frozen=True)
class SaturdaySchedule:
    shift_start: time
    shift_end: time
    breaks: tuple[Break, ...]


DEFAULT = SaturdaySchedule(
    shift_start=time(6, 0),
    shift_end=time(12, 0),
    breaks=(
        Break(time(8, 0), time(8, 15), "Morning break"),
        Break(time(10, 0), time(10, 30), "Lunch"),
    ),
)


def _row_to_schedule(row: dict) -> SaturdaySchedule:
    start = _parse_time(row.get("shift_start")) or DEFAULT.shift_start
    end = _parse_time(row.get("shift_end")) or DEFAULT.shift_end
    brks: list[Break] = []
    for b in (row.get("breaks") or []):
        if not isinstance(b, dict):
            continue
        bs = _parse_time(b.get("start"))
        be = _parse_time(b.get("end"))
        if not (bs and be) or be <= bs:
            continue
        name = str(b.get("name") or "Break")[:40]
        brks.append(Break(bs, be, name))
    brks.sort(key=lambda b: b.start)
    return SaturdaySchedule(start, end, tuple(brks))


def _load_from_db() -> SaturdaySchedule:
    from . import db
    rows = db.query(
        "SELECT shift_start, shift_end, breaks FROM saturday_schedule WHERE id = 1"
    )
    if not rows:
        return DEFAULT
    return _row_to_schedule(rows[0])


_store: CachedSingleton[SaturdaySchedule] = CachedSingleton(_load_from_db)


def current() -> SaturdaySchedule:
    """Cached singleton; DEFAULT until the first save() writes a row."""
    return _store.current()


def save(sched: SaturdaySchedule) -> None:
    from . import db
    db.execute(
        "INSERT INTO saturday_schedule (id, shift_start, shift_end, breaks, updated_at) "
        "VALUES (1, %s, %s, %s::jsonb, now()) "
        "ON CONFLICT (id) DO UPDATE SET shift_start = EXCLUDED.shift_start, "
        "shift_end = EXCLUDED.shift_end, breaks = EXCLUDED.breaks, updated_at = now()",
        (
            sched.shift_start,
            sched.shift_end,
            json.dumps([
                {"start": _format_time(b.start), "end": _format_time(b.end), "name": b.name}
                for b in sched.breaks
            ]),
        ),
    )
    _store.set(sched)


def reload() -> SaturdaySchedule:
    return _store.reload()
