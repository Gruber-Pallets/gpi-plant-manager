"""Private, durable employee birthday and work-anniversary celebrations."""

from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Literal

from . import db
from .plant_day import today as plant_today


CelebrationKind = Literal["birthday", "work_anniversary"]

# Serializes the local celebration queue rebuild with the roster writer. This
# is intentionally transaction-scoped so an Odoo source snapshot and its
# dependent queue changes cannot interleave or deadlock over `people` rows.
CELEBRATION_SOURCE_SYNC_LOCK_KEY = 7_243_094_217


@dataclass(frozen=True)
class CelebrationEvent:
    """One future event to persist in the local employee queue."""

    person_odoo_id: int
    kind: CelebrationKind
    event_day: date
    completed_years: int | None


@dataclass(frozen=True)
class Celebration:
    """One due, employee-owned celebration read from the local queue."""

    id: int
    person_odoo_id: int
    kind: CelebrationKind
    event_day: date
    completed_years: int | None


def normalize_birthday(raw: object) -> tuple[int, int] | None:
    """Return an Odoo birthday's month/day without retaining its birth year."""
    try:
        value = date.fromisoformat(raw) if isinstance(raw, str) else None
    except ValueError:
        return None
    return (value.month, value.day) if value is not None else None


def normalize_first_contract_date(raw: object) -> date | None:
    """Parse a valid Odoo contract date without accepting malformed values."""
    try:
        return date.fromisoformat(raw) if isinstance(raw, str) else None
    except ValueError:
        return None


def event_day_for(year: int, month: int, day: int) -> date:
    """Return the observed event date, moving Feb. 29 to Feb. 28 when needed."""
    if month == 2 and day == 29 and not calendar.isleap(year):
        return date(year, 2, 28)
    return date(year, month, day)


def future_events_for_person(
    person_odoo_id: int,
    birthday: tuple[int, int] | None,
    first_contract_date: date | None,
    today: date,
    end_day: date,
) -> tuple[CelebrationEvent, ...]:
    """Return only this person's observed events inside the inclusive window."""
    events: list[CelebrationEvent] = []
    for year in range(today.year, end_day.year + 1):
        if birthday is not None:
            event_day = event_day_for(year, *birthday)
            if today <= event_day <= end_day:
                events.append(
                    CelebrationEvent(person_odoo_id, "birthday", event_day, None)
                )
        if first_contract_date is not None:
            event_day = event_day_for(
                year, first_contract_date.month, first_contract_date.day
            )
            completed_years = year - first_contract_date.year
            if completed_years > 0 and today <= event_day <= end_day:
                events.append(
                    CelebrationEvent(
                        person_odoo_id,
                        "work_anniversary",
                        event_day,
                        completed_years,
                    )
                )
    return tuple(sorted(events, key=lambda event: (event.event_day, event.kind)))


def _birthday_from_row(row: dict) -> tuple[int, int] | None:
    month = row.get("birthday_month")
    day = row.get("birthday_day")
    if not isinstance(month, int) or isinstance(month, bool):
        return None
    if not isinstance(day, int) or isinstance(day, bool):
        return None
    try:
        event_day_for(2024, month, day)
    except ValueError:
        return None
    return month, day


def _expected_days(events: tuple[CelebrationEvent, ...], kind: CelebrationKind) -> tuple[date, ...]:
    return tuple(event.event_day for event in events if event.kind == kind)


def lock_celebration_source_sync(cursor) -> None:
    """Serialize celebration source reads with the local roster writer."""
    cursor.execute(
        "SELECT pg_advisory_xact_lock(%s::bigint)",
        (CELEBRATION_SOURCE_SYNC_LOCK_KEY,),
    )


