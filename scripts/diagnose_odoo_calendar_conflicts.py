#!/usr/bin/env python3
"""List employees whose Odoo work schedule conflicts with the plant's workdays.

Read-only. Declaring someone absent creates an Odoo Time Off leave, which Odoo
refuses with "The following employees are not supposed to work during that
period" when the employee's resource.calendar has no working hours that day —
even though the plant schedule had them on. PR #7 made that sync best-effort so
it no longer blocks the manager, but the absence then never reaches Odoo Time
Off. This script finds every active, non-reserve employee whose Odoo calendar
would trigger that rejection, so HR can fix the calendars in Odoo.

Run on Railway (needs ODOO_* creds; writes nothing):

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


def _gather_rows(plant_weekdays):
    """Join the local roster to Odoo calendar data; classify each person.

    Returns a list of row dicts. Imports zira_dashboard lazily so the pure
    classifier above can be imported (and tested) without Odoo creds.
    """
    from zira_dashboard import odoo_client, schedule_store, staffing  # noqa: F401

    roster = [p for p in staffing.load_roster() if p.active and not p.reserve]

    # Odoo employee id -> resource.calendar id (or None).
    emp_cal: dict[int, int | None] = {}
    for e in odoo_client.fetch_employees():
        cal_id = odoo_client.unwrap_m2o(e.get("resource_calendar_id"))
        valid = isinstance(cal_id, int) and not isinstance(cal_id, bool)
        emp_cal[int(e["id"])] = cal_id if valid else None

    # calendar id -> (name, is_flexible)
    cal_meta = {
        int(s["id"]): (s.get("name") or "(unnamed)", bool(s.get("is_flexible")))
        for s in odoo_client.fetch_work_schedules()
    }

    cal_ids = {c for c in emp_cal.values() if c is not None}
    cal_hours = odoo_client.fetch_calendar_hours(cal_ids)
    covered = {cid: {int(wd) for wd in days} for cid, days in cal_hours.items()}

    plant = set(plant_weekdays)
    rows = []
    for p in roster:
        eid = p.employee_id
        cal_id = emp_cal.get(int(eid)) if eid is not None else None
        has_cal = cal_id is not None
        if has_cal:
            cal_name, is_flex = cal_meta.get(cal_id, ("(unknown)", False))
            cov = covered.get(cal_id, set())
        else:
            cal_name, is_flex, cov = "(no Odoo work schedule)", False, set()
        rows.append({
            "name": p.name,
            "odoo_id": eid,
            "cal_name": cal_name,
            "covered": cov,
            "missing": plant - cov,
            "verdict": classify_conflict(plant, cov, is_flexible=is_flex, has_calendar=has_cal),
        })
    return rows


def _print_report(rows, plant_weekdays, show_all: bool) -> None:
    conflicts = [r for r in rows if r["verdict"] != "ok"]
    print(
        f"{len(conflicts)} of {len(rows)} active non-reserve employees have an "
        f"Odoo work-schedule conflict (plant runs {_fmt_days(plant_weekdays)})."
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
    from zira_dashboard import schedule_store

    plant_weekdays = schedule_store.current().work_weekdays or frozenset({0, 1, 2, 3, 4})
    rows = _gather_rows(plant_weekdays)
    _print_report(rows, plant_weekdays, show_all=args.all)
    return 0


if __name__ == "__main__":
    sys.exit(main())
