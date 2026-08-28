"""Restart-safe review and reconciliation for linked absence PTO requests."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import logging
from uuid import UUID, uuid4

from . import absence_pto_conversion as conversion
from . import absence_pto_store as store
from . import staffing_hours
from .plant_day import today as plant_today


_ROLLOVER_ERROR = "Configured pay period closed before approval."
_log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ReconcileResult:
    scanned: int
    resumed: int
    escalated: int
    failed: int


def _clock() -> datetime:
    return datetime.now(UTC)


def _now(value: datetime | None) -> datetime:
    current = _clock() if value is None else value
    if current.tzinfo is None or current.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    return current


def _reconcile_claimed(
    request: store.AbsencePtoRequest,
    owner: UUID,
) -> str:
    if request.state == "pending":
        store.mark_needs_review(
            request.id,
            owner,
            error=_ROLLOVER_ERROR,
            now=_now(None),
        )
        return "escalated"
    if request.state != "converting":
        return "failed"
    result = conversion.resume_claimed(request, owner)
    if result.status == "needs_review":
        return "escalated"
    if result.status == "busy":
        return "failed"
    return "resumed"


def _log_exception(request_id: int, phase: str, error: Exception) -> None:
    _log.error(
        "absence PTO reconcile %s failed for request %s: %s",
        phase,
        request_id,
        error,
        exc_info=(type(error), error, error.__traceback__),
    )


def reconcile_once(now: datetime | None = None, limit: int = 25) -> ReconcileResult:
    """Claim and isolate a bounded batch of rollover and recovery work."""
    current = _now(now)
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("limit must be between 1 and 100")
    period_start, period_end = staffing_hours.current_pay_period_bounds(
        plant_today(current)
    )
    owner = uuid4()
    requests = store.claim_due(
        owner,
        current,
        period_start=period_start,
        period_end=period_end,
        limit=limit,
    )
    counts = {"resumed": 0, "escalated": 0, "failed": 0}
    for request in requests:
        operation_error = None
        try:
            outcome = _reconcile_claimed(request, owner)
        except Exception as error:  # noqa: BLE001 - isolate each recovery row
            operation_error = error
            outcome = "failed"

        release_error = None
        try:
            released = store.release_claim(request.id, owner, now=_now(None))
            if not released:
                release_error = RuntimeError("lease release returned false")
        except Exception as error:  # noqa: BLE001 - isolate lease cleanup too
            release_error = error

        if operation_error is not None:
            _log_exception(request.id, "operation", operation_error)
        if release_error is not None:
            _log_exception(request.id, "release", release_error)
            outcome = "failed"
        counts[outcome] += 1
    return ReconcileResult(
        scanned=len(requests),
        resumed=counts["resumed"],
        escalated=counts["escalated"],
        failed=counts["failed"],
    )


__all__ = ["ReconcileResult", "reconcile_once"]
