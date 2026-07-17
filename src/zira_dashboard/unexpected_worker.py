"""Persistence and resolution checks for workers clocking in on approved leave."""
from __future__ import annotations

from datetime import date
from typing import Any

from . import db


def placement_resolves_unexpected_event(
    *,
    event_person_odoo_id: int,
    assigned_person_odoo_id: int,
    schedule_published: bool,
) -> bool:
    """Whether one schedule placement is sufficient to resolve an event.

    ``open_events`` applies this rule in one set-based database update so the
    inbox does not need a query and update for every event.
    """
    return schedule_published and event_person_odoo_id == assigned_person_odoo_id


def approved_full_day_leave(person_odoo_id: int, day: date) -> dict[str, Any] | None:
    """Return this person's approved full-day leave covering ``day``, if any."""
    rows = db.query(
        "SELECT id, odoo_leave_id, person_odoo_id, date_from, date_to "
        "FROM time_off_requests "
        "WHERE person_odoo_id = %s "
        "AND state = 'validate' "
        "AND shape = 'full_day' "
        "AND date_from <= %s AND date_to >= %s "
        "ORDER BY id DESC LIMIT 1",
        (person_odoo_id, day, day),
    )
    return rows[0] if rows else None


def record(
    *,
    day: date,
    person_odoo_id: int,
    leave: dict[str, Any],
    clock_in_wc: str,
) -> dict[str, Any]:
    """Create one unexpected-worker event, returning an existing event on retry."""
    params = (
        day,
        person_odoo_id,
        leave.get("id"),
        leave.get("odoo_leave_id"),
        clock_in_wc,
    )
    rows = db.query(
        "INSERT INTO unexpected_worker_events "
        "(day, person_odoo_id, time_off_request_id, odoo_leave_id, clock_in_wc) "
        "VALUES (%s, %s, %s, %s, %s) "
        "ON CONFLICT (day, person_odoo_id) DO NOTHING "
        "RETURNING id, day, person_odoo_id, time_off_request_id, odoo_leave_id, clock_in_wc",
        params,
    )
    if rows:
        return rows[0]
    rows = db.query(
        "SELECT id, day, person_odoo_id, time_off_request_id, odoo_leave_id, clock_in_wc "
        "FROM unexpected_worker_events WHERE day = %s AND person_odoo_id = %s",
        (day, person_odoo_id),
    )
    if not rows:
        raise RuntimeError("unexpected-worker event was not persisted")
    return rows[0]


def open_events(day: date) -> list[dict[str, Any]]:
    """Return unresolved events after clearing workers placed in a published schedule."""
    unresolved_events = db.query(
        "SELECT id, person_odoo_id FROM unexpected_worker_events "
        "WHERE day = %s AND resolved_at IS NULL",
        (day,),
    )
    published_placements = db.query(
        "SELECT DISTINCT pe.odoo_id AS person_odoo_id "
        "FROM schedules s "
        "JOIN schedule_assignments sa ON sa.day = s.day "
        "JOIN people pe ON pe.id = sa.person_id "
        "WHERE s.day = %s AND s.published = TRUE",
        (day,),
    )
    published_worker_ids = {
        placement["person_odoo_id"] for placement in published_placements
    }
    resolved_event_ids = [
        event["id"]
        for event in unresolved_events
        if any(
            placement_resolves_unexpected_event(
                event_person_odoo_id=event["person_odoo_id"],
                assigned_person_odoo_id=assigned_person_odoo_id,
                schedule_published=True,
            )
            for assigned_person_odoo_id in published_worker_ids
        )
    ]
    if resolved_event_ids:
        db.execute(
            "UPDATE unexpected_worker_events uwe SET resolved_at = now() "
            "WHERE uwe.id = ANY(%s) AND uwe.resolved_at IS NULL "
            "AND EXISTS ("
            "SELECT 1 FROM schedules s "
            "JOIN schedule_assignments sa ON sa.day = s.day "
            "JOIN people pe ON pe.id = sa.person_id "
            "WHERE s.day = uwe.day AND s.published = TRUE "
            "AND pe.odoo_id = uwe.person_odoo_id"
            ")",
            (resolved_event_ids,),
        )
    return db.query(
        "SELECT uwe.id, uwe.day, uwe.person_odoo_id, pe.name AS person_name, "
        "uwe.time_off_request_id, uwe.odoo_leave_id, uwe.clock_in_wc, uwe.confirmed_at "
        "FROM unexpected_worker_events uwe "
        "LEFT JOIN people pe ON pe.odoo_id = uwe.person_odoo_id "
        "WHERE uwe.day = %s AND uwe.resolved_at IS NULL "
        "ORDER BY uwe.confirmed_at, uwe.id",
        (day,),
    )
