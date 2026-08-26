"""Auto salaried punch worker: writes four hr.attendance punches per weekday
for every active fixed-wage (wage_type='monthly') employee, mimicking an
hourly employee's two-block day. Skips weekends, company holidays, and days
with approved leave; a reconciler cleans up when leave is approved after
punches exist.

The decision logic (due_slots / skip_reason / scheduled_at) is pure and
unit-testable. run_tick() / run_reconcile() wire the I/O around it.

See docs/superpowers/specs/2026-08-26-auto-salaried-punch-design.md.
"""
from __future__ import annotations

import logging
import os
from datetime import date, datetime, time, timedelta

from . import company_holidays, db, shift_config, timeclock_sync

_log = logging.getLogger(__name__)

SUSTAINING_WC = "Sustaining"

PUNCH_TIMES: dict[str, time] = {
    "morning_in": time(6, 0),
    "lunch_out": time(11, 0),
    "lunch_in": time(11, 30),
    "day_out": time(15, 30),
}
SLOT_ORDER: tuple[str, ...] = ("morning_in", "lunch_out", "lunch_in", "day_out")
SLOT_ACTION: dict[str, str] = {
    "morning_in": "clock_in",
    "lunch_out": "clock_out",
    "lunch_in": "clock_in",
    "day_out": "clock_out",
}

# Dry-run mode marks a slot done with this sentinel instead of a real
# timeclock_punches_log id, so simulation advances the scoreboard (no log
# spam every 60s) without writing punches.
SIMULATED_PUNCH_ID = 0

RECONCILE_LOOKBACK_DAYS = 7


def mode() -> str:
    """'dry_run' | 'live' | 'off'. Dry-run wins so a deploy with both vars
    set can never write real punches by accident."""
    if os.environ.get("AUTO_SALARIED_DRY_RUN") == "1":
        return "dry_run"
    if os.environ.get("AUTO_SALARIED_ENABLED") == "1":
        return "live"
    return "off"


def scheduled_at(day: date, slot: str) -> datetime:
    return datetime.combine(day, PUNCH_TIMES[slot], tzinfo=shift_config.SITE_TZ)


def skip_reason(day: date, *, is_company_holiday: bool,
                has_approved_leave: bool) -> str | None:
    if day.weekday() >= 5:
        return "weekend"
    if is_company_holiday:
        return "holiday"
    if has_approved_leave:
        return "approved_leave"
    return None


def due_slots(now: datetime, day: date, run: dict | None) -> list[str]:
    """Slots whose scheduled time has arrived and that haven't punched yet,
    in punch order. Catch-up after downtime falls out naturally: every
    overdue slot is returned at once, each backdated to its scheduled time
    by the caller."""
    done = run or {}
    return [
        s for s in SLOT_ORDER
        if done.get(f"{s}_punch_id") is None and now >= scheduled_at(day, s)
    ]


# ---------- I/O ----------

def _fixed_wage_ids() -> list[int]:
    rows = db.query(
        "SELECT odoo_id FROM people "
        "WHERE active = TRUE AND wage_type = 'monthly' AND odoo_id IS NOT NULL "
        "ORDER BY odoo_id"
    )
    return [int(r["odoo_id"]) for r in rows]


def _approved_leave_ids(day: date, person_ids: list[int]) -> set[int]:
    """People with approved (state='validate') leave overlapping `day`."""
    if not person_ids:
        return set()
    rows = db.query(
        "SELECT DISTINCT person_odoo_id FROM time_off_requests "
        "WHERE state = 'validate' AND date_from <= %s AND date_to >= %s "
        "AND person_odoo_id = ANY(%s)",
        (day, day, person_ids),
    )
    return {int(r["person_odoo_id"]) for r in rows}


def _get_runs_bulk(day: date, person_ids: list[int]) -> dict[int, dict]:
    if not person_ids:
        return {}
    rows = db.query(
        "SELECT * FROM auto_salaried_runs WHERE day = %s AND person_odoo_id = ANY(%s)",
        (day, person_ids),
    )
    return {int(r["person_odoo_id"]): r for r in rows}


def _day_bounds(day: date) -> tuple[datetime, datetime]:
    start = datetime.combine(day, time.min, tzinfo=shift_config.SITE_TZ)
    return start, start + timedelta(days=1)


def _foreign_punches_today(person_odoo_id: int, day: date) -> bool:
    """True when the person already has punches today NOT created by this
    worker — a manual Odoo/kiosk punch or a same-day promotion from hourly.
    Enrolling anyway would double their morning."""
    start, end = _day_bounds(day)
    rows = db.query(
        "SELECT 1 FROM timeclock_punches_log WHERE person_odoo_id = %s "
        "AND COALESCE(source, '') <> 'auto_salaried' "
        "AND COALESCE(rounded_at, occurred_at) >= %s "
        "AND COALESCE(rounded_at, occurred_at) < %s LIMIT 1",
        (person_odoo_id, start, end),
    )
    return bool(rows)


