#!/usr/bin/env python3
"""Plan or apply historical Auto-Lunch attendance repairs.

The command is read-only by default. Pass ``--apply`` only after reviewing the
printed intervals. It uses the same fixed-calendar and flexible-schedule lunch
rules as the live worker and skips every person/day with an existing run row.
"""
from __future__ import annotations

import argparse
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

try:
    from dotenv import load_dotenv
    load_dotenv(dotenv_path=ROOT / ".env", override=False)
except ImportError:
    pass

from zira_dashboard import (  # noqa: E402
    auto_lunch,
    auto_lunch_settings,
    db,
    odoo_client,
    shift_config,
)
from zira_dashboard.auto_lunch_backfill import (  # noqa: E402
    Repair,
    apply_repair,
    plan_return_only_repairs,
    persist_repair,
    plan_repairs,
)


def _date_arg(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("expected YYYY-MM-DD") from exc


def _days(first: date, last: date):
    if last < first:
        raise ValueError("--through-date must not be before --from-date")
    day = first
    while day <= last:
        yield day
        day += timedelta(days=1)


def _names_by_id() -> dict[int, str]:
    return {
        int(row["odoo_id"]): str(row["name"])
        for row in db.query(
            "SELECT odoo_id, name FROM people WHERE odoo_id IS NOT NULL"
        )
    }


def _existing_run_people(day: date) -> set[int]:
    return {
        int(row["person_odoo_id"])
        for row in db.query(
            "SELECT person_odoo_id FROM auto_lunch_runs WHERE day = %s", (day,)
        )
    }


def _local_clock_outs(day: date) -> dict[int, datetime]:
    start, end = auto_lunch._day_bounds(day)
    rows = db.query(
        "SELECT person_odoo_id, MAX(COALESCE(rounded_at, occurred_at)) AS clock_out "
        "FROM timeclock_punches_log "
        "WHERE action = 'clock_out' "
        "AND COALESCE(rounded_at, occurred_at) >= %s "
        "AND COALESCE(rounded_at, occurred_at) < %s "
        "GROUP BY person_odoo_id",
        (start, end),
    )
    return {
        int(row["person_odoo_id"]): row["clock_out"]
        for row in rows
        if row.get("clock_out") is not None
    }


def _windows_for_day(day: date, intervals: list[dict]):
    person_ids = {int(row["employee_odoo_id"]) for row in intervals}
    flex_ids = auto_lunch._flex_person_ids()
    scheduled_ids = person_ids - flex_ids
    app_window = None
    if shift_config.is_workday(day):
        app_window = auto_lunch.lunch_window_for_day(shift_config.breaks_for(day), day)
    fixed_windows = auto_lunch._fixed_windows_for_candidates(
        day, scheduled_ids, app_window
    )
    windows = {}
    kinds = {}
    settings = auto_lunch_settings.current()
    for person_odoo_id in person_ids:
        if person_odoo_id in flex_ids:
            first_clock_in = auto_lunch._first_clock_in(person_odoo_id, day)
            if first_clock_in is None:
                continue
            windows[person_odoo_id] = auto_lunch.flex_window(
                first_clock_in, settings.flex_after_hours, settings.flex_minutes
            )
            kinds[person_odoo_id] = "flex"
            continue
        window = fixed_windows.get(person_odoo_id, app_window)
        if window is not None:
            windows[person_odoo_id] = window
            kinds[person_odoo_id] = "scheduled"
    return windows, kinds


def _repairs_for_day(day: date, now: datetime) -> list[tuple[str, Repair]]:
    intervals = odoo_client.fetch_attendance_intervals_for_day(day)
    for row in intervals:
        person_odoo_id = int(row["employee_odoo_id"])
        row["wc_name"] = row.get("wc_name") or auto_lunch._latest_in_wc(
            person_odoo_id, day
        )
    windows, kinds = _windows_for_day(day, intervals)
    existing_runs = _existing_run_people(day)
    repairs = plan_repairs(
        intervals,
        windows,
        existing_runs,
        as_of=now if day == now.date() else None,
    )
    repairs.extend(plan_return_only_repairs(
        intervals, windows, existing_runs, _local_clock_outs(day)
    ))
    return [(kinds[repair.person_odoo_id], repair) for repair in repairs]


def _persist(repair: Repair, kind: str, returned_attendance_id: int | None) -> None:
    with db.cursor() as cur:
        def write_punch(
            person_odoo_id: int,
            action: str,
            wc_name: str | None,
            occurred_at: datetime,
            odoo_attendance_id: int | None,
        ) -> int:
            cur.execute(
                "INSERT INTO timeclock_punches_log "
                "(person_odoo_id, action, wc_name, occurred_at, rounded_at, source, "
                "synced_to_odoo, synced_at, odoo_attendance_id) "
                "VALUES (%s, %s, %s, %s, %s, 'auto_lunch', TRUE, now(), %s) "
                "RETURNING id",
                (person_odoo_id, action, wc_name, occurred_at, occurred_at,
                 odoo_attendance_id),
            )
            return int(cur.fetchone()["id"])

        def write_run(
            person_odoo_id: int,
            run_day: date,
            run_kind: str,
            out_at: datetime,
            in_at: datetime,
            wc_name: str | None,
            out_punch_id: int,
            in_punch_id: int | None,
        ) -> None:
            cur.execute(
                "INSERT INTO auto_lunch_runs "
                "(person_odoo_id, day, kind, state, target_out_at, target_in_at, "
                "wc_name, out_punch_id, in_punch_id) "
                "VALUES (%s, %s, %s, 'done', %s, %s, %s, %s, %s) "
                "ON CONFLICT (person_odoo_id, day) DO NOTHING",
                (person_odoo_id, run_day, run_kind, out_at, in_at, wc_name,
                 out_punch_id, in_punch_id),
            )

        persist_repair(
            repair,
            kind=kind,
            returned_attendance_id=returned_attendance_id,
            write_punch=write_punch,
            write_run=write_run,
        )


def _apply_one(kind: str, repair: Repair) -> None:
    def create_return(
        person_odoo_id: int, wc_name: str | None, check_in: datetime
    ) -> int:
        if repair.return_end_at is not None:
            return odoo_client.create_closed_attendance(
                person_odoo_id, wc_name, check_in, repair.return_end_at
            )
        return odoo_client.clock_in(person_odoo_id, wc_name, check_in)

    close = (
        odoo_client.close_historical_attendance
        if repair.return_end_at is not None
        else odoo_client.clock_out
    )

    apply_repair(
        repair,
        close=close,
        clock_in=create_return,
        persist=lambda planned, returned_id: _persist(planned, kind, returned_id),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--from-date", type=_date_arg, required=True)
    parser.add_argument("--through-date", type=_date_arg, required=True)
    parser.add_argument(
        "--apply", action="store_true", help="write the reviewed Odoo and Postgres repairs"
    )
    parser.add_argument(
        "--limit", type=int, help="apply or display at most this many repairs"
    )
    args = parser.parse_args(argv)
    if args.limit is not None and args.limit < 1:
        parser.error("--limit must be at least 1")
    now = datetime.now(shift_config.SITE_TZ)
    names = _names_by_id()
    action = "APPLY" if args.apply else "DRY RUN"
    if not args.apply:
        print("Dry run — pass --apply only after reviewing these repairs.")
    repair_count = 0
    reached_limit = False
    for day in _days(args.from_date, args.through_date):
        for kind, repair in _repairs_for_day(day, now):
            name = names.get(repair.person_odoo_id, f"#{repair.person_odoo_id}")
            return_note = "return" if repair.create_return else "no return"
            local_out = repair.out_at.astimezone(shift_config.SITE_TZ)
            local_in = repair.in_at.astimezone(shift_config.SITE_TZ)
            print(
                f"{action} {day} {name}: {kind} {local_out:%H:%M}-"
                f"{local_in:%H:%M} ({return_note})"
            )
            repair_count += 1
            if args.apply:
                _apply_one(kind, repair)
            if args.limit is not None and repair_count >= args.limit:
                reached_limit = True
                break
        if reached_limit:
            break
    print(f"{action}: {repair_count} attendance repair(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
