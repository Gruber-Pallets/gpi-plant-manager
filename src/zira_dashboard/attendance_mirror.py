"""Durable, transactional mirror of normalized Odoo attendance rows."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
import json
from typing import Any

from . import db
from .shift_config import SITE_TZ


_ERROR_LIMIT = 500
_ERROR_OWNERS = ("incremental", "sweep", "baseline")
_ERROR_DISPLAY_ORDER = (*_ERROR_OWNERS, "legacy")
_SYNC_ADVISORY_LOCK_KEY = 0x5A49524141545445
_ROW_FIELDS = (
    "odoo_attendance_id",
    "employee_odoo_id",
    "employee_name",
    "check_in_utc",
    "check_out_utc",
    "odoo_work_center_id",
    "odoo_work_center_name",
    "odoo_department_id",
    "odoo_department_name",
    "odoo_write_date",
)
_MATERIAL_FIELDS = (
    "employee_odoo_id",
    "check_in_utc",
    "check_out_utc",
    "odoo_work_center_id",
    "odoo_work_center_name",
    "odoo_department_id",
    "odoo_department_name",
)


@dataclass(frozen=True)
class MirrorHealth:
    last_incremental_completed_at: datetime | None
    last_full_sweep_completed_at: datetime | None
    baseline_completed_at: datetime | None
    oldest_recalc_requested_at: datetime | None
    last_error: str | None


@dataclass(frozen=True)
class SyncState:
    cursor_write_date: datetime | None
    cursor_id: int | None
    last_incremental_completed_at: datetime | None
    last_full_sweep_completed_at: datetime | None
    full_sweep_generation: int
    baseline_completed_at: datetime | None


@dataclass(frozen=True)
class _FullSweepStoreResult:
    affected_days: frozenset[date]
    deleted_count: int


@contextmanager
def _logical_run_lock():
    """Serialize a complete attendance source snapshot through its commit."""
    with db.cursor() as cur:
        cur.execute(
            "SELECT pg_advisory_xact_lock(%s)",
            (_SYNC_ADVISORY_LOCK_KEY,),
        )
        yield cur


def _decode_error_state(raw: object) -> dict[str, str]:
    if raw is None or str(raw) == "":
        return {}
    text = str(raw)
    try:
        decoded = json.loads(text)
    except (TypeError, ValueError, json.JSONDecodeError):
        return {"legacy": text[:_ERROR_LIMIT]}
    if not isinstance(decoded, dict):
        return {"legacy": text[:_ERROR_LIMIT]}
    errors = {
        owner: str(decoded[owner])
        for owner in _ERROR_DISPLAY_ORDER
        if owner in decoded and str(decoded[owner])
    }
    return errors or {"legacy": text[:_ERROR_LIMIT]}


def _encode_error_state(errors: Mapping[str, str]) -> str | None:
    values = {
        owner: str(errors[owner])
        for owner in _ERROR_DISPLAY_ORDER
        if owner in errors and str(errors[owner])
    }
    if not values:
        return None
    per_owner_limit = _ERROR_LIMIT
    while True:
        bounded = {owner: value[:per_owner_limit] for owner, value in values.items()}
        encoded = json.dumps(bounded, separators=(",", ":"), sort_keys=True)
        if len(encoded) <= _ERROR_LIMIT:
            return encoded
        excess = len(encoded) - _ERROR_LIMIT
        per_owner_limit = max(
            1,
            per_owner_limit - ((excess + len(bounded) - 1) // len(bounded)),
        )


def _require_error_owner(owner: str) -> str:
    if owner not in _ERROR_OWNERS:
        raise ValueError(f"invalid attendance sync error owner: {owner}")
    return owner


def _error_with_failure(raw: object, owner: str, error: object) -> str:
    owner = _require_error_owner(owner)
    errors = _decode_error_state(raw)
    errors[owner] = str(error) or "unknown error"
    encoded = _encode_error_state(errors)
    assert encoded is not None
    return encoded


def _error_after_success(raw: object, owner: str) -> str | None:
    owner = _require_error_owner(owner)
    errors = _decode_error_state(raw)
    errors.pop(owner, None)
    return _encode_error_state(errors)


def _format_error_state(raw: object) -> str | None:
    if raw is None or str(raw) == "":
        return None
    errors = _decode_error_state(raw)
    formatted = "; ".join(
        f"{owner}: {errors[owner]}" for owner in _ERROR_DISPLAY_ORDER if owner in errors
    )
    if set(errors) == {"legacy"} and not str(raw).lstrip().startswith("{"):
        return errors["legacy"][:_ERROR_LIMIT]
    return formatted[:_ERROR_LIMIT] or None


def _aware_utc(value: Any, field_name: str) -> datetime:
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise TypeError(f"{field_name} must be an aware datetime")
    return value.astimezone(UTC)


def _optional_aware_utc(value: Any, field_name: str) -> datetime | None:
    if value is None:
        return None
    return _aware_utc(value, field_name)


def _positive_int(value: Any, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{field_name} must be a positive integer")
    return value


def _optional_positive_int(value: Any, field_name: str) -> int | None:
    if value is None:
        return None
    return _positive_int(value, field_name)


def _optional_text(value: Any, field_name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be text or None")
    return value


def _normalize_row(row: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(row, Mapping):
        raise TypeError("attendance row must be a mapping")
    missing = [field for field in _ROW_FIELDS if field not in row]
    if missing:
        raise ValueError("attendance row omitted required field(s): " + ", ".join(missing))
    check_in = _aware_utc(row["check_in_utc"], "check_in_utc")
    check_out = _optional_aware_utc(row["check_out_utc"], "check_out_utc")
    if check_out is not None and check_out < check_in:
        raise ValueError("check_out_utc cannot be before check_in_utc")
    return {
        "odoo_attendance_id": _positive_int(row["odoo_attendance_id"], "odoo_attendance_id"),
        "employee_odoo_id": _positive_int(row["employee_odoo_id"], "employee_odoo_id"),
        "employee_name": _optional_text(row["employee_name"], "employee_name"),
        "check_in_utc": check_in,
        "check_out_utc": check_out,
        "odoo_work_center_id": _optional_positive_int(
            row["odoo_work_center_id"], "odoo_work_center_id"
        ),
        "odoo_work_center_name": _optional_text(
            row["odoo_work_center_name"], "odoo_work_center_name"
        ),
        "odoo_department_id": _optional_positive_int(
            row["odoo_department_id"], "odoo_department_id"
        ),
        "odoo_department_name": _optional_text(row["odoo_department_name"], "odoo_department_name"),
        "odoo_write_date": _aware_utc(row["odoo_write_date"], "odoo_write_date"),
    }


def _normalized_rows(rows: Sequence[dict]) -> tuple[dict[str, Any], ...]:
    if isinstance(rows, (str, bytes)) or not isinstance(rows, Sequence):
        raise TypeError("rows must be a sequence")
    newest: dict[int, dict[str, Any]] = {}
    for raw_row in rows:
        row = _normalize_row(raw_row)
        attendance_id = row["odoo_attendance_id"]
        existing = newest.get(attendance_id)
        if existing is None or row["odoo_write_date"] >= existing["odoo_write_date"]:
            newest[attendance_id] = row
    return tuple(newest[key] for key in sorted(newest))


def local_days_touched(start_utc: datetime, end_utc: datetime | None) -> set[date]:
    """Return plant calendar days intersected by the half-open UTC interval."""
    start = _aware_utc(start_utc, "start_utc")
    end = _optional_aware_utc(end_utc, "end_utc")
    if end is None:
        return {start.astimezone(SITE_TZ).date()}
    if end == start:
        return set()
    first = start.astimezone(SITE_TZ).date()
    # Attendance intervals are half-open. A checkout exactly at local midnight
    # belongs to the preceding day, not to a zero-length slice of the next day.
    last = (end - timedelta(microseconds=1)).astimezone(SITE_TZ).date()
    days: set[date] = set()
    current = first
    while current <= last:
        days.add(current)
        current += timedelta(days=1)
    return days


def _row_days(row: Mapping[str, Any], open_end: datetime) -> set[date]:
    return local_days_touched(row["check_in_utc"], row["check_out_utc"] or open_end)


def _materially_different(existing: Mapping[str, Any], incoming: Mapping[str, Any]) -> bool:
    return any(existing[field] != incoming[field] for field in _MATERIAL_FIELDS)


def _observation_can_revive(*, observed_at: datetime, deleted_at: datetime | None) -> bool:
    """Return whether an observation is newer than a confirmed deletion."""
    observed = _aware_utc(observed_at, "observed_at")
    deleted = _optional_aware_utc(deleted_at, "deleted_at")
    return deleted is None or observed > deleted


def _enqueue_recalc_cur(
    cur,
    days: Iterable[date],
    reason: str,
    *,
    requested_at: datetime,
) -> None:
    unique_days = sorted(set(days))
    for day in unique_days:
        if not isinstance(day, date) or isinstance(day, datetime):
            raise TypeError("recalculation days must be date values")
        cur.execute(
            "INSERT INTO attendance_recalc_queue "
            "(day, reason, requested_at, started_at, completed_at, "
            "cache_started_at, cache_ready_at, attempt_count, last_error) "
            "VALUES (%s, %s, %s, NULL, NULL, NULL, NULL, 0, NULL) "
            "ON CONFLICT (day) DO UPDATE SET "
            "reason = EXCLUDED.reason, "
            "requested_at = CASE "
            "WHEN attendance_recalc_queue.completed_at IS NULL "
            "THEN LEAST(attendance_recalc_queue.requested_at, EXCLUDED.requested_at) "
            "ELSE EXCLUDED.requested_at END, "
            "started_at = NULL, completed_at = NULL, "
            "cache_started_at = NULL, cache_ready_at = NULL, "
            "attempt_count = CASE "
            "WHEN attendance_recalc_queue.completed_at IS NULL "
            "THEN attendance_recalc_queue.attempt_count ELSE 0 END, "
            "last_error = NULL",
            (day, reason, requested_at),
        )


def enqueue_recalc(days: Iterable[date], reason: str) -> None:
    requested_at = datetime.now(UTC)
    with db.cursor() as cur:
        _enqueue_recalc_cur(
            cur,
            days,
            reason,
            requested_at=requested_at,
        )


def _locked_sync_state(cur) -> Mapping[str, Any]:
    cur.execute(
        "SELECT cursor_write_date, cursor_id, last_incremental_completed_at, "
        "last_full_sweep_completed_at, full_sweep_generation, "
        "baseline_completed_at, last_error FROM odoo_attendance_sync_state "
        "WHERE singleton = TRUE FOR UPDATE"
    )
    state = cur.fetchone()
    if state is None:
        raise RuntimeError("Odoo attendance sync state is missing")
    return state


def _sync_state_from_row(row: Mapping[str, Any]) -> SyncState:
    return SyncState(
        cursor_write_date=_optional_aware_utc(row["cursor_write_date"], "cursor_write_date"),
        cursor_id=row["cursor_id"],
        last_incremental_completed_at=_optional_aware_utc(
            row["last_incremental_completed_at"],
            "last_incremental_completed_at",
        ),
        last_full_sweep_completed_at=_optional_aware_utc(
            row["last_full_sweep_completed_at"],
            "last_full_sweep_completed_at",
        ),
        full_sweep_generation=int(row["full_sweep_generation"]),
        baseline_completed_at=_optional_aware_utc(
            row["baseline_completed_at"], "baseline_completed_at"
        ),
    )


def _sync_state_cur(cur) -> SyncState:
    return _sync_state_from_row(_locked_sync_state(cur))


def _upsert_rows_cur(
    cur,
    rows: tuple[dict[str, Any], ...],
    *,
    sync_completed_at: datetime,
    observed_at: datetime,
    baseline_completed: bool,
) -> set[date]:
    affected_days: set[date] = set()
    for incoming in rows:
        attendance_id = incoming["odoo_attendance_id"]
        cur.execute(
            "SELECT odoo_attendance_id, employee_odoo_id, employee_name, "
            "check_in_utc, check_out_utc, odoo_work_center_id, "
            "odoo_work_center_name, odoo_department_id, odoo_department_name, "
            "odoo_write_date, last_seen_at, deleted_at "
            "FROM odoo_attendance_mirror "
            "WHERE odoo_attendance_id = %s FOR UPDATE",
            (attendance_id,),
        )
        existing = cur.fetchone()
        if existing is None:
            cur.execute(
                "INSERT INTO odoo_attendance_mirror "
                "(odoo_attendance_id, employee_odoo_id, employee_name, "
                "check_in_utc, check_out_utc, odoo_work_center_id, "
                "odoo_work_center_name, odoo_department_id, "
                "odoo_department_name, odoo_write_date, first_seen_at, "
                "last_seen_at, deleted_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NULL)",
                tuple(incoming[field] for field in _ROW_FIELDS) + (observed_at, observed_at),
            )
            if baseline_completed:
                affected_days.update(_row_days(incoming, sync_completed_at))
            continue

        incoming_is_stale = incoming["odoo_write_date"] < existing["odoo_write_date"]
        if incoming_is_stale:
            # An older raw Odoo version cannot confirm current existence. It
            # must not revive a tombstone or move last_seen_at.
            continue

        if not _observation_can_revive(
            observed_at=observed_at,
            deleted_at=existing["deleted_at"],
        ):
            # This row was fetched before a newer complete sweep confirmed it
            # missing. Advancing the cycle cursor is safe, but reviving this
            # tombstone would make an older observation overwrite newer truth.
            continue

        source = incoming
        changed = bool(existing["deleted_at"] is not None) or _materially_different(
            existing, incoming
        )
        if baseline_completed and changed:
            affected_days.update(_row_days(existing, sync_completed_at))
            affected_days.update(_row_days(source, sync_completed_at))
        cur.execute(
            "UPDATE odoo_attendance_mirror SET "
            "employee_odoo_id = %s, employee_name = %s, check_in_utc = %s, "
            "check_out_utc = %s, odoo_work_center_id = %s, "
            "odoo_work_center_name = %s, odoo_department_id = %s, "
            "odoo_department_name = %s, odoo_write_date = %s, "
            "last_seen_at = GREATEST(last_seen_at, %s), deleted_at = NULL "
            "WHERE odoo_attendance_id = %s",
            tuple(source[field] for field in _ROW_FIELDS[1:]) + (observed_at, attendance_id),
        )

    if affected_days:
        _enqueue_recalc_cur(
            cur,
            affected_days,
            "odoo_attendance_changed",
            requested_at=sync_completed_at,
        )
    return affected_days


def _max_cursor(
    rows: Sequence[Mapping[str, Any]],
) -> tuple[datetime | None, int | None]:
    if not rows:
        return None, None
    row = max(
        rows,
        key=lambda item: (item["odoo_write_date"], item["odoo_attendance_id"]),
    )
    return row["odoo_write_date"], row["odoo_attendance_id"]


def _later_cursor(
    current_date: datetime | None,
    current_id: int | None,
    candidate_date: datetime | None,
    candidate_id: int | None,
) -> tuple[datetime | None, int | None]:
    if candidate_date is None:
        return current_date, current_id
    if current_date is None or (candidate_date, candidate_id or 0) > (
        current_date,
        current_id or 0,
    ):
        return candidate_date, candidate_id
    return current_date, current_id


def _store_incremental_cycle_cur(
    cur,
    rows: Sequence[dict],
    *,
    cursor_write_date: datetime | None,
    cursor_id: int | None,
    completed_at: datetime,
    observed_at: datetime | None = None,
) -> set[date]:
    completed = _aware_utc(completed_at, "completed_at")
    observed = completed if observed_at is None else _aware_utc(observed_at, "observed_at")
    normalized = _normalized_rows(rows)
    if cursor_write_date is not None:
        cursor_write_date = _aware_utc(cursor_write_date, "cursor_write_date")
        cursor_id = _positive_int(cursor_id, "cursor_id")
    elif cursor_id is not None:
        raise ValueError("cursor_id requires cursor_write_date")

    state = _locked_sync_state(cur)
    affected = _upsert_rows_cur(
        cur,
        normalized,
        sync_completed_at=completed,
        observed_at=observed,
        baseline_completed=state["baseline_completed_at"] is not None,
    )
    next_date, next_id = _later_cursor(
        state["cursor_write_date"],
        state["cursor_id"],
        cursor_write_date,
        cursor_id,
    )
    cur.execute(
        "UPDATE odoo_attendance_sync_state SET "
        "cursor_write_date = %s, cursor_id = %s, "
        "last_incremental_completed_at = %s, last_error = %s "
        "WHERE singleton = TRUE",
        (
            next_date,
            next_id,
            completed,
            _error_after_success(state["last_error"], "incremental"),
        ),
    )
    return affected


def _store_incremental_cycle(
    rows: Sequence[dict],
    *,
    cursor_write_date: datetime | None,
    cursor_id: int | None,
    completed_at: datetime,
    observed_at: datetime | None = None,
) -> set[date]:
    with db.cursor() as cur:
        return _store_incremental_cycle_cur(
            cur,
            rows,
            cursor_write_date=cursor_write_date,
            cursor_id=cursor_id,
            completed_at=completed_at,
            observed_at=observed_at,
        )


def upsert_rows(rows: Sequence[dict], *, sync_completed_at: datetime) -> set[date]:
    """Upsert one complete poll and advance its cursor in the same transaction."""
    completed = _aware_utc(sync_completed_at, "sync_completed_at")
    normalized = _normalized_rows(rows)
    cursor_write_date, cursor_id = _max_cursor(normalized)
    return _store_incremental_cycle(
        normalized,
        cursor_write_date=cursor_write_date,
        cursor_id=cursor_id,
        completed_at=completed,
    )


def _validate_range(
    start_utc: datetime, end_utc: datetime | None
) -> tuple[datetime, datetime | None]:
    start = _aware_utc(start_utc, "start_utc")
    end = _optional_aware_utc(end_utc, "end_utc")
    if end is not None and end <= start:
        raise ValueError("end_utc must be after start_utc")
    return start, end


def _utc_database_row(row: Mapping[str, Any]) -> dict[str, Any]:
    normalized = dict(row)
    for field in (
        "check_in_utc",
        "check_out_utc",
        "odoo_write_date",
        "first_seen_at",
        "last_seen_at",
        "deleted_at",
    ):
        if normalized.get(field) is not None:
            normalized[field] = _aware_utc(normalized[field], field)
    return normalized


def rows_overlapping(start_utc: datetime, end_utc: datetime) -> tuple[dict, ...]:
    start, end = _validate_range(start_utc, end_utc)
    assert end is not None
    return tuple(
        _utc_database_row(row)
        for row in db.query(
            "SELECT * FROM odoo_attendance_mirror "
            "WHERE deleted_at IS NULL "
            "AND (check_out_utc IS NULL OR check_out_utc > check_in_utc) "
            "AND check_in_utc < %s "
            "AND (check_out_utc IS NULL OR check_out_utc > %s) "
            "ORDER BY check_in_utc, odoo_attendance_id",
            (end, start),
        )
    )


def day_presence(day: date) -> dict[str, dict[str, object]]:
    """Return first arrival and current-open state for one plant-local day."""
    if type(day) is not date:
        raise TypeError("day must be a date")
    start_local = datetime.combine(day, datetime.min.time(), tzinfo=SITE_TZ)
    start_utc = start_local.astimezone(UTC)
    end_utc = (start_local + timedelta(days=1)).astimezone(UTC)
    rows = db.query(
        "SELECT employee_odoo_id, MIN(check_in_utc) AS first_check_in, "
        "BOOL_OR(check_out_utc IS NULL) AS currently_open "
        "FROM odoo_attendance_mirror "
        "WHERE deleted_at IS NULL "
        "AND (check_out_utc IS NULL OR check_out_utc > check_in_utc) "
        "AND check_in_utc >= %s "
        "AND check_in_utc < %s "
        "GROUP BY employee_odoo_id ORDER BY employee_odoo_id",
        (start_utc, end_utc),
    )
    return {
        str(_positive_int(row["employee_odoo_id"], "employee_odoo_id")): {
            "first_check_in": _aware_utc(
                row["first_check_in"], "first_check_in"
            ).isoformat(),
            "currently_open": bool(row["currently_open"]),
        }
        for row in rows
    }


def day_presence_from_rows(
    day: date, rows: Sequence[Mapping[str, Any]]
) -> dict[str, dict[str, object]]:
    """Derive day presence from an already-frozen mirror row snapshot."""
    if type(day) is not date:
        raise TypeError("day must be a date")
    start_local = datetime.combine(day, datetime.min.time(), tzinfo=SITE_TZ)
    start_utc = start_local.astimezone(UTC)
    end_utc = (start_local + timedelta(days=1)).astimezone(UTC)
    by_employee: dict[int, dict[str, object]] = {}
    for raw in rows:
        employee_id = _positive_int(raw["employee_odoo_id"], "employee_odoo_id")
        check_in = _aware_utc(raw["check_in_utc"], "check_in_utc")
        if not start_utc <= check_in < end_utc:
            continue
        current = by_employee.get(employee_id)
        if current is None:
            by_employee[employee_id] = {
                "first_check_in": check_in,
                "currently_open": raw.get("check_out_utc") is None,
            }
            continue
        current["first_check_in"] = min(current["first_check_in"], check_in)
        current["currently_open"] = bool(current["currently_open"]) or (
            raw.get("check_out_utc") is None
        )
    return {
        str(employee_id): {
            "first_check_in": value["first_check_in"].isoformat(),
            "currently_open": bool(value["currently_open"]),
        }
        for employee_id, value in sorted(by_employee.items())
    }


def current_open_attendance() -> tuple[dict[str, Any], ...]:
    """Return every non-deleted open row for the live-location snapshot."""
    return tuple(
        _utc_database_row(row)
        for row in db.query(
            "SELECT odoo_attendance_id, employee_odoo_id, check_in_utc, "
            "odoo_work_center_id, odoo_work_center_name "
            "FROM odoo_attendance_mirror "
            "WHERE deleted_at IS NULL AND check_out_utc IS NULL "
            "ORDER BY employee_odoo_id, check_in_utc, odoo_attendance_id"
        )
    )


def rows_for_employee(
    employee_odoo_id: int,
    start_utc: datetime,
    end_utc: datetime | None,
) -> tuple[dict, ...]:
    employee_id = _positive_int(employee_odoo_id, "employee_odoo_id")
    start, end = _validate_range(start_utc, end_utc)
    if end is None:
        return tuple(
            _utc_database_row(row)
            for row in db.query(
                "SELECT * FROM odoo_attendance_mirror "
                "WHERE employee_odoo_id = %s AND deleted_at IS NULL "
                "AND (check_out_utc IS NULL OR check_out_utc > check_in_utc) "
                "AND (check_out_utc IS NULL OR check_out_utc > %s) "
                "ORDER BY check_in_utc, odoo_attendance_id",
                (employee_id, start),
            )
        )
    return tuple(
        _utc_database_row(row)
        for row in db.query(
            "SELECT * FROM odoo_attendance_mirror "
            "WHERE employee_odoo_id = %s AND deleted_at IS NULL "
            "AND (check_out_utc IS NULL OR check_out_utc > check_in_utc) "
            "AND check_in_utc < %s "
            "AND (check_out_utc IS NULL OR check_out_utc > %s) "
            "ORDER BY check_in_utc, odoo_attendance_id",
            (employee_id, end, start),
        )
    )


def _validated_ids(ids: Iterable[int]) -> set[int]:
    if isinstance(ids, (str, bytes)):
        raise TypeError("ids must be an iterable of positive integers")
    return {_positive_int(value, "attendance id") for value in ids}


def _active_attendance_ids_cur(cur) -> set[int]:
    cur.execute(
        "SELECT odoo_attendance_id FROM odoo_attendance_mirror "
        "WHERE deleted_at IS NULL ORDER BY odoo_attendance_id"
    )
    return {int(row["odoo_attendance_id"]) for row in cur.fetchall()}


def _active_attendance_ids() -> set[int]:
    with db.cursor() as cur:
        return _active_attendance_ids_cur(cur)


def _tombstoned_attendance_ids_cur(cur, ids: set[int]) -> set[int]:
    requested = _validated_ids(ids)
    if not requested:
        return set()
    cur.execute(
        "SELECT odoo_attendance_id FROM odoo_attendance_mirror "
        "WHERE deleted_at IS NOT NULL AND odoo_attendance_id = ANY(%s) "
        "ORDER BY odoo_attendance_id",
        (sorted(requested),),
    )
    return {int(row["odoo_attendance_id"]) for row in cur.fetchall()}


def _tombstoned_attendance_ids(ids: set[int]) -> set[int]:
    with db.cursor() as cur:
        return _tombstoned_attendance_ids_cur(cur, ids)


def _store_full_sweep_cur(
    cur,
    ids: set[int],
    *,
    recovery_rows: Sequence[dict] = (),
    generation: int,
    completed_at: datetime,
    observed_at: datetime | None = None,
) -> _FullSweepStoreResult:
    present_ids = _validated_ids(ids)
    normalized_recovery = _normalized_rows(recovery_rows)
    recovery_ids = {row["odoo_attendance_id"] for row in normalized_recovery}
    if not recovery_ids <= present_ids:
        raise ValueError("recovery rows must be present in the completed sweep")
    sweep_generation = _positive_int(generation, "generation")
    completed = _aware_utc(completed_at, "completed_at")
    observed = completed if observed_at is None else _aware_utc(observed_at, "observed_at")
    state = _locked_sync_state(cur)
    if sweep_generation != int(state["full_sweep_generation"]) + 1:
        raise ValueError("full sweep generation is stale")
    if recovery_ids:
        cur.execute(
            "SELECT odoo_attendance_id FROM odoo_attendance_mirror "
            "WHERE deleted_at IS NOT NULL "
            "AND odoo_attendance_id = ANY(%s) FOR UPDATE",
            (sorted(recovery_ids),),
        )
        locked_recovery_ids = {int(row["odoo_attendance_id"]) for row in cur.fetchall()}
        if locked_recovery_ids != recovery_ids:
            raise RuntimeError("tombstone recovery state changed before commit")
    recovered_days = _upsert_rows_cur(
        cur,
        normalized_recovery,
        sync_completed_at=completed,
        observed_at=observed,
        baseline_completed=state["baseline_completed_at"] is not None,
    )
    if present_ids:
        cur.execute(
            "UPDATE odoo_attendance_mirror SET last_sweep_generation = %s "
            "WHERE deleted_at IS NULL AND odoo_attendance_id = ANY(%s)",
            (sweep_generation, sorted(present_ids)),
        )
        cur.execute(
            "SELECT odoo_attendance_id, check_in_utc, check_out_utc "
            "FROM odoo_attendance_mirror WHERE deleted_at IS NULL "
            "AND NOT (odoo_attendance_id = ANY(%s)) FOR UPDATE",
            (sorted(present_ids),),
        )
    else:
        cur.execute(
            "SELECT odoo_attendance_id, check_in_utc, check_out_utc "
            "FROM odoo_attendance_mirror WHERE deleted_at IS NULL FOR UPDATE"
        )
    deleted_rows = tuple(cur.fetchall())
    deleted_ids = [row["odoo_attendance_id"] for row in deleted_rows]
    if deleted_ids:
        cur.execute(
            "UPDATE odoo_attendance_mirror SET deleted_at = %s, "
            "last_sweep_generation = %s "
            "WHERE odoo_attendance_id = ANY(%s) AND deleted_at IS NULL",
            (observed, sweep_generation, deleted_ids),
        )
    deleted_days: set[date] = set()
    if state["baseline_completed_at"] is not None:
        for row in deleted_rows:
            deleted_days.update(_row_days(row, completed))
        if deleted_days:
            _enqueue_recalc_cur(
                cur,
                deleted_days,
                "odoo_attendance_deleted",
                requested_at=completed,
            )
    cur.execute(
        "UPDATE odoo_attendance_sync_state SET "
        "last_full_sweep_completed_at = %s, "
        "last_full_sweep_deletion_count = %s, "
        "full_sweep_generation = %s, last_error = %s "
        "WHERE singleton = TRUE",
        (
            completed,
            len(deleted_ids),
            sweep_generation,
            _error_after_success(state["last_error"], "sweep"),
        ),
    )
    return _FullSweepStoreResult(
        affected_days=frozenset(recovered_days | deleted_days),
        deleted_count=len(deleted_ids),
    )


def _store_full_sweep(
    ids: set[int],
    *,
    recovery_rows: Sequence[dict] = (),
    generation: int,
    completed_at: datetime,
    observed_at: datetime | None = None,
) -> _FullSweepStoreResult:
    with db.cursor() as cur:
        return _store_full_sweep_cur(
            cur,
            ids,
            recovery_rows=recovery_rows,
            generation=generation,
            completed_at=completed_at,
            observed_at=observed_at,
        )


def mark_deleted_after_successful_sweep(ids: set[int], generation: int) -> set[date]:
    result = _store_full_sweep(
        ids,
        generation=generation,
        completed_at=datetime.now(UTC),
    )
    return set(result.affected_days)


def _sync_state_snapshot() -> SyncState:
    rows = db.query(
        "SELECT cursor_write_date, cursor_id, last_incremental_completed_at, "
        "last_full_sweep_completed_at, full_sweep_generation, "
        "baseline_completed_at FROM odoo_attendance_sync_state "
        "WHERE singleton = TRUE"
    )
    if not rows:
        raise RuntimeError("Odoo attendance sync state is missing")
    row = rows[0]
    return _sync_state_from_row(row)


def _record_incremental_started_cur(cur, started_at: datetime) -> None:
    started = _aware_utc(started_at, "started_at")
    cur.execute(
        "UPDATE odoo_attendance_sync_state SET last_incremental_started_at = %s "
        "WHERE singleton = TRUE",
        (started,),
    )


def _record_incremental_started(started_at: datetime) -> None:
    with db.cursor() as cur:
        _record_incremental_started_cur(cur, started_at)


def _record_failure(owner: str, error: object) -> None:
    owner = _require_error_owner(owner)
    with db.cursor() as cur:
        state = _locked_sync_state(cur)
        cur.execute(
            "UPDATE odoo_attendance_sync_state SET last_error = %s WHERE singleton = TRUE",
            (_error_with_failure(state["last_error"], owner, error),),
        )


def _complete_baseline_if_ready(completed_at: datetime) -> bool:
    completed = _aware_utc(completed_at, "completed_at")
    with db.cursor() as cur:
        state = _locked_sync_state(cur)
        baseline_completed_at = state["baseline_completed_at"]
        if (
            baseline_completed_at is None
            and state["last_incremental_completed_at"] is not None
            and state["last_full_sweep_completed_at"] is not None
        ):
            baseline_completed_at = completed
        if baseline_completed_at is not None:
            cur.execute(
                "UPDATE odoo_attendance_sync_state SET "
                "baseline_completed_at = %s, last_error = %s "
                "WHERE singleton = TRUE",
                (
                    baseline_completed_at,
                    _error_after_success(state["last_error"], "baseline"),
                ),
            )
    return baseline_completed_at is not None


def health_snapshot() -> MirrorHealth:
    rows = db.query(
        "SELECT s.last_incremental_completed_at, "
        "s.last_full_sweep_completed_at, s.baseline_completed_at, "
        "(SELECT MIN(requested_at) FROM attendance_recalc_queue "
        " WHERE completed_at IS NULL) AS oldest_recalc_requested_at, "
        "s.last_error FROM odoo_attendance_sync_state s "
        "WHERE s.singleton = TRUE"
    )
    if not rows:
        return MirrorHealth(None, None, None, None, "sync state is missing")
    row = rows[0]
    return MirrorHealth(
        last_incremental_completed_at=_optional_aware_utc(
            row["last_incremental_completed_at"],
            "last_incremental_completed_at",
        ),
        last_full_sweep_completed_at=_optional_aware_utc(
            row["last_full_sweep_completed_at"],
            "last_full_sweep_completed_at",
        ),
        baseline_completed_at=_optional_aware_utc(
            row["baseline_completed_at"], "baseline_completed_at"
        ),
        oldest_recalc_requested_at=_optional_aware_utc(
            row["oldest_recalc_requested_at"], "oldest_recalc_requested_at"
        ),
        last_error=_format_error_state(row["last_error"]),
    )


__all__ = [
    "MirrorHealth",
    "current_open_attendance",
    "day_presence",
    "day_presence_from_rows",
    "enqueue_recalc",
    "health_snapshot",
    "local_days_touched",
    "mark_deleted_after_successful_sweep",
    "rows_for_employee",
    "rows_overlapping",
    "upsert_rows",
]
