#!/usr/bin/env python3
"""Approve existing regular attendance rows in Odoo's To Approve queue.

This is deliberately conservative:
- only rows already closed with ``check_out`` are considered
- positive ``overtime_hours`` stays ``to_approve``
- bad/missing overtime values are skipped

Default mode is a dry run. Pass ``--apply`` to write ``overtime_status=approved``
for eligible rows.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))


def classify_attendance(row: dict) -> str:
    if not row.get("check_out"):
        return "open"
    try:
        overtime_hours = float(row.get("overtime_hours"))
    except (TypeError, ValueError):
        return "unknown_overtime"
    return "positive_ot" if overtime_hours > 0 else "eligible"


def _chunks(values: list[int], size: int):
    for i in range(0, len(values), size):
        yield values[i:i + size]


def _fetch_to_approve(batch_size: int, max_rows: int | None) -> list[dict]:
    from zira_dashboard import odoo_client

    rows: list[dict] = []
    offset = 0
    while True:
        limit = batch_size
        if max_rows is not None:
            remaining = max_rows - len(rows)
            if remaining <= 0:
                break
            limit = min(limit, remaining)
        batch = odoo_client.execute(
            "hr.attendance", "search_read",
            [("overtime_status", "=", "to_approve")],
            fields=[
                "id",
                "employee_id",
                "check_in",
                "check_out",
                "worked_hours",
                "expected_hours",
                "overtime_hours",
                "validated_overtime_hours",
                "overtime_status",
            ],
            order="check_in asc",
            limit=limit,
            offset=offset,
        )
        if not batch:
            break
        rows.extend(batch)
        offset += len(batch)
        if len(batch) < limit:
            break
    return rows


def _summarize(rows: list[dict]) -> dict[str, list[dict]]:
    buckets = {
        "eligible": [],
        "positive_ot": [],
        "open": [],
        "unknown_overtime": [],
    }
    for row in rows:
        buckets[classify_attendance(row)].append(row)
    return buckets


def _label(row: dict) -> str:
    emp = row.get("employee_id")
    emp_name = emp[1] if isinstance(emp, list) and len(emp) > 1 else f"#{emp}"
    return (
        f"id={row.get('id')} employee={emp_name!r} "
        f"check_in={row.get('check_in')} check_out={row.get('check_out')} "
        f"worked={row.get('worked_hours')} overtime={row.get('overtime_hours')}"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true",
                        help="write approved status for eligible rows")
    parser.add_argument("--batch-size", type=int, default=200)
    parser.add_argument("--max-rows", type=int, default=None)
    parser.add_argument("--sample", type=int, default=10)
    args = parser.parse_args(argv)

    rows = _fetch_to_approve(args.batch_size, args.max_rows)
    buckets = _summarize(rows)
    print(f"To Approve rows scanned: {len(rows)}")
    print(f"Eligible regular/non-positive closed rows: {len(buckets['eligible'])}")
    print(f"Positive OT left for approval: {len(buckets['positive_ot'])}")
    print(f"Open rows left untouched: {len(buckets['open'])}")
    print(f"Unknown overtime left untouched: {len(buckets['unknown_overtime'])}")

    for name in ("eligible", "positive_ot", "open", "unknown_overtime"):
        sample = buckets[name][:args.sample]
        if not sample:
            continue
        print(f"\nSample {name}:")
        for row in sample:
            print(f"  {_label(row)}")

    eligible_ids = [int(r["id"]) for r in buckets["eligible"]]
    if not args.apply:
        print("\nDry run only. Re-run with --apply to approve eligible rows.")
        return 0
    if not eligible_ids:
        print("\nNo eligible rows to approve.")
        return 0

    from zira_dashboard import odoo_client

    updated = 0
    for chunk in _chunks(eligible_ids, args.batch_size):
        odoo_client.execute(
            "hr.attendance", "write",
            chunk,
            {"overtime_status": "approved"},
        )
        updated += len(chunk)
    print(f"\nApproved eligible rows: {updated}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
