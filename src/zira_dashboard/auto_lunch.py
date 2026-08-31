"""Auto-lunch worker: sign employees out for lunch and back in, creating the
unpaid gap in Odoo. Fixed schedules use the day's Lunch break; Odoo-flexible
schedules trigger on elapsed time since first clock-in. One lunch per day.

The decision logic (decide / lunch_window_for_day / flex_window) is pure and
unit-testable. run_tick() wires the I/O (settings, schedule, open-attendance
cache, punch log) around it.

See docs/superpowers/specs/2026-06-02-auto-lunch-timeclock-design.md.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta

from . import shift_config, db, live_cache, attendance_state, auto_lunch_settings, timeclock_sync

_log = logging.getLogger(__name__)

TERMINAL = ("done", "skipped", "ended_by_employee")

# Shared "not supplied" sentinel — the same object attendance_state.current_state
# uses, so _advance_person can forward it untouched.
_UNSET = attendance_state._UNSET


@dataclass(frozen=True)
class Window:
    out_at: datetime
    in_at: datetime


@dataclass(frozen=True)
class Transition:
    new_state: str
    action: str | None  # None | 'clock_out' | 'clock_in'
    at: datetime | None = None


def lunch_window_for_day(breaks: tuple, day: date) -> Window | None:
    """(out_at, in_at) for the break named 'lunch' on `day` in site-local tz,
    or None if there's no lunch break. `breaks` is shift_config.breaks_for(day)."""
    for b in breaks:
        if (getattr(b, "name", "") or "").strip().lower() == "lunch":
            out_at = datetime.combine(day, b.start, tzinfo=shift_config.SITE_TZ)
            in_at = datetime.combine(day, b.end, tzinfo=shift_config.SITE_TZ)
            return Window(out_at, in_at)
    return None


def flex_window(first_clock_in: datetime, after_hours: float, minutes: int) -> Window:
    out_at = first_clock_in + timedelta(hours=float(after_hours))
    in_at = out_at + timedelta(minutes=int(minutes))
    return Window(out_at, in_at)


def _parse_hhmm(value) -> time | None:
    if isinstance(value, time):
        return value
    if not isinstance(value, str):
        return None
    try:
        hh, mm = value.split(":")[:2]
        return time(int(hh), int(mm))
    except (TypeError, ValueError):
        return None


def fixed_windows_for_people(
    day: date,
    person_ids,
    app_window: Window | None,
    people_calendars: dict[int, int | None],
    odoo_lunches: dict,
) -> dict[int, Window | None]:
    """Resolve fixed-schedule lunch windows per person.

    If Odoo has a lunch row for the employee's resource calendar on this
    weekday, use it. Odoo computes attendance worked/overtime hours, so an
    app-only custom lunch can make Odoo subtract lunch from the wrong row.
    """
    out: dict[int, Window | None] = {}
    weekday = str(day.weekday())
    for raw_pid in person_ids:
        pid = int(raw_pid)
        window = app_window
        cal_id = people_calendars.get(pid)
        if cal_id is not None:
            lunch = (odoo_lunches.get(int(cal_id)) or {}).get(weekday)
            if lunch and len(lunch) == 2:
                lunch_start = _parse_hhmm(lunch[0])
                lunch_end = _parse_hhmm(lunch[1])
                if lunch_start is not None and lunch_end is not None and lunch_end > lunch_start:
                    window = Window(
                        datetime.combine(day, lunch_start, tzinfo=shift_config.SITE_TZ),
                        datetime.combine(day, lunch_end, tzinfo=shift_config.SITE_TZ),
                    )
                    if app_window is not None and window != app_window:
                        _log.warning(
                            "auto-lunch: using Odoo calendar lunch %s-%s for person %s "
                            "instead of app lunch %s-%s",
                            lunch_start, lunch_end, pid,
                            app_window.out_at.timetz(), app_window.in_at.timetz(),
                        )
        out[pid] = window
    return out


