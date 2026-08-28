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


class _OdooSource:
    def fetch_attendance_changes(self, **kwargs):
        return odoo_client.fetch_attendance_changes(**kwargs)

    def fetch_open_attendance_rows(self):
        return odoo_client.fetch_open_attendance_rows()

    def fetch_all_attendance_ids(self):
        return odoo_client.fetch_all_attendance_ids()


class _MirrorBackend:
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
        self, ids: set[int], *, generation: int, completed_at: datetime
    ) -> tuple[set[date], int]:
        return attendance_mirror._store_full_sweep(
            ids, generation=generation, completed_at=completed_at
        )

    def record_failure(self, error: object) -> None:
        attendance_mirror._record_failure(error)

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


def _record_failure_safely(error: object) -> None:
    try:
        _backend.record_failure(_error_text(error))
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


def run_incremental_sync(*, now_utc: datetime | None = None) -> SyncResult:
    """Fetch changes plus all open rows, then commit one completed cycle."""
    now = _now_utc(now_utc)
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
        _record_failure_safely(exc)
        return SyncResult(success=False, error=_error_text(exc))
    return SyncResult(
        success=True,
        incremental_completed=True,
        rows_stored=len(merged),
        affected_days=frozenset(affected),
    )


def _validated_sweep_ids(value: object) -> set[int]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise RuntimeError("Odoo attendance ID sweep must be a sequence")
    if not value:
        # The Task 2 contract cannot distinguish an actually empty model from
        # an unexpectedly empty/filtered response. Failing closed is safer
        # than tombstoning every durable row.
        raise RuntimeError("Odoo attendance ID sweep was ambiguously empty")
    ids: list[int] = []
    for value_id in value:
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


def run_full_sweep(*, now_utc: datetime | None = None) -> SyncResult:
    """Tombstone rows only after one complete, validated Task 2 ID sweep."""
    now = _now_utc(now_utc)
    try:
        state = _backend.sync_state()
        ids = _validated_sweep_ids(_source.fetch_all_attendance_ids())
        generation = state.full_sweep_generation + 1
        affected, rows_deleted = _backend.store_full_sweep(
            ids, generation=generation, completed_at=now
        )
    except Exception as exc:  # noqa: BLE001 - fail closed without tombstones
        _record_failure_safely(exc)
        return SyncResult(success=False, error=_error_text(exc))
    return SyncResult(
        success=True,
        full_sweep_completed=True,
        rows_deleted=rows_deleted,
        affected_days=frozenset(affected),
    )


def _sweep_is_due(state: SyncState, now: datetime) -> bool:
    last_sweep = state.last_full_sweep_completed_at
    return last_sweep is None or now - last_sweep >= _FULL_SWEEP_INTERVAL


def tick(*, now_utc: datetime | None = None) -> SyncResult:
    """Run the live poll each tick and the safe deletion sweep once per hour."""
    now = _now_utc(now_utc)
    try:
        initial_state = _backend.sync_state()
    except Exception as exc:  # noqa: BLE001 - present a bounded health result
        _record_failure_safely(exc)
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
            _record_failure_safely(exc)
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

    # A successful sweep clears the shared error field transactionally. If the
    # incremental half failed during this same tick, restore that more relevant
    # failure without touching its prior cursor or freshness timestamp.
    if not incremental.success and sweep.success and sweep_due:
        _record_failure_safely(incremental.error or "incremental sync failed")

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
