"""Shared classification and lifecycle rules for optional plant workdays."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, timedelta
from threading import RLock
from typing import Literal

from . import company_holidays


OptionalWorkdayKind = Literal["saturday", "holiday"]


class NoNormalWorkday(LookupError):
    """No configured, non-holiday workday exists inside the search bound."""


@dataclass(frozen=True)
class OptionalWorkday:
    day: date
    kind: OptionalWorkdayKind
    name: str
    holiday_odoo_id: int | None


@dataclass(frozen=True)
class OptionalWorkdayState:
    workday: OptionalWorkday
    recruiting_status: str | None
    schedule_published: bool
    operational: bool


_publication_state_lock = RLock()
_publication_state_by_day: dict[date, object | None] = {}


def invalidate(day: date) -> None:
    """Discard the cached recruiting publication projection for one day."""
    with _publication_state_lock:
        _publication_state_by_day.pop(day, None)


def invalidate_all() -> None:
    """Discard every cached recruiting publication projection."""
    with _publication_state_lock:
        _publication_state_by_day.clear()


def _publication_state(day: date):
    from . import saturday_recruiting_store

    with _publication_state_lock:
        if day not in _publication_state_by_day:
            _publication_state_by_day[day] = saturday_recruiting_store.publication_state(day)
        return _publication_state_by_day[day]


def for_day(day: date) -> OptionalWorkday | None:
    """Classify a holiday or Saturday, giving a holiday precedence."""
    holiday = company_holidays.for_day(day)
    if holiday is not None:
        return OptionalWorkday(day, "holiday", holiday.name, holiday.odoo_id)
    if day.weekday() == 5:
        return OptionalWorkday(day, "saturday", "Saturday", None)
    return None


def state_for_day(day: date) -> OptionalWorkdayState | None:
    """Resolve the saved lifecycle and operational state for an optional day."""
    workday = for_day(day)
    if workday is None:
        return None

    # Lazy imports avoid the existing
    # shift_config -> staffing -> schedule_store lifecycle import cycle.
    from . import staffing

    recruitment = _publication_state(day)
    schedule = staffing.load_schedule(day)
    recruiting_status = getattr(recruitment, "status", None)
    schedule_published = bool(getattr(schedule, "published", False))

    if workday.kind == "holiday":
        operational = bool(
            recruitment
            and getattr(recruitment, "day_kind", None) == "holiday"
            and getattr(recruitment, "holiday_odoo_id", None) == workday.holiday_odoo_id
            and recruiting_status == "published"
            and schedule_published
        )
    else:
        # Preserve legacy Saturdays that were published before recruiting
        # lifecycle rows existed.
        operational = schedule_published

    return OptionalWorkdayState(
        workday=workday,
        recruiting_status=recruiting_status,
        schedule_published=schedule_published,
        operational=operational,
    )


def holiday_is_explicitly_published(day: date) -> bool:
    """Whether the current mirrored holiday has matching dual publication."""
    state = state_for_day(day)
    return bool(state and state.workday.kind == "holiday" and state.operational)


def _adjacent_normal_workday(
    day: date,
    work_weekdays: frozenset[int],
    step: int,
    *,
    is_holiday: Callable[[date], bool] | None = None,
) -> date:
    cursor = day
    for _ in range(14):
        cursor += timedelta(days=step)
        mirrored_holiday = company_holidays.for_day(cursor) is not None
        supplied_holiday = bool(is_holiday and is_holiday(cursor))
        if (
            cursor.weekday() in work_weekdays
            and cursor.weekday() != 5
            and not mirrored_holiday
            and not supplied_holiday
        ):
            return cursor
    raise NoNormalWorkday("No configured normal plant workday")


def previous_normal_workday(
    day: date,
    work_weekdays: frozenset[int],
    *,
    is_holiday: Callable[[date], bool] | None = None,
) -> date:
    """Find the prior configured weekday, excluding mirrored holidays."""
    return _adjacent_normal_workday(day, work_weekdays, -1, is_holiday=is_holiday)


def next_normal_workday(
    day: date,
    work_weekdays: frozenset[int],
    *,
    is_holiday: Callable[[date], bool] | None = None,
) -> date:
    """Find the next configured weekday, excluding mirrored holidays."""
    return _adjacent_normal_workday(day, work_weekdays, 1, is_holiday=is_holiday)