def _flag(person_odoo_id: int, day: date, reason: str, details: str, *, cur=None) -> None:
    sql = (
        "INSERT INTO auto_salaried_flags (person_odoo_id, day, reason, details) "
        "VALUES (%s, %s, %s, %s) ON CONFLICT (person_odoo_id, day, reason) DO NOTHING"
    )
    params = (person_odoo_id, day, reason, details[:500])
    if cur is not None:
        cur.execute(sql, params)
    else:
        db.execute(sql, params)


def _insert_skipped_run(person_odoo_id: int, day: date, reason: str) -> None:
    db.execute(
        "INSERT INTO auto_salaried_runs (person_odoo_id, day, skipped, skip_reason) "
        "VALUES (%s, %s, TRUE, %s) ON CONFLICT (person_odoo_id, day) DO NOTHING",
        (person_odoo_id, day, reason),
    )


def _ensure_run(person_odoo_id: int, day: date) -> None:
    db.execute(
        "INSERT INTO auto_salaried_runs (person_odoo_id, day) VALUES (%s, %s) "
        "ON CONFLICT (person_odoo_id, day) DO NOTHING",
        (person_odoo_id, day),
    )


def _record_punch(person_odoo_id: int, day: date, slot: str, punch_id: int, *, cur=None) -> None:
    if slot not in SLOT_ORDER:  # guards the f-string column name
        raise ValueError(f"unknown slot {slot!r}")
    sql = (
        f"UPDATE auto_salaried_runs SET {slot}_punch_id = %s, updated_at = now() "
        f"WHERE person_odoo_id = %s AND day = %s AND {slot}_punch_id IS NULL"
    )
    params = (punch_id, person_odoo_id, day)
    if cur is not None:
        cur.execute(sql, params)
    else:
        db.execute(sql, params)


def _write_auto_punch(person_odoo_id: int, action: str, wc_name: str | None,
                      occurred_at: datetime, *, cur) -> int:
    """Insert an auto-salaried punch stamped at the scheduled time. Caller's
    open cursor makes the punch + scoreboard update one transaction (same
    crash-safety contract as auto_lunch._write_auto_punch)."""
    cur.execute(
        "INSERT INTO timeclock_punches_log "
        "(person_odoo_id, action, wc_name, occurred_at, rounded_at, source) "
        "VALUES (%s, %s, %s, %s, %s, 'auto_salaried') RETURNING id",
        (person_odoo_id, action, wc_name, occurred_at, occurred_at),
    )
    return cur.fetchone()["id"]


def _capture_lunch_department(person_odoo_id: int, day: date, *, cur) -> None:
    """At lunch-out, read the department off the person's OPEN Odoo record —
    Odoo is the referee because outside apps transfer salaried people without
    telling this app's log. Unreadable → default Sustaining after lunch and
    flag the day (spec: missing 30 minutes is worse than a wrong department)."""
    dept_id = dept_name = None
    try:
        from . import odoo_client
        current = odoo_client.get_current_attendance(person_odoo_id)
        if current:
            dept_id = current.get("department_id")
            dept_name = current.get("department_name")
    except Exception as e:  # noqa: BLE001 — flag and carry on
        _log.warning("auto-salaried: dept read failed for person %s: %s",
                     person_odoo_id, e)
    if dept_id is None:
        _flag(person_odoo_id, day, "lunch_dept_unread",
              "Could not read pre-lunch department from Odoo; "
              "lunch return defaulted to Sustaining.", cur=cur)
        patch_state = "none"
    elif "sustaining" in (dept_name or "").strip().lower():
        patch_state = "none"  # pipeline already writes Sustaining
    else:
        patch_state = "pending"
    cur.execute(
        "UPDATE auto_salaried_runs SET lunch_dept_id = %s, lunch_dept_name = %s, "
        "dept_patch_state = %s, updated_at = now() "
        "WHERE person_odoo_id = %s AND day = %s",
        (dept_id, dept_name, patch_state, person_odoo_id, day),
    )


