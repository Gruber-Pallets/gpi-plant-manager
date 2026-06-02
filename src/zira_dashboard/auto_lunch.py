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
from datetime import date, datetime, time, timedelta

from . import shift_config, db, live_cache, attendance_state, auto_lunch_settings, timeclock_sync

_log = logging.getLogger(__name__)

TERMINAL = ("done", "skipped", "ended_by_employee")


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


def _day_bounds(day: date) -> tuple[datetime, datetime]:
    start = datetime.combine(day, time.min, tzinfo=shift_config.SITE_TZ)
    return start, start + timedelta(days=1)


def _first_clock_in(person_odoo_id: int, day: date) -> datetime | None:
    """The person's earliest clock_in on `day` (their morning punch). Used as
    the flex elapsed-time anchor."""
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


def _get_run(person_odoo_id: int, day: date) -> dict | None:
    rows = db.query(
        "SELECT person_odoo_id, day, kind, state, target_out_at, target_in_at, "
        "wc_name, out_punch_id, in_punch_id FROM auto_lunch_runs "
        "WHERE person_odoo_id = %s AND day = %s",
        (person_odoo_id, day),
    )
    return rows[0] if rows else None


def _upsert_run(person_odoo_id, day, kind, state, *, target_out_at=None,
                target_in_at=None, wc_name=None, out_punch_id=None,
                in_punch_id=None) -> None:
    db.execute(
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
        "updated_at = now()",
        (person_odoo_id, day, kind, state, target_out_at, target_in_at,
         wc_name, out_punch_id, in_punch_id),
    )


def _write_auto_punch(person_odoo_id, action, wc_name, occurred_at) -> int:
    """Insert an auto-lunch punch stamped at the scheduled boundary time.
    source='auto_lunch'; rounded_at = occurred_at (it IS the schedule, no
    rounding). Returns the new log id; caller triggers the Odoo sync."""
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO timeclock_punches_log "
            "(person_odoo_id, action, wc_name, occurred_at, rounded_at, source) "
            "VALUES (%s, %s, %s, %s, %s, 'auto_lunch') RETURNING id",
            (person_odoo_id, action, wc_name, occurred_at, occurred_at),
        )
        return cur.fetchone()["id"]


def _window_for(person_odoo_id, kind, today, fixed_window, settings) -> Window | None:
    if kind == "scheduled":
        return fixed_window
    first_in = _first_clock_in(person_odoo_id, today)
    if first_in is None:
        return None
    return flex_window(first_in, settings.flex_after_hours, settings.flex_minutes)


def _apply(person_odoo_id, today, kind, run, t, state, window, settings) -> None:
    if t.action == "clock_out":
        wc_name = state["current_wc"]
        out_id = None
        if not settings.observe_only:
            out_id = _write_auto_punch(person_odoo_id, "clock_out", None, t.at)
            timeclock_sync.sync_one_by_id(out_id)
        _log.info("auto-lunch %s: person %s clock_out @ %s (wc=%s)",
                  "OBSERVE" if settings.observe_only else "LIVE",
                  person_odoo_id, t.at, wc_name)
        _upsert_run(person_odoo_id, today, kind, "auto_out",
                    target_out_at=window.out_at, target_in_at=window.in_at,
                    wc_name=wc_name, out_punch_id=out_id)
    elif t.action == "clock_in":
        wc_name = run["wc_name"] if run else None
        in_id = None
        if not settings.observe_only:
            in_id = _write_auto_punch(person_odoo_id, "clock_in", wc_name, t.at)
            timeclock_sync.sync_one_by_id(in_id)
        _log.info("auto-lunch %s: person %s clock_in @ %s (wc=%s)",
                  "OBSERVE" if settings.observe_only else "LIVE",
                  person_odoo_id, t.at, wc_name)
        _upsert_run(person_odoo_id, today, kind, "done", in_punch_id=in_id)
    else:
        _upsert_run(person_odoo_id, today, kind, t.new_state,
                    target_out_at=window.out_at, target_in_at=window.in_at)


def _advance_person(person_odoo_id, today, now, fixed_window, flex_ids, settings) -> None:
    kind = "flex" if person_odoo_id in flex_ids else "scheduled"
    window = _window_for(person_odoo_id, kind, today, fixed_window, settings)
    if window is None:
        return
    run = _get_run(person_odoo_id, today)
    run_state = run["state"] if run else "pending"
    if run_state in TERMINAL:
        return
    state = attendance_state.current_state(person_odoo_id)
    is_in = state["is_clocked_in"]
    # Observe-only simulation: we never actually clocked them out, so the real
    # state still reads clocked-in. Pretend clocked-out after an observed
    # auto_out so the auto sign-in is previewed too.
    if settings.observe_only and run_state == "auto_out":
        is_in = False
    t = decide(run_state, is_in, window, now)
    if t.new_state == run_state and t.action is None:
        return
    _apply(person_odoo_id, today, kind, run, t, state, window, settings)


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

    snapshot, refreshed_at = live_cache.read_open_attendance()
    if snapshot is None or live_cache.is_stale(refreshed_at):
        _log.info("auto-lunch: open-attendance cache missing/stale; skipping tick")
        return

    flex_ids = _flex_person_ids()
    clocked_in = {int(k) for k in snapshot.keys()}
    open_runs = {int(r["person_odoo_id"]) for r in db.query(
        "SELECT person_odoo_id FROM auto_lunch_runs WHERE day = %s "
        "AND state NOT IN ('done','skipped','ended_by_employee')", (today,))}
    for pid in clocked_in | open_runs:
        try:
            _advance_person(pid, today, now, fixed_window, flex_ids, settings)
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