def decide(run_state: str, is_clocked_in: bool, window: Window, now: datetime) -> Transition:
    """One state-machine step. See the spec's Part-5 table."""
    if run_state == "pending":
        if now >= window.out_at:
            if is_clocked_in:
                return Transition("auto_out", "clock_out", window.out_at)
            return Transition("skipped", None)
        return Transition("pending", None)
    if run_state == "auto_out":
        if now >= window.in_at:
            if not is_clocked_in:
                return Transition("done", "clock_in", window.in_at)
            return Transition("done", None)
        return Transition("auto_out", None)
    return Transition(run_state, None)


# ---------- I/O ----------

def _flex_person_ids() -> set[int]:
    rows = db.query(
        "SELECT odoo_id FROM people "
        "WHERE is_flexible = TRUE AND active = TRUE AND odoo_id IS NOT NULL"
    )
    return {int(r["odoo_id"]) for r in rows}


def _calendar_ids_for_people(person_ids) -> dict[int, int | None]:
    ids = [int(i) for i in person_ids]
    if not ids:
        return {}
    rows = db.query(
        "SELECT odoo_id, resource_calendar_id FROM people "
        "WHERE odoo_id = ANY(%s)",
        (ids,),
    )
    return {int(r["odoo_id"]): r.get("resource_calendar_id") for r in rows}


def _day_bounds(day: date) -> tuple[datetime, datetime]:
    start = datetime.combine(day, time.min, tzinfo=shift_config.SITE_TZ)
    return start, start + timedelta(days=1)


def _first_clock_in(
    person_odoo_id: int, day: date, *, source=None
) -> datetime | None:
    """The person's earliest clock_in on `day` (their morning punch). Used as
    the flex elapsed-time anchor."""
    source = source or live_cache.read_attendance_source(day)
    if source.mirror_owned:
        entry = (source.payload or {}).get(str(person_odoo_id))
        raw = entry.get("first_check_in") if entry else None
        return datetime.fromisoformat(raw) if raw else None
    start, end = _day_bounds(day)
    rows = db.query(
        "SELECT MIN(COALESCE(rounded_at, occurred_at)) AS first_in "
        "FROM timeclock_punches_log WHERE person_odoo_id = %s "
        "AND action = 'clock_in' "
        "AND COALESCE(rounded_at, occurred_at) >= %s "
        "AND COALESCE(rounded_at, occurred_at) < %s",
        (person_odoo_id, start, end),
    )
    return rows[0]["first_in"] if rows and rows[0]["first_in"] else None


def _latest_in_wc(person_odoo_id: int, day: date, *, source=None) -> str | None:
    """Work center on the employee's most recent clock_in/transfer_in punch
    today, from the local log. Fallback for the sign-out WC capture when the
    reconciled state carries no work center (e.g. the open-attendance cache
    doesn't surface it) — so the auto sign-in still restores the WC and its
    Kiosk Department, matching the regular timeclock."""
    source = source or live_cache.read_open_attendance_source()
    if source.mirror_owned:
        entry = (source.payload or {}).get(str(person_odoo_id))
        return entry.get("wc_name") if entry else None
    start, end = _day_bounds(day)
    rows = db.query(
        "SELECT wc_name FROM timeclock_punches_log "
        "WHERE person_odoo_id = %s AND action IN ('clock_in', 'transfer_in') "
        "AND wc_name IS NOT NULL "
        "AND COALESCE(rounded_at, occurred_at) >= %s "
        "AND COALESCE(rounded_at, occurred_at) < %s "
        "ORDER BY COALESCE(rounded_at, occurred_at) DESC, id DESC LIMIT 1",
        (person_odoo_id, start, end),
    )
    return rows[0]["wc_name"] if rows and rows[0]["wc_name"] else None