def _patch_departments() -> None:
    """Write the remembered pre-lunch department onto lunch-in attendances
    once the sync has landed them in Odoo (we only learn the Odoo id then)."""
    rows = db.query(
        "SELECT r.person_odoo_id, r.day, r.lunch_dept_id, l.odoo_attendance_id "
        "FROM auto_salaried_runs r "
        "JOIN timeclock_punches_log l ON l.id = r.lunch_in_punch_id "
        "WHERE r.dept_patch_state = 'pending' AND l.synced_to_odoo = TRUE "
        "AND l.odoo_attendance_id IS NOT NULL"
    )
    for r in rows:
        try:
            from . import odoo_client
            ok = odoo_client.set_attendance_department(
                int(r["odoo_attendance_id"]), int(r["lunch_dept_id"]))
        except Exception as e:  # noqa: BLE001 — retry next tick
            _log.warning("auto-salaried: dept patch failed for person %s %s: %s",
                         r["person_odoo_id"], r["day"], e)
            continue
        db.execute(
            "UPDATE auto_salaried_runs SET dept_patch_state = %s, updated_at = now() "
            "WHERE person_odoo_id = %s AND day = %s",
            ("done" if ok else "failed", r["person_odoo_id"], r["day"]),
        )


def _advance_person(person_odoo_id: int, day: date, now: datetime,
                    run: dict | None, is_holiday: bool, has_leave: bool,
                    worker_mode: str) -> None:
    if run is not None and (run.get("skipped") or run.get("reverted")):
        return
    if run is None:
        reason = skip_reason(day, is_company_holiday=is_holiday,
                             has_approved_leave=has_leave)
        if reason is None and _foreign_punches_today(person_odoo_id, day):
            reason = "other_punches"
            _flag(person_odoo_id, day, "other_punches",
                  "Person already had non-robot punches at enrollment time "
                  "(manual punch or same-day wage-type change); day skipped.")
        if reason:
            _insert_skipped_run(person_odoo_id, day, reason)
            return
        _ensure_run(person_odoo_id, day)
        run = {}
    for slot in due_slots(now, day, run):
        at = scheduled_at(day, slot)
        action = SLOT_ACTION[slot]
        wc_name = SUSTAINING_WC if action == "clock_in" else None
        if worker_mode == "dry_run":
            _log.info("auto-salaried DRY-RUN: person %s %s (%s) @ %s",
                      person_odoo_id, action, slot, at)
            _record_punch(person_odoo_id, day, slot, SIMULATED_PUNCH_ID)
            run = dict(run, **{f"{slot}_punch_id": SIMULATED_PUNCH_ID})
            continue
        _log.info("auto-salaried LIVE: person %s %s (%s) @ %s",
                  person_odoo_id, action, slot, at)
        with db.cursor() as cur:
            if slot == "lunch_out":
                _capture_lunch_department(person_odoo_id, day, cur=cur)
            punch_id = _write_auto_punch(person_odoo_id, action, wc_name, at, cur=cur)
            _record_punch(person_odoo_id, day, slot, punch_id, cur=cur)
        timeclock_sync.sync_one_by_id(punch_id)
        run = dict(run, **{f"{slot}_punch_id": punch_id})


def run_tick(now: datetime | None = None) -> None:
    """One worker sweep. Safe to call every ~60s."""
    worker_mode = mode()
    if worker_mode == "off":
        return
    now = (now or datetime.now(shift_config.SITE_TZ)).astimezone(shift_config.SITE_TZ)
    today = now.date()
    if today.weekday() >= 5:
        return
    if now.time() < PUNCH_TIMES["morning_in"]:
        return
    if not company_holidays.has_synced():
        _log.info("auto-salaried: holiday mirror never synced; skipping tick")
        return
    person_ids = _fixed_wage_ids()
    if not person_ids:
        return
    runs = _get_runs_bulk(today, person_ids)
    is_holiday = company_holidays.for_day(today) is not None
    leave_ids = _approved_leave_ids(today, person_ids)
    for pid in person_ids:
        try:
            _advance_person(pid, today, now, runs.get(pid), is_holiday,
                            pid in leave_ids, worker_mode)
        except Exception as e:  # noqa: BLE001 — one person never kills the tick
            _log.warning("auto-salaried: failed for person %s: %s", pid, e)
    if worker_mode == "live":
        try:
            _patch_departments()
        except Exception as e:  # noqa: BLE001 — patching resumes next tick
            _log.warning("auto-salaried: department patch sweep failed: %s", e)


# ---------- Reconciler ----------

def _runs_with_late_leave(start: date, end: date) -> list[dict]:
    """Punched, unhandled runs in [start, end] whose person now has approved
    leave overlapping that day (leave arrived AFTER the 6:00 skip check)."""
    return db.query(
        "SELECT r.* FROM auto_salaried_runs r WHERE r.day BETWEEN %s AND %s "
        "AND r.skipped = FALSE AND r.reverted = FALSE AND r.flagged = FALSE "
        "AND r.morning_in_punch_id IS NOT NULL "
        "AND EXISTS (SELECT 1 FROM time_off_requests t "
        "  WHERE t.person_odoo_id = r.person_odoo_id AND t.state = 'validate' "
        "  AND t.date_from <= r.day AND t.date_to >= r.day)",
        (start, end),
    )


