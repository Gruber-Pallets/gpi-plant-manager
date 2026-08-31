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
from dataclasses import dataclass
from datetime import date, datetime, timedelta, UTC
from typing import Any

from . import attendance_location_policy, attendance_mirror, work_centers_store

_log = logging.getLogger(__name__)

STALE_THRESHOLD = timedelta(minutes=3)


@dataclass(frozen=True)
class AttendanceReadPolicy:
    mirror_owned: bool
    available: bool
    refreshed_at: datetime | None
    error: str | None = None
    mode: str = "off"
    stale: bool = False


@dataclass(frozen=True)
class AttendanceSourceSnapshot:
    payload: dict | None
    refreshed_at: datetime | None
    mirror_owned: bool
    available: bool
    error: str | None = None
    stale: bool = False


def attendance_read_policy(*, now_utc: datetime | None = None) -> AttendanceReadPolicy:
    """Freeze the rollout and mirror-health decision for one source read."""
    frozen_now = now_utc or datetime.now(UTC)
    if frozen_now.utcoffset() is None:
        raise ValueError("now_utc must be timezone-aware")
    frozen_now = frozen_now.astimezone(UTC)
    try:
        config = attendance_location_policy.get_rollout_config()
    except Exception as exc:  # Existing rollback path when settings cannot be read.
        return AttendanceReadPolicy(False, True, None, str(exc), "off")
    if config.mode == "off":
        return AttendanceReadPolicy(False, True, None, mode="off")
    try:
        health = attendance_mirror.health_snapshot()
    except Exception as exc:  # Saved shadow/live must never fall through to legacy.
        return AttendanceReadPolicy(True, False, None, str(exc), config.mode)
    if health.baseline_completed_at is None:
        return AttendanceReadPolicy(False, True, None, mode=config.mode)
    refreshed_at = health.last_incremental_completed_at
    if refreshed_at is None:
        return AttendanceReadPolicy(
            True,
            False,
            None,
            health.last_error or "attendance mirror has no verified refresh",
            config.mode,
        )
    return AttendanceReadPolicy(
        True,
        True,
        refreshed_at,
        health.last_error,
        config.mode,
        frozen_now - refreshed_at.astimezone(UTC) > STALE_THRESHOLD,
    )


def mirror_owns_attendance_reads() -> bool:
    return attendance_read_policy().mirror_owned


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


def read_attendance_source(
    day: date,
    *,
    policy: AttendanceReadPolicy | None = None,
) -> AttendanceSourceSnapshot:
    policy = policy or attendance_read_policy()
    if not policy.mirror_owned:
        payload, refreshed_at = read_attendance(day)
        return AttendanceSourceSnapshot(
            payload, refreshed_at, False, True, policy.error, policy.stale
        )
    if not policy.available:
        return AttendanceSourceSnapshot(
            None, policy.refreshed_at, True, False, policy.error, policy.stale
        )
    try:
        payload = attendance_mirror.day_presence(day)
    except Exception as exc:
        return AttendanceSourceSnapshot(
            None, policy.refreshed_at, True, False, str(exc), policy.stale
        )
    return AttendanceSourceSnapshot(
        payload, policy.refreshed_at, True, True, policy.error, policy.stale
    )


def read_attendance(day: date) -> tuple[Any | None, datetime | None]:
    """Legacy cache read retained for rollback and compatibility callers."""
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


def _read_open_attendance_legacy() -> tuple[dict | None, datetime | None]:
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


def read_open_attendance_source(
    *,
    policy: AttendanceReadPolicy | None = None,
) -> AttendanceSourceSnapshot:
    policy = policy or attendance_read_policy()
    if not policy.mirror_owned:
        payload, refreshed_at = read_open_attendance()
        return AttendanceSourceSnapshot(
            payload, refreshed_at, False, True, policy.error, policy.stale
        )
    if not policy.available:
        return AttendanceSourceSnapshot(
            None, policy.refreshed_at, True, False, policy.error, policy.stale
        )
    try:
        rows = attendance_mirror.current_open_attendance()
        snapshot: dict[str, dict] = {}
        for row in rows:
            person_id = str(row["employee_odoo_id"])
            attendance_id = int(row["odoo_attendance_id"])
            check_in = row["check_in_utc"]
            mapped_wc = work_centers_store.app_work_center_name_for_odoo_id(
                row.get("odoo_work_center_id")
            )
            candidate = {
                "att_id": attendance_id,
                "check_in": check_in.isoformat(),
                "wc_name": mapped_wc,
                "raw_odoo_wc_name": row.get("odoo_work_center_name"),
            }
            existing = snapshot.get(person_id)
            candidate_key = (check_in, attendance_id)
            existing_key = (
                (datetime.fromisoformat(existing["check_in"]), int(existing["att_id"]))
                if existing
                else None
            )
            if existing_key is None or candidate_key > existing_key:
                snapshot[person_id] = candidate
    except Exception as exc:
        return AttendanceSourceSnapshot(
            None, policy.refreshed_at, True, False, str(exc), policy.stale
        )
    return AttendanceSourceSnapshot(
        snapshot, policy.refreshed_at, True, True, policy.error, policy.stale
    )


def read_open_attendance() -> tuple[dict | None, datetime | None]:
    """Legacy cache read retained for rollback and compatibility callers."""
    return _read_open_attendance_legacy()


def refresh_odoo_open_attendance() -> None:
    """Pull every open hr.attendance from Odoo and overwrite the keyed
    snapshot. Errors are logged and swallowed — the previous good snapshot
    stays in place, then falls back to local once it crosses is_stale."""
    if mirror_owns_attendance_reads():
        return
    try:
        from . import odoo_client
        rows = odoo_client.fetch_open_attendances()
        # A tablet sign-in can briefly leave more than one attendance row open
        # for an employee.  Odoo does not promise a row order here, so a plain
        # dict comprehension could preserve an earlier mistaken station. Keep
        # the newest attendance explicitly; its work-center tag is the live
        # source of truth for the dashboard and timeclock.
        snapshot: dict[str, dict] = {}
        for row in rows:
            person_id = str(row["employee_odoo_id"])
            candidate = {
                "att_id": row["att_id"],
                "check_in": row["check_in"],
                "wc_name": row["wc_name"],
            }
            existing = snapshot.get(person_id)
            candidate_key = (str(candidate["check_in"] or ""), int(candidate["att_id"] or 0))
            existing_key = (
                (str(existing["check_in"] or ""), int(existing["att_id"] or 0))
                if existing else None
            )
            if existing_key is None or candidate_key > existing_key:
                snapshot[person_id] = candidate
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
    if mirror_owns_attendance_reads():
        return
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