def _legacy_attendance_inputs_bulk(
    person_ids, day: date
) -> tuple[dict[int, datetime | None], dict[int, str | None]]:
    """Fetch each candidate's flex anchor and latest work center in one query."""
    ids = [int(person_id) for person_id in person_ids]
    if not ids:
        return {}, {}
    start, end = _day_bounds(day)
    rows = db.query(
        "SELECT person_odoo_id, "
        "MIN(COALESCE(rounded_at, occurred_at)) "
        "  FILTER (WHERE action = 'clock_in') AS first_in, "
        "(ARRAY_AGG(wc_name ORDER BY COALESCE(rounded_at, occurred_at) DESC, id DESC) "
        "  FILTER (WHERE action IN ('clock_in', 'transfer_in') "
        "          AND wc_name IS NOT NULL))[1] AS latest_wc "
        "FROM timeclock_punches_log "
        "WHERE person_odoo_id = ANY(%s) "
        "  AND action IN ('clock_in', 'transfer_in') "
        "  AND COALESCE(rounded_at, occurred_at) >= %s "
        "  AND COALESCE(rounded_at, occurred_at) < %s "
        "GROUP BY person_odoo_id",
        (ids, start, end),
    )
    first_clock_ins = {
        int(row["person_odoo_id"]): row.get("first_in") for row in rows
    }
    latest_in_wcs = {
        int(row["person_odoo_id"]): row.get("latest_wc") for row in rows
    }
    return first_clock_ins, latest_in_wcs


def _get_run(person_odoo_id: int, day: date) -> dict | None:
    rows = db.query(
        "SELECT person_odoo_id, day, kind, state, target_out_at, target_in_at, "
        "wc_name, out_punch_id, in_punch_id FROM auto_lunch_runs "
        "WHERE person_odoo_id = %s AND day = %s",
        (person_odoo_id, day),
    )
    return rows[0] if rows else None


def _get_runs_bulk(day: date, person_ids) -> dict[int, dict]:
    """_get_run for many people in ONE query: {person_odoo_id: run_row}.
    People with no run row are absent from the map. Used by run_tick so a
    sweep doesn't run one query per candidate."""
    ids = [int(i) for i in person_ids]
    if not ids:
        return {}
    rows = db.query(
        "SELECT person_odoo_id, day, kind, state, target_out_at, target_in_at, "
        "wc_name, out_punch_id, in_punch_id FROM auto_lunch_runs "
        "WHERE day = %s AND person_odoo_id = ANY(%s)",
        (day, ids),
    )
    return {int(r["person_odoo_id"]): r for r in rows}


def _upsert_run(person_odoo_id, day, kind, state, *, target_out_at=None,
                target_in_at=None, wc_name=None, out_punch_id=None,
                in_punch_id=None, cur=None) -> None:
    """Upsert the per-person/day run row. Pass an open `cur` to run inside an
    existing transaction (so the run-state change commits atomically with the
    punch insert); omit it for a standalone write."""
    sql = (
        "INSERT INTO auto_lunch_runs (person_odoo_id, day, kind, state, "
        "target_out_at, target_in_at, wc_name, out_punch_id, in_punch_id, updated_at) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s, now()) "
        "ON CONFLICT (person_odoo_id, day) DO UPDATE SET "
        "kind = EXCLUDED.kind, state = EXCLUDED.state, "
        "target_out_at = COALESCE(EXCLUDED.target_out_at, auto_lunch_runs.target_out_at), "
        "target_in_at  = COALESCE(EXCLUDED.target_in_at,  auto_lunch_runs.target_in_at), "
        "wc_name       = COALESCE(EXCLUDED.wc_name,       auto_lunch_runs.wc_name), "
        "out_punch_id  = COALESCE(EXCLUDED.out_punch_id,  auto_lunch_runs.out_punch_id), "
        "in_punch_id   = COALESCE(EXCLUDED.in_punch_id,   auto_lunch_runs.in_punch_id), "
        "updated_at = now()"
    )
    params = (person_odoo_id, day, kind, state, target_out_at, target_in_at,
              wc_name, out_punch_id, in_punch_id)
    if cur is not None:
        cur.execute(sql, params)
    else:
        db.execute(sql, params)


def _write_auto_punch(person_odoo_id, action, wc_name, occurred_at, *, cur) -> int:
    """Insert an auto-lunch punch stamped at the scheduled boundary time, using
    the caller's open cursor `cur` so it commits atomically with the run-state
    update (see _apply). source='auto_lunch'; rounded_at = occurred_at (it IS
    the schedule, no rounding). Callers pass wc_name=None for a clock_out
    (matching the kiosk convention); the work center to restore is kept on the
    auto_lunch_runs row and supplied on the clock_in. Returns the new log id."""
    cur.execute(
        "INSERT INTO timeclock_punches_log "
        "(person_odoo_id, action, wc_name, occurred_at, rounded_at, source) "
        "VALUES (%s, %s, %s, %s, %s, 'auto_lunch') RETURNING id",
        (person_odoo_id, action, wc_name, occurred_at, occurred_at),
    )
    return cur.fetchone()["id"]


