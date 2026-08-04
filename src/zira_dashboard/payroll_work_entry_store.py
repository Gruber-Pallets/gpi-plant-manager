"""Database storage for payroll guard corrections and alert state."""

from __future__ import annotations

import math
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from datetime import datetime

from . import db
from .payroll_work_entry_rules import Decision


# Stable signed 64-bit namespace key for the singleton monitor lifecycle.
MONITOR_LOCK_KEY = 5_138_693_322_114_445_361
# Separate stable namespace key for the complete payroll correction run.
GUARD_LOCK_KEY = 2_274_731_015_993_244_807


@contextmanager
def monitor_lock() -> Iterator[None]:
    """Serialize one complete payroll-monitor lifecycle.

    Task 4 callers must keep this context open around the complete
    load-state/Odoo-act/save-state sequence. The transaction-scoped advisory
    lock is acquired before the caller runs and is released only when the
    caller leaves this context (including exceptional exits).
    """
    with db.cursor() as cur:
        cur.execute(
            "SELECT pg_advisory_xact_lock(%s::bigint)",
            (MONITOR_LOCK_KEY,),
        )
        yield


@contextmanager
def guard_lock() -> Iterator[None]:
    """Serialize one complete enabled payroll correction run."""
    with db.cursor() as cur:
        cur.execute(
            "SELECT pg_advisory_xact_lock(%s::bigint)",
            (GUARD_LOCK_KEY,),
        )
        yield


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

    numeric_totals = (
        decision.before_duration,
        decision.after_duration,
        decision.attendance_regular,
        decision.attendance_overtime,
        decision.work_regular,
        decision.work_overtime,
    )
    if not all(math.isfinite(value) for value in numeric_totals):
        raise ValueError("all correction totals must be finite")

    if decision.action == "delete_zero_regular":
        if decision.after_duration != 0.0:
            raise ValueError("delete correction after duration must be exactly 0.0")
    elif decision.action == "duration_update":
        if decision.after_duration <= 0.0:
            raise ValueError("duration update after duration must be greater than 0.0")
    else:
        raise ValueError("unsupported correction action")

    if not isinstance(verification_detail, str) or not verification_detail.strip():
        raise ValueError("verification detail must not be blank")
    if corrected_at.tzinfo is None or corrected_at.utcoffset() is None:
        raise ValueError("corrected_at must be timezone-aware")

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
