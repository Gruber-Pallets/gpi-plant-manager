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
    """Fleet calls/hour from days with both demand and on-call history.

    Daily call totals are authoritative in ``forklift_calls_daily``. Historical
    utilization reconstruction can create ``forklift_driver_daily`` rows with
    valid on-call time but no per-driver calls, so using that table for both
    sides would understate throughput. Return None when the paired signal is too
    thin so the advisor keeps its existing default-throughput fallback.
    """
    from . import db

    rows = db.query(
        "WITH driver_days AS ("
        "  SELECT day, COALESCE(SUM(on_call_ms), 0) AS ms "
        "  FROM forklift_driver_daily "
        "  WHERE day >= (CURRENT_DATE - %s::int) "
        "  GROUP BY day"
        ") "
        "SELECT COALESCE(SUM(c.total_calls), 0) AS calls, "
        "       COALESCE(SUM(d.ms), 0) AS ms "
        "FROM driver_days d "
        "JOIN forklift_calls_daily c ON c.day = d.day "
        "WHERE d.ms > 0 AND c.total_calls > 0",
        (days,),
    )
    if not rows:
        return None
    calls = float(rows[0]["calls"] or 0)
    hours = float(rows[0]["ms"] or 0) / 3.6e6
    if hours < _MIN_ONCALL_HOURS or calls <= 0:
        return None
    return calls / hours


def recent_claim_seconds(window_days: int = 90) -> float | None:
    """Observed mean time-to-claim (seconds) over the window: the calls-weighted
    mean of forklift_driver_daily.avg_ms. None when there are no calls. This is a
    MEASURED outcome, not a prediction."""
    from . import db
    rows = db.query(
        "SELECT COALESCE(SUM(avg_ms * calls),0) AS wms, COALESCE(SUM(calls),0) AS calls "
        "FROM forklift_driver_daily WHERE day >= (CURRENT_DATE - %s::int)",
        (window_days,),
    )
    if not rows or not rows[0]["calls"]:
        return None
    return float(rows[0]["wms"]) / float(rows[0]["calls"]) / 1000.0


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


def name_map(kind: str) -> dict[str, str]:
    from . import db
    rows = db.query(
        "SELECT forklift_name, plant_name FROM forklift_name_map WHERE kind = %s",
        (kind,),
    )
    return {r["forklift_name"]: r["plant_name"] for r in rows}


def _active_people_by_first_name() -> dict[str, list[str]]:
    """{first-name-casefold: [full plant name, ...]} over active, non-excluded
    people. Used to auto-resolve the forklift app's first-name-only driver
    names to full plant names when the first name is unambiguous."""
    from . import db
    try:
        rows = db.query(
            "SELECT name FROM people WHERE active = TRUE AND NOT excluded"
        )
    except Exception:  # noqa: BLE001 - resolution helper, degrade gracefully
        return {}
    idx: dict[str, list[str]] = {}
    for r in rows:
        parts = (r["name"] or "").split()
        if not parts:
            continue
        idx.setdefault(parts[0].casefold(), []).append(r["name"])
    return idx


def resolve_forklift_to_plant(forklift_names) -> dict[str, str]:
    """Map each forklift driver name to a display name. Priority:
    manual `forklift_name_map` override → unique first-name roster match
    (e.g. "Isidro" → "Isidro Moctezuma") → the raw forklift name when the
    first name is shared (the three "Jesus"es) or unmatched."""
    overrides = name_map("driver")
    idx = _active_people_by_first_name()
    out: dict[str, str] = {}
    for fn in forklift_names:
        if fn in overrides:
            out[fn] = overrides[fn]
            continue
        parts = (fn or "").split()
        matches = idx.get(parts[0].casefold(), []) if parts else []
        out[fn] = matches[0] if len(matches) == 1 else fn
    return out


def resolve_plant_to_forklift(plant_name: str) -> str | None:
    """Inverse of :func:`resolve_forklift_to_plant` for a single plant name.
    Manual override (reversed) wins; else the person's first name when it's
    unique in the roster (that's how the forklift app labels them); else the
    plant name unchanged (a direct forklift_name == plant_name match). A
    shared first name (e.g. "Jesus") therefore never resolves to a single
    driver, so one driver's stats can't land on the wrong person's card —
    that filter simply finds no matching driver rows."""
    overrides = name_map("driver")
    fk = next((f for f, pl in overrides.items() if pl == plant_name), None)
    if fk is not None:
        return fk
    if plant_name in overrides:
        return None  # itself a mapped forklift name with a plant override
    parts = (plant_name or "").split()
    if not parts:
        return None
    matches = _active_people_by_first_name().get(parts[0].casefold(), [])
    return parts[0] if len(matches) == 1 else plant_name
