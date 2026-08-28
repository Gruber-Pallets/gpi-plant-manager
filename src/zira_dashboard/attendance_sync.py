"""Safe incremental orchestration for the durable Odoo attendance mirror."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
import logging
from typing import Any

from . import attendance_mirror, odoo_client


_log = logging.getLogger(__name__)
_FULL_SWEEP_INTERVAL = timedelta(hours=1)


@dataclass(frozen=True)
class SyncState:
    cursor_write_date: datetime | None
    cursor_id: int | None
    last_incremental_completed_at: datetime | None
    last_full_sweep_completed_at: datetime | None
    full_sweep_generation: int
    baseline_completed_at: datetime | None


@dataclass(frozen=True)
class SyncResult:
    success: bool
    incremental_completed: bool = False
    full_sweep_completed: bool = False
    baseline_completed: bool = False
    rows_stored: int = 0
    rows_deleted: int = 0
    affected_days: frozenset[date] = frozenset()
    error: str | None = None


@dataclass(frozen=True)
class SweepStoreResult:
    affected_days: frozenset[date]
    deleted_count: int


@dataclass(frozen=True)
class AttendanceIdSweepSnapshot:
    ids: tuple[int, ...]
    complete: bool


class _OdooSource:
    def fetch_attendance_changes(self, **kwargs):
        return odoo_client.fetch_attendance_changes(**kwargs)

    def fetch_open_attendance_rows(self):
        return odoo_client.fetch_open_attendance_rows()

    def fetch_complete_attendance_id_sweep(self) -> AttendanceIdSweepSnapshot:
        ids = odoo_client.fetch_all_attendance_ids()
        return AttendanceIdSweepSnapshot(ids=tuple(ids), complete=True)

    def fetch_attendance_rows_by_ids(self, ids: Sequence[int]):
        return odoo_client.fetch_attendance_rows_by_ids(ids)


class _MirrorBackend:
    def logical_run_lock(self):
        return attendance_mirror._logical_run_lock()

    def sync_state(self) -> SyncState:
        state = attendance_mirror._sync_state_snapshot()
        return SyncState(
            cursor_write_date=state.cursor_write_date,
            cursor_id=state.cursor_id,
            last_incremental_completed_at=state.last_incremental_completed_at,
            last_full_sweep_completed_at=state.last_full_sweep_completed_at,
            full_sweep_generation=state.full_sweep_generation,
            baseline_completed_at=state.baseline_completed_at,
        )

    def record_incremental_started(self, started_at: datetime) -> None:
        attendance_mirror._record_incremental_started(started_at)

    def store_incremental_cycle(
        self,
        rows: Sequence[dict],
        *,
        cursor_write_date: datetime | None,
        cursor_id: int | None,
        completed_at: datetime,
    ) -> set[date]:
        return attendance_mirror._store_incremental_cycle(
            rows,
            cursor_write_date=cursor_write_date,
            cursor_id=cursor_id,
            completed_at=completed_at,
        )

    def store_full_sweep(
        self,
        ids: set[int],
        *,
        recovery_rows: Sequence[dict],
        generation: int,
        completed_at: datetime,
    ) -> SweepStoreResult:
        result = attendance_mirror._store_full_sweep(
            ids,
            recovery_rows=recovery_rows,
            generation=generation,
            completed_at=completed_at,
        )
        return SweepStoreResult(
            affected_days=result.affected_days,
            deleted_count=result.deleted_count,
        )

    def active_attendance_ids(self) -> set[int]:
        return attendance_mirror._active_attendance_ids()

    def tombstoned_attendance_ids(self, ids: set[int]) -> set[int]:
        return attendance_mirror._tombstoned_attendance_ids(ids)

    def record_failure(self, owner: str, error: object) -> None:
        attendance_mirror._record_failure(owner, error)

    def complete_baseline_if_ready(self, completed_at: datetime) -> bool:
        return attendance_mirror._complete_baseline_if_ready(completed_at)


# These are complete source/store boundaries. Tests replace each whole object,
# while production always uses Task 2's facade and the Postgres mirror.
_source: Any = _OdooSource()
_backend: Any = _MirrorBackend()


def _now_utc(value: datetime | None) -> datetime:
    if value is None:
        return datetime.now(UTC)
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise TypeError("now_utc must be an aware datetime")
    return value.astimezone(UTC)


def _error_text(error: object) -> str:
    return str(error)[:500]


def _record_failure_safely(owner: str, error: object) -> None:
    try:
        _backend.record_failure(owner, _error_text(error))
    except Exception:  # noqa: BLE001 - preserve the source/store failure
        _log.exception("could not record Odoo attendance mirror failure")


def _normalize_source_rows(value: object, *, context: str) -> tuple[dict, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise RuntimeError(f"Odoo {context} response must be a sequence")
    rows: list[dict] = []
    for raw_row in value:
        if not isinstance(raw_row, Mapping):
            raise RuntimeError(f"Odoo {context} response contained a malformed row")
        # The mirror owns the exact normalized-row validation contract. Running
        # it before opening the transaction prevents a malformed page from
        # partially changing the last-good mirror.
        rows.append(attendance_mirror._normalize_row(raw_row))
    return tuple(rows)


def _merged_rows(
    changed_rows: Sequence[dict], open_rows: Sequence[dict]
) -> tuple[dict, ...]:
    newest: dict[int, dict] = {}
    for row in (*changed_rows, *open_rows):
        attendance_id = row["odoo_attendance_id"]
        existing = newest.get(attendance_id)
        if existing is None or row["odoo_write_date"] >= existing["odoo_write_date"]:
            newest[attendance_id] = row
    return tuple(newest[key] for key in sorted(newest))


def _cursor_for(rows: Sequence[dict]) -> tuple[datetime | None, int | None]:
    if not rows:
        return None, None
    newest = max(
        rows,
        key=lambda row: (row["odoo_write_date"], row["odoo_attendance_id"]),
    )
    return newest["odoo_write_date"], newest["odoo_attendance_id"]


def _run_incremental_locked(now: datetime) -> SyncResult:
    try:
        state = _backend.sync_state()
        _backend.record_incremental_started(now)
        raw_changes = _source.fetch_attendance_changes(
            after_write_date=state.cursor_write_date,
            after_id=state.cursor_id,
        )
        # Fetching all open rows is deliberately part of the same logical
        # cycle. Nothing is stored if either complete Task 2 read fails.
        raw_open_rows = _source.fetch_open_attendance_rows()
        changes = _normalize_source_rows(
            raw_changes, context="attendance changes"
        )
        open_rows = _normalize_source_rows(
            raw_open_rows, context="open attendance"
        )
        merged = _merged_rows(changes, open_rows)
        cursor_write_date, cursor_id = _cursor_for(changes)
        affected = _backend.store_incremental_cycle(
            merged,
            cursor_write_date=cursor_write_date,
            cursor_id=cursor_id,
            completed_at=now,
        )
    except Exception as exc:  # noqa: BLE001 - report failure, keep last-good state
        _record_failure_safely("incremental", exc)
        return SyncResult(success=False, error=_error_text(exc))
    return SyncResult(
        success=True,
        incremental_completed=True,
        rows_stored=len(merged),
        affected_days=frozenset(affected),
    )


def run_incremental_sync(*, now_utc: datetime | None = None) -> SyncResult:
    """Fetch changes plus all open rows, then commit one completed cycle."""
    now = _now_utc(now_utc)
    try:
        with _backend.logical_run_lock():
            return _run_incremental_locked(now)
    except Exception as exc:  # noqa: BLE001 - lock acquisition/commit failure
        _record_failure_safely("incremental", exc)
        return SyncResult(success=False, error=_error_text(exc))


def _validated_sweep_snapshot(value: object) -> set[int]:
    if value is None or not hasattr(value, "ids") or not hasattr(value, "complete"):
        raise RuntimeError("Odoo attendance ID sweep omitted its completion boundary")
    if value.complete is not True:
        raise RuntimeError("Odoo attendance ID sweep was not complete")
    raw_ids = value.ids
    if isinstance(raw_ids, (str, bytes)) or not isinstance(raw_ids, Sequence):
        raise RuntimeError("Odoo attendance ID sweep IDs must be a sequence")
    ids: list[int] = []
    for value_id in raw_ids:
        if (
            isinstance(value_id, bool)
            or not isinstance(value_id, int)
            or value_id <= 0
        ):
            raise RuntimeError("Odoo attendance ID sweep contained an invalid id")
        ids.append(value_id)
    if len(ids) != len(set(ids)):
        raise RuntimeError("Odoo attendance ID sweep contained duplicate ids")
    return set(ids)


_DIRECT_ID_CHUNK_SIZE = 250


def _read_rows_by_ids(ids: set[int], *, require_all: bool) -> tuple[dict, ...]:
    requested_ids = sorted(ids)
    rows: list[dict] = []
    for offset in range(0, len(requested_ids), _DIRECT_ID_CHUNK_SIZE):
        chunk = requested_ids[offset : offset + _DIRECT_ID_CHUNK_SIZE]
        raw_rows = _source.fetch_attendance_rows_by_ids(chunk)
        normalized = _normalize_source_rows(
            raw_rows, context="attendance rows by ID"
        )
        normalized_ids = [row["odoo_attendance_id"] for row in normalized]
        if len(normalized_ids) != len(set(normalized_ids)):
            raise RuntimeError("Odoo attendance rows by ID contained duplicate ids")
        unrelated = set(normalized_ids) - set(chunk)
        if unrelated:
            raise RuntimeError("Odoo attendance rows by ID contained unrelated ids")
        rows.extend(normalized)
    found_ids = {row["odoo_attendance_id"] for row in rows}
    if require_all and found_ids != ids:
        raise RuntimeError("Odoo attendance tombstone recovery omitted requested ids")
    return tuple(rows)


def _run_full_sweep_locked(now: datetime) -> SyncResult:
    try:
        state = _backend.sync_state()
        ids = _validated_sweep_snapshot(
            _source.fetch_complete_attendance_id_sweep()
        )
        active_ids = _backend.active_attendance_ids()
        if not ids and active_ids:
            confirmation_rows = _read_rows_by_ids(active_ids, require_all=False)
            if confirmation_rows:
                raise RuntimeError(
                    "Odoo empty attendance sweep contradicted direct ID confirmation"
                )
        recovery_ids = _backend.tombstoned_attendance_ids(ids)
        recovery_rows = _read_rows_by_ids(recovery_ids, require_all=True)
        generation = state.full_sweep_generation + 1
        committed = _backend.store_full_sweep(
            ids,
            recovery_rows=recovery_rows,
            generation=generation,
            completed_at=now,
        )
    except Exception as exc:  # noqa: BLE001 - fail closed without tombstones
        _record_failure_safely("sweep", exc)
        return SyncResult(success=False, error=_error_text(exc))
    return SyncResult(
        success=True,
        full_sweep_completed=True,
        rows_deleted=committed.deleted_count,
        affected_days=frozenset(committed.affected_days),
    )


def run_full_sweep(*, now_utc: datetime | None = None) -> SyncResult:
    """Tombstone rows only after one complete, validated Task 2 ID sweep."""
    now = _now_utc(now_utc)
    try:
        with _backend.logical_run_lock():
            return _run_full_sweep_locked(now)
    except Exception as exc:  # noqa: BLE001 - lock acquisition/commit failure
        _record_failure_safely("sweep", exc)
        return SyncResult(success=False, error=_error_text(exc))


def _sweep_is_due(state: SyncState, now: datetime) -> bool:
    last_sweep = state.last_full_sweep_completed_at
    return last_sweep is None or now - last_sweep >= _FULL_SWEEP_INTERVAL


def tick(*, now_utc: datetime | None = None) -> SyncResult:
    """Run the live poll each tick and the safe deletion sweep once per hour."""
    now = _now_utc(now_utc)
    try:
        initial_state = _backend.sync_state()
    except Exception as exc:  # noqa: BLE001 - present a bounded health result
        _record_failure_safely("incremental", exc)
        return SyncResult(success=False, error=_error_text(exc))

    sweep_due = _sweep_is_due(initial_state, now)
    incremental = run_incremental_sync(now_utc=now)
    sweep = run_full_sweep(now_utc=now) if sweep_due else SyncResult(success=True)

    baseline_completed = initial_state.baseline_completed_at is not None
    can_complete_baseline = incremental.success and (not sweep_due or sweep.success)
    if can_complete_baseline:
        try:
            baseline_completed = _backend.complete_baseline_if_ready(now)
        except Exception as exc:  # noqa: BLE001 - baseline must remain incomplete
            _record_failure_safely("baseline", exc)
            return SyncResult(
                success=False,
                incremental_completed=incremental.incremental_completed,
                full_sweep_completed=sweep.full_sweep_completed,
                baseline_completed=False,
                rows_stored=incremental.rows_stored,
                rows_deleted=sweep.rows_deleted,
                affected_days=incremental.affected_days | sweep.affected_days,
                error=_error_text(exc),
            )

    success = incremental.success and (not sweep_due or sweep.success)
    error = incremental.error or sweep.error
    return SyncResult(
        success=success,
        incremental_completed=incremental.incremental_completed,
        full_sweep_completed=sweep.full_sweep_completed,
        baseline_completed=baseline_completed,
        rows_stored=incremental.rows_stored,
        rows_deleted=sweep.rows_deleted,
        affected_days=incremental.affected_days | sweep.affected_days,
        error=error,
    )


__all__ = [
    "SyncResult",
    "run_full_sweep",
    "run_incremental_sync",
    "tick",
]