def _mark_flagged(person_odoo_id: int, day: date) -> None:
    db.execute(
        "UPDATE auto_salaried_runs SET flagged = TRUE, updated_at = now() "
        "WHERE person_odoo_id = %s AND day = %s", (person_odoo_id, day))


def _revert_day(run: dict) -> None:
    """Delete the robot's punches for a leave-conflicted day — only when the
    day is clean (nothing but auto_salaried punches locally AND in Odoo)."""
    pid, day = int(run["person_odoo_id"]), run["day"]
    start, end = _day_bounds(day)
    log_rows = db.query(
        "SELECT id, source, synced_to_odoo, odoo_attendance_id "
        "FROM timeclock_punches_log WHERE person_odoo_id = %s "
        "AND COALESCE(rounded_at, occurred_at) >= %s "
        "AND COALESCE(rounded_at, occurred_at) < %s",
        (pid, start, end),
    )
    if any(r["source"] != "auto_salaried" for r in log_rows):
        _flag(pid, day, "leave_conflict",
              "Approved leave arrived after punches, but the day has "
              "non-robot punches (transfer or manual). Clean up in Odoo.")
        _mark_flagged(pid, day)
        return
    own_ids = sorted({int(r["odoo_attendance_id"]) for r in log_rows
                      if r["odoo_attendance_id"]})
    from . import odoo_client
    odoo_atts = odoo_client.fetch_employee_attendances_for_day(pid, day)
    strangers = [a for a in odoo_atts if int(a["id"]) not in own_ids]
    if strangers:
        _flag(pid, day, "leave_conflict",
              f"Approved leave arrived after punches, but Odoo has "
              f"{len(strangers)} attendance record(s) the robot didn't "
              f"create (outside-app transfer or manual entry). Clean up in Odoo.")
        _mark_flagged(pid, day)
        return
    odoo_client.delete_attendances(own_ids)
    log_ids = [int(r["id"]) for r in log_rows]
    if log_ids:
        # Also neutralize any not-yet-synced rows so the retry sweep can't
        # resurrect the deleted day.
        db.execute(
            "UPDATE timeclock_punches_log SET synced_to_odoo = TRUE, "
            "sync_error = 'reverted: approved leave', synced_at = now() "
            "WHERE id = ANY(%s)", (log_ids,))
    db.execute(
        "UPDATE auto_salaried_runs SET reverted = TRUE, updated_at = now() "
        "WHERE person_odoo_id = %s AND day = %s", (pid, day))
    _log.info("auto-salaried: reverted %s punches for person %s on %s "
              "(approved leave)", len(own_ids), pid, day)


def _flag_incomplete_days(start: date, end_exclusive: date) -> None:
    """Past days where the robot started but never finished all four slots."""
    rows = db.query(
        "SELECT person_odoo_id, day FROM auto_salaried_runs "
        "WHERE day >= %s AND day < %s AND skipped = FALSE AND reverted = FALSE "
        "AND flagged = FALSE AND (morning_in_punch_id IS NULL "
        "OR lunch_out_punch_id IS NULL OR lunch_in_punch_id IS NULL "
        "OR day_out_punch_id IS NULL)",
        (start, end_exclusive),
    )
    for r in rows:
        pid, day = int(r["person_odoo_id"]), r["day"]
        _flag(pid, day, "incomplete_day",
              "The day ended without all four auto punches (extended app "
              "downtime?). Check the person's attendance in Odoo.")
        _mark_flagged(pid, day)


def run_reconcile(now: datetime | None = None) -> None:
    """Slow sweep (~600s): late-approved-leave cleanup + incomplete-day flags.
    Live mode only — dry-run wrote no real punches, so there is nothing to
    revert, and 'incomplete' days are expected while simulating."""
    if mode() != "live":
        return
    now = (now or datetime.now(shift_config.SITE_TZ)).astimezone(shift_config.SITE_TZ)
    today = now.date()
    start = today - timedelta(days=RECONCILE_LOOKBACK_DAYS)
    for run in _runs_with_late_leave(start, today):
        try:
            _revert_day(run)
        except Exception as e:  # noqa: BLE001 — one day never kills the sweep
            _log.warning("auto-salaried reconcile: failed for person %s %s: %s",
                         run["person_odoo_id"], run["day"], e)
    _flag_incomplete_days(start, today)
