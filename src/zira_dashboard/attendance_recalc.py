"""Durable one-day worker for attendance-driven production recalculation."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
import logging
from typing import Literal

from . import db


CLAIM_LEASE = timedelta(minutes=15)
ERROR_LIMIT = 500
_MAX_BACKOFF = timedelta(minutes=15)
_BASE_BACKOFF_SECONDS = 15

_log = logging.getLogger(__name__)


@dataclass(frozen=True)
class RecalcClaim:
    day: date
    attempt_count: int
    lease_until: datetime


@dataclass(frozen=True)
class CacheRefreshClaim:
    day: date
    attempt_count: int
    completed_at: datetime
    lease_until: datetime


@dataclass(frozen=True)
class RecalcResult:
    day: date
    status: Literal["completed", "failed", "superseded"]
    attempt_count: int
    rows_written: int = 0
    error: str | None = None
    record_error: str | None = None
    retry_at: datetime | None = None


def _aware_utc(value: datetime | None) -> datetime:
    resolved = value or datetime.now(UTC)
    if not isinstance(resolved, datetime) or resolved.utcoffset() is None:
        raise TypeError("now_utc must be timezone-aware")
    return resolved.astimezone(UTC)


def _retry_delay(attempt_count: int) -> timedelta:
    if isinstance(attempt_count, bool) or not isinstance(attempt_count, int):
        raise TypeError("attempt_count must be an integer")
    if attempt_count <= 0:
        raise ValueError("attempt_count must be positive")
    seconds = _BASE_BACKOFF_SECONDS * (2 ** min(attempt_count - 1, 20))
    return min(timedelta(seconds=seconds), _MAX_BACKOFF)


def _claim_next(now_utc: datetime) -> RecalcClaim | None:
    """Claim the oldest eligible day and fence it with a durable lease."""
    now = _aware_utc(now_utc)
    with db.cursor() as cur:
        cur.execute(
            """
            SELECT day, attempt_count
            FROM attendance_recalc_queue
            WHERE completed_at IS NULL
              AND (started_at IS NULL OR started_at <= %s)
            ORDER BY requested_at ASC, day ASC
            LIMIT 1
            FOR UPDATE SKIP LOCKED
            """,
            (now,),
        )
        row = cur.fetchone()
        if row is None:
            return None
        day = row["day"]
        attempt_count = int(row["attempt_count"] or 0) + 1
        lease_until = now + CLAIM_LEASE
        cur.execute(
            """
            UPDATE attendance_recalc_queue
            SET started_at = %s, attempt_count = %s
            WHERE day = %s
            RETURNING day, attempt_count
            """,
            (lease_until, attempt_count, day),
        )
        updated = cur.fetchone()
        if updated is None:
            raise RuntimeError("attendance recalculation claim disappeared")
    return RecalcClaim(
        day=day,
        attempt_count=attempt_count,
        lease_until=lease_until,
    )


def _claim_pending_cache(now_utc: datetime) -> CacheRefreshClaim | None:
    """Claim one completed production day whose cache refresh needs recovery."""
    now = _aware_utc(now_utc)
    with db.cursor() as cur:
        cur.execute(
            """
            SELECT day, attempt_count, completed_at
            FROM attendance_recalc_queue
            WHERE completed_at IS NOT NULL
              AND cache_ready_at IS NULL
              AND (cache_started_at IS NULL OR cache_started_at <= %s)
            ORDER BY completed_at ASC, requested_at ASC, day ASC
            LIMIT 1
            FOR UPDATE SKIP LOCKED
            """,
            (now,),
        )
        row = cur.fetchone()
        if row is None:
            return None
        day = row["day"]
        attempt_count = int(row["attempt_count"] or 0)
        completed_at = _aware_utc(row["completed_at"])
        lease_until = now + CLAIM_LEASE
        cur.execute(
            """
            UPDATE attendance_recalc_queue
            SET cache_started_at = %s
            WHERE day = %s
              AND completed_at = %s
              AND cache_ready_at IS NULL
            RETURNING day
            """,
            (lease_until, day, completed_at),
        )
        if cur.fetchone() is None:
            raise RuntimeError("attendance cache refresh claim disappeared")
    return CacheRefreshClaim(
        day=day,
        attempt_count=attempt_count,
        completed_at=completed_at,
        lease_until=lease_until,
    )


def _complete_claim(claim: RecalcClaim, prepared, completed_at: datetime) -> int | None:
    """Atomically fence, write, and complete the current recalculation claim."""
    from . import precompute

    completed = _aware_utc(completed_at)
    if prepared.day != claim.day:
        raise ValueError("prepared production day does not match recalculation claim")
    with db.cursor() as cur:
        cur.execute(
            """
            SELECT day, attempt_count, started_at, completed_at
            FROM attendance_recalc_queue
            WHERE day = %s
            FOR UPDATE
            """,
            (claim.day,),
        )
        row = cur.fetchone()
        if (
            row is None
            or row["completed_at"] is not None
            or int(row["attempt_count"] or 0) != claim.attempt_count
            or row["started_at"] != claim.lease_until
        ):
            return None
        rows_written = precompute.store_prepared_day(prepared, cur=cur)
        cur.execute(
            """
            UPDATE attendance_recalc_queue
            SET completed_at = %s, started_at = NULL,
                cache_started_at = %s, cache_ready_at = NULL,
                last_error = NULL
            WHERE day = %s
              AND completed_at IS NULL
              AND attempt_count = %s
              AND started_at = %s
            RETURNING day
            """,
            (
                completed,
                completed + CLAIM_LEASE,
                claim.day,
                claim.attempt_count,
                claim.lease_until,
            ),
        )
        if cur.fetchone() is None:
            raise RuntimeError("attendance recalculation claim changed while completing")
        return rows_written


def _mark_cache_ready(
    claim: CacheRefreshClaim,
    ready_at: datetime,
) -> bool:
    """Fence cache readiness to the worker that owns this refresh lease."""
    ready = _aware_utc(ready_at)
    with db.cursor() as cur:
        cur.execute(
            """
            UPDATE attendance_recalc_queue
            SET cache_started_at = NULL, cache_ready_at = %s
            WHERE day = %s
              AND completed_at = %s
              AND attempt_count = %s
              AND cache_ready_at IS NULL
              AND cache_started_at = %s
            RETURNING day
            """,
            (
                ready,
                claim.day,
                claim.completed_at,
                claim.attempt_count,
                claim.lease_until,
            ),
        )
        return cur.fetchone() is not None


def _record_failure(
    claim: RecalcClaim,
    error: Exception,
    failed_at: datetime,
) -> datetime:
    failed = _aware_utc(failed_at)
    retry_at = failed + _retry_delay(claim.attempt_count)
    error_text = (str(error) or type(error).__name__)[:ERROR_LIMIT]
    with db.cursor() as cur:
        cur.execute(
            """
            UPDATE attendance_recalc_queue
            SET started_at = %s, last_error = %s
            WHERE day = %s
              AND completed_at IS NULL
              AND attempt_count = %s
              AND started_at = %s
            RETURNING day
            """,
            (retry_at, error_text, claim.day, claim.attempt_count, claim.lease_until),
        )
        if cur.fetchone() is None:
            raise RuntimeError("attendance recalculation failure claim was superseded")
    return retry_at


def _default_production_client():
    """Import the shared Zira client only after durable work is available."""
    from .deps import client

    return client


def _precompute_module():
    """Resolve the precompute module only after durable work is claimed."""
    from . import precompute

    return precompute


def _finished_at(clock: Callable[[], datetime] | None) -> datetime:
    return _aware_utc(clock() if clock is not None else None)


def _refresh_caches(day: date) -> None:
    """Refresh attribution-dependent views after the queue commit succeeds."""
    from . import _http_cache, staffing

    errors: list[Exception] = []
    try:
        staffing.invalidate_schedule_cache(day)
    except Exception as error:
        errors.append(error)
        _log.warning(
            "attendance recalculation staffing cache refresh failed for %s",
            day,
            exc_info=True,
        )
    try:
        _http_cache.invalidate_all_cache()
    except Exception as error:
        errors.append(error)
        _log.warning(
            "attendance recalculation HTTP cache refresh failed for %s",
            day,
            exc_info=True,
        )
    if errors:
        raise RuntimeError("attendance recalculation cache refresh did not finish") from errors[0]


def _finish_cache_refresh(
    claim: CacheRefreshClaim,
    *,
    rows_written: int,
    clock: Callable[[], datetime] | None,
) -> RecalcResult:
    """Refresh once for this lease and durably publish cache readiness."""
    try:
        _refresh_caches(claim.day)
    except Exception as error:  # noqa: BLE001 - lease expiry owns crash recovery
        _log.warning(
            "attendance recalculation cache refresh remains pending for %s",
            claim.day,
            exc_info=True,
        )
        return RecalcResult(
            day=claim.day,
            status="failed",
            attempt_count=claim.attempt_count,
            rows_written=rows_written,
            error=str(error) or type(error).__name__,
            retry_at=claim.lease_until,
        )
    if not _mark_cache_ready(claim, _finished_at(clock)):
        return RecalcResult(
            day=claim.day,
            status="superseded",
            attempt_count=claim.attempt_count,
            rows_written=rows_written,
        )
    return RecalcResult(
        day=claim.day,
        status="completed",
        attempt_count=claim.attempt_count,
        rows_written=rows_written,
    )


def process_next(
    *,
    production_client=None,
    now_utc: datetime | None = None,
    clock: Callable[[], datetime] | None = None,
) -> RecalcResult | None:
    """Claim, recompute, and complete one queue day, or return ``None``."""
    now = _aware_utc(now_utc)
    cache_claim = _claim_pending_cache(now)
    if cache_claim is not None:
        return _finish_cache_refresh(cache_claim, rows_written=0, clock=clock)
    claim = _claim_next(now)
    if claim is None:
        return None
    try:
        precompute = _precompute_module()
        client = production_client
        if client is None:
            client = _default_production_client()
        prepared = precompute.prepare_day(claim.day, client)
        completed_at = _finished_at(clock)
        rows_written = _complete_claim(claim, prepared, completed_at)
    except Exception as error:  # noqa: BLE001 - every failure remains retryable
        retry_at = None
        record_error = None
        try:
            retry_at = _record_failure(claim, error, _finished_at(clock))
        except Exception as failure_recording_error:  # noqa: BLE001
            record_error = str(failure_recording_error) or type(failure_recording_error).__name__
            _log.warning(
                "could not record attendance recalculation failure for %s: %s",
                claim.day,
                record_error,
                exc_info=True,
            )
        return RecalcResult(
            day=claim.day,
            status="failed",
            attempt_count=claim.attempt_count,
            error=str(error) or type(error).__name__,
            record_error=record_error,
            retry_at=retry_at,
        )

    if rows_written is None:
        return RecalcResult(
            day=claim.day,
            status="superseded",
            attempt_count=claim.attempt_count,
        )

    cache_claim = CacheRefreshClaim(
        day=claim.day,
        attempt_count=claim.attempt_count,
        completed_at=completed_at,
        lease_until=completed_at + CLAIM_LEASE,
    )
    return _finish_cache_refresh(cache_claim, rows_written=rows_written, clock=clock)


__all__ = ["RecalcResult", "process_next"]
