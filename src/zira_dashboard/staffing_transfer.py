"""Decide whether attributing production at a work center implies a department
transfer in Odoo, and apply it.

Called from the staffing attribute endpoints. One server-side chokepoint so
every assign path (footer modal + inline popovers) gets the same behavior.
"""

from __future__ import annotations

from datetime import datetime

from . import attendance_location_policy


def _live_location_active() -> bool:
    try:
        return attendance_location_policy.live_is_active()
    except Exception:  # Preserve the rollback behavior if settings are unreadable.
        return False


def _wc_department_label(wc_name: str) -> str | None:
    """The human department label for a WC (e.g. 'New'), from staffing
    LOCATIONS. None if the WC is unknown."""
    from . import staffing
    return staffing.department_for_wc(wc_name)


def _employee_id_for(person_name: str) -> int | None:
    """The Odoo employee id for a person on the live roster, or None (legacy person / unknown name)."""
    from . import staffing
    for p in staffing.load_roster():
        if p.name == person_name:
            return p.employee_id
    return None


def decide_and_apply(
    person_name: str, wc_name: str, window_start_utc: datetime
) -> dict:
    """Transfer ``person_name`` to ``wc_name``'s department in Odoo if needed.

    Returns a dict describing what happened, suitable for the UI toast:
      {"transfer": "skipped_no_employee", "person"}
      {"transfer": "already_in_dept", "person", "to_dept"}
      {"transfer": "moved", "person", "closed_id", "new_id", "from_dept", "to_dept"}
      {"transfer": "opened", "person", "new_id", "to_dept"}

    Decision rules:
      * No Odoo employee id -> skip (legacy person).
      * transfer_ts = max(window_start_utc, current check_in) so we never
        close a punch before it opened.
      * Open punch already in the WC's department -> no-op.
      * Open punch in a different (or unknown) department -> transfer at ts.
      * Unknown current dept or unknown WC dept (either id is None) -> treated as "differs", so the transfer/open proceeds (reversible via undo).
      * No open punch -> open a fresh punch at the WC's department.
    """
    if _live_location_active():
        return {
            "transfer": "blocked_live",
            "person": person_name,
            "error": "Use the plant-floor app to change live work areas.",
        }

    from . import odoo_client

    emp_id = _employee_id_for(person_name)
    to_dept = _wc_department_label(wc_name)
    if emp_id is None:
        return {"transfer": "skipped_no_employee", "person": person_name}

    wc_dept_id = odoo_client._department_id_for_wc(wc_name)
    current = odoo_client.get_current_attendance(emp_id)

    if current is None:
        new_id = odoo_client.clock_in(emp_id, wc_name, window_start_utc)
        return {"transfer": "opened", "person": person_name,
                "new_id": new_id, "to_dept": to_dept}

    check_in_iso = odoo_client._odoo_dt_to_iso(current.get("check_in"))
    check_in_dt = datetime.fromisoformat(check_in_iso) if check_in_iso else None
    transfer_ts = (
        max(window_start_utc, check_in_dt) if check_in_dt else window_start_utc
    )

    cur_dept_id = current.get("department_id")
    if (cur_dept_id is not None and wc_dept_id is not None
            and cur_dept_id == wc_dept_id):
        return {"transfer": "already_in_dept", "person": person_name,
                "to_dept": to_dept}

    closed_id, new_id = odoo_client.transfer(
        emp_id, wc_name, transfer_ts, current=current
    )
    return {"transfer": "moved", "person": person_name,
            "closed_id": closed_id, "new_id": new_id,
            "from_dept": current.get("department_name"), "to_dept": to_dept}
