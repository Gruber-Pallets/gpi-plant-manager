#!/usr/bin/env python3
"""List employees whose Odoo work schedule conflicts with the plant's workdays.

Read-only. Declaring someone absent creates an Odoo Time Off leave, which Odoo
refuses with "The following employees are not supposed to work during that
period" when the employee's resource.calendar has no working hours that day —
even though the plant schedule had them on. PR #7 made that sync best-effort so
it no longer blocks the manager, but the absence then never reaches Odoo Time
Off. This script finds every active employee whose Odoo calendar would trigger
that rejection, so HR can fix the calendars in Odoo.

Population comes from Odoo (active employees); the local roster (Postgres) is
used only as optional enrichment to drop reserves and read the real plant
work-week. Both degrade gracefully when Postgres isn't reachable — so this runs
from a laptop via `railway run`, which injects Odoo creds but can't reach the
internal Postgres:

    railway run python scripts/diagnose_odoo_calendar_conflicts.py [--all]

See docs/superpowers/specs/2026-06-27-odoo-calendar-conflict-diagnostic-design.md
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

_WD_ABBR = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
_DEFAULT_WEEKDAYS = frozenset({0, 1, 2, 3, 4})  # Mon–Fri

# Conflict buckets, in report order. "ok" is intentionally absent.
_BUCKETS = [
    ("no_calendar", "No Odoo work schedule"),
    ("flexible", "Flexible / no fixed hours"),
    ("missing_days", "Calendar missing plant workday(s)"),
]


def classify_conflict(
    plant_weekdays,
    covered_weekdays,
    is_flexible=False,
    has_calendar=True,
) -> str:
    """Classify one employee's Odoo work schedule against the plant workdays.

    Weekdays are ints, 0=Mon..6=Sun (Python ``weekday()``). Returns one of:
      "no_calendar"  — no Odoo resource.calendar at all
      "flexible"     — flexible schedule, or a calendar with no fixed hours
      "missing_days" — fixed calendar that omits one or more plant workdays
      "ok"           — covers every plant workday

    Odoo rejects a fixed-period leave for the first three; "ok" is fine.
    """
    if not has_calendar:
        return "no_calendar"
    if is_flexible or not covered_weekdays:
        return "flexible"
    if set(plant_weekdays) - set(covered_weekdays):
        return "missing_days"
    return "ok"


def _fmt_days(days) -> str:
    return ", ".join(_WD_ABBR[d] for d in sorted(days)) if days else "—"


def _plant_weekdays():
    """(weekdays, note). Reads the real plant work-week from Postgres; falls
    back to Mon–Fri (with a note) when the DB isn't reachable."""
    try:
        from zira_dashboard import schedule_store

        wd = schedule_store.current().work_weekdays
        if wd:
            return frozenset(wd), None
        return _DEFAULT_WEEKDAYS, None
    except Exception as e:  # noqa: BLE001 -- DB optional; default the work-week
        return _DEFAULT_WEEKDAYS, (
            f"Plant work-week unavailable ({type(e).__name__}); assuming Mon–Fri."
        )


def _load_roster_by_id():
    """({odoo_id: Person}, note). Optional Postgres enrichment used to restrict
    to rostered people and drop reserves. Returns (None, note) when the DB
    isn't reachable, so the caller falls back to all active Odoo employees."""
    try:
        from zira_dashboard import staffing

        by_id = {
            int(p.employee_id): p
            for p in staffing.load_roster()
            if p.employee_id is not None
        }
        return by_id, None
    except Exception as e:  # noqa: BLE001 -- roster is optional enrichment
        return None, (
            f"Local roster unavailable ({type(e).__name__}); listing ALL active "
            "Odoo employees (reserves not filtered out)."
        )


