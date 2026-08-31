"""Safe incremental orchestration for the durable Odoo attendance mirror."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from contextlib import contextmanager
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
    repair_attendance_ids: frozenset[int] = frozenset()
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
    @contextmanager
    def logical_run(self):
        with attendance_mirror._logical_run_lock() as cur:
            yield _MirrorRunBackend(cur)

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

    def record_failure(self, owner: str, error: object) -> None:
        attendance_mirror._record_failure(owner, error)

    def complete_baseline_if_ready(self, completed_at: datetime) -> bool:
        return attendance_mirror._complete_baseline_if_ready(completed_at)


class _MirrorRunBackend:
    """Mirror operations bound to the cursor holding the logical-run lock."""

    def __init__(self, cur):
        self._cur = cur

    def sync_state(self) -> SyncState:
        state = attendance_mirror._sync_state_cur(self._cur)
        return SyncState(
            cursor_write_date=state.cursor_write_date,
            cursor_id=state.cursor_id,
            last_incremental_completed_at=state.last_incremental_completed_at,
            last_full_sweep_completed_at=state.last_full_sweep_completed_at,
            full_sweep_generation=state.full_sweep_generation,
            baseline_completed_at=state.baseline_completed_at,
        )

    def record_incremental_started(self, started_at: datetime) -> None:
        attendance_mirror._record_incremental_started_cur(self._cur, started_at)

    def store_incremental_cycle(
        self,
        rows: Sequence[dict],
        *,
        cursor_write_date: datetime | None,
        cursor_id: int | None,
        completed_at: datetime,
        observed_at: datetime,
    ) -> set[date]:
        return attendance_mirror._store_incremental_cycle_cur(
            self._cur,
            rows,
            cursor_write_date=cursor_write_date,
            cursor_id=cursor_id,
            completed_at=completed_at,
            observed_at=observed_at,
        )

    def active_attendance_ids(self) -> set[int]:
        return attendance_mirror._active_attendance_ids_cur(self._cur)

    def tombstoned_attendance_ids(self, ids: set[int]) -> set[int]:
        return attendance_mirror._tombstoned_attendance_ids_cur(self._cur, ids)

    def store_full_sweep(
        self,
        ids: set[int],
        *,
        recovery_rows: Sequence[dict],
        generation: int,
        completed_at: datetime,
        observed_at: datetime,
    ) -> SweepStoreResult:
        result = attendance_mirror._store_full_sweep_cur(
            self._cur,
            ids,
            recovery_rows=recovery_rows,
            generation=generation,
            completed_at=completed_at,
            observed_at=observed_at,
        )
        return SweepStoreResult(
            affected_days=result.affected_days,
            deleted_count=result.deleted_count,
        )


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


def _requested_time(value: datetime | None) -> datetime | None:
    """Validate a caller clock without resolving the default before the lock."""
    return None if value is None else _now_utc(value)


def _locked_run_times(requested_at: datetime | None) -> tuple[datetime, datetime]:
    """Resolve clocks without letting a caller clock order source observations.

    ``requested_at`` remains the deterministic clock for scheduling and sync
    completion state. The internal observation clock is always sampled after
    the logical-run lock is held, so its order matches serialized source reads.
    """
    completed_at = requested_at if requested_at is not None else _now_utc(None)
    observed_at = _now_utc(None)
    return completed_at, observed_at


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
        # it before any mirror write ensures a malformed page rolls back the
        # locked transaction without changing the last-good mirror.
        rows.append(attendance_mirror._normalize_row(raw_row))
    return tuple(rows)


def _merged_rows(changed_rows: Sequence[dict], open_rows: Sequence[dict]) -> tuple[dict, ...]:
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


def _run_incremental_locked(
    run_backend,
    completed_at: datetime,
    *,
    observed_at: datetime,
) -> SyncResult:
    state = run_backend.sync_state()
    run_backend.record_incremental_started(completed_at)
    raw_changes = _source.fetch_attendance_changes(
        after_write_date=state.cursor_write_date,
        after_id=state.cursor_id,
    )
    # Fetching all open rows is deliberately part of the same logical
    # cycle. Nothing is stored if either complete Task 2 read fails.
    raw_open_rows = _source.fetch_open_attendance_rows()
    changes = _normalize_source_rows(raw_changes, context="attendance changes")
    open_rows = _normalize_source_rows(raw_open_rows, context="open attendance")
    merged = _merged_rows(changes, open_rows)
    cursor_write_date, cursor_id = _cursor_for(changes)
    affected = run_backend.store_incremental_cycle(
        merged,
        cursor_write_date=cursor_write_date,
        cursor_id=cursor_id,
        completed_at=completed_at,
        observed_at=observed_at,
    )
    return SyncResult(
        success=True,
        incremental_completed=True,
        rows_stored=len(merged),
        affected_days=frozenset(affected),
        repair_attendance_ids=frozenset(row["odoo_attendance_id"] for row in merged),
    )


def run_incremental_sync(*, now_utc: datetime | None = None) -> SyncResult:
    """Fetch changes plus all open rows, then commit one completed cycle."""
    requested_at = _requested_time(now_utc)
    try:
        with _backend.logical_run() as run_backend:
            completed_at, observed_at = _locked_run_times(requested_at)
            result = _run_incremental_locked(
                run_backend,
                completed_at,
                observed_at=observed_at,
            )
        try:
            _enqueue_department_repairs_after_sync(
                result,
                now_utc=completed_at,
                include_current_day=True,
            )
        except Exception:  # noqa: BLE001 - discovery cannot relabel a committed sync
            _log.exception("could not enqueue Odoo attendance department repairs")
        return result
    except Exception as exc:  # noqa: BLE001 - rollback before failure recording
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
        if isinstance(value_id, bool) or not isinstance(value_id, int) or value_id <= 0:
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
        normalized = _normalize_source_rows(raw_rows, context="attendance rows by ID")
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


def _run_full_sweep_locked(
    run_backend,
    completed_at: datetime,
    *,
    observed_at: datetime,
    only_if_due: bool = False,
) -> SyncResult:
    state = run_backend.sync_state()
    if only_if_due and not _sweep_is_due(state, completed_at):
        return SyncResult(success=True)
    ids = _validated_sweep_snapshot(_source.fetch_complete_attendance_id_sweep())
    active_ids = run_backend.active_attendance_ids()
    if not ids and active_ids:
        confirmation_rows = _read_rows_by_ids(active_ids, require_all=False)
        if confirmation_rows:
            raise RuntimeError("Odoo empty attendance sweep contradicted direct ID confirmation")
    recovery_ids = run_backend.tombstoned_attendance_ids(ids)
    recovery_rows = _read_rows_by_ids(recovery_ids, require_all=True)
    generation = state.full_sweep_generation + 1
    committed = run_backend.store_full_sweep(
        ids,
        recovery_rows=recovery_rows,
        generation=generation,
        completed_at=completed_at,
        observed_at=observed_at,
    )
    return SyncResult(
        success=True,
        full_sweep_completed=True,
        baseline_completed=state.baseline_completed_at is not None,
        rows_deleted=committed.deleted_count,
        affected_days=frozenset(committed.affected_days),
        repair_attendance_ids=frozenset(row["odoo_attendance_id"] for row in recovery_rows),
    )


def _run_full_sweep(requested_at: datetime | None, *, only_if_due: bool) -> SyncResult:
    try:
        with _backend.logical_run() as run_backend:
            completed_at, observed_at = _locked_run_times(requested_at)
            result = _run_full_sweep_locked(
                run_backend,
                completed_at,
                observed_at=observed_at,
                only_if_due=only_if_due,
            )
        if result.full_sweep_completed:
            try:
                _enqueue_department_repairs_after_sync(
                    result,
                    now_utc=completed_at,
                    include_current_day=False,
                )
            except Exception:  # noqa: BLE001 - discovery cannot relabel a committed sweep
                _log.exception("could not enqueue Odoo attendance department repairs")
        return result
    except Exception as exc:  # noqa: BLE001 - rollback before failure recording
        _record_failure_safely("sweep", exc)
        return SyncResult(success=False, error=_error_text(exc))


def run_full_sweep(*, now_utc: datetime | None = None) -> SyncResult:
    """Force one complete, validated Task 2 ID sweep and safe tombstone pass."""
    return _run_full_sweep(_requested_time(now_utc), only_if_due=False)


def _run_full_sweep_if_due(requested_at: datetime | None) -> SyncResult:
    return _run_full_sweep(requested_at, only_if_due=True)


def _sweep_is_due(state: SyncState, now: datetime) -> bool:
    last_sweep = state.last_full_sweep_completed_at
    return last_sweep is None or now - last_sweep >= _FULL_SWEEP_INTERVAL


def _enqueue_department_repairs_after_sync(
    result: SyncResult,
    *,
    now_utc: datetime,
    include_current_day: bool,
) -> int:
    """Project only committed mirror state after a successful source pass."""
    if not result.success:
        return 0
    from . import attendance_department_repair

    return attendance_department_repair.enqueue_after_successful_sync(
        affected_days=result.affected_days,
        attendance_ids=result.repair_attendance_ids,
        now_utc=now_utc,
        include_current_day=include_current_day,
        include_baseline=result.baseline_completed,
    )


def tick(*, now_utc: datetime | None = None) -> SyncResult:
    """Run the live poll each tick and the safe deletion sweep once per hour."""
    requested_at = _requested_time(now_utc)
    initial_now = requested_at if requested_at is not None else _now_utc(None)
    try:
        initial_state = _backend.sync_state()
    except Exception as exc:  # noqa: BLE001 - present a bounded health result
        _record_failure_safely("incremental", exc)
        return SyncResult(success=False, error=_error_text(exc))

    sweep_due = _sweep_is_due(initial_state, initial_now)
    incremental = run_incremental_sync(now_utc=requested_at)
    sweep = _run_full_sweep_if_due(requested_at) if sweep_due else SyncResult(success=True)

    baseline_completed = initial_state.baseline_completed_at is not None
    baseline_became_complete = False
    can_complete_baseline = incremental.success and (not sweep_due or sweep.success)
    if can_complete_baseline:
        try:
            baseline_completed_at = requested_at if requested_at is not None else _now_utc(None)
            baseline_completed = _backend.complete_baseline_if_ready(baseline_completed_at)
            baseline_became_complete = (
                initial_state.baseline_completed_at is None and baseline_completed
            )
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
                repair_attendance_ids=(
                    incremental.repair_attendance_ids | sweep.repair_attendance_ids
                ),
                error=_error_text(exc),
            )

    success = incremental.success and (not sweep_due or sweep.success)
    error = incremental.error or sweep.error
    result = SyncResult(
        success=success,
        incremental_completed=incremental.incremental_completed,
        full_sweep_completed=sweep.full_sweep_completed,
        baseline_completed=baseline_completed,
        rows_stored=incremental.rows_stored,
        rows_deleted=sweep.rows_deleted,
        affected_days=incremental.affected_days | sweep.affected_days,
        repair_attendance_ids=(incremental.repair_attendance_ids | sweep.repair_attendance_ids),
        error=error,
    )
    if result.success and baseline_became_complete:
        try:
            _enqueue_department_repairs_after_sync(
                result,
                now_utc=requested_at if requested_at is not None else _now_utc(None),
                include_current_day=False,
            )
        except Exception:  # noqa: BLE001 - discovery cannot relabel baseline completion
            _log.exception("could not enqueue baseline Odoo attendance department repairs")
    return result


__all__ = [
    "SyncResult",
    "run_full_sweep",
    "run_incremental_sync",
    "tick",
]
