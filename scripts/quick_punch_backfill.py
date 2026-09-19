"""Dry-run-first cleanup of quick-punch mistakes in the current pay period.

    python -m scripts.quick_punch_backfill
    python -m scripts.quick_punch_backfill --yes
"""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import date, timedelta
import sys
from typing import TextIO

from zira_dashboard import (
    attendance_timeline,
    deps,
    plant_day,
    quick_punch_fix_settings,
    quick_punch_fixer,
    quick_punch_fixes,
    staffing_hours,
)


def _parse_day(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("expected YYYY-MM-DD") from exc


def _days(start: date, end: date):
    day = start
    while day <= end:
        yield day
        day += timedelta(days=1)


def _plant_day_end(day: date):
    _start, end = attendance_timeline._plant_day_bounds(day)
    return end - timedelta(microseconds=1)


def _spans_for_past_day(day: date):
    start, end = attendance_timeline._plant_day_bounds(day)
    return attendance_timeline.timeline_for_range(start, end, as_of_utc=end)


def _print_fix(out: TextIO, day: date, fix) -> None:
    before = quick_punch_fixes.before_summary(fix)
    after = quick_punch_fixes.after_summary(fix)
    print(f"{day.isoformat()}  {fix.person_name}  {before} → {after}", file=out)


def _print_skip(out: TextIO, day: date, person: str, reason: str, before: str) -> None:
    print(f"{day.isoformat()}  {person}  skip {reason}  {before}", file=out)


def run(argv: list[str] | None = None, *, out: TextIO | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m scripts.quick_punch_backfill",
        description="List or apply settled quick-punch merges for the current pay period.",
    )
    parser.add_argument("--start", type=_parse_day, help="First plant day (clamped to the pay period)")
    parser.add_argument("--end", type=_parse_day, help="Last plant day (defaults to yesterday)")
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Create Live merge jobs. Refuses unless the fixer setting is Live.",
    )
    args = parser.parse_args(argv)
    out = sys.stdout if out is None else out

    today = plant_day.today()
    period_start, _period_end = staffing_hours.current_pay_period_bounds(today)
    start = args.start or period_start
    if start < period_start:
        start = period_start
    end = args.end or (today - timedelta(days=1))
    if end < start:
        print("Nothing to scan: end is before start.", file=out)
        return 0

    if args.yes and quick_punch_fix_settings.current().mode != "live":
        print(
            "Refusing --yes: the quick-punch fixer setting is not Live.",
            file=out,
        )
        return 2

    totals: Counter[str] = Counter()
    for day in _days(start, end):
        now_utc = _plant_day_end(day)
        spans = None
        if day < today:
            try:
                spans = _spans_for_past_day(day)
            except Exception as exc:
                print(f"{day.isoformat()}  skip timeline  {type(exc).__name__}", file=out)
                totals["timeline"] += 1
                continue
        run_result = quick_punch_fixer.run_for_day(
            day,
            now_utc=now_utc,
            mode="live",
            client=deps.client,
            apply=args.yes,
            spans=spans,
        )
        for fix in run_result.eligible:
            _print_fix(out, day, fix)
            totals["fix"] += 1
        for skip in run_result.scan.skipped:
            _print_skip(
                out,
                day,
                skip.person_name,
                skip.reason,
                quick_punch_fixes.source_stints_summary(skip.before),
            )
            totals[skip.reason] += 1
        for employee_id in run_result.payroll_skipped:
            totals["payroll"] += 1
            print(f"{day.isoformat()}  employee {employee_id}  skip payroll", file=out)
        for employee_id in run_result.active_job_skipped:
            totals["active_job"] += 1
            print(f"{day.isoformat()}  employee {employee_id}  skip active_job", file=out)
        for key in run_result.deduped_item_keys:
            totals["deduped"] += 1
            print(f"{day.isoformat()}  skip deduped  {key}", file=out)

    if totals:
        print("Totals: " + ", ".join(f"{name}={count}" for name, count in sorted(totals.items())), file=out)
    if not args.yes:
        print("Dry run — nothing changed. Re-run with --yes to apply.", file=out)
    return 0


def main(argv: list[str] | None = None) -> int:
    return run(argv)


if __name__ == "__main__":
    raise SystemExit(main())