def _stored_window(run) -> Window | None:
    if not run:
        return None
    out_at = run.get("target_out_at")
    in_at = run.get("target_in_at")
    if out_at is None or in_at is None or in_at <= out_at:
        return None
    return Window(out_at, in_at)


def _window_for(
    person_odoo_id,
    kind,
    today,
    fixed_window,
    settings,
    *,
    run=None,
    first_clock_in=_UNSET,
) -> Window | None:
    stored = _stored_window(run)
    if stored is not None:
        return stored
    if kind == "scheduled":
        return fixed_window
    first_in = (
        _first_clock_in(person_odoo_id, today)
        if first_clock_in is _UNSET
        else first_clock_in
    )
    if first_in is None:
        return None
    return flex_window(first_in, settings.flex_after_hours, settings.flex_minutes)


def _kind_for(person_odoo_id, run, flex_ids) -> str:
    return run["kind"] if run else ("flex" if person_odoo_id in flex_ids else "scheduled")


def _fixed_windows_for_candidates(today, person_ids, app_fixed_window):
    ids = {int(i) for i in person_ids}
    if not ids:
        return {}
    people_calendars = _calendar_ids_for_people(ids)
    cal_ids = {int(c) for c in people_calendars.values() if c is not None}
    no_calendar_ids = {pid for pid in ids if people_calendars.get(pid) is None}
    windows = {pid: app_fixed_window for pid in no_calendar_ids}
    if not cal_ids:
        return windows
    try:
        from . import odoo_client
        odoo_lunches = odoo_client.fetch_calendar_lunch_windows(cal_ids)
    except Exception as e:  # noqa: BLE001 - safer to skip than split against unknown Odoo lunch
        _log.warning("auto-lunch: could not verify Odoo lunch windows; skipping fixed-calendar people: %s", e)
        windows.update({pid: None for pid in ids - no_calendar_ids})
        return windows
    windows.update(fixed_windows_for_people(
        today, ids - no_calendar_ids, app_fixed_window, people_calendars, odoo_lunches))
    return windows


def _apply(
    person_odoo_id,
    today,
    kind,
    run,
    t,
    state,
    window,
    settings,
    *,
    source=None,
    latest_in_wc=_UNSET,
) -> None:
    if t.action == "clock_out":
        # Capture the WC to restore at sign-in. Prefer the reconciled current
        # WC; fall back to the WC on the employee's own latest in-punch so the
        # afternoon record still carries the work center (and its Kiosk
        # Department) even when the open-attendance cache doesn't surface it.
        wc_name = state["current_wc"]
        if not wc_name:
            wc_name = (
                _latest_in_wc(person_odoo_id, today, source=source)
                if latest_in_wc is _UNSET
                else latest_in_wc
            )
        _log.info("auto-lunch %s: person %s clock_out @ %s (wc=%s)",
                  "OBSERVE" if settings.observe_only else "LIVE",
                  person_odoo_id, t.at, wc_name)
        if settings.observe_only:
            _upsert_run(person_odoo_id, today, kind, "auto_out",
                        target_out_at=window.out_at, target_in_at=window.in_at,
                        wc_name=wc_name)
            return
        # Punch insert + run->auto_out commit in ONE transaction, so a crash
        # between them can't strand the employee (signed out with no owed auto
        # sign-in). The Odoo sync runs after the local commit; if it lags, the
        # 60s sweep lands it and the run still owes the auto sign-in.
        with db.cursor() as cur:
            out_id = _write_auto_punch(person_odoo_id, "clock_out", None, t.at, cur=cur)
            _upsert_run(person_odoo_id, today, kind, "auto_out",
                        target_out_at=window.out_at, target_in_at=window.in_at,
                        wc_name=wc_name, out_punch_id=out_id, cur=cur)
        timeclock_sync.sync_one_by_id(out_id)
    elif t.action == "clock_in":
        wc_name = run["wc_name"] if run else None
        _log.info("auto-lunch %s: person %s clock_in @ %s (wc=%s)",
                  "OBSERVE" if settings.observe_only else "LIVE",
                  person_odoo_id, t.at, wc_name)
        if settings.observe_only:
            _upsert_run(person_odoo_id, today, kind, "done")
            return
        with db.cursor() as cur:
            in_id = _write_auto_punch(person_odoo_id, "clock_in", wc_name, t.at, cur=cur)
            _upsert_run(person_odoo_id, today, kind, "done",
                        in_punch_id=in_id, cur=cur)
        timeclock_sync.sync_one_by_id(in_id)
    else:
        _upsert_run(person_odoo_id, today, kind, t.new_state,
                    target_out_at=window.out_at, target_in_at=window.in_at)


