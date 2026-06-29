"""Odoo Time Off sync for manager-declared absences.

Attendance Absence Management creates technical attendance rows and negative
overtime. Real absences belong in Odoo Time Off as ``hr.leave`` records.
"""
from __future__ import annotations

import os
from datetime import date

from . import db, odoo_client


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


def mirror_approved_absence(
    *,
    employee_odoo_id: int,
    holiday_status_id: int,
    leave_id: int,
    day: date,
    employee_name: str,
    reason: str,
) -> None:
    """Mirror a manager-declared absence as approved local Time Off."""
    note = _absence_note(employee_name, reason)
    rows = db.query(
        "SELECT id, state, person_odoo_id, shape, holiday_status_id, "
        "date_from, date_to, hour_from, hour_to, working_hours_json, odoo_leave_id "
        "FROM time_off_requests "
        "WHERE odoo_leave_id = %s OR (person_odoo_id = %s "
        "AND holiday_status_id = %s AND date_from = %s AND date_to = %s "
        "AND state IN ('draft','draft_edit','confirm','validate1')) "
        "ORDER BY CASE WHEN odoo_leave_id = %s THEN 0 ELSE 1 END, id "
        "LIMIT 1",
        (leave_id, employee_odoo_id, holiday_status_id, day, day, leave_id),
    )
    new_row = {
        "state": "validate",
        "person_odoo_id": employee_odoo_id,
        "shape": "full_day",
        "holiday_status_id": holiday_status_id,
        "date_from": day,
        "date_to": day,
        "hour_from": None,
        "hour_to": None,
        "working_hours_json": None,
        "odoo_leave_id": leave_id,
    }
    if rows:
        old = rows[0]
        db.execute(
            "UPDATE time_off_requests SET originating_kiosk_user = FALSE, "
            "shape = 'full_day', holiday_status_id = %s, date_from = %s, date_to = %s, "
            "hour_from = NULL, hour_to = NULL, note = %s, state = 'validate', "
            "odoo_leave_id = %s, synced_to_odoo = TRUE, sync_error = NULL, "
            "last_pulled_at = now(), last_pushed_at = now(), updated_at = now() "
            "WHERE id = %s",
            (holiday_status_id, day, day, note, leave_id, old["id"]),
        )
        new_row["id"] = old["id"]
        from . import time_off_sync
        time_off_sync.cascade_on_state_change(old, new_row)
        return

    db.execute(
        "INSERT INTO time_off_requests "
        "(person_odoo_id, originating_kiosk_user, shape, holiday_status_id, "
        "date_from, date_to, hour_from, hour_to, note, state, odoo_leave_id, "
        "synced_to_odoo, last_pulled_at, last_pushed_at) "
        "VALUES (%s, FALSE, %s, %s, %s, %s, NULL, NULL, %s, %s, %s, TRUE, now(), now())",
        (employee_odoo_id, "full_day", holiday_status_id, day, day, note, "validate", leave_id),
    )
    from . import time_off_sync
    time_off_sync.cascade_on_state_change({"state": "draft"}, new_row)


def describe_sync_failure(exc: Exception) -> str:
    """One-line, human-readable reason the Odoo Time Off sync didn't happen.

    xmlrpc faults carry the useful text on ``.faultString`` and stringify as
    the noisy ``<Fault 2: '...'>`` repr; prefer the clean message. Collapse
    newlines so it fits on a single status line in the inbox.
    """
    msg = getattr(exc, "faultString", None) or str(exc)
    msg = " ".join(str(msg).split())
    return f"absence saved locally, but Odoo Time Off wasn't updated — {msg}"


def refuse_absence_leave(leave_id: int | None) -> None:
    if leave_id is None:
        return
    odoo_client.refuse_leave(int(leave_id))
