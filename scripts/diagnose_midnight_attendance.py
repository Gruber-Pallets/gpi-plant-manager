#!/usr/bin/env python3
"""Diagnose near-zero midnight attendance rows in Odoo.

Read-only. Fetches recent hr.attendance rows whose duration is under a minute,
prints Odoo metadata, and correlates them to local timeclock_punches_log rows
when DATABASE_URL is available.
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))


def _parse_odoo_dt(value):
    if not value:
        return None
    if isinstance(value, str):
        return datetime.strptime(value, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    if hasattr(value, "astimezone"):
        return value.astimezone(timezone.utc)
    return None


def _duration_seconds(row: dict) -> float | None:
    start = _parse_odoo_dt(row.get("check_in"))
    end = _parse_odoo_dt(row.get("check_out"))
    if not start or not end:
        return None
    return (end - start).total_seconds()


def _m2o_name(value):
    if isinstance(value, list) and len(value) > 1:
        return value[1]
    return str(value)


def _fetch_rows(days: int, limit: int) -> list[dict]:
    from zira_dashboard import odoo_client

    since = datetime.now(timezone.utc) - timedelta(days=days)
    fields = [
        "id",
        "employee_id",
        "check_in",
        "check_out",
        "worked_hours",
        "expected_hours",
        "overtime_hours",
        "overtime_status",
        "validated_overtime_hours",
        "in_mode",
        "out_mode",
        "create_uid",
        "create_date",
        "write_uid",
        "write_date",
    ]
    for field_name in (odoo_client._kiosk_wc_field(), odoo_client._kiosk_department_field()):
        if field_name and field_name not in fields:
            fields.append(field_name)
    rows = odoo_client.execute(
        "hr.attendance", "search_read",
        [
            ("check_in", ">=", odoo_client._to_odoo_dt(since)),
            ("check_out", "!=", False),
        ],
        fields=fields,
        order="check_in asc",
        limit=limit,
    )
    return [
        row for row in rows
        if (dur := _duration_seconds(row)) is not None and 0 <= dur < 60
    ]


def _local_matches(rows: list[dict]) -> dict[int, list[dict]]:
    if not rows:
        return {}
    try:
        from zira_dashboard import db
    except Exception:
        return {}
    ids = [int(row["id"]) for row in rows]
    try:
        direct = db.query(
            "SELECT id, person_odoo_id, action, wc_name, odoo_attendance_id, "
            "occurred_at, rounded_at, source, synced_to_odoo, sync_error, synced_at "
            "FROM timeclock_punches_log WHERE odoo_attendance_id = ANY(%s) "
            "ORDER BY person_odoo_id, occurred_at, id",
            (ids,),
        )
    except Exception as exc:  # noqa: BLE001 - diagnostic should still print Odoo data
        print(f"\nLocal DB correlation unavailable: {type(exc).__name__}: {exc}")
        return {}
    out: dict[int, list[dict]] = defaultdict(list)
    for row in direct:
        out[int(row["odoo_attendance_id"])].append(row)
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=21)
    parser.add_argument("--limit", type=int, default=5000)
    parser.add_argument("--sample", type=int, default=40)
    args = parser.parse_args(argv)

    rows = _fetch_rows(args.days, args.limit)
    matches = _local_matches(rows)

    print(f"Near-zero closed attendance rows in last {args.days} day(s): {len(rows)}")
    print("By Odoo create user:")
    for name, count in Counter(_m2o_name(r.get("create_uid")) for r in rows).most_common():
        print(f"  {name}: {count}")
    print("By in/out mode:")
    for key, count in Counter((r.get("in_mode"), r.get("out_mode")) for r in rows).most_common():
        print(f"  {key}: {count}")
    print(f"Rows with matching local timeclock_punches_log.odoo_attendance_id: {len(matches)}")

    print("\nSample rows:")
    for row in rows[:args.sample]:
        dur = _duration_seconds(row)
        emp = _m2o_name(row.get("employee_id"))
        print(
            f"id={row.get('id')} emp={emp!r} check_in={row.get('check_in')} "
            f"check_out={row.get('check_out')} dur_s={dur} worked={row.get('worked_hours')} "
            f"expected={row.get('expected_hours')} overtime={row.get('overtime_hours')} "
            f"status={row.get('overtime_status')} mode={row.get('in_mode')}/{row.get('out_mode')} "
            f"created_by={_m2o_name(row.get('create_uid'))!r} create_date={row.get('create_date')} "
            f"written_by={_m2o_name(row.get('write_uid'))!r} write_date={row.get('write_date')}"
        )
        for match in matches.get(int(row["id"]), []):
            print(
                f"  local_log id={match.get('id')} action={match.get('action')} "
                f"source={match.get('source')} occurred_at={match.get('occurred_at')} "
                f"rounded_at={match.get('rounded_at')} wc={match.get('wc_name')!r} "
                f"synced={match.get('synced_to_odoo')} error={match.get('sync_error')!r}"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
