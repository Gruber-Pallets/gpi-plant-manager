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
from collections.abc import Iterable


def flatten_attribution(
    day: date,
    attribution: dict[str, dict[str, dict[str, float]]],
    name_to_emp_id: dict[str, str],
) -> list[dict]:
    """Turn {person: {wc: {units, downtime, hours, days_worked,
    excluded_minutes}}} into a flat list of rows ready for UPSERT into
    production_daily.

    Rows where units == 0 are dropped (the attribution dict can carry
    zero-unit rows for multi-person WCs with no production; they add
    no value in the table).

    Operators not found in the name->id map fall back to using their name
    as the row key (the column is TEXT and every production_daily read is
    by name), so a production row is never silently dropped.
    """
    rows: list[dict] = []
    for person, wc_map in attribution.items():
        emp_id = name_to_emp_id.get(person) or person  # fall back to name; never drop
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
                "excluded_minutes": float(totals.get("excluded_minutes") or 0),
            })
    return rows


def upsert_production_daily(rows: Iterable[dict]) -> int:
    """UPSERT a batch of rows into production_daily. Returns count written.

    Idempotent: PK conflict triggers an UPDATE of every non-PK column
    and bumps `computed_at`. Re-running the same day overwrites cleanly.

    `excluded_minutes` is read with a default of 0 (rather than a bare
    index) so callers that still build rows without the key -- pre-dating
    the machine-breakdown feature -- keep working unchanged.
    """
    from . import db
    rows = list(rows)
    if not rows:
        return 0
    sql = """
        INSERT INTO production_daily (
            day, emp_id, name, wc_name,
            units, downtime, hours, days_worked, excluded_minutes, computed_at
        ) VALUES %s
        ON CONFLICT (day, emp_id, wc_name) DO UPDATE SET
            name             = EXCLUDED.name,
            units            = EXCLUDED.units,
            downtime         = EXCLUDED.downtime,
            hours            = EXCLUDED.hours,
            days_worked      = EXCLUDED.days_worked,
            excluded_minutes = EXCLUDED.excluded_minutes,
            computed_at      = now()
    """
    with db.cursor() as cur:
        # execute_values folds every row into one statement — a single
        # round-trip instead of executemany's one per row (this runs
        # every 45s from the live warmer).
        db.execute_values(cur, sql, [
            (r["day"], r["emp_id"], r["name"], r["wc_name"],
             r["units"], r["downtime"], r["hours"], r["days_worked"],
             r.get("excluded_minutes", 0))
            for r in rows
        ], template="(%s, %s, %s, %s, %s, %s, %s, %s, %s, now())")
    return len(rows)


def precompute_day(day: date, client) -> dict:
    """Compute attribution for one day and UPSERT into production_daily.

    Returns {"day": iso, "rows_written": int}. Idempotent; safe to re-run.
    """
    from . import production_history, attendance
    attribution = production_history.attribution_for(day, client)
    name_to_emp_id = attendance.name_to_person_id()
    rows = flatten_attribution(day, attribution, name_to_emp_id)
    written = upsert_production_daily(rows)
    return {"day": day.isoformat(), "rows_written": written}


def sum_by_range(
    start: date,
    end: date,
    wc_names: list[str] | None = None,
    group_by: str = "name",
) -> list[dict]:
    """Sum units / downtime / hours / days_worked over [start, end]
    grouped by `group_by` (currently only "name").

    `wc_names` filters which WCs to include. None = all WCs.
    """
    from . import db
    if group_by != "name":
        raise ValueError(f"group_by must be 'name'; got {group_by!r}")
    params: list = [start, end]
    sql = """
        SELECT name,
               SUM(units)       AS units,
               SUM(downtime)    AS downtime,
               SUM(hours)       AS hours,
               SUM(days_worked) AS days_worked
        FROM production_daily
        WHERE day BETWEEN %s AND %s
    """
    if wc_names:
        sql += " AND wc_name = ANY(%s)"
        params.append(list(wc_names))
    sql += " GROUP BY name"
    return db.query(sql, params)


def sum_by_name(name: str, start: date, end: date) -> list[dict]:
    """Per-WC totals for one person across [start, end].

    Return rows: {wc_name, units, downtime, hours, days_worked}.
    """
    from . import db
    return db.query(
        """
        SELECT wc_name,
               SUM(units)       AS units,
               SUM(downtime)    AS downtime,
               SUM(hours)       AS hours,
               SUM(days_worked) AS days_worked
        FROM production_daily
        WHERE name = %s AND day BETWEEN %s AND %s
        GROUP BY wc_name
        """,
        (name, start, end),
    )


def daily_records_in_range(start: date, end: date) -> list[dict]:
    """One row per (day, person, wc) in [start, end], matching the shape
    of the existing `production_history.daily_records` so awards/trophy
    code can swap over with no behavior change.

    Each row: {day, person, wc, units, downtime, hours, excluded_minutes}.
    """
    from . import db
    rows = db.query(
        """
        SELECT day, name AS person, wc_name AS wc,
               units, downtime, hours, excluded_minutes
        FROM production_daily
        WHERE day BETWEEN %s AND %s AND units > 0
          AND NOT EXISTS (
            SELECT 1 FROM manual_absences ma
            WHERE ma.day = production_daily.day
              AND ma.name = production_daily.name
          )
        """,
        (start, end),
    )
    return [
        {
            "day": r["day"],
            "person": r["person"],
            "wc": r["wc"],
            "units": float(r["units"]),
            "downtime": float(r["downtime"]),
            "hours": float(r["hours"]),
            "excluded_minutes": float(r["excluded_minutes"]),
        }
        for r in rows
    ]
