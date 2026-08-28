"""Restart-safe review and reconciliation for linked absence PTO requests."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import uuid4

from . import absence_pto_conversion as conversion
from . import absence_pto_store as store
from . import staffing_hours
from .plant_day import today as plant_today


_ROLLOVER_ERROR = "Configured pay period closed before approval."


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
    resumed = escalated = failed = 0
    for request in requests:
        try:
            if request.state == "pending":
                store.mark_needs_review(
                    request.id,
                    owner,
                    error=_ROLLOVER_ERROR,
                    now=_now(None),
                )
                escalated += 1
                continue
            if request.state != "converting":
                failed += 1
                continue
            result = conversion.resume_claimed(request, owner)
            if result.status == "needs_review":
                escalated += 1
            elif result.status == "busy":
                failed += 1
            else:
                resumed += 1
        except Exception:  # noqa: BLE001 - one recovery row cannot stop its batch
            failed += 1
        finally:
            store.release_claim(request.id, owner, now=_now(None))
    return ReconcileResult(
        scanned=len(requests),
        resumed=resumed,
        escalated=escalated,
        failed=failed,
    )


__all__ = ["ReconcileResult", "reconcile_once"]
