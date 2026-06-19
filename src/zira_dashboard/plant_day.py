"""Plant-local business-day helpers.

Daily operations in this app are run by the plant's local calendar, not UTC.
Use these helpers for "today" on staffing, attendance, alerts, and live
dashboards so evening Central time does not roll the app into tomorrow early.
"""

from __future__ import annotations

from datetime import date, datetime, timezone

from .shift_config import SITE_TZ


def today(now: datetime | None = None) -> date:
    """Return the current plant-local date.

    ``now`` is accepted for tests and may be naive or timezone-aware. Naive
    datetimes are treated as UTC to match the app's server-side timestamps.
    """
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return now.astimezone(SITE_TZ).date()


def now() -> datetime:
    """Current plant-local datetime."""
    return datetime.now(timezone.utc).astimezone(SITE_TZ)


def parse_day(value: str | None) -> date:
    """Parse an ISO day or fall back to plant-local today."""
    if not value:
        return today()
    return date.fromisoformat(value)
