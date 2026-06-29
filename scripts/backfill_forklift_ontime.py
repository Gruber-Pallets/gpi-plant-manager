"""One-time: reconstruct per-day forklift on-time/utilization history.

Fetches one cumulative dashboard per day boundary from gpiforklift.com, differences
consecutive days to isolate each day's on-time/late/on-call/available counts, and
upserts those columns into forklift_driver_daily (leaving calls/avg_ms/max_ms
untouched). Idempotent and re-runnable; clamps negative differences at 0.

Usage:
    python -m scripts.backfill_forklift_ontime [--days-back N]
Default: --days-back 120.
"""
from __future__ import annotations

import argparse


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days-back", type=int, default=120,
                    help="How many days of history to reconstruct (default 120).")
    args = ap.parse_args()

    from zira_dashboard import db, forklift_backfill

    db.init_pool()
    summary = forklift_backfill.reconstruct_ontime_history(days_back=args.days_back)
    print(f"Forklift on-time reconstruction complete: {summary}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
