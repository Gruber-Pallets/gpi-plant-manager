"""Live cache for today's Odoo data.

Owns the single-row JSONB table today_attendance_cache plus the keyed
odoo_open_attendance_cache snapshot. The warmer (in app.py) overwrites
them on a short interval. Live routes read through this module instead
of calling the external APIs in the request path.

The `is_stale` helper supports the cold-start safety valve: if a route
reads a cache row whose refreshed_at is older than ~3 minutes, it can
trigger an inline refresh before returning.
"""
from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta, UTC
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
    return (datetime.now(UTC) - refreshed_at) > STALE_THRESHOLD


def refresh_attendance(day: date) -> None:
    """Pull today's Odoo punches for every employee and write the keyed
    payload to cache: {str(person_odoo_id): {first_check_in, currently_open}}.
    Routes read it and compute status against now (see attendance.compute_status).

    Errors are logged and swallowed — the warmer keeps running and the
    previous good payload (if any) remains in the cache table."""
    try:
        from . import attendance
        payload = attendance.punches_for_day(day)
        write_attendance(day, payload)
    except Exception as e:
        _log.warning("refresh_attendance(%s) failed: %s", day, e)


def refresh_production(day: date, client) -> None:
    """UPSERT today's production_daily rows from Zira.

    precompute_day calls attribution_for(day) + flatten + upsert, so MTD /
    today leaderboards see today's partial-day data without a separate query
    path. (There is no separate production cache row — production_daily is the
    store.)
    """
    try:
        from . import precompute
        precompute.precompute_day(day, client)
    except Exception as e:
        _log.warning("refresh_production(%s) failed: %s", day, e)
