#!/usr/bin/env python3
"""Read-only diagnostic for the "couldn't punch out on Saturday" bug.

For one site-local day it dumps everything needed to see WHY scheduled
workers ended up unable to clock out and flagged as missed punch-outs:

  1) auto_lunch_settings           — is the worker live (enabled, not observe)?
  2) resolved schedule for the day — is_workday, shift hours, breaks, and
     whether a break named "Lunch" exists (what auto-lunch acts on)
  3) auto_lunch_runs               — per person: state, target out/in, the
     punch ids it wrote (a run stuck in 'auto_out' = never signed back in)
  4) timeclock_punches_log         — every punch that day: action, source
     (kiosk vs auto_lunch), synced_to_odoo, sync_error (failed Odoo closes)
  5) missed_punch_out              — which attendances got auto-closed/flagged
  6) Odoo hr.attendance            — the actual records: open vs closed,
     check_in/out, so we can compare Odoo truth to local state
  7) per-person reconciliation     — the one-line story for each person

NOTHING is written. Only SELECTs and Odoo search_read. Safe to run against
production.

Run from the project root (locally with a .env, or on Railway):
    python -m scripts.diagnose_saturday_punches --date 2026-06-13
    railway run python -m scripts.diagnose_saturday_punches --date 2026-06-13

Defaults to the most recent Saturday if --date is omitted.
"""
from __future__ import annotations

import argparse
import sys
from datetime import date, datetime, time, timedelta, UTC
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

# Load .env from the project root so a local run picks up the same values the
# app sees (matches scripts/probe_odoo_auth.py). On Railway the vars are
# already in the environment, so the missing-dotenv path is fine.
try:
    from dotenv import load_dotenv
    load_dotenv(dotenv_path=ROOT / ".env", override=False)
except ImportError:
    pass


def _most_recent_saturday(today: date) -> date:
    # weekday(): Mon=0 .. Sat=5. Days since the last Saturday (0 if today is Sat).
    return today - timedelta(days=(today.weekday() - 5) % 7)


def _hdr(title: str) -> None:
    print()
    print("=" * 72)
    print(title)
    print("=" * 72)


