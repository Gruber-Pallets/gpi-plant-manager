"""Attendance + late-report data assembly for the staffing views.

I/O-backed with degrade-to-empty exception handling so a backend hiccup
yields an empty panel, not a 500. Extracted from routes/staffing.py.
"""

from __future__ import annotations

import time as _time
from datetime import datetime
from threading import RLock

from . import attendance, plant_day, shift_config, staffing

# One staffing render asks for the day's time-off entries 2-3 times
# (_safe_time_off_entries from the route plus _timeoff_names_with_fallback
# inside _safe_attendance, fired concurrently on the route's pool). Memoize
# the fetch for a few seconds so a render does ONE query and both shapes
# derive from it. The TTL is deliberately tiny: long enough to span a render,
# short enough that a save-then-reload round trip always re-reads.
_TIME_OFF_MEMO_TTL = 3.0
_time_off_memo: dict = {}  # day -> (monotonic_ts, entries)
_time_off_memo_lock = RLock()


def _time_off_entries_cached(day):
    """time_off_entries_for_day(day) with a render-scoped memo. The fetch
    runs under the lock on purpose (single-flight): the render's concurrent
    callers would otherwise race the miss and re-query anyway. Errors
    propagate (and are never cached) so each caller keeps its own degrade
    behavior."""
    from . import scheduler_time_off
    now = _time.monotonic()
    with _time_off_memo_lock:
        hit = _time_off_memo.get(day)
        if hit is not None and now - hit[0] < _TIME_OFF_MEMO_TTL:
            return hit[1]
        entries = scheduler_time_off.time_off_entries_for_day(day)
        stale = [k for k, (ts, _v) in _time_off_memo.items()
                 if now - ts >= _TIME_OFF_MEMO_TTL]
        for k in stale:
            del _time_off_memo[k]
        _time_off_memo[day] = (_time.monotonic(), entries)
        return entries


def _live_or_fallback(day, *, read, refresh, fallback, transform):
    """Cold-start safety valve for live_cache lookups.

    Reads `read(day)`; if missing or stale, calls `refresh(day)` and re-reads;
    if still empty, returns `fallback()` (caller-provided direct fetch).
    Otherwise returns `transform(payload)`. The fallback already returns the
    caller's final shape, so it's not passed through `transform`.
    """
    from . import live_cache
    payload, refreshed_at = read(day)
    if payload is None or live_cache.is_stale(refreshed_at):
        try:
            refresh(day)
            payload, _ = read(day)
        except Exception:
            payload = None
        if payload is None:
            return fallback()
    return transform(payload)


def _safe_time_off_entries(d):
    """Time-off entries for the scheduler, sourced from the Odoo-backed
    time_off_requests mirror (approved + pending). Never raises — a query
    failure degrades to an empty panel rather than a 500."""
    try:
        return _time_off_entries_cached(d)
    except Exception:  # noqa: BLE001 — empty panel beats a broken scheduler
        return []


def _attendance_with_fallback(day, ids):
    """Return today's per-id Odoo punch dict, filtered to `ids`.

    The cache holds punches for ALL employees; we filter here so callers
    get exactly the subset they asked for. Keys are str(person_odoo_id);
    values are {first_check_in, currently_open}. _safe_attendance turns
    these into a status dict via attendance.compute_status.
    """
    from . import live_cache, attendance
    wanted = {str(i) for i in ids}
    return _live_or_fallback(
        day,
        read=live_cache.read_attendance,
        refresh=live_cache.refresh_attendance,
        fallback=lambda: attendance.punches_for_day(day),
        transform=lambda payload: {
            sid: info for sid, info in payload.items() if sid in wanted
        },
    )


def _timeoff_names_with_fallback(day):
    """Set of names off on ``day`` (full-day OR partial), from the Odoo-backed
    time_off_requests mirror. Used by _safe_attendance to excuse these people
    from the late/absence report — a partial (e.g. an approved late arrival)
    must still count as excused, so this returns ALL off names, not just the
    full-day ones the scheduler pool excludes."""
    try:
        return {
            e["name"]
            for e in _time_off_entries_cached(day)
            if e.get("name")
        }
    except Exception:  # noqa: BLE001 — degrade to "nobody excused" rather than 500
        return set()


