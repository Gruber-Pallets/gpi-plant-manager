"""Seat today's schedule from a live Odoo work-center transfer.

Luke's floor app writes the current work center onto Odoo attendance. Plant
Manager already mirrors that location. This module is the missing write:
when a known roster person is validly at an app work center, they are seated
there on today's schedule and removed from any other planned seat.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date

from .staffing_view import StaffingPersonLocation

log = logging.getLogger(__name__)

_LIVE_SOURCE = "generated"


@dataclass(frozen=True)
class LiveAssignmentPlan:
    assignments: dict[str, list[str]]
    sources: dict[str, dict[str, str]]
    changed: bool


def _copy_assignments(
    planned_by_wc: Mapping[str, Sequence[str]],
) -> dict[str, list[str]]:
    from . import staffing

    copied = {
        wc_name: list(names)
        for wc_name, names in planned_by_wc.items()
        if wc_name != staffing.TIME_OFF_KEY
    }
    return copied


def _copy_sources(
    sources: Mapping[str, Mapping[str, str]],
) -> dict[str, dict[str, str]]:
    return {
        wc_name: dict(people)
        for wc_name, people in sources.items()
        if isinstance(people, Mapping)
    }


def _roster_name(location: StaffingPersonLocation) -> str | None:
    if location.identity_disambiguator:
        return None
    name = (location.profile_person_name or location.person_name or "").strip()
    return name or None


def _remove_person(
    assignments: dict[str, list[str]],
    sources: dict[str, dict[str, str]],
    person_name: str,
    *,
    keep_wc: str | None = None,
) -> None:
    for wc_name, names in list(assignments.items()):
        if wc_name == keep_wc:
            continue
        if person_name not in names:
            continue
        assignments[wc_name] = [name for name in names if name != person_name]
        people_sources = sources.get(wc_name)
        if people_sources and person_name in people_sources:
            rest = {
                name: source
                for name, source in people_sources.items()
                if name != person_name
            }
            if rest:
                sources[wc_name] = rest
            else:
                sources.pop(wc_name, None)


def plan_live_odoo_seats(
    planned_by_wc: Mapping[str, Sequence[str]],
    sources: Mapping[str, Mapping[str, str]],
    seats: Sequence[tuple[str, str]],
) -> LiveAssignmentPlan:
    """Seat each ``(person, live_work_center)`` pair, one person per center."""
    assignments = _copy_assignments(planned_by_wc)
    next_sources = _copy_sources(sources)

    for person_name, live_wc in seats:
        if not person_name or not live_wc:
            continue
        current = assignments.setdefault(live_wc, [])
        if person_name in current:
            continue
        _remove_person(assignments, next_sources, person_name, keep_wc=live_wc)
        assignments[live_wc] = [*assignments.get(live_wc, []), person_name]
        next_sources.setdefault(live_wc, {})[person_name] = _LIVE_SOURCE

    changed = False
    all_wcs = set(assignments) | set(planned_by_wc)
    for wc_name in all_wcs:
        if tuple(assignments.get(wc_name) or ()) != tuple(planned_by_wc.get(wc_name) or ()):
            changed = True
            break
    return LiveAssignmentPlan(assignments, next_sources, changed)


def plan_live_odoo_assignments(
    planned_by_wc: Mapping[str, Sequence[str]],
    sources: Mapping[str, Mapping[str, str]],
    locations: Sequence[StaffingPersonLocation],
) -> LiveAssignmentPlan:
    """Translate valid live locations into schedule seat moves."""
    seats: list[tuple[str, str]] = []
    for location in locations:
        name = _roster_name(location)
        if (
            name is None
            or location.status != "valid"
            or not location.live_work_center
        ):
            continue
        seats.append((name, location.live_work_center))
    return plan_live_odoo_seats(planned_by_wc, sources, seats)


def current_live_seats() -> tuple[tuple[str, str], ...]:
    """Return ``(roster_name, app_work_center)`` for people currently in Odoo."""
    try:
        from . import timeclock_windows

        windows, refreshed_at = timeclock_windows.current_attendance_windows()
        if refreshed_at is None and not windows:
            return ()
    except Exception:
        log.exception("Could not read current Odoo attendance windows")
        return ()
    seats: list[tuple[str, str]] = []
    for name, person_windows in windows.items():
        if not person_windows:
            continue
        wc_name = person_windows[-1][0]
        if name and wc_name:
            seats.append((name, wc_name))
    return tuple(seats)


def live_occupied_work_centers(day: date) -> set[str]:
    """App work centers that currently have a valid Odoo occupant on ``day``."""
    from . import plant_day

    if day != plant_day.today():
        return set()
    return {wc_name for _name, wc_name in current_live_seats()}


def live_people_at_work_center(wc_name: str, day: date) -> list[str]:
    """Roster names currently at ``wc_name`` in Odoo, for today only."""
    from . import plant_day

    if day != plant_day.today() or not wc_name:
        return []
    return [
        name
        for name, live_wc in current_live_seats()
        if live_wc == wc_name
    ]


def _after_schedule_write() -> None:
    """Drop the short-lived today caches that still show the old empty seat."""
    try:
        from . import _http_cache

        _http_cache.invalidate_today_cache()
    except Exception:
        log.exception("Could not invalidate today page cache after live assign")
    try:
        from .routes.staffing import _bust_assignments_todo_cache

        _bust_assignments_todo_cache()
    except Exception:
        log.exception("Could not bust assignments-todo cache after live assign")


def apply_live_odoo_assignments_for_day(
    day: date,
    locations: Sequence[StaffingPersonLocation] | None = None,
) -> bool:
    """Write inbound live Odoo seats onto ``day``'s saved schedule.

    Returns True only when the persisted assignments changed.
    """
    from . import staffing

    schedule = staffing.load_schedule(day)
    if locations is None:
        plan = plan_live_odoo_seats(
            schedule.assignments or {},
            schedule.assignment_sources or {},
            current_live_seats(),
        )
    else:
        plan = plan_live_odoo_assignments(
            schedule.assignments or {},
            schedule.assignment_sources or {},
            locations,
        )
    if not plan.changed:
        return False
    staffing.save_schedule(
        replace(
            schedule,
            assignments=plan.assignments,
            assignment_sources=plan.sources,
        )
    )
    _after_schedule_write()
    return True


def apply_after_attendance_sync(affected_days: frozenset[date] | None = None) -> bool:
    """Seat today's board when the attendance mirror just saw today change."""
    from . import plant_day

    today = plant_day.today()
    if affected_days is not None and today not in affected_days:
        return False
    try:
        return apply_live_odoo_assignments_for_day(today)
    except Exception:
        log.exception("Could not apply live Odoo assignments for %s", today)
        return False
