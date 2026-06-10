"""Persistent cache for Zira leaderboard results.

Past-day production data is immutable. Serializing each StationTotal
to JSONB in `zira_daily_cache` lets us survive Railway redeploys
without re-paying the Zira API cost.

Today's rows are upserted here too (throttled in leaderboard.py to at
most once per 5 minutes per station) so a redeploy or the midnight
day-rollover doesn't lose the in-progress snapshot. Live reads of
today's data still come from the in-process TODAY cache in
leaderboard.py, not this table.

Schema (_schema.SCHEMA_DDL):
    CREATE TABLE zira_daily_cache (
        meter_id    TEXT,
        day         DATE,
        payload     JSONB,
        computed_at TIMESTAMPTZ,
        PRIMARY KEY (meter_id, day)
    );

Per-station rows let partial cache hits work across overlapping
station sets (e.g., /recycling and /new may share some stations).
"""

from __future__ import annotations

import json
from datetime import date, datetime, timezone

from .leaderboard import StationTotal
from .stations import Station


def _serialize_dt(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


def _deserialize_dt(s: str | None) -> datetime | None:
    if not s:
        return None
    # Postgres-stored ISO with tz; isoformat round-trips cleanly.
    return datetime.fromisoformat(s)


def _serialize_total(total: StationTotal) -> dict:
    """StationTotal -> JSON-serializable dict."""
    return {
        "station": {
            "meter_id": total.station.meter_id,
            "name": total.station.name,
            "category": total.station.category,
            "cell": total.station.cell,
        },
        "units": total.units,
        "reading_count": total.reading_count,
        "truncated": total.truncated,
        "downtime_minutes": total.downtime_minutes,
        "active_minutes": total.active_minutes,
        "last_reading_at": _serialize_dt(total.last_reading_at),
        "last_status": total.last_status,
        "samples": [[_serialize_dt(s[0]), s[1]] for s in total.samples],
        "active_intervals": [
            [_serialize_dt(a), _serialize_dt(b)] for a, b in total.active_intervals
        ],
    }


def _deserialize_total(payload: dict) -> StationTotal:
    """JSON dict -> StationTotal."""
    s = payload["station"]
    station = Station(
        meter_id=s["meter_id"],
        name=s["name"],
        category=s["category"],
        cell=s["cell"],
    )
    return StationTotal(
        station=station,
        units=payload["units"],
        reading_count=payload["reading_count"],
        truncated=payload["truncated"],
        downtime_minutes=payload["downtime_minutes"],
        active_minutes=payload["active_minutes"],
        last_reading_at=_deserialize_dt(payload.get("last_reading_at")),
        last_status=payload.get("last_status"),
        samples=tuple(
            (_deserialize_dt(s[0]), s[1]) for s in payload.get("samples", [])
        ),
        active_intervals=tuple(
            (_deserialize_dt(a), _deserialize_dt(b))
            for a, b in payload.get("active_intervals", [])
        ),
    )


def load_day(stations: list[Station], day: date) -> list[StationTotal] | None:
    """Return cached StationTotal list for the given (stations, day),
    or None if any station's row is missing.

    Returns the totals in the same order as `stations`, except sorted
    by (-units, name) to match the existing leaderboard contract.
    """
    if not stations:
        return None
    from . import db
    meter_ids = [s.meter_id for s in stations]
    rows = db.query(
        "SELECT meter_id, payload FROM zira_daily_cache "
        "WHERE day = %s AND meter_id = ANY(%s)",
        (day, meter_ids),
    )
    if len(rows) < len(meter_ids):
        # Partial cache miss — easier to refetch all than reason about
        # which stations need refresh.
        return None
    by_meter = {r["meter_id"]: r["payload"] for r in rows}
    totals = []
    for s in stations:
        payload = by_meter.get(s.meter_id)
        if payload is None:
            return None
        # psycopg2 returns JSONB as a Python dict already; if the driver
        # returns a string (some configurations), parse it.
        if isinstance(payload, str):
            payload = json.loads(payload)
        totals.append(_deserialize_total(payload))
    totals.sort(key=lambda r: (-r.units, r.station.name))
    return totals


def save_day(totals: list[StationTotal], day: date) -> None:
    """Upsert each StationTotal as a row keyed by (meter_id, day)."""
    from . import db
    if not totals:
        return
    now_ts = datetime.now(timezone.utc)
    rows = [
        (
            total.station.meter_id,
            day,
            json.dumps(_serialize_total(total)),
            now_ts,
        )
        for total in totals
    ]
    with db.cursor() as cur:
        # execute_values folds every row into one statement — a single
        # round-trip instead of executemany's one per row.
        db.execute_values(
            cur,
            "INSERT INTO zira_daily_cache (meter_id, day, payload, computed_at) "
            "VALUES %s "
            "ON CONFLICT (meter_id, day) DO UPDATE SET "
            "  payload = EXCLUDED.payload, computed_at = EXCLUDED.computed_at",
            rows,
            template="(%s, %s, %s::jsonb, %s)",
        )
