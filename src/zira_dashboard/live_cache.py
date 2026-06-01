"""Live cache for today's StratusTime + Odoo data.

Owns three single-row JSONB tables (today_attendance_cache,
today_timeoff_cache, today_production_cache). The warmer (in app.py)
overwrites them every 45 s. Live routes read through this module
instead of calling the external APIs in the request path.

The `is_stale` helper supports the cold-start safety valve: if a route
reads a cache row whose refreshed_at is older than ~3 minutes, it can
trigger an inline refresh before returning.
"""
from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta, timezone
from typing import Any

_log = logging.getLogger(__name__)

STALE_THRESHOLD = timedelta(minutes=3)


def _write(table: str, day: date, payload: Any) -> None:
    from . import db
    db.execute(
        f"""
        INSERT INTO {table} (day, payload, refreshed_at)
        VALUES (%s, %s::jsonb, now())
        ON CONFLICT (day) DO UPDATE SET
          payload = EXCLUDED.payload,
          refreshed_at = now()
        """,
        (day, json.dumps(payload, default=str)),
    )


def _read(table: str, day: date) -> tuple[Any | None, datetime | None]:
    from . import db
    rows = db.query(
        f"SELECT payload, refreshed_at FROM {table} WHERE day = %s",
        (day,),
    )
    if not rows:
        return (None, None)
    return (rows[0]["payload"], rows[0]["refreshed_at"])


def write_attendance(day: date, payload: Any) -> None:
    _write("today_attendance_cache", day, payload)


def read_attendance(day: date) -> tuple[Any | None, datetime | None]:
    return _read("today_attendance_cache", day)


def write_timeoff(day: date, payload: Any) -> None:
    _write("today_timeoff_cache", day, payload)


def read_timeoff(day: date) -> tuple[Any | None, datetime | None]:
    return _read("today_timeoff_cache", day)


def write_production(day: date, payload: Any) -> None:
    _write("today_production_cache", day, payload)


def read_production(day: date) -> tuple[Any | None, datetime | None]:
    return _read("today_production_cache", day)


# ---- Odoo open-attendance snapshot (single-row, keyed by person id) ----


def write_open_attendance(snapshot: dict) -> None:
    """Overwrite the single-row Odoo open-attendance snapshot and stamp
    refreshed_at. `snapshot` is {str(person_odoo_id): {att_id, check_in,
    wc_name}}."""
    from . import db
    db.execute(
        """
        INSERT INTO odoo_open_attendance_cache (id, snapshot, refreshed_at)
        VALUES (1, %s::jsonb, now())
        ON CONFLICT (id) DO UPDATE SET
          snapshot = EXCLUDED.snapshot,
          refreshed_at = now()
        """,
        (json.dumps(snapshot, default=str),),
    )


def read_open_attendance() -> tuple[dict | None, datetime | None]:
    """Return (snapshot, refreshed_at). (None, None) if the warmer has
    never run. An empty dict snapshot means 'Odoo shows nobody clocked in'
    — distinct from None, which means 'no data yet, fall back to local'."""
    from . import db
    rows = db.query(
        "SELECT snapshot, refreshed_at FROM odoo_open_attendance_cache "
        "WHERE id = 1"
    )
    if not rows:
        return (None, None)
    return (rows[0]["snapshot"], rows[0]["refreshed_at"])


def refresh_odoo_open_attendance() -> None:
    """Pull every open hr.attendance from Odoo and overwrite the keyed
    snapshot. Errors are logged and swallowed — the previous good snapshot
    stays in place, then falls back to local once it crosses is_stale."""
    try:
        from . import odoo_client
        rows = odoo_client.fetch_open_attendances()
        snapshot = {
            str(r["employee_odoo_id"]): {
                "att_id": r["att_id"],
                "check_in": r["check_in"],
                "wc_name": r["wc_name"],
            }
            for r in rows
        }
        write_open_attendance(snapshot)
    except Exception as e:  # noqa: BLE001 — warmer must never die
        _log.warning("refresh_odoo_open_attendance failed: %s", e)


def is_stale(refreshed_at: datetime | None) -> bool:
    """True if the row is missing or older than STALE_THRESHOLD."""
    if refreshed_at is None:
        return True
    return (datetime.now(timezone.utc) - refreshed_at) > STALE_THRESHOLD


def refresh_attendance(day: date) -> None:
    """Pull today's StratusTime attendance for every known emp_id and
    write the full dict to cache.

    Routes read the cached payload and filter to the emp_ids they care
    about. Caching the superset means one warmer tick serves every
    consumer of attendance data.

    Errors are logged and swallowed — the warmer keeps running and the
    previous good payload (if any) remains in the cache table."""
    try:
        from . import stratustime_client
        emp_ids = list(stratustime_client._employee_id_to_name_map().keys())
        payload = stratustime_client.attendance_for_day(day, emp_ids)
        write_attendance(day, payload)
    except Exception as e:
        _log.warning("refresh_attendance(%s) failed: %s", day, e)


def refresh_timeoff(day: date) -> None:
    """Pull today's StratusTime time-off entries, write to cache."""
    try:
        from . import stratustime_client
        payload = stratustime_client.time_off_entries_for_day(day)
        write_timeoff(day, payload)
    except Exception as e:
        _log.warning("refresh_timeoff(%s) failed: %s", day, e)


def refresh_production(day: date, client) -> None:
    """Refresh today's Zira production AND today's production_daily rows.

    The cache table holds the raw payload (used by the /recycling and
    /new dashboards); production_daily rows are written so MTD / today
    leaderboards
    see today's partial-day data without a separate query path.
    """
    try:
        from . import precompute
        # Side effect: also UPSERTs today's production_daily rows because
        # precompute_day calls attribution_for(day) + flatten + upsert.
        result = precompute.precompute_day(day, client)
        write_production(day, result)
    except Exception as e:
        _log.warning("refresh_production(%s) failed: %s", day, e)
