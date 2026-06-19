"""Odoo-era attendance + absence logic (replaces the StratusTime client's
attendance_for_day / full_day_absent_names_for_day / partial_off_intervals_for_day
/ derived_absences_for_day).

Pure cores take injected punch dicts + a fixed clock so they are testable
without mocking time or Odoo. Cache-backed wrappers call Odoo via live_cache.

Identity is ``str(person_odoo_id)`` throughout, matching the rest of the
Odoo-era stack and the string-keyed late_report helpers.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Iterable

GRACE_MINUTES = 7
ABSENT_BUFFER_MINUTES = 30


def compute_status(
    punches: dict,
    ids: Iterable[str],
    now_local: datetime,
    shift_start_local: datetime,
    grace_minutes: int = GRACE_MINUTES,
) -> dict:
    """Per-id attendance status. ``punches`` is {str_id: {first_check_in(iso UTC),
    currently_open(bool)}}. Returns {str_id: {status, minutes_late,
    clocked_in_at, currently_open}} for every id in ``ids``.

    status: no_punch | on_time | late | clocked_out. Judged on the FIRST
    check-in of the day (actual arrival). A punched-but-currently-out person
    is clocked_out; a currently-open person is on_time/late vs shift_start+grace.
    """
    from . import shift_config
    cutoff = shift_start_local + timedelta(minutes=grace_minutes)
    out: dict = {}
    for raw in ids:
        sid = str(raw)
        p = punches.get(sid)
        entry = {"status": "no_punch", "minutes_late": 0, "clocked_in_at": None, "currently_open": False}
        ci = (p or {}).get("first_check_in")
        if p and ci:
            ci_local = datetime.fromisoformat(ci).astimezone(shift_config.SITE_TZ)
            entry["clocked_in_at"] = ci_local.strftime("%I:%M %p").lstrip("0")
            entry["currently_open"] = bool(p.get("currently_open"))
            if not entry["currently_open"]:
                entry["status"] = "clocked_out"
            elif ci_local <= cutoff:
                entry["status"] = "on_time"
            else:
                entry["status"] = "late"
                entry["minutes_late"] = max(0, int((ci_local - shift_start_local).total_seconds() // 60))
        out[sid] = entry
    return out


def punches_for_day(day) -> dict:
    """Pull today's Odoo punches and key them by str(person_odoo_id).
    {str_id: {first_check_in, currently_open}}. This is what the live_cache
    warmer stores in today_attendance_cache."""
    from . import odoo_client
    rows = odoo_client.fetch_attendances_for_day(day)
    return {
        str(r["employee_odoo_id"]): {
            "first_check_in": r["first_check_in"],
            "currently_open": r["currently_open"],
        }
        for r in rows
    }


def status_for_day(day, ids, now_local, shift_start_local) -> dict:
    """Cache-aware status for ``ids`` on ``day``: read punches from live_cache
    (warmer-populated), fall back to a direct Odoo pull, then compute_status
    against the supplied clock so minutes_late stays fresh."""
    from . import live_cache
    payload, _refreshed = live_cache.read_attendance(day)
    if payload is None:
        payload = punches_for_day(day)
    return compute_status(payload or {}, ids, now_local, shift_start_local)


def name_to_person_id() -> dict:
    """{roster_name: str(person_odoo_id)} for active employees with an Odoo
    id. Replaces stratustime_client.name_to_emp_id_map. Names align with
    roster names (both from odoo_sync._short_name)."""
    from . import db
    rows = db.query(
        "SELECT name, odoo_id FROM people WHERE active = TRUE AND odoo_id IS NOT NULL"
    )
    return {r["name"]: str(r["odoo_id"]) for r in rows}


def person_id_to_name(name_to_id: dict | None = None) -> dict:
    """{str(person_odoo_id): roster_name} — the inverse of name_to_person_id().

    Pass an already-fetched ``name_to_id`` map to avoid a re-query; omit it to
    fetch one. Identity is ``str(person_odoo_id)`` throughout (see module
    docstring), so the returned keys are strings."""
    if name_to_id is None:
        name_to_id = name_to_person_id()
    return {v: k for k, v in name_to_id.items()}


def derived_absent_names(day) -> set:
    """Active, non-reserve roster people with NO Odoo punch by
    shift_start + ABSENT_BUFFER_MINUTES who are not on approved/pending
    time off. Today only (matches the old derived_absences_for_day) —
    past/future days return an empty set."""
    from datetime import datetime, timedelta
    from . import shift_config, staffing, scheduler_time_off, live_cache
    from . import plant_day
    if day != plant_day.today():
        return set()
    now_local = plant_day.now()
    shift_start_local = datetime.combine(day, shift_config.shift_start_for(day), tzinfo=shift_config.SITE_TZ)
    if now_local < shift_start_local + timedelta(minutes=ABSENT_BUFFER_MINUTES):
        return set()
    try:
        off = {e["name"] for e in scheduler_time_off.time_off_entries_for_day(day) if e.get("name")}
    except Exception:  # noqa: BLE001 — degrade to "nobody on leave"
        off = set()
    name_to_id = name_to_person_id()
    # Cache-first (same pattern as status_for_day): the warmer mirrors this
    # exact payload into live_cache every ~45s, so don't hit Odoo per render.
    punches, _refreshed = live_cache.read_attendance(day)
    if punches is None:
        punches = punches_for_day(day)
    punches = punches or {}
    out: set = set()
    for p in staffing.load_roster():
        if not p.active or p.reserve or p.name in off:
            continue
        sid = name_to_id.get(p.name)
        if sid is None:
            continue  # can't check punches without an Odoo id
        if sid not in punches:
            out.add(p.name)
    return out


def full_day_absent_names(day) -> set:
    """Roster names out for the WHOLE day: full-day approved/pending leave
    ∪ manually-declared absences ∪ derived no-shows. Partial-day people are
    excluded (their time is subtracted via partial_off_intervals instead).
    Replaces stratustime_client.full_day_absent_names_for_day. Never raises."""
    from . import scheduler_time_off, late_report
    out: set = set()
    try:
        out |= set(scheduler_time_off.full_day_off_names(day))
    except Exception:  # noqa: BLE001
        pass
    try:
        out |= set(late_report.absent_names_for_day(day))
    except Exception:  # noqa: BLE001
        pass
    try:
        out |= derived_absent_names(day)
    except Exception:  # noqa: BLE001
        pass
    return out


def partial_off_intervals(day) -> dict:
    """{roster_name: [(start_utc, end_utc), ...]} of partial-day off windows
    on ``day``, as tz-aware UTC datetimes for overlap math in
    staffing.effective_minutes_worked. Full-day shapes are excluded. Source
    is the Odoo time_off_requests mirror via scheduler_time_off._rows_for_day.
    Replaces stratustime_client.partial_off_intervals_for_day. Never raises."""
    from datetime import datetime, timezone, timedelta, time as _time
    from . import shift_config, scheduler_time_off
    out: dict = {}
    try:
        rows = scheduler_time_off._rows_for_day(day)
    except Exception:  # noqa: BLE001
        return out
    site_tz = shift_config.SITE_TZ
    for r in rows:
        if r.get("shape") == "full_day":
            continue
        hf = r.get("hour_from")
        ht = r.get("hour_to")
        if hf is None or ht is None:
            continue
        hf = float(hf)
        ht = float(ht)
        if ht <= hf:
            continue
        s_local = datetime.combine(day, _time(0, 0), tzinfo=site_tz) + timedelta(hours=hf)
        e_local = datetime.combine(day, _time(0, 0), tzinfo=site_tz) + timedelta(hours=ht)
        out.setdefault(r["name"], []).append(
            (s_local.astimezone(timezone.utc), e_local.astimezone(timezone.utc))
        )
    return out
