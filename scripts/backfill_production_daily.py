"""Rebuild production_daily for a date window (delete-then-recompute per day).

Use after the precompute fix to recover days that wrote zero rows while the
StratusTime directory was empty (StratusTime turned off -> name_to_emp_id_map
returned empty -> every production row was dropped). Idempotent: each day is
deleted then recomputed from Zira, so re-runs and overlapping windows are safe
and never double-count.

Usage:
    python -m scripts.backfill_production_daily [--days N] [--start YYYY-MM-DD] [--end YYYY-MM-DD]
Default window: the last 60 days ending today.
"""
from __future__ import annotations

import argparse
from datetime import date, datetime, timedelta, UTC


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=60)
    ap.add_argument("--start")
    ap.add_argument("--end")
    args = ap.parse_args()

    from zira_dashboard import db, precompute
    from zira_dashboard.deps import client

    db.init_pool()
    today = datetime.now(UTC).date()
    end = date.fromisoformat(args.end) if args.end else today
    start = date.fromisoformat(args.start) if args.start else end - timedelta(days=args.days)

    d = start
    total = 0
    while d <= end:
        db.execute("DELETE FROM production_daily WHERE day = %s", (d,))
        res = precompute.precompute_day(d, client)
        total += res["rows_written"]
        print(f"{d}: {res['rows_written']} rows")
        d += timedelta(days=1)
    print(f"Backfill complete: {total} rows across {start}..{end}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
