"""Durable, transactional mirror of normalized Odoo attendance rows."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

from . import db
from .shift_config import SITE_TZ


_ERROR_LIMIT = 500
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
        raise ValueError(
            "attendance row omitted required field(s): " + ", ".join(missing)
        )
    check_in = _aware_utc(row["check_in_utc"], "check_in_utc")
    check_out = _optional_aware_utc(row["check_out_utc"], "check_out_utc")
    if check_out is not None and check_out < check_in:
        raise ValueError("check_out_utc cannot be before check_in_utc")
    return {
        "odoo_attendance_id": _positive_int(
            row["odoo_attendance_id"], "odoo_attendance_id"
        ),
        "employee_odoo_id": _positive_int(
            row["employee_odoo_id"], "employee_odoo_id"
        ),
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
        "odoo_department_name": _optional_text(
            row["odoo_department_name"], "odoo_department_name"
        ),
        "odoo_write_date": _aware_utc(
            row["odoo_write_date"], "odoo_write_date"
        ),
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


def local_days_touched(
    start_utc: datetime, end_utc: datetime | None
) -> set[date]:
    """Return plant calendar days intersected by the half-open UTC interval."""
    start = _aware_utc(start_utc, "start_utc")
    end = _optional_aware_utc(end_utc, "end_utc")
    if end is None or end <= start:
        return {start.astimezone(SITE_TZ).date()}
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
    return local_days_touched(
        row["check_in_utc"], row["check_out_utc"] or open_end
    )


def _materially_different(
    existing: Mapping[str, Any], incoming: Mapping[str, Any]
) -> bool:
    return any(existing[field] != incoming[field] for field in _MATERIAL_FIELDS)


def _observation_can_revive(
    *, observed_at: datetime, deleted_at: datetime | None
) -> bool:
    """Return whether an observation is newer than a confirmed deletion."""
    observed = _aware_utc(observed_at, "observed_at")
    deleted = _optional_aware_utc(deleted_at, "deleted_at")
    return deleted is None or observed > deleted


def _sweep_can_delete(
    *, last_seen_at: datetime, sweep_started_at: datetime
) -> bool:
    """Return whether the row lacks an observation after the sweep began."""
    last_seen = _aware_utc(last_seen_at, "last_seen_at")
    sweep_started = _aware_utc(sweep_started_at, "sweep_started_at")
    return last_seen <= sweep_started


def _enqueue_recalc_cur(
    cur,
    days: Iterable[date],
    reason: str,
    *,
    mark_strict: bool,
    requested_at: datetime,
) -> None:
    unique_days = sorted(set(days))
    for day in unique_days:
        if not isinstance(day, date) or isinstance(day, datetime):
            raise TypeError("recalculation days must be date values")
        cur.execute(
            "INSERT INTO attendance_recalc_queue "
            "(day, reason, requested_at, started_at, completed_at, "
            "attempt_count, last_error) VALUES (%s, %s, %s, NULL, NULL, 0, NULL) "
            "ON CONFLICT (day) DO UPDATE SET "
            "reason = EXCLUDED.reason, "
            "requested_at = CASE "
            "WHEN attendance_recalc_queue.completed_at IS NULL "
            "THEN LEAST(attendance_recalc_queue.requested_at, EXCLUDED.requested_at) "
            "ELSE EXCLUDED.requested_at END, "
            "started_at = NULL, completed_at = NULL, "
            "attempt_count = CASE "
            "WHEN attendance_recalc_queue.completed_at IS NULL "
            "THEN attendance_recalc_queue.attempt_count ELSE 0 END, "
            "last_error = NULL",
            (day, reason, requested_at),
        )
        if mark_strict:
            cur.execute(
                "INSERT INTO attendance_strict_days "
                "(day, reason, source_changed_at) VALUES (%s, %s, %s) "
                "ON CONFLICT (day) DO UPDATE SET "
                "reason = EXCLUDED.reason, "
                "source_changed_at = EXCLUDED.source_changed_at",
                (day, reason, requested_at),
            )


def enqueue_recalc(
    days: Iterable[date], reason: str, *, mark_strict: bool
) -> None:
    requested_at = datetime.now(UTC)
    with db.cursor() as cur:
        _enqueue_recalc_cur(
            cur,
            days,
            reason,
            mark_strict=mark_strict,
            requested_at=requested_at,
        )


def _locked_sync_state(cur) -> Mapping[str, Any]:
    cur.execute(
        "SELECT cursor_write_date, cursor_id, last_incremental_completed_at, "
        "last_full_sweep_completed_at, full_sweep_generation, "
        "baseline_completed_at FROM odoo_attendance_sync_state "
        "WHERE singleton = TRUE FOR UPDATE"
    )
    state = cur.fetchone()
    if state is None:
        raise RuntimeError("Odoo attendance sync state is missing")
    return state


def _upsert_rows_cur(
    cur,
    rows: tuple[dict[str, Any], ...],
    *,
    sync_completed_at: datetime,
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
                tuple(incoming[field] for field in _ROW_FIELDS)
                + (sync_completed_at, sync_completed_at),
            )
            if baseline_completed:
                affected_days.update(_row_days(incoming, sync_completed_at))
            continue

        incoming_is_stale = (
            incoming["odoo_write_date"] < existing["odoo_write_date"]
        )
        if incoming_is_stale:
            # An older raw Odoo version cannot confirm current existence. It
            # must not revive a tombstone, move last_seen_at, or reset a
            # complete-sweep absence recorded against newer source truth.
            continue

        if not _observation_can_revive(
            observed_at=sync_completed_at,
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
            "last_seen_at = GREATEST(last_seen_at, %s), deleted_at = NULL, "
            "missing_since_sweep_generation = NULL "
            "WHERE odoo_attendance_id = %s",
            tuple(source[field] for field in _ROW_FIELDS[1:])
            + (sync_completed_at, attendance_id),
        )

    if affected_days:
        _enqueue_recalc_cur(
            cur,
            affected_days,
            "odoo_attendance_changed",
            mark_strict=True,
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


def _store_incremental_cycle(
    rows: Sequence[dict],
    *,
    cursor_write_date: datetime | None,
    cursor_id: int | None,
    completed_at: datetime,
) -> set[date]:
    completed = _aware_utc(completed_at, "completed_at")
    normalized = _normalized_rows(rows)
    if cursor_write_date is not None:
        cursor_write_date = _aware_utc(cursor_write_date, "cursor_write_date")
        cursor_id = _positive_int(cursor_id, "cursor_id")
    elif cursor_id is not None:
        raise ValueError("cursor_id requires cursor_write_date")

    with db.cursor() as cur:
        state = _locked_sync_state(cur)
        affected = _upsert_rows_cur(
            cur,
            normalized,
            sync_completed_at=completed,
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
            "last_incremental_completed_at = %s, last_error = NULL "
            "WHERE singleton = TRUE",
            (next_date, next_id, completed),
        )
    return affected


def upsert_rows(
    rows: Sequence[dict], *, sync_completed_at: datetime
) -> set[date]:
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


def rows_overlapping(
    start_utc: datetime, end_utc: datetime
) -> tuple[dict, ...]:
    start, end = _validate_range(start_utc, end_utc)
    assert end is not None
    return tuple(
        _utc_database_row(row)
        for row in db.query(
            "SELECT * FROM odoo_attendance_mirror "
            "WHERE deleted_at IS NULL AND check_in_utc < %s "
            "AND (check_out_utc IS NULL OR check_out_utc > %s) "
            "ORDER BY check_in_utc, odoo_attendance_id",
            (end, start),
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


def _store_full_sweep(
    ids: set[int], *, generation: int, completed_at: datetime
) -> tuple[set[date], int]:
    present_ids = _validated_ids(ids)
    sweep_generation = _positive_int(generation, "generation")
    completed = _aware_utc(completed_at, "completed_at")
    with db.cursor() as cur:
        state = _locked_sync_state(cur)
        if sweep_generation != int(state["full_sweep_generation"]) + 1:
            raise ValueError("full sweep generation is stale")
        if present_ids:
            cur.execute(
                "UPDATE odoo_attendance_mirror SET last_sweep_generation = %s, "
                "missing_since_sweep_generation = NULL "
                "WHERE deleted_at IS NULL AND odoo_attendance_id = ANY(%s)",
                (sweep_generation, sorted(present_ids)),
            )
            cur.execute(
                "SELECT odoo_attendance_id, check_in_utc, check_out_utc, "
                "last_seen_at, missing_since_sweep_generation "
                "FROM odoo_attendance_mirror WHERE deleted_at IS NULL "
                "AND NOT (odoo_attendance_id = ANY(%s)) FOR UPDATE",
                (sorted(present_ids),),
            )
        else:
            cur.execute(
                "SELECT odoo_attendance_id, check_in_utc, check_out_utc, "
                "last_seen_at, missing_since_sweep_generation "
                "FROM odoo_attendance_mirror WHERE deleted_at IS NULL FOR UPDATE"
            )
        missing_rows = tuple(cur.fetchall())
        deleted_rows = tuple(
            row
            for row in missing_rows
            if row["missing_since_sweep_generation"] == sweep_generation - 1
            and _sweep_can_delete(
                last_seen_at=row["last_seen_at"], sweep_started_at=completed
            )
        )
        deleted_ids = [row["odoo_attendance_id"] for row in deleted_rows]
        deleted_id_set = set(deleted_ids)
        deferred_ids = [
            row["odoo_attendance_id"]
            for row in missing_rows
            if row["odoo_attendance_id"] not in deleted_id_set
        ]
        if deferred_ids:
            cur.execute(
                "UPDATE odoo_attendance_mirror SET last_sweep_generation = %s, "
                "missing_since_sweep_generation = %s "
                "WHERE odoo_attendance_id = ANY(%s) AND deleted_at IS NULL",
                (sweep_generation, sweep_generation, deferred_ids),
            )
        if deleted_ids:
            cur.execute(
                "UPDATE odoo_attendance_mirror SET deleted_at = %s, "
                "last_sweep_generation = %s "
                "WHERE odoo_attendance_id = ANY(%s) AND deleted_at IS NULL",
                (completed, sweep_generation, deleted_ids),
            )
        affected_days: set[date] = set()
        if state["baseline_completed_at"] is not None:
            for row in deleted_rows:
                affected_days.update(_row_days(row, completed))
            if affected_days:
                _enqueue_recalc_cur(
                    cur,
                    affected_days,
                    "odoo_attendance_deleted",
                    mark_strict=True,
                    requested_at=completed,
                )
        cur.execute(
            "UPDATE odoo_attendance_sync_state SET "
            "last_full_sweep_completed_at = %s, "
            "last_full_sweep_deletion_count = %s, "
            "full_sweep_generation = %s, last_error = NULL "
            "WHERE singleton = TRUE",
            (completed, len(deleted_ids), sweep_generation),
        )
    return affected_days, len(deleted_ids)


def mark_deleted_after_successful_sweep(
    ids: set[int], generation: int
) -> set[date]:
    affected_days, _rows_deleted = _store_full_sweep(
        ids,
        generation=generation,
        completed_at=datetime.now(UTC),
    )
    return affected_days


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
    return SyncState(
        cursor_write_date=_optional_aware_utc(
            row["cursor_write_date"], "cursor_write_date"
        ),
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


def _record_incremental_started(started_at: datetime) -> None:
    started = _aware_utc(started_at, "started_at")
    db.execute(
        "UPDATE odoo_attendance_sync_state SET last_incremental_started_at = %s "
        "WHERE singleton = TRUE",
        (started,),
    )


def _bounded_error(error: object) -> str:
    return str(error)[:_ERROR_LIMIT]


def _record_failure(error: object) -> None:
    db.execute(
        "UPDATE odoo_attendance_sync_state SET last_error = %s "
        "WHERE singleton = TRUE",
        (_bounded_error(error),),
    )


def _complete_baseline_if_ready(completed_at: datetime) -> bool:
    completed = _aware_utc(completed_at, "completed_at")
    with db.cursor() as cur:
        cur.execute(
            "UPDATE odoo_attendance_sync_state SET baseline_completed_at = %s "
            "WHERE singleton = TRUE AND baseline_completed_at IS NULL "
            "AND last_incremental_completed_at IS NOT NULL "
            "AND last_full_sweep_completed_at IS NOT NULL",
            (completed,),
        )
        cur.execute(
            "SELECT baseline_completed_at FROM odoo_attendance_sync_state "
            "WHERE singleton = TRUE"
        )
        row = cur.fetchone()
    return bool(row and row["baseline_completed_at"] is not None)


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
        last_error=(
            _bounded_error(row["last_error"])
            if row["last_error"] is not None
            else None
        ),
    )


__all__ = [
    "MirrorHealth",
    "enqueue_recalc",
    "health_snapshot",
    "local_days_touched",
    "mark_deleted_after_successful_sweep",
    "rows_for_employee",
    "rows_overlapping",
    "upsert_rows",
]
