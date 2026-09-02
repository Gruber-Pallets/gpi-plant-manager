from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta

from . import (
    forklift_event_store,
    forklift_identity_store,
    forklift_store,
    shift_config,
    staffing,
)


def _time_label(value: datetime) -> str:
    return value.astimezone(shift_config.SITE_TZ).strftime("%-I:%M %p")


def _changed_label(value: datetime) -> str:
    return value.astimezone(shift_config.SITE_TZ).strftime("%b %-d, %-I:%M %p")


def identity_context(day: date) -> dict:
    if type(day) is not date:
        raise TypeError("day must be a date")
    start = datetime.combine(day, time.min, tzinfo=shift_config.SITE_TZ).astimezone(UTC)
    end = datetime.combine(
        day + timedelta(days=1), time.min, tzinfo=shift_config.SITE_TZ
    ).astimezone(UTC)
    events = forklift_event_store.completion_events_for_range(start, end)
    people = tuple(
        person
        for person in staffing.load_roster()
        if person.active and person.employee_id is not None
    )
    employee_rows = tuple(
        {"employee_odoo_id": int(person.employee_id), "employee_name": person.name}
        for person in sorted(people, key=lambda item: item.name.casefold())
    )
    events_by_driver: dict[str, list] = {}
    names_by_driver: dict[str, list[str]] = {}
    for event in events:
        events_by_driver.setdefault(event.driver_id, []).append(event)
        names = names_by_driver.setdefault(event.driver_id, [])
        if event.driver_name and event.driver_name not in names:
            names.append(event.driver_name)
    evidence = {
        driver_id: set(names) for driver_id, names in names_by_driver.items()
    }
    resolved = forklift_store.resolve_forklift_driver_ids(
        evidence,
        allowed_employee_ids={int(person.employee_id) for person in people},
    )
    unresolved_rows = tuple(
        {
            "external_driver_id": driver_id,
            "source_names": tuple(names_by_driver.get(driver_id, ())),
            "call_count": len(driver_events),
            "first_call": _time_label(
                min(item.created_at_utc for item in driver_events)
            ),
            "last_call": _time_label(
                max(item.created_at_utc for item in driver_events)
            ),
            "name_conflict": len(names_by_driver.get(driver_id, ())) != 1,
            "version": None,
        }
        for driver_id, driver_events in sorted(events_by_driver.items())
        if driver_id not in resolved
    )
    mapping_rows = tuple(
        {
            "external_driver_id": item.external_driver_id,
            "source_name": item.source_name,
            "employee_odoo_id": item.employee_odoo_id,
            "employee_name": item.employee_name,
            "version": item.version,
            "updated_at": _changed_label(item.updated_at),
            "updated_by_upn": item.updated_by_upn,
        }
        for item in forklift_identity_store.list_mappings()
    )
    return {
        "day": day.isoformat(),
        "mappings": mapping_rows,
        "unresolved": unresolved_rows,
        "employee_options": employee_rows,
    }
