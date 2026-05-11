"""Precompute layer for daily-OK pages.

Owns the `production_daily` table: every write (nightly + live-warmer)
and every read (leaderboards, player cards, trophies, value streams)
goes through this module.

The two halves:

  - Write path:  precompute_day(day, client) — calls
    production_history.attribution_for(), flattens the nested dict into
    per-(day, emp_id, wc) rows, UPSERTs them.
  - Read path:   sum_by_range, sum_by_name, daily_records — replace the
    on-demand attribution loops that used to run inside each request.

Cores that operate on lists/dicts of rows (rank_by_category,
apply_overrides, _rank_single_day) live in their own modules and are
unaffected.
"""
from __future__ import annotations

from datetime import date
from typing import Iterable


def flatten_attribution(
    day: date,
    attribution: dict[str, dict[str, dict[str, float]]],
    name_to_emp_id: dict[str, str],
) -> list[dict]:
    """Turn {person: {wc: {units, downtime, hours, days_worked}}} into
    a flat list of rows ready for UPSERT into production_daily.

    Rows where units == 0 are dropped (the attribution dict can carry
    zero-unit rows for multi-person WCs with no production; they add
    no value in the table).

    Rows where the operator has no emp_id (not in the StratusTime
    directory) are dropped silently — the caller is expected to log
    and the next Odoo sync will pull the person in.
    """
    rows: list[dict] = []
    for person, wc_map in attribution.items():
        emp_id = name_to_emp_id.get(person)
        if not emp_id:
            continue
        for wc_name, totals in wc_map.items():
            units = float(totals.get("units") or 0)
            if units <= 0:
                continue
            rows.append({
                "day": day,
                "emp_id": str(emp_id),
                "name": person,
                "wc_name": wc_name,
                "units": units,
                "downtime": float(totals.get("downtime") or 0),
                "hours": float(totals.get("hours") or 0),
                "days_worked": float(totals.get("days_worked") or 0),
            })
    return rows


def upsert_production_daily(rows: Iterable[dict]) -> int:
    """UPSERT a batch of rows into production_daily. Returns count written.

    Idempotent: PK conflict triggers an UPDATE of every non-PK column
    and bumps `computed_at`. Re-running the same day overwrites cleanly.
    """
    from . import db
    rows = list(rows)
    if not rows:
        return 0
    sql = """
        INSERT INTO production_daily (
            day, emp_id, name, wc_name,
            units, downtime, hours, days_worked, computed_at
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, now())
        ON CONFLICT (day, emp_id, wc_name) DO UPDATE SET
            name        = EXCLUDED.name,
            units       = EXCLUDED.units,
            downtime    = EXCLUDED.downtime,
            hours       = EXCLUDED.hours,
            days_worked = EXCLUDED.days_worked,
            computed_at = now()
    """
    db.execute_many(sql, [
        (r["day"], r["emp_id"], r["name"], r["wc_name"],
         r["units"], r["downtime"], r["hours"], r["days_worked"])
        for r in rows
    ])
    return len(rows)
