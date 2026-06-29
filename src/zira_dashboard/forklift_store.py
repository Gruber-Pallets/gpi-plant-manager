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


def upsert_driver_metrics(rows: list[dict]) -> int:
    """Fill on-time/late/utilization columns for existing driver-day rows
    without touching calls/avg_ms/max_ms. Rows missing in the table are
    inserted with calls=0 (reconstruction may run before the snapshot).

    Used by both write paths (the warmer's forward capture and the one-time
    historical reconstruction); the per-call completions feed can't supply
    these, so they come from the dashboard endpoint."""
    from . import db
    rows = list(rows)
    if not rows:
        return 0
    with db.cursor() as cur:
        for r in rows:
            cur.execute(
                """
                INSERT INTO forklift_driver_daily
                    (day, driver_id, name, calls, on_time, late,
                     avg_ms, max_ms, utilization_pct, on_call_ms, available_ms,
                     computed_at)
                VALUES (%(day)s, %(driver_id)s, %(name)s, 0, %(on_time)s, %(late)s,
                        0, 0, %(utilization_pct)s, %(on_call_ms)s, %(available_ms)s,
                        now())
                ON CONFLICT (day, driver_id) DO UPDATE SET
                    on_time = EXCLUDED.on_time,
                    late = EXCLUDED.late,
                    utilization_pct = EXCLUDED.utilization_pct,
                    on_call_ms = EXCLUDED.on_call_ms,
                    available_ms = EXCLUDED.available_ms,
                    computed_at = now()
                """,
                {"name": r.get("name", r["driver_id"]), **r},
            )
    return len(rows)


def driver_rows_for_day(day) -> list[dict]:
    from . import db
    return db.query(
        "SELECT * FROM forklift_driver_daily WHERE day = %s", (day,)
    )


def driver_days_between(start, end) -> list[dict]:
    """All per-driver per-day rows in [start, end], ordered by day. The
    range source for forklift_awards' scoring/leaderboard computations."""
    from . import db
    return db.query(
        "SELECT * FROM forklift_driver_daily WHERE day BETWEEN %s AND %s "
        "ORDER BY day",
        (start, end),
    )


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


def ontime_history_day_count() -> int:
    """How many distinct days actually have on-time/utilization data. Used to
    decide whether to run the one-time on-time history reconstruction (the
    completions feed that drives history_day_count() can't supply on-time)."""
    from . import db
    rows = db.query(
        "SELECT COUNT(DISTINCT day) AS n FROM forklift_driver_daily "
        "WHERE on_time > 0 OR late > 0"
    )
    return int(rows[0]["n"]) if rows else 0


def mean_handle_seconds(window_days: int = 90) -> float | None:
    """Mean handling time per call (seconds) over the window:
    sum(on_call_ms) / sum(calls) / 1000 from forklift_driver_daily.
    None when there are no calls in the window."""
    from . import db
    rows = db.query(
        "SELECT COALESCE(SUM(on_call_ms),0) AS ms, COALESCE(SUM(calls),0) AS calls "
        "FROM forklift_driver_daily WHERE day >= (CURRENT_DATE - %s::int)",
        (window_days,),
    )
    if not rows or not rows[0]["calls"]:
        return None
    return float(rows[0]["ms"]) / float(rows[0]["calls"]) / 1000.0


# Eligibility floor for a calibration day: too few calls makes the day's rate
# and actual wait too noisy to learn from.
_MIN_DAY_CALLS = 20


def _operating_hours(raw_by_hour) -> int:
    """Count of hours with calls > 0 in a day's raw `by_hour` JSONB payload.
    Total and never raises: a non-JSON-string, a non-dict payload, or a
    non-dict hour entry all coerce to "no operating hours" / "skip the hour"
    rather than propagating (honors calibration_samples' "never raises" docstring).
    Parses the value itself (handles psycopg2's dict or a stray JSON string)."""
    try:
        by_hour = _coerce_json(raw_by_hour)
    except (TypeError, ValueError, AttributeError):
        return 0
    if not isinstance(by_hour, dict):
        return 0
    op_hours = 0
    for payload in by_hour.values():
        try:
            if float((payload or {}).get("calls") or 0) > 0:
                op_hours += 1
        except (TypeError, ValueError, AttributeError):
            continue
    return op_hours


def calibration_samples(window_days: int = 90) -> list[dict]:
    """One row per eligible historical day for fitting the queue model:
      - avg_lambda: total calls / operating hours (hours with calls > 0 that day,
        read from forklift_calls_daily.by_hour);
      - crew: distinct drivers with calls that day (forklift_driver_daily);
      - actual_wait_seconds: the day's calls-weighted mean avg_ms / 1000.
    Eligible = day total calls >= 20 and >= 1 active driver. Never raises into
    the request path; a day we can't parse is just skipped."""
    from . import db
    # operating hours + total calls per day come from the hourly shape in by_hour.
    calls_rows = db.query(
        "SELECT day, total_calls, by_hour FROM forklift_calls_daily "
        "WHERE day >= (CURRENT_DATE - %s::int)",
        (window_days,),
    )
    cmap: dict = {}
    for r in calls_rows:
        op_hours = _operating_hours(r["by_hour"])
        cmap[r["day"]] = {"total": int(r["total_calls"] or 0), "op_hours": op_hours}

    drv = db.query(
        "SELECT day, COUNT(DISTINCT driver_id) FILTER (WHERE calls > 0) AS crew, "
        "COALESCE(SUM(avg_ms * calls),0) AS wms, COALESCE(SUM(calls),0) AS calls "
        "FROM forklift_driver_daily WHERE day >= (CURRENT_DATE - %s::int) "
        "GROUP BY day",
        (window_days,),
    )
    out = []
    for r in drv:
        c = cmap.get(r["day"])
        if not c or not c["op_hours"] or not c["total"]:
            continue
        calls = int(r["calls"] or 0)
        crew = int(r["crew"] or 0)
        if c["total"] < _MIN_DAY_CALLS or crew < 1 or calls < 1:
            continue
        avg_lambda = float(c["total"]) / float(c["op_hours"])
        actual_wait = float(r["wms"]) / calls / 1000.0
        out.append({"day": r["day"], "avg_lambda": avg_lambda,
                    "crew": crew, "actual_wait_seconds": actual_wait})
    return out


def name_map(kind: str) -> dict[str, str]:
    from . import db
    rows = db.query(
        "SELECT forklift_name, plant_name FROM forklift_name_map WHERE kind = %s",
        (kind,),
    )
    return {r["forklift_name"]: r["plant_name"] for r in rows}