def _safe_attendance(d, sched, today):
    """Wrap the Odoo attendance/status lookup. Returns
    {by_name, by_id, name_to_id, scheduled_ids, unscheduled_ids}.

    Returns empty dicts on any error or when attendance isn't applicable
    (not today, or before shift start). by_name keys are roster names;
    by_id keys are str(person_odoo_id) (used by late_report).

    Fetches attendance for both scheduled people AND active non-reserve
    people who weren't assigned to a WC today — so the Late/Absence
    Report can flag both groups.
    """
    empty = {
        "by_name": {}, "by_id": {}, "name_to_id": {},
        "scheduled_ids": [], "unscheduled_ids": [],
    }
    if d != today:
        return empty
    try:
        now_local = plant_day.now()
        shift_start_local = datetime.combine(
            d, shift_config.shift_start_for(d), tzinfo=shift_config.SITE_TZ
        )
        if now_local < shift_start_local:
            return empty
        name_to_id = attendance.name_to_person_id()
        scheduled_names: set[str] = set()
        for ops in sched.assignments.values():
            for n in (ops or []):
                if n:
                    scheduled_names.add(n)

        # Anyone with an active Odoo time-off entry today —
        # full-day or partial — is officially excused. They don't
        # belong on the late/absence report. Drop them from both
        # scheduled and unscheduled lists before fetching attendance.
        try:
            time_off_today = _timeoff_names_with_fallback(d)
        except Exception:
            time_off_today = set()
        scheduled_names = {n for n in scheduled_names if n not in time_off_today}

        scheduled_ids = [name_to_id[n] for n in scheduled_names if n in name_to_id]

        # Unscheduled = active non-reserve people not in scheduled_names
        # and not on time off (matches the /staffing left-rail
        # "Unscheduled" definition).
        roster = staffing.load_roster()
        unscheduled_names = [
            p.name for p in roster
            if p.active
            and not p.reserve
            and p.name not in scheduled_names
            and p.name not in time_off_today
        ]
        unscheduled_ids = [name_to_id[n] for n in unscheduled_names if n in name_to_id]

        all_ids = list({*scheduled_ids, *unscheduled_ids})
        id_to_name = attendance.person_id_to_name(name_to_id)
        punches = _attendance_with_fallback(d, all_ids)
        attendance_by_id = attendance.compute_status(
            punches, all_ids, now_local, shift_start_local
        )
        by_name: dict[str, dict] = {}
        for emp_id, info in attendance_by_id.items():
            name = id_to_name.get(emp_id)
            if name:
                by_name[name] = info
        return {
            "by_name": by_name,
            "by_id": attendance_by_id,
            "name_to_id": name_to_id,
            "scheduled_ids": scheduled_ids,
            "unscheduled_ids": unscheduled_ids,
        }
    except Exception:
        return empty


def _late_emp_ids(d, today, attendance_pkg) -> set[str]:
    """Compute the set of currently-late person ids for `d`.

    Uses the same threshold + filtering as the Late/Absence Report so the
    scheduler highlight stays in sync with the global modal.
    """
    if d != today or not attendance_pkg.get("by_id"):
        return set()
    try:
        from . import late_report
        now_local = plant_day.now()
        shift_start_local = datetime.combine(
            d, shift_config.shift_start_for(d), tzinfo=shift_config.SITE_TZ
        )
        # Same eligibility filter as the report (GET /api/late-report): only
        # hourly, fixed-schedule people. Keeps the scheduler highlight in sync
        # with the modal — flex/salaried people no longer light up red.
        eligible = late_report.report_eligible_emp_ids(
            staffing.load_roster(), attendance_pkg.get("name_to_id") or {}
        )
        scheduled_ids = [
            e for e in (attendance_pkg.get("scheduled_ids") or []) if e in eligible
        ]
        late = late_report.late_people_for_day(
            d,
            scheduled_ids,
            attendance_pkg.get("by_id") or {},
            now_local,
            shift_start_local,
        )
        return {r["emp_id"] for r in late}
    except Exception:
        return set()
