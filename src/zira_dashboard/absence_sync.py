"""Odoo Time Off sync for manager-declared absences.

Attendance Absence Management creates technical attendance rows and negative
overtime. Real absences belong in Odoo Time Off as ``hr.leave`` records.
"""
from __future__ import annotations

import os
from datetime import date

from . import odoo_client


class AbsenceSyncError(RuntimeError):
    """Raised when an absence cannot be represented in Odoo Time Off."""


def _absence_type_name() -> str:
    return (os.environ.get("ODOO_ABSENCE_LEAVE_TYPE_NAME") or "Absence").strip() or "Absence"


def resolve_absence_leave_type_id() -> int:
    """Return the active Odoo Time Off type id named ``Absence``.

    The leave-type cache can be stale immediately after HR creates the type,
    so retry once after clearing the Odoo-client cache on a miss.
    """
    target = _absence_type_name()
    target_norm = target.casefold()
    for attempt in range(2):
        for row in odoo_client.fetch_leave_types():
            if str(row.get("name") or "").strip().casefold() == target_norm:
                return int(row["id"])
        if attempt == 0 and hasattr(odoo_client, "_leave_types_cache"):
            odoo_client._leave_types_cache = None
    raise AbsenceSyncError(
        f"Active Odoo Time Off type named {target!r} was not found."
    )


def _absence_note(employee_name: str, reason: str) -> str:
    clean_reason = reason.strip()
    if clean_reason:
        return f"Absent - {employee_name}: {clean_reason}"
    return f"Absent - {employee_name}"


def create_absence_for_day(
    *,
    employee_odoo_id: int,
    employee_name: str,
    day: date,
    reason: str,
) -> dict:
    """Create or adopt an approved Odoo ``hr.leave`` for one absence day."""
    holiday_status_id = resolve_absence_leave_type_id()
    existing = odoo_client.find_duplicate_leave(
        employee_odoo_id=employee_odoo_id,
        holiday_status_id=holiday_status_id,
        date_from=day,
        date_to=day,
    )
    if existing is not None:
        leave_id = existing
    else:
        leave_id = odoo_client.create_leave(
            employee_odoo_id=employee_odoo_id,
            holiday_status_id=holiday_status_id,
            date_from=day,
            date_to=day,
            hour_from=None,
            hour_to=None,
            note=_absence_note(employee_name, reason),
        )
    odoo_client.confirm_leave(leave_id)
    state = odoo_client.approve_leave(leave_id)
    if state != "validate":
        raise AbsenceSyncError(
            f"Odoo absence leave {leave_id} ended in state {state!r}, not 'validate'."
        )
    return {
        "holiday_status_id": holiday_status_id,
        "leave_id": int(leave_id),
        "state": state,
    }


def refuse_absence_leave(leave_id: int | None) -> None:
    if leave_id is None:
        return
    odoo_client.refuse_leave(int(leave_id))