def _fmt(dt) -> str:
    """A datetime (any tz) -> 'YYYY-MM-DD HH:MM:SS TZ' in site-local, or ''."""
    from zira_dashboard import shift_config
    if dt is None:
        return ""
    if isinstance(dt, str):
        try:
            dt = datetime.fromisoformat(dt)
        except ValueError:
            return dt
    if getattr(dt, "tzinfo", None) is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(shift_config.SITE_TZ).strftime("%Y-%m-%d %H:%M:%S %Z")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--date", help="site-local day YYYY-MM-DD (default: last Saturday)")
    args = ap.parse_args()

    from zira_dashboard import (
        db, shift_config, staffing, auto_lunch, auto_lunch_settings,
        saturday_schedule_store, odoo_client,
    )

    db.init_pool()
    site = shift_config.SITE_TZ
    today = datetime.now(site).date()
    day = date.fromisoformat(args.date) if args.date else _most_recent_saturday(today)

    start_local = datetime.combine(day, time.min, tzinfo=site)
    end_local = start_local + timedelta(days=1)
    start_utc = start_local.astimezone(UTC)
    end_utc = end_local.astimezone(UTC)

    print(f"Diagnosing {day} ({day.strftime('%A')})  [site tz: {site}]")
    print(f"Local day window: {start_local}  ->  {end_local}")

    # name map
    people = db.query("SELECT odoo_id, name FROM people WHERE odoo_id IS NOT NULL")
    name_of = {int(r["odoo_id"]): r["name"] for r in people}

    def label(pid) -> str:
        return name_of.get(int(pid), f"#{pid}")

    # ---- 1) auto-lunch settings ----
    _hdr("1) auto_lunch_settings (is the worker live?)")
    s = auto_lunch_settings.current()
    print(f"  enabled          = {s.enabled}")
    print(f"  observe_only     = {s.observe_only}   "
          f"(True = simulates only, writes NO punches)")
    print(f"  flex_after_hours = {s.flex_after_hours}")
    print(f"  flex_minutes     = {s.flex_minutes}")
    live = s.enabled and not s.observe_only
    print(f"  -> auto-lunch is {'LIVE (writes punches)' if live else 'NOT writing punches'}")

    # ---- 2) resolved schedule for the day ----
    _hdr("2) resolved schedule for the day")
    is_wd = shift_config.is_workday(day)
    print(f"  is_workday({day})   = {is_wd}")
    try:
        sched = staffing.load_schedule(day)
        print(f"  schedule.published  = {getattr(sched, 'published', None)}")
        print(f"  schedule.custom_hours = {getattr(sched, 'custom_hours', None)}")
    except Exception as e:  # noqa: BLE001
        print(f"  (load_schedule failed: {e})")
    print(f"  shift_start_for     = {shift_config.shift_start_for(day)}")
    print(f"  shift_end_for       = {shift_config.shift_end_for(day)}")
    brks = shift_config.breaks_for(day)
    print(f"  breaks_for          = {[(b.name, str(b.start), str(b.end)) for b in brks]}")
    win = auto_lunch.lunch_window_for_day(brks, day)
    if win:
        print(f"  -> auto-lunch lunch window: OUT {_fmt(win.out_at)}  IN {_fmt(win.in_at)}")
    else:
        print("  -> NO break named 'lunch' for this day; scheduled workers are NOT auto-lunched")
    if day.weekday() == shift_config.SATURDAY:
        sat = saturday_schedule_store.current()
        print(f"  saturday_default    = {sat.shift_start}-{sat.shift_end} "
              f"breaks={[(b.name, str(b.start), str(b.end)) for b in sat.breaks]}")

    # ---- 3) auto_lunch_runs ----
    _hdr("3) auto_lunch_runs for the day (state 'auto_out' = signed out, never back in)")
    runs = db.query(
        "SELECT person_odoo_id, kind, state, target_out_at, target_in_at, "
        "wc_name, out_punch_id, in_punch_id, updated_at "
        "FROM auto_lunch_runs WHERE day = %s ORDER BY person_odoo_id", (day,))
    if not runs:
        print("  (no auto_lunch_runs rows for this day)")
    for r in runs:
        print(f"  {label(r['person_odoo_id']):<24} kind={r['kind']:<9} "
              f"state={r['state']:<16} out@{_fmt(r['target_out_at'])} "
              f"in@{_fmt(r['target_in_at'])} out_id={r['out_punch_id']} "
              f"in_id={r['in_punch_id']} wc={r['wc_name']}")
    runs_by_pid = {int(r["person_odoo_id"]): r for r in runs}

    # ---- 4) timeclock_punches_log ----
    _hdr("4) timeclock_punches_log for the day (action, source, sync status)")
    punches = db.query(
        "SELECT id, person_odoo_id, action, wc_name, source, "
        "occurred_at, rounded_at, synced_to_odoo, synced_at, sync_error, "
        "odoo_attendance_id "
        "FROM timeclock_punches_log "
        "WHERE COALESCE(rounded_at, occurred_at) >= %s "
        "  AND COALESCE(rounded_at, occurred_at) <  %s "
        "ORDER BY person_odoo_id, COALESCE(rounded_at, occurred_at), id",
        (start_utc, end_utc))
    if not punches:
        print("  (no punches logged for this day)")
    n_failed = 0
    for p in punches:
        at = p["rounded_at"] or p["occurred_at"]
        flag = "" if p["synced_to_odoo"] else "  <-- NOT SYNCED"
        if p["sync_error"]:
            flag = f"  <-- SYNC ERROR: {p['sync_error']}"
            n_failed += 1
        print(f"  {label(p['person_odoo_id']):<24} {p['action']:<13} "
              f"src={(p['source'] or 'kiosk'):<10} {_fmt(at)} "
              f"att_id={p['odoo_attendance_id']}{flag}")
    print(f"\n  punches with a sync error: {n_failed}")

    # ---- 5) missed_punch_out flags for this day's check-ins ----
    _hdr("5) missed_punch_out flags (auto-closed at midnight)")
    flags = db.query(
        "SELECT attendance_id, employee_odoo_id, name, check_in, "
        "auto_closed_at, corrected_at, resolved_at "
        "FROM missed_punch_out "
        "WHERE check_in >= %s AND check_in < %s ORDER BY employee_odoo_id",
        (start_utc, end_utc))
    if not flags:
        print("  (no missed_punch_out rows whose check_in is this day)")
    flagged_pids = set()
    for f in flags:
        flagged_pids.add(int(f["employee_odoo_id"]))
        status = "RESOLVED" if f["resolved_at"] else "UNRESOLVED"
        print(f"  {label(f['employee_odoo_id']):<24} att={f['attendance_id']} "
              f"in={_fmt(f['check_in'])} closed@{_fmt(f['auto_closed_at'])} "
              f"corrected={_fmt(f['corrected_at'])} [{status}]")

    # ---- 6) Odoo hr.attendance for the day ----
    _hdr("6) Odoo hr.attendance intervals for the day (open vs closed)")
    try:
        intervals = odoo_client.fetch_attendance_intervals_for_day(day)
    except Exception as e:  # noqa: BLE001
        intervals = []
        print(f"  (Odoo fetch failed: {type(e).__name__}: {e})")
    by_pid: dict[int, list[dict]] = {}
    for it in intervals:
        pid = it.get("employee_odoo_id")
        if pid is None:
            continue
        by_pid.setdefault(int(pid), []).append(it)
    if not intervals:
        print("  (no Odoo attendance records check_in on this day)")
    for pid in sorted(by_pid):
        for it in sorted(by_pid[pid], key=lambda x: x.get("check_in") or ""):
            is_open = not it.get("check_out")
            print(f"  {label(pid):<24} in={_fmt(it.get('check_in'))} "
                  f"out={_fmt(it.get('check_out')) or 'OPEN':<22} "
                  f"wc={it.get('wc_name')}{'   <-- STILL OPEN' if is_open else ''}")

    # ---- 7) per-person reconciliation ----
    _hdr("7) per-person reconciliation (the one-line story)")
    all_pids = (set(runs_by_pid) | flagged_pids | set(by_pid)
                | {int(p["person_odoo_id"]) for p in punches})
    if not all_pids:
        print("  (nobody clocked in / no activity this day)")
    for pid in sorted(all_pids):
        run = runs_by_pid.get(pid)
        odoo_recs = by_pid.get(pid, [])
        n_open = sum(1 for it in odoo_recs if not it.get("check_out"))
        my_punches = [p for p in punches if int(p["person_odoo_id"]) == pid]
        last = my_punches[-1] if my_punches else None
        bits = []
        bits.append(f"odoo_records={len(odoo_recs)} (open={n_open})")
        if run:
            bits.append(f"auto_lunch={run['state']}")
        if last:
            bits.append(f"last_local_punch={last['action']}"
                        f"{'' if last['synced_to_odoo'] else '/UNSYNCED'}")
        if pid in flagged_pids:
            bits.append("FLAGGED missed-punch-out")
        # The smoking-gun pattern: an open Odoo record while the last local
        # punch reads clock_out (kiosk would show 'clocked out' -> no button).
        if n_open and last and last["action"] in ("clock_out", "transfer_out"):
            bits.append("*** STRANDED: open Odoo record but local shows clocked-out ***")
        print(f"  {label(pid):<24} " + "  ".join(bits))

    print("\nDone. (read-only — nothing was modified)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
