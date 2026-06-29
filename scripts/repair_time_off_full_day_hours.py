#!/usr/bin/env python3
"""Repair local time_off_requests rows that look partial but are full-day in Odoo.

Fetches Odoo hr.leave rows, runs the same mirror normalizer as the poller, and
clears local hour bounds for rows that should be full-day. Dry-run by default.

Run with Railway-injected env:
    railway run --service web python -m scripts.repair_time_off_full_day_hours --apply
"""
from __future__ import annotations

import argparse
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

try:
    from dotenv import load_dotenv
    load_dotenv(dotenv_path=ROOT / ".env", override=False)
except ImportError:
    pass

from zira_dashboard import time_off_sync  # noqa: E402


def _needs_full_day_repair(row: dict[str, Any]) -> bool:
    return (
        row.get("shape") != "full_day"
        or row.get("hour_from") is not None
        or row.get("hour_to") is not None
        or row.get("working_hours_json") is not None
    )


def corrections_for(
    leaves: list[dict[str, Any]],
    existing_by_leave_id: dict[int, dict[str, Any]],
) -> list[dict[str, Any]]:
    """Return local rows whose Odoo leave normalizes to full_day."""
    corrections: list[dict[str, Any]] = []
    for leave in leaves:
        shape, hour_from, hour_to = time_off_sync._mirror_shape_and_hours(leave)
        if shape != "full_day" or hour_from is not None or hour_to is not None:
            continue
        row = existing_by_leave_id.get(int(leave["id"]))
        if row is None or not _needs_full_day_repair(row):
            continue
        corrections.append({
            "id": row["id"],
            "odoo_leave_id": row["odoo_leave_id"],
            "person_name": row.get("person_name"),
            "date_from": row.get("date_from"),
            "date_to": row.get("date_to"),
            "from_shape": row.get("shape"),
            "from_hours": f"{row.get('hour_from')}-{row.get('hour_to')}",
        })
    corrections.sort(key=lambda r: (r["date_from"], str(r["person_name"]).lower(), r["id"]))
    return corrections


def _existing_by_leave_id(leave_ids: list[int]) -> dict[int, dict[str, Any]]:
    if not leave_ids:
        return {}
    from zira_dashboard import db

    rows = db.query(
        "SELECT r.id, r.odoo_leave_id, "
        "COALESCE(p.name, '#' || r.person_odoo_id::text) AS person_name, "
        "r.shape, r.date_from, r.date_to, r.hour_from, r.hour_to, "
        "r.working_hours_json "
        "FROM time_off_requests r "
        "LEFT JOIN people p ON p.odoo_id = r.person_odoo_id "
        "WHERE r.odoo_leave_id = ANY(%s)",
        (leave_ids,),
    )
    return {int(r["odoo_leave_id"]): r for r in rows}


def _apply_corrections(corrections: list[dict[str, Any]]) -> int:
    if not corrections:
        return 0
    from zira_dashboard import db

    updated = 0
    with db.cursor() as cur:
        for row in corrections:
            cur.execute(
                "UPDATE time_off_requests "
                "SET shape = 'full_day', hour_from = NULL, hour_to = NULL, "
                "working_hours_json = NULL, last_pulled_at = now(), updated_at = now() "
                "WHERE id = %s "
                "AND (shape <> 'full_day' OR hour_from IS NOT NULL "
                "OR hour_to IS NOT NULL OR working_hours_json IS NOT NULL)",
                (row["id"],),
            )
            updated += cur.rowcount
    return updated


def _print_corrections(corrections: list[dict[str, Any]], limit: int = 100) -> None:
    for row in corrections[:limit]:
        print(
            f"  id={row['id']} leave={row['odoo_leave_id']} "
            f"{row['person_name']} {row['date_from']}..{row['date_to']} "
            f"{row['from_shape']} {row['from_hours']} -> full_day"
        )
    if len(corrections) > limit:
        print(f"  ... {len(corrections) - limit} more")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days-back", type=int, default=365)
    ap.add_argument("--days-forward", type=int, default=365)
    ap.add_argument("--apply", action="store_true", help="write the repairs")
    args = ap.parse_args(argv)

    from zira_dashboard import db, odoo_client

    db.init_pool()
    today = date.today()
    start_d = today - timedelta(days=args.days_back)
    end_d = today + timedelta(days=args.days_forward)
    leaves = odoo_client.fetch_leaves_for_range(start_d, end_d)
    existing = _existing_by_leave_id([int(leave["id"]) for leave in leaves])
    corrections = corrections_for(leaves, existing)

    print(f"Fetched {len(leaves)} Odoo leaves for {start_d}..{end_d}.")
    print(f"Full-day local repairs needed: {len(corrections)}")
    _print_corrections(corrections)
    if not args.apply:
        print("Dry run only. Re-run with --apply to update these rows.")
        return 0
    updated = _apply_corrections(corrections)
    print(f"Updated {updated} time_off_requests rows.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
