"""Database storage for payroll guard corrections and alert state."""

from __future__ import annotations

import math
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime
from uuid import UUID

from . import db
from .payroll_work_entry_rules import Decision


# Stable signed 64-bit namespace key for the singleton monitor lifecycle.
MONITOR_LOCK_KEY = 5_138_693_322_114_445_361
# Separate stable namespace key for the complete payroll correction run.
GUARD_LOCK_KEY = 2_274_731_015_993_244_807

_ATTEMPT_COLUMNS = (
    "attempt_id, odoo_work_entry_id, action, employee_odoo_id, employee_name, "
    "work_date, attendance_id, before_duration, after_duration, "
    "attendance_regular, attendance_overtime, work_regular_before, "
    "work_overtime, last_reason, last_detail, created_at, updated_at"
)


@dataclass(frozen=True)
class CorrectionAttempt:
    attempt_id: UUID
    decision: Decision
    last_reason: str
    last_detail: str
    created_at: datetime
    updated_at: datetime


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


def _validate_aware(value: datetime, field: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")


def _validate_decision(decision: Decision, *, require_attendance: bool) -> None:
    if decision.kind != "correct":
        raise ValueError("decision must be a correction")
    if decision.reason_codes:
        raise ValueError("correction decision must not contain review reasons")
    if not isinstance(decision.employee_id, int) or decision.employee_id <= 0:
        raise ValueError("employee id is required")
    if not isinstance(decision.employee_name, str) or not decision.employee_name.strip():
        raise ValueError("employee name is required")
    if not isinstance(decision.work_date, date):
        raise ValueError("work date is required")
    if not isinstance(decision.work_entry_id, int) or decision.work_entry_id <= 0:
        raise ValueError("Work Entry id is required")
    if require_attendance and (
        not isinstance(decision.attendance_id, int) or decision.attendance_id <= 0
    ):
        raise ValueError("Attendance id is required")
    if decision.action is None:
        raise ValueError("correction action is required")
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
    if not all(
        isinstance(value, (int, float)) and math.isfinite(value)
        for value in numeric_totals
    ):
        raise ValueError("all correction totals must be finite")

    if decision.action == "delete_zero_regular":
        if decision.after_duration != 0.0:
            raise ValueError("delete correction after duration must be exactly 0.0")
    elif decision.action == "duration_update":
        if decision.after_duration <= 0.0:
            raise ValueError("duration update after duration must be greater than 0.0")
    else:
        raise ValueError("unsupported correction action")


def _validate_attempt_id(attempt_id: UUID) -> None:
    if not isinstance(attempt_id, UUID):
        raise ValueError("attempt_id must be a UUID")


def _attempt_id_from_db(value: object) -> UUID:
    if isinstance(value, UUID):
        return value
    if isinstance(value, str):
        try:
            return UUID(value)
        except ValueError as error:
            raise ValueError("stored attempt_id must be a UUID") from error
    raise ValueError("stored attempt_id must be a UUID")


def _decision_from_attempt_row(row: dict) -> Decision:
    decision = Decision(
        kind="correct",
        employee_id=row["employee_odoo_id"],
        employee_name=row["employee_name"],
        work_date=row["work_date"],
        reason_codes=(),
        action=row["action"],
        work_entry_id=row["odoo_work_entry_id"],
        attendance_id=row["attendance_id"],
        before_duration=row["before_duration"],
        after_duration=row["after_duration"],
        attendance_regular=row["attendance_regular"],
        attendance_overtime=row["attendance_overtime"],
        work_regular=row["work_regular_before"],
        work_overtime=row["work_overtime"],
    )
    _validate_decision(decision, require_attendance=True)
    return decision


def _attempt_from_row(row: dict) -> CorrectionAttempt:
    attempt_id = _attempt_id_from_db(row["attempt_id"])
    last_reason = row["last_reason"]
    last_detail = row["last_detail"]
    if not isinstance(last_reason, str) or not last_reason.strip():
        raise ValueError("attempt reason must not be blank")
    if not isinstance(last_detail, str) or not last_detail.strip():
        raise ValueError("attempt detail must not be blank")
    _validate_aware(row["created_at"], "created_at")
    _validate_aware(row["updated_at"], "updated_at")
    return CorrectionAttempt(
        attempt_id=attempt_id,
        decision=_decision_from_attempt_row(row),
        last_reason=last_reason,
        last_detail=last_detail,
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _attempt_params(
    attempt_id: UUID, decision: Decision, now: datetime
) -> tuple[object, ...]:
    return (
        str(attempt_id),
        decision.work_entry_id,
        decision.action,
        decision.employee_id,
        decision.employee_name,
        decision.work_date,
        decision.attendance_id,
        decision.before_duration,
        decision.after_duration,
        decision.attendance_regular,
        decision.attendance_overtime,
        decision.work_regular,
        decision.work_overtime,
        "pending_correction",
        "correction intent saved",
        now,
        now,
    )


def create_attempt(
    attempt_id: UUID, decision: Decision, now: datetime
) -> CorrectionAttempt:
    """Persist one exact correction intent before any Odoo mutation."""
    _validate_attempt_id(attempt_id)
    _validate_decision(decision, require_attendance=True)
    _validate_aware(now, "created_at")
    attempt_key = str(attempt_id)
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO payroll_work_entry_correction_attempts "
            f"({_ATTEMPT_COLUMNS}) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, "
            "%s, %s, %s, %s, %s) "
            "ON CONFLICT (attempt_id) DO NOTHING",
            _attempt_params(attempt_id, decision, now),
        )
        cur.execute(
            f"SELECT {_ATTEMPT_COLUMNS} "
            "FROM payroll_work_entry_correction_attempts WHERE attempt_id = %s",
            (attempt_key,),
        )
        row = cur.fetchone()
        if row is None:
            raise RuntimeError("correction attempt was not durably persisted")
        attempt = _attempt_from_row(row)
        if attempt.decision != decision:
            raise RuntimeError("correction attempt snapshot mismatch")
        return attempt


def load_pending_attempts() -> list[CorrectionAttempt]:
    """Load and validate every durable correction intent."""
    rows = db.query(
        f"SELECT {_ATTEMPT_COLUMNS} "
        "FROM payroll_work_entry_correction_attempts ORDER BY created_at, attempt_id"
    )
    return [_attempt_from_row(row) for row in rows]


def mark_attempt_issue(
    attempt_id: UUID,
    reason: str,
    detail: str,
    updated_at: datetime,
) -> None:
    """Persist the latest unresolved status without changing its snapshot."""
    _validate_attempt_id(attempt_id)
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError("attempt reason must not be blank")
    if not isinstance(detail, str) or not detail.strip():
        raise ValueError("attempt detail must not be blank")
    _validate_aware(updated_at, "updated_at")
    db.execute(
        "UPDATE payroll_work_entry_correction_attempts "
        "SET last_reason = %s, last_detail = %s, updated_at = %s "
        "WHERE attempt_id = %s",
        (reason, detail, updated_at, str(attempt_id)),
    )


def finalize_attempt(
    attempt_id: UUID, verification_detail: str, corrected_at: datetime
) -> bool:
    """Atomically append exactly one audit and remove its pending intent."""
    _validate_attempt_id(attempt_id)
    if not isinstance(verification_detail, str) or not verification_detail.strip():
        raise ValueError("verification detail must not be blank")
    _validate_aware(corrected_at, "corrected_at")
    attempt_key = str(attempt_id)

    with db.cursor() as cur:
        cur.execute(
            f"SELECT {_ATTEMPT_COLUMNS} "
            "FROM payroll_work_entry_correction_attempts "
            "WHERE attempt_id = %s FOR UPDATE",
            (attempt_key,),
        )
        row = cur.fetchone()
        if row is None:
            return False
        attempt = _attempt_from_row(row)
        decision = attempt.decision
        cur.execute(
            "INSERT INTO payroll_work_entry_corrections "
            "(attempt_id, odoo_work_entry_id, action, employee_odoo_id, "
            "employee_name, work_date, before_duration, after_duration, "
            "attendance_regular, attendance_overtime, work_regular_before, "
            "work_overtime, verification_detail, corrected_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
            "ON CONFLICT (attempt_id) WHERE attempt_id IS NOT NULL DO NOTHING",
            (
                attempt_key,
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
        cur.execute(
            "DELETE FROM payroll_work_entry_correction_attempts "
            "WHERE attempt_id = %s",
            (attempt_key,),
        )
        return True


def append_correction(
    decision: Decision, verification_detail: str, corrected_at: datetime
) -> None:
    """Append one verified payroll correction to the permanent audit history."""
    _validate_decision(decision, require_attendance=False)

    if not isinstance(verification_detail, str) or not verification_detail.strip():
        raise ValueError("verification detail must not be blank")
    _validate_aware(corrected_at, "corrected_at")

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
