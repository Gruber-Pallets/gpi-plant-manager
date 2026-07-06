"""Plant work schedule: shift hours, work days, breaks. Persisted in the
`global_schedule` table (singleton row id=1).

Heavy callers (in_shift_on, shift_start_for, etc.) invoke current() in
hot loops — once per sample inside fetch_station_day, which itself runs
in 10-way parallel from the leaderboard. Without an in-process cache,
that's thousands of DB round trips per page render and the connection
pool exhausts. We cache the singleton in module state and invalidate
on save().
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import time

from ._singleton import CachedSingleton

WEEKDAY_NAMES = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


@dataclass(frozen=True)
class Break:
    start: time
    end: time
    name: str = "Break"


@dataclass(frozen=True)
class Schedule:
    shift_start: time
    shift_end: time
    work_weekdays: frozenset[int]   # 0=Mon .. 6=Sun
    breaks: tuple[Break, ...]

    @property
    def shift_len(self) -> float:
        """Shift length in decimal hours (e.g. 7:00–15:30 → 8.5). Used to
        tell a whole-day off-window from a genuine partial on the time-off
        calendars (see ``time_off_calendar.is_full_day``)."""
        start = self.shift_start.hour + self.shift_start.minute / 60.0
        end = self.shift_end.hour + self.shift_end.minute / 60.0
        return end - start


DEFAULT_SCHEDULE = Schedule(
    shift_start=time(7, 0),
    shift_end=time(15, 30),
    work_weekdays=frozenset({0, 1, 2, 3, 4}),
    breaks=(
        Break(time(9, 0), time(9, 15), "Morning break"),
        Break(time(11, 0), time(11, 30), "Lunch"),
        Break(time(13, 30), time(13, 45), "Afternoon break"),
        Break(time(15, 15), time(15, 30), "Cleanup"),
    ),
)


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


def _format_time(t: time) -> str:
    return f"{t.hour:02d}:{t.minute:02d}"


def _row_to_schedule(row: dict) -> Schedule:
    start = _parse_time(row.get("shift_start")) or DEFAULT_SCHEDULE.shift_start
    end = _parse_time(row.get("shift_end")) or DEFAULT_SCHEDULE.shift_end
    wd_raw = row.get("work_weekdays") or []
    wd = frozenset(int(x) for x in wd_raw if isinstance(x, int) and 0 <= x <= 6)
    if not wd:
        wd = DEFAULT_SCHEDULE.work_weekdays
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
    return Schedule(start, end, wd, tuple(brks))


def _load_from_db() -> Schedule:
    from . import db
    rows = db.query(
        "SELECT shift_start, shift_end, work_weekdays, breaks FROM global_schedule WHERE id = 1"
    )
    if not rows:
        return DEFAULT_SCHEDULE
    return _row_to_schedule(rows[0])


_store: CachedSingleton[Schedule] = CachedSingleton(_load_from_db)


def current() -> Schedule:
    """Return the singleton global_schedule. Cached in process memory after
    first read; invalidated on save(). Falls back to DEFAULT_SCHEDULE if the
    table has no row yet."""
    return _store.current()


def save(sched: Schedule) -> None:
    """Persist + invalidate the cache so the next current() call re-reads."""
    from . import db
    db.execute(
        "INSERT INTO global_schedule (id, shift_start, shift_end, work_weekdays, breaks, updated_at) "
        "VALUES (1, %s, %s, %s, %s::jsonb, now()) "
        "ON CONFLICT (id) DO UPDATE SET shift_start = EXCLUDED.shift_start, "
        "shift_end = EXCLUDED.shift_end, work_weekdays = EXCLUDED.work_weekdays, "
        "breaks = EXCLUDED.breaks, updated_at = now()",
        (
            sched.shift_start,
            sched.shift_end,
            sorted(sched.work_weekdays),
            json.dumps([
                {"start": _format_time(b.start), "end": _format_time(b.end), "name": b.name}
                for b in sched.breaks
            ]),
        ),
    )
    _store.set(sched)


def reload() -> Schedule:
    """Force a fresh read from Postgres, bypassing the cache."""
    return _store.reload()
