"""Backfill the full forklift history from the authenticated external API.

One-shot historical load of every completed call from gpiforklift.com's external
completions feed (all history by default) into forklift_calls_daily +
forklift_driver_daily. Idempotent: each (day) and (day, driver) row is
overwritten, so re-runs are safe.

Requires FORKLIFT_API_KEY (the external feed needs a Bearer token). Without it
the backfill logs a warning and writes nothing.

Usage:
    python -m scripts.backfill_forklift_history [--since MS]
Default: --since 0 = all history.
"""
from __future__ import annotations

import argparse


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", type=int, default=0,
                    help="Only calls created at/after this epoch-ms timestamp (0 = all history).")
    args = ap.parse_args()

    from zira_dashboard import db, forklift_backfill

    db.init_pool()
    summary = forklift_backfill.backfill_history(since=args.since)
    print(f"Forklift backfill complete: {summary}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