def _gather_rows(plant_weekdays):
    """Classify active Odoo employees against the plant workdays.

    Returns (rows, notes). Odoo is the population; the local roster is optional.
    Imports zira_dashboard lazily so the pure classifier above stays importable
    (and testable) without Odoo creds.
    """
    from zira_dashboard import odoo_client

    employees = odoo_client.fetch_employees()  # active only
    roster_by_id, roster_note = _load_roster_by_id()

    cal_meta = {
        int(s["id"]): (s.get("name") or "(unnamed)", bool(s.get("is_flexible")))
        for s in odoo_client.fetch_work_schedules()
    }

    emp_cal: dict[int, int | None] = {}
    for e in employees:
        cal_id = odoo_client.unwrap_m2o(e.get("resource_calendar_id"))
        valid = isinstance(cal_id, int) and not isinstance(cal_id, bool)
        emp_cal[int(e["id"])] = cal_id if valid else None

    cal_ids = {c for c in emp_cal.values() if c is not None}
    covered = {
        cid: {int(wd) for wd in days}
        for cid, days in odoo_client.fetch_calendar_hours(cal_ids).items()
    }

    plant = set(plant_weekdays)
    rows = []
    for e in employees:
        eid = int(e["id"])
        if roster_by_id is not None:
            p = roster_by_id.get(eid)
            if p is None or p.reserve:
                continue  # not on our roster, or a reserve — never declared absent
        cal_id = emp_cal.get(eid)
        has_cal = cal_id is not None
        if has_cal:
            cal_name, is_flex = cal_meta.get(cal_id, ("(unknown)", False))
            cov = covered.get(cal_id, set())
        else:
            cal_name, is_flex, cov = "(no Odoo work schedule)", False, set()
        rows.append({
            "name": e.get("name") or f"(id {eid})",
            "odoo_id": eid,
            "cal_name": cal_name,
            "covered": cov,
            "missing": plant - cov,
            "verdict": classify_conflict(plant, cov, is_flexible=is_flex, has_calendar=has_cal),
        })
    return rows, [n for n in (roster_note,) if n]


def _print_report(rows, plant_weekdays, show_all: bool, notes=()) -> None:
    for note in notes:
        print(f"NOTE: {note}")
    if notes:
        print()
    conflicts = [r for r in rows if r["verdict"] != "ok"]
    print(
        f"{len(conflicts)} of {len(rows)} employees have an Odoo work-schedule "
        f"conflict (plant runs {_fmt_days(plant_weekdays)})."
    )
    print()
    for key, title in _BUCKETS:
        group = sorted((r for r in rows if r["verdict"] == key), key=lambda r: r["name"].lower())
        if not group:
            continue
        print(f"{title} ({len(group)}):")
        for r in group:
            line = f"  • {r['name']} (id {r['odoo_id']}) · calendar {r['cal_name']!r}"
            if key == "missing_days":
                line += f" · covers {_fmt_days(r['covered'])} · missing {_fmt_days(r['missing'])}"
            print(line)
        print()
    if show_all:
        ok = sorted((r for r in rows if r["verdict"] == "ok"), key=lambda r: r["name"].lower())
        print(f"OK ({len(ok)}):")
        for r in ok:
            print(
                f"  • {r['name']} (id {r['odoo_id']}) · calendar {r['cal_name']!r} "
                f"· covers {_fmt_days(r['covered'])}"
            )


def _parse_args(argv):
    ap = argparse.ArgumentParser(
        description="List employees whose Odoo work schedule conflicts with plant workdays."
    )
    ap.add_argument(
        "--all", action="store_true", help="Also list employees whose calendar is fine."
    )
    return ap.parse_args(argv)


def main(argv=None) -> int:
    args = _parse_args(argv)
    plant_weekdays, week_note = _plant_weekdays()
    rows, notes = _gather_rows(plant_weekdays)
    _print_report(rows, plant_weekdays, show_all=args.all, notes=([week_note] if week_note else []) + notes)
    return 0


if __name__ == "__main__":
    sys.exit(main())
