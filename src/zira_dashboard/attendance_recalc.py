"""Retryable worker for attendance-driven daily production recalculation."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Literal

from . import db


RecalcStatus = Literal["completed", "failed"]


@dataclass(frozen=True)
class RecalcResult:
    day: date
    status: RecalcStatus
    attempt_count: int
    rows_written: int
    error: str | None


@dataclass(frozen=True)
class _ClaimedJob:
    day: date
    reason: str
    requested_at: datetime
    attempt_count: int
    claimed_at: datetime


def _aware_utc(value: datetime | None) -> datetime:
    resolved = value or datetime.now(UTC)
    if resolved.tzinfo is None or resolved.utcoffset() is None:
        raise ValueError("now_utc must be timezone-aware")
    return resolved.astimezone(UTC)


def _claim_next(now_utc: datetime) -> _ClaimedJob | None:
    """Claim one eligible day without blocking another worker's row lock."""
    with db.cursor() as cur:
        cur.execute(
            """
            SELECT day, reason, requested_at, attempt_count
            FROM attendance_recalc_queue
            WHERE completed_at IS NULL
              AND (
                started_at IS NULL
                OR (
                  last_error IS NOT NULL
                  AND started_at <= %s - (
                    LEAST(300, power(2, LEAST(attempt_count, 8))::integer)
                    * interval '1 second'
                  )
                )
                OR (
                  last_error IS NULL
                  AND started_at <= %s - interval '15 minutes'
                )
              )
            ORDER BY requested_at, day
            FOR UPDATE SKIP LOCKED
            LIMIT 1
            """,
            (now_utc, now_utc),
        )
        row = cur.fetchone()
        if row is None:
            return None
        attempt_count = int(row["attempt_count"] or 0) + 1
        cur.execute(
            "UPDATE attendance_recalc_queue SET started_at = %s, "
            "attempt_count = attempt_count + 1, last_error = NULL "
            "WHERE day = %s",
            (now_utc, row["day"]),
        )
    return _ClaimedJob(
        day=row["day"],
        reason=str(row["reason"]),
        requested_at=row["requested_at"],
        attempt_count=attempt_count,
        claimed_at=now_utc,
    )


def _mark_failed(job: _ClaimedJob, error: str, failed_at: datetime) -> None:
    with db.cursor() as cur:
        cur.execute(
            "UPDATE attendance_recalc_queue SET last_error = %s, "
            "started_at = %s, completed_at = NULL "
            "WHERE day = %s AND (started_at = %s OR started_at IS NULL)",
            (error[:500], failed_at, job.day, job.claimed_at),
        )


def _mark_completed(job: _ClaimedJob, completed_at: datetime) -> None:
    with db.cursor() as cur:
        cur.execute(
            "UPDATE attendance_recalc_queue SET completed_at = %s, "
            "started_at = NULL, last_error = NULL "
            "WHERE day = %s AND started_at = %s",
            (completed_at, job.day, job.claimed_at),
        )


def process_next(
    *,
    production_client=None,
    now_utc: datetime | None = None,
) -> RecalcResult | None:
    """Recalculate the oldest eligible local day and persist its outcome."""
    now = _aware_utc(now_utc)
    job = _claim_next(now)
    if job is None:
        return None
    if production_client is None:
        from .deps import client as production_client
    from . import _http_cache, precompute

    try:
        result = precompute.precompute_day(job.day, production_client)
        _http_cache.invalidate_all_cache()
    except Exception as exc:  # noqa: BLE001 - failure is durable and retryable
        error = str(exc) or exc.__class__.__name__
        _mark_failed(job, error, now)
        return RecalcResult(
            day=job.day,
            status="failed",
            attempt_count=job.attempt_count,
            rows_written=0,
            error=error[:500],
        )
    _mark_completed(job, now)
    return RecalcResult(
        day=job.day,
        status="completed",
        attempt_count=job.attempt_count,
        rows_written=int(result.get("rows_written", 0)),
        error=None,
    )


__all__ = ["RecalcResult", "process_next"]
