"""Database storage for payroll guard corrections and alert state."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime

from . import db
from .payroll_work_entry_rules import Decision


def append_correction(
    decision: Decision, verification_detail: str, corrected_at: datetime
) -> None:
    """Append one verified payroll correction to the permanent audit history."""
    if decision.kind != "correct":
        raise ValueError("decision must be a correction")
    if decision.action is None:
        raise ValueError("correction action is required")
    if decision.work_entry_id is None:
        raise ValueError("Work Entry id is required")
    if decision.after_duration is None:
        raise ValueError("after duration is required")

    db.execute(
        "INSERT INTO payroll_work_entry_corrections "
        "(odoo_work_entry_id, action, employee_odoo_id, employee_name, work_date, "
        "before_duration, after_duration, attendance_regular, attendance_overtime, "
        "work_regular_before, work_overtime, verification_detail, corrected_at) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (
            decision.work_entry_id,
            decision.action,
            decision.employee_id,
            decision.employee_name,
            decision.work_date,
            decision.before_duration,
            decision.after_duration,
            decision.attendance_regular,
            decision.attendance_overtime,
            decision.work_regular,
            decision.work_overtime,
            verification_detail,
            corrected_at,
        ),
    )


def load_monitor_state() -> dict:
    """Load the singleton alert state, returning empty defaults if absent."""
    rows = db.query(
        "SELECT odoo_task_id, reported_issue_keys "
        "FROM payroll_work_entry_guard_monitor WHERE id = 1"
    )
    if not rows:
        return {"odoo_task_id": None, "reported_issue_keys": []}
    row = rows[0]
    return {
        "odoo_task_id": row["odoo_task_id"],
        "reported_issue_keys": list(row["reported_issue_keys"] or []),
    }


def save_monitor_state(
    odoo_task_id: int | None,
    reported_issue_keys: Iterable[str],
    updated_at: datetime,
) -> None:
    """Upsert the singleton alert state with stable, unique issue keys."""
    db.execute(
        "INSERT INTO payroll_work_entry_guard_monitor "
        "(id, odoo_task_id, reported_issue_keys, updated_at) VALUES (1, %s, %s, %s) "
        "ON CONFLICT (id) DO UPDATE SET "
        "odoo_task_id = EXCLUDED.odoo_task_id, "
        "reported_issue_keys = EXCLUDED.reported_issue_keys, "
        "updated_at = EXCLUDED.updated_at",
        (odoo_task_id, sorted(set(reported_issue_keys)), updated_at),
    )