def _advance_person(person_odoo_id, today, now, fixed_window, flex_ids, settings, *,
                    run=_UNSET, snapshot=None, refreshed_at=None, latest=_UNSET,
                    first_clock_in=_UNSET, source=None,
                    latest_in_wc=_UNSET) -> None:
    # run_tick passes the per-person inputs it already batch-read (run row,
    # open-attendance snapshot, latest punch); when omitted, each is fetched
    # here so the per-person behavior is unchanged for direct callers.
    if run is _UNSET:
        run = _get_run(person_odoo_id, today)
    # Classify once: a run's kind is fixed when the row is first created, so a
    # mid-day is_flexible change in Odoo can't reclassify an in-progress run.
    kind = run["kind"] if run else ("flex" if person_odoo_id in flex_ids else "scheduled")
    window = _window_for(
        person_odoo_id,
        kind,
        today,
        fixed_window,
        settings,
        run=run,
        first_clock_in=first_clock_in,
    )
    if window is None:
        return
    run_state = run["state"] if run else "pending"
    if run_state in TERMINAL:
        return
    state = attendance_state.current_state(
        person_odoo_id,
        snapshot=snapshot,
        refreshed_at=refreshed_at,
        latest=latest,
        source=source,
    )
    is_in = state["is_clocked_in"]
    # Observe-only simulation: we never actually clocked them out, so the real
    # state still reads clocked-in. Pretend clocked-out after an observed
    # auto_out so the auto sign-in is previewed too.
    if settings.observe_only and run_state == "auto_out":
        is_in = False
    t = decide(run_state, is_in, window, now)
    if t.new_state == run_state and t.action is None:
        return
    _apply(
        person_odoo_id,
        today,
        kind,
        run,
        t,
        state,
        window,
        settings,
        source=source,
        latest_in_wc=latest_in_wc,
    )