def _remove_unexpected_future_events(
    cursor,
    person_odoo_id: int,
    kind: CelebrationKind,
    expected_days: tuple[date, ...],
    today: date,
) -> None:
    if not expected_days:
        cursor.execute(
            "DELETE FROM employee_celebrations "
            "WHERE person_odoo_id = %s AND kind = %s AND event_day > %s "
            "AND acknowledged_at IS NULL",
            (person_odoo_id, kind, today),
        )
        return
    placeholders = ", ".join("%s" for _ in expected_days)
    cursor.execute(
        "DELETE FROM employee_celebrations "
        "WHERE person_odoo_id = %s AND kind = %s AND event_day > %s "
        "AND acknowledged_at IS NULL "
        f"AND event_day NOT IN ({placeholders})",
        (person_odoo_id, kind, today, *expected_days),
    )


def reconcile_future(today: date | None = None) -> None:
    """Refresh the future queue from source rows locked for this transaction."""
    today = today or plant_today()
    end_day = today + timedelta(days=370)
    with db.cursor() as cursor:
        lock_celebration_source_sync(cursor)
        # Lock every locally mirrored Odoo source row, not just the active
        # subset. A concurrent roster sync must either finish before this read
        # or wait until this queue rebuild commits, so stale future events
        # cannot be recreated from a superseded source snapshot.
        cursor.execute(
            "SELECT odoo_id, active, birthday_month, birthday_day, first_contract_date "
            "FROM people WHERE odoo_id IS NOT NULL FOR UPDATE"
        )
        people = [person for person in cursor.fetchall() if person["active"]]

        cursor.execute(
            "DELETE FROM employee_celebrations "
            "WHERE event_day > %s AND acknowledged_at IS NULL "
            "AND NOT EXISTS ("
            "SELECT 1 FROM people "
            "WHERE people.odoo_id = employee_celebrations.person_odoo_id "
            "AND people.active = TRUE)",
            (today,),
        )

        for person in people:
            person_odoo_id = person["odoo_id"]
            events = future_events_for_person(
                person_odoo_id,
                _birthday_from_row(person),
                person.get("first_contract_date"),
                today,
                end_day,
            )
            for kind in ("birthday", "work_anniversary"):
                _remove_unexpected_future_events(
                    cursor,
                    person_odoo_id,
                    kind,
                    _expected_days(events, kind),
                    today,
                )
            for event in events:
                cursor.execute(
                    "INSERT INTO employee_celebrations "
                    "(person_odoo_id, kind, event_day, completed_years) "
                    "VALUES (%s, %s, %s, %s) "
                    "ON CONFLICT (person_odoo_id, kind, event_day) DO UPDATE "
                    "SET completed_years = EXCLUDED.completed_years "
                    "WHERE employee_celebrations.kind = 'work_anniversary' "
                    "AND employee_celebrations.event_day > %s "
                    "AND employee_celebrations.acknowledged_at IS NULL",
                    (
                        event.person_odoo_id,
                        event.kind,
                        event.event_day,
                        event.completed_years,
                        today,
                    ),
                )


def _to_celebration(row: dict) -> Celebration:
    return Celebration(
        id=row["id"],
        person_odoo_id=row["person_odoo_id"],
        kind=row["kind"],
        event_day=row["event_day"],
        completed_years=row["completed_years"],
    )


def next_due(person_odoo_id: int, today: date | None = None) -> Celebration | None:
    """Return this signed-in employee's oldest unacknowledged due celebration."""
    rows = db.query(
        "SELECT id, person_odoo_id, kind, event_day, completed_years "
        "FROM employee_celebrations "
        "WHERE person_odoo_id = %s AND acknowledged_at IS NULL "
        "AND event_day <= %s ORDER BY event_day, id LIMIT 1",
        (person_odoo_id, today or plant_today()),
    )
    return _to_celebration(rows[0]) if rows else None


def acknowledge(celebration_id: int, person_odoo_id: int) -> bool:
    """Atomically acknowledge a due celebration only for its signed-in owner."""
    rows = db.query(
        "UPDATE employee_celebrations SET acknowledged_at = now() "
        "WHERE id = %s AND person_odoo_id = %s AND acknowledged_at IS NULL "
        "AND event_day <= %s "
        "RETURNING id",
        (celebration_id, person_odoo_id, plant_today()),
    )
    return bool(rows)
