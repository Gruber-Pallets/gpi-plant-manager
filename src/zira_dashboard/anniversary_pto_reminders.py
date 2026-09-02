"""Generate durable kiosk reminders for upcoming anniversaries with unused PTO."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from . import db, employee_celebrations, employee_notifications, time_off_balances
from .plant_day import today as plant_today

WINDOW_DAYS = 30


def upcoming_anniversary(first_contract_date: date, today: date) -> date | None:
    """Return the next observed anniversary when it falls in the open window."""
    end_day = today + timedelta(days=WINDOW_DAYS)
    for year in (today.year, today.year + 1):
        event = employee_celebrations.event_day_for(
            year,
            first_contract_date.month,
            first_contract_date.day,
        )
        if year - first_contract_date.year > 0 and today <= event <= end_day:
            return event
    return None


def _paid_time_off_type_id() -> int | None:
    rows = db.query(
        "SELECT holiday_status_id, name, request_unit, requires_allocation, active "
        "FROM leave_types_cache ORDER BY holiday_status_id"
    )
    matches = [
        row
        for row in rows
        if row.get("name") == "Paid Time Off"
        and row.get("request_unit") in {"day", "half_day", "hour"}
        and row.get("requires_allocation") == "yes"
        and row.get("active") is True
    ]
    return int(matches[0]["holiday_status_id"]) if len(matches) == 1 else None


def run(today: date | None = None) -> int:
    """Refresh fresh PTO and reconcile every currently expected reminder."""
    if not employee_notifications.notifications_enabled():
        return 0
    today = today or plant_today()
    people = db.query(
        "SELECT odoo_id, first_contract_date FROM people "
        "WHERE active = TRUE AND excluded = FALSE AND odoo_id IS NOT NULL "
        "AND first_contract_date IS NOT NULL ORDER BY odoo_id"
    )
    candidates = [
        (int(row["odoo_id"]), anniversary)
        for row in people
        if isinstance(row.get("first_contract_date"), date)
        if (
            anniversary := upcoming_anniversary(row["first_contract_date"], today)
        )
        is not None
    ]
    type_id = _paid_time_off_type_id()
    if type_id is None:
        return 0
    fresh = time_off_balances.refresh_for_employees(
        [person_id for person_id, _ in candidates]
    )
    if fresh is None:
        return 0

    notices: list[employee_notifications.AnniversaryPtoNotice] = []
    for person_id, anniversary in candidates:
        matches = [
            row
            for row in fresh.get(person_id, [])
            if row.get("holiday_status_id") == type_id
        ]
        if len(matches) != 1:
            continue
        balance = Decimal(str(matches[0].get("available_practical", 0)))
        unit = matches[0].get("unit")
        if balance > 0 and unit in {"days", "hours"}:
            notices.append(
                employee_notifications.AnniversaryPtoNotice(
                    person_id,
                    anniversary,
                    balance,
                    unit,
                )
            )
    employee_notifications.reconcile_anniversary_pto(tuple(notices))
    return len(notices)
