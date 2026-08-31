"""Durable one-day worker for attendance-driven production recalculation."""

from __future__ import annotations

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
            SET completed_at = %s, started_at = NULL, last_error = NULL
            WHERE day = %s
              AND completed_at IS NULL
              AND attempt_count = %s
              AND started_at = %s
            RETURNING day
            """,
            (completed, claim.day, claim.attempt_count, claim.lease_until),
        )
        if cur.fetchone() is None:
            raise RuntimeError("attendance recalculation claim changed while completing")
        return rows_written


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


def _refresh_caches(day: date) -> None:
    """Refresh attribution-dependent views after the queue commit succeeds."""
    from . import _http_cache, staffing

    try:
        staffing.invalidate_schedule_cache(day)
    except Exception:
        _log.warning(
            "attendance recalculation staffing cache refresh failed for %s",
            day,
            exc_info=True,
        )
    try:
        _http_cache.invalidate_all_cache()
    except Exception:
        _log.warning(
            "attendance recalculation HTTP cache refresh failed for %s",
            day,
            exc_info=True,
        )


def process_next(
    *,
    production_client=None,
    now_utc: datetime | None = None,
) -> RecalcResult | None:
    """Claim, recompute, and complete one queue day, or return ``None``."""
    from . import precompute

    now = _aware_utc(now_utc)
    claim = _claim_next(now)
    if claim is None:
        return None
    try:
        client = production_client
        if client is None:
            client = _default_production_client()
        prepared = precompute.prepare_day(claim.day, client)
        rows_written = _complete_claim(claim, prepared, now)
    except Exception as error:  # noqa: BLE001 - every failure remains retryable
        retry_at = None
        record_error = None
        try:
            retry_at = _record_failure(claim, error, now)
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

    try:
        _refresh_caches(claim.day)
    except Exception:  # pragma: no cover - injected replacements may still fail
        _log.warning(
            "attendance recalculation cache refresh failed for %s",
            claim.day,
            exc_info=True,
        )
    return RecalcResult(
        day=claim.day,
        status="completed",
        attempt_count=claim.attempt_count,
        rows_written=rows_written,
    )


__all__ = ["RecalcResult", "process_next"]
