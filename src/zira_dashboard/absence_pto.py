"""Eligibility and local submission for PTO linked to recorded absences.

This module does not create, edit, approve, or refuse Odoo leave records.
Employee submissions remain local until the separate manager workflow acts.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Final

from psycopg2.errors import UniqueViolation

from . import absence_pto_store, db, staffing_hours, time_off_balances


_BLOCKING_STATES: Final = frozenset(
    {"pending", "converting", "approved", "needs_review", "resolved_manually"}
)
_ACTIVE_UNIQUE_INDEX: Final = "absence_pto_requests_active_uniq"
_DUPLICATE_MESSAGE: Final = "A PTO request already exists for this absence."
_TYPE_MESSAGE: Final = "Paid Time Off is not available right now."
_BALANCE_MESSAGE: Final = "Your PTO balance is not available right now."


class SubmissionError(ValueError):
    """The requested absence is not currently eligible for PTO."""


@dataclass(frozen=True)
class PtoType:
    holiday_status_id: int
    name: str


@dataclass(frozen=True)
class AbsenceCandidate:
    day: date
    eligible: bool
    blocked_reason: str | None
    available_practical: float | None


def resolve_paid_time_off_type() -> PtoType:
    """Return the one exact, active, allocated day-based PTO type."""
    rows = db.query(
        "SELECT holiday_status_id, name, request_unit, requires_allocation, active "
        "FROM leave_types_cache ORDER BY holiday_status_id"
    )
    matches = [
        row
        for row in rows
        if row.get("name") == "Paid Time Off"
        and row.get("request_unit") in {"day", "half_day"}
        and row.get("requires_allocation") == "yes"
        and row.get("active") is True
    ]
    if len(matches) != 1:
        raise SubmissionError(_TYPE_MESSAGE)
    return PtoType(
        holiday_status_id=int(matches[0]["holiday_status_id"]),
        name="Paid Time Off",
    )


def _blocking_days(person_odoo_id: int) -> set[date]:
    return {
        request.absence_day
        for request in absence_pto_store.list_for_person(str(person_odoo_id))
        if request.state in _BLOCKING_STATES
    }


def _refresh_and_read_balance(person_odoo_id: int, type_id: int) -> float | None:
    # Refresh failures must not erase a usable cached balance. The balance
    # module already swallows Odoo failures, and this guard preserves that
    # fallback contract if a test double or future implementation raises.
    try:
        time_off_balances.refresh_for_employee(person_odoo_id)
    except Exception:  # noqa: BLE001 - cached balance remains authoritative fallback
        pass
    rows = time_off_balances.get_for_employee(person_odoo_id)
    matches = [row for row in rows if row.get("holiday_status_id") == type_id]
    if len(matches) != 1 or matches[0].get("available_practical") is None:
        return None
    return float(matches[0]["available_practical"])


def _low_balance_message(balance: float) -> str:
    return f"You need 1 PTO day. You have {balance:g}."


def list_candidates(person_odoo_id: int, today: date) -> list[AbsenceCandidate]:
    """List only this employee's past recorded absences in the current period."""
    start, _end = staffing_hours.current_pay_period_bounds(today)
    rows = db.query(
        "SELECT day, odoo_leave_id FROM manual_absences "
        "WHERE emp_id = %s AND day >= %s AND day < %s ORDER BY day DESC",
        (str(person_odoo_id), start, today),
    )
    if not rows:
        return []

    try:
        pto_type = resolve_paid_time_off_type()
    except SubmissionError as error:
        return [
            AbsenceCandidate(row["day"], False, str(error), None) for row in rows
        ]

    blocked_days = _blocking_days(person_odoo_id)
    balance = _refresh_and_read_balance(person_odoo_id, pto_type.holiday_status_id)
    candidates = []
    for row in rows:
        day = row["day"]
        if day in blocked_days:
            candidates.append(AbsenceCandidate(day, False, _DUPLICATE_MESSAGE, balance))
        elif balance is None:
            candidates.append(AbsenceCandidate(day, False, _BALANCE_MESSAGE, None))
        elif balance < 1.0:
            candidates.append(
                AbsenceCandidate(day, False, _low_balance_message(balance), balance)
            )
        else:
            candidates.append(AbsenceCandidate(day, True, None, balance))
    return candidates


def _validate_submission(
    person_odoo_id: int, day: date, today: date
) -> tuple[PtoType, float, int | None]:
    start, end = staffing_hours.current_pay_period_bounds(today)
    if day >= today:
        raise SubmissionError("Choose an absence before today.")
    if not start <= day <= end:
        raise SubmissionError("That absence is not in the current pay period.")

    absence_rows = db.query(
        "SELECT day, odoo_leave_id FROM manual_absences WHERE day = %s AND emp_id = %s",
        (day, str(person_odoo_id)),
    )
    if len(absence_rows) != 1:
        raise SubmissionError("That absence was not found for this employee.")

    pto_type = resolve_paid_time_off_type()
    if day in _blocking_days(person_odoo_id):
        raise SubmissionError(_DUPLICATE_MESSAGE)

    balance = _refresh_and_read_balance(person_odoo_id, pto_type.holiday_status_id)
    if balance is None:
        raise SubmissionError(_BALANCE_MESSAGE)
    if balance < 1.0:
        raise SubmissionError(_low_balance_message(balance))
    return pto_type, balance, absence_rows[0].get("odoo_leave_id")


def submit(
    person_id: int,
    person_odoo_id: int,
    person_name: str,
    day: date,
    note: str,
    today: date,
) -> absence_pto_store.AbsencePtoRequest:
    """Revalidate and create one local pending request without touching Odoo."""
    pto_type, balance, original_leave_id = _validate_submission(
        person_odoo_id, day, today
    )
    cleaned_note = note.strip()
    try:
        return absence_pto_store.create_request(
            absence_day=day,
            emp_id=str(person_odoo_id),
            person_odoo_id=person_odoo_id,
            person_name=person_name,
            holiday_status_id=pto_type.holiday_status_id,
            leave_type_name=pto_type.name,
            balance_at_submit=Decimal(str(balance)),
            original_absence_leave_id=original_leave_id,
            employee_note=cleaned_note or None,
            requested_by_person_id=person_id,
        )
    except UniqueViolation as error:
        constraint_name = getattr(getattr(error, "diag", None), "constraint_name", None)
        if constraint_name == _ACTIVE_UNIQUE_INDEX:
            raise SubmissionError(_DUPLICATE_MESSAGE) from error
        raise


def employee_requests(
    person_odoo_id: int,
) -> list[absence_pto_store.AbsencePtoRequest]:
    """Return linked request history for the authenticated Odoo employee."""
    return absence_pto_store.list_for_person(str(person_odoo_id))
