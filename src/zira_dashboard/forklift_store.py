"""Read/write the forklift snapshot tables. Mirrors the precompute/store
patterns: execute_values batch UPSERT for drivers, single-row UPSERT for the
day summary, plain reads for the advisor. JSONB columns round-trip via
psycopg2's Json adapter."""
from __future__ import annotations

import json

from psycopg2.extras import Json


def upsert_calls_daily(row: dict) -> None:
    from . import db
    db.execute(
        """
        INSERT INTO forklift_calls_daily (
            day, total_calls, urgent_calls, overload_count, neglected_count,
            by_hour, by_station, by_skill, computed_at
        ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s, now())
        ON CONFLICT (day) DO UPDATE SET
            total_calls=EXCLUDED.total_calls, urgent_calls=EXCLUDED.urgent_calls,
            overload_count=EXCLUDED.overload_count,
            neglected_count=EXCLUDED.neglected_count,
            by_hour=EXCLUDED.by_hour, by_station=EXCLUDED.by_station,
            by_skill=EXCLUDED.by_skill, computed_at=now()
        """,
        (row["day"], row["total_calls"], row["urgent_calls"],
         row["overload_count"], row["neglected_count"],
         Json(row["by_hour"]), Json(row["by_station"]), Json(row["by_skill"])),
    )


def upsert_driver_daily(rows: list[dict]) -> int:
    from . import db
    rows = list(rows)
    if not rows:
        return 0
    sql = """
        INSERT INTO forklift_driver_daily (
            day, driver_id, name, calls, on_time, late, avg_ms, max_ms,
            utilization_pct, on_call_ms, available_ms, computed_at
        ) VALUES %s
        ON CONFLICT (day, driver_id) DO UPDATE SET
            name=EXCLUDED.name, calls=EXCLUDED.calls, on_time=EXCLUDED.on_time,
            late=EXCLUDED.late, avg_ms=EXCLUDED.avg_ms, max_ms=EXCLUDED.max_ms,
            utilization_pct=EXCLUDED.utilization_pct,
            on_call_ms=EXCLUDED.on_call_ms, available_ms=EXCLUDED.available_ms,
            computed_at=now()
    """
    with db.cursor() as cur:
        db.execute_values(cur, sql, [
            (r["day"], r["driver_id"], r["name"], r["calls"], r["on_time"],
             r["late"], r["avg_ms"], r["max_ms"], r["utilization_pct"],
             r["on_call_ms"], r["available_ms"])
            for r in rows
        ], template="(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s, now())")
    return len(rows)


def _coerce_json(value):
    """psycopg2 returns JSONB as dict already; tolerate str just in case."""
    return json.loads(value) if isinstance(value, str) else (value or {})


def calls_daily_for_weekday(weekday: int, limit: int = 8) -> list[dict]:
    """Most-recent `limit` snapshots whose day-of-week == weekday (Mon=0)."""
    from . import db
    rows = db.query(
        "SELECT * FROM forklift_calls_daily "
        "WHERE EXTRACT(ISODOW FROM day) = %s "   # ISODOW: Mon=1..Sun=7
        "ORDER BY day DESC LIMIT %s",
        (weekday + 1, limit),
    )
    for r in rows:
        r["by_hour"] = _coerce_json(r["by_hour"])
        r["by_station"] = _coerce_json(r["by_station"])
        r["by_skill"] = _coerce_json(r["by_skill"])
    return rows


# Below this many total on-call hours in the window, the derived rate is too
# noisy to trust -> caller falls back to the default throughput.
_MIN_ONCALL_HOURS = 2.0


def recent_driver_throughput(days: int = 28) -> float | None:
    """Data-derived per-driver throughput (calls/hour) = total completed calls
    / total on-call hours across forklift_driver_daily in the last `days`.
    None when there isn't enough on-call time to be meaningful."""
    from . import db
    rows = db.query(
        "SELECT COALESCE(SUM(calls),0) AS calls, COALESCE(SUM(on_call_ms),0) AS ms "
        "FROM forklift_driver_daily WHERE day >= (CURRENT_DATE - %s::int)",
        (days,),
    )
    if not rows:
        return None
    calls = float(rows[0]["calls"] or 0)
    hours = float(rows[0]["ms"] or 0) / 3.6e6
    if hours < _MIN_ONCALL_HOURS or calls <= 0:
        return None
    return calls / hours


def history_day_count() -> int:
    """How many distinct days of demand history we've snapshotted. Used to
    decide whether to run the one-time full-history backfill."""
    from . import db
    rows = db.query("SELECT COUNT(*) AS n FROM forklift_calls_daily")
    return int(rows[0]["n"]) if rows else 0


def name_map(kind: str) -> dict[str, str]:
    from . import db
    rows = db.query(
        "SELECT forklift_name, plant_name FROM forklift_name_map WHERE kind = %s",
        (kind,),
    )
    return {r["forklift_name"]: r["plant_name"] for r in rows}