def run_tick(now: datetime | None = None) -> None:
    """One worker sweep. Safe to call every ~60s. No-op when disabled or when
    the open-attendance cache is missing/stale (never act on unknown state)."""
    settings = auto_lunch_settings.current()
    if not settings.enabled:
        return
    now = (now or datetime.now(shift_config.SITE_TZ)).astimezone(shift_config.SITE_TZ)
    today = now.date()

    fixed_window = None
    if shift_config.is_workday(today):
        fixed_window = lunch_window_for_day(shift_config.breaks_for(today), today)

    policy = live_cache.attendance_read_policy(now_utc=now.astimezone(UTC))
    source = live_cache.read_open_attendance_source(policy=policy)
    snapshot, refreshed_at = source.payload, source.refreshed_at
    source_stale = (
        bool(getattr(source, "stale", False))
        if source.mirror_owned
        else live_cache.is_stale(refreshed_at)
    )
    if not source.available or snapshot is None or source_stale:
        _log.info("auto-lunch: open-attendance cache missing/stale; skipping tick")
        return

    flex_ids = _flex_person_ids()
    clocked_in = {int(k) for k in snapshot.keys()}
    open_runs = {int(r["person_odoo_id"]) for r in db.query(
        "SELECT person_odoo_id FROM auto_lunch_runs WHERE day = %s "
        "AND state NOT IN ('done','skipped','ended_by_employee')", (today,))}
    candidates = clocked_in | open_runs
    salaried = {int(r["odoo_id"]) for r in db.query(
        "SELECT odoo_id FROM people WHERE wage_type = 'monthly' AND odoo_id IS NOT NULL")}
    candidates -= salaried  # auto-salaried punch owns their lunch (see auto_salaried.py)
    # Batch the per-person reads (run rows + latest punches) into one query
    # each, and reuse the snapshot read above — a sweep used to re-read all
    # three sources per person.
    runs = _get_runs_bulk(today, candidates)
    latest_by_pid = attendance_state.latest_punches_bulk(candidates)
    flex_candidates = {
        pid for pid in candidates if _kind_for(pid, runs.get(pid), flex_ids) == "flex"
    }
    first_clock_ins: dict[int, datetime | None] = {}
    latest_in_wcs: dict[int, str | None] = {}
    if source.mirror_owned and flex_candidates:
        day_source = live_cache.read_attendance_source(today, policy=policy)
        if not day_source.available or day_source.payload is None:
            _log.info("auto-lunch: day attendance mirror unavailable; skipping tick")
            return
        for pid in flex_candidates:
            entry = day_source.payload.get(str(pid)) or {}
            raw_first = entry.get("first_check_in")
            try:
                first_clock_ins[pid] = (
                    datetime.fromisoformat(raw_first) if raw_first else None
                )
            except (TypeError, ValueError):
                first_clock_ins[pid] = None
    if source.mirror_owned:
        for pid in candidates:
            entry = snapshot.get(str(pid)) or snapshot.get(pid) or {}
            latest_in_wcs[pid] = entry.get("wc_name")
    elif candidates:
        legacy_first_clock_ins, latest_in_wcs = _legacy_attendance_inputs_bulk(
            candidates, today
        )
        first_clock_ins = {
            pid: legacy_first_clock_ins.get(pid) for pid in flex_candidates
        }
    scheduled_candidates = {
        pid for pid in candidates
        if _kind_for(pid, runs.get(pid), flex_ids) == "scheduled"
    }
    fixed_windows = _fixed_windows_for_candidates(today, scheduled_candidates, fixed_window)
    for pid in candidates:
        try:
            _advance_person(pid, today, now, fixed_windows.get(pid, fixed_window), flex_ids, settings,
                            run=runs.get(pid), snapshot=snapshot,
                            refreshed_at=refreshed_at,
                            latest=latest_by_pid.get(pid),
                            first_clock_in=(
                                first_clock_ins.get(pid)
                                if pid in flex_candidates
                                else _UNSET
                            ),
                            latest_in_wc=latest_in_wcs.get(pid),
                            source=source)
        except Exception as e:  # noqa: BLE001 — one person never kills the tick
            _log.warning("auto-lunch: failed for person %s: %s", pid, e)


def active_lunch_run(person_odoo_id: int, now: datetime) -> dict | None:
    """The in-progress lunch gap for this person right now (state 'auto_out'
    and now within [target_out_at, target_in_at)), or None. The kiosk uses it
    to keep showing the on-shift 'sign out' action during the gap."""
    now = now.astimezone(shift_config.SITE_TZ)
    run = _get_run(person_odoo_id, now.date())
    if not run or run["state"] != "auto_out":
        return None
    out_at, in_at = run["target_out_at"], run["target_in_at"]
    if out_at is None or in_at is None:
        return None
    return run if out_at <= now < in_at else None


def note_employee_clock_out(person_odoo_id: int, now: datetime | None = None) -> bool:
    """Called when an employee signs out. If they're mid auto-lunch-gap, end
    their day here: cancel the pending auto sign-in. Returns True if a run was
    ended. Idempotent."""
    now = (now or datetime.now(shift_config.SITE_TZ)).astimezone(shift_config.SITE_TZ)
    if active_lunch_run(person_odoo_id, now) is None:
        return False
    db.execute(
        "UPDATE auto_lunch_runs SET state = 'ended_by_employee', updated_at = now() "
        "WHERE person_odoo_id = %s AND day = %s AND state = 'auto_out'",
        (person_odoo_id, now.date()),
    )
    return True
