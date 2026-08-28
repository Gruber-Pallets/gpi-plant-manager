"""Durable local storage for retroactive absence-PTO requests.

This module owns only Postgres persistence and compare-and-set leases.  It is
deliberately detached from Odoo so callers never hold one of these short
database transactions open around a remote call.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Final
from uuid import UUID

from . import db, inbox_log, time_off_audit


STATES: Final = frozenset(
    {"pending", "converting", "approved", "denied", "needs_review", "resolved_manually"}
)
CONVERSION_STEPS: Final = frozenset(
    {"not_started", "absence_refused", "pto_created", "pto_approved"}
)
RESOLUTION_STEPS: Final = frozenset({"none", "message_posted", "closed"})

REQUEST_COLUMNS = (
    "id, absence_day, emp_id, person_odoo_id, person_name, holiday_status_id, "
    "leave_type_name, balance_at_submit, original_absence_leave_id, pto_leave_id, "
    "state, conversion_step, employee_note, denial_reason, manual_resolution_note, "
    "sync_error, odoo_task_id, task_attempts, task_next_at, lease_owner, lease_until, "
    "requested_by_person_id, decided_by_upn, decided_by_name, requested_at, "
    "decided_at, resolved_at, created_at, updated_at, task_resolution_step, "
    "task_resolution_attempts, task_resolution_next_at, task_resolution_error"
)
QUALIFIED_REQUEST_COLUMNS = ", ".join(
    f"request.{column.strip()} AS {column.strip()}"
    for column in REQUEST_COLUMNS.split(",")
)


class StaleTransition(RuntimeError):
    """The durable row no longer matches the caller's lease or expected state."""


@dataclass(frozen=True)
class AbsencePtoRequest:
    id: int
    absence_day: date
    emp_id: str
    person_odoo_id: int
    person_name: str
    holiday_status_id: int
    leave_type_name: str
    balance_at_submit: Decimal
    original_absence_leave_id: int | None
    pto_leave_id: int | None
    state: str
    conversion_step: str
    employee_note: str | None
    denial_reason: str | None
    manual_resolution_note: str | None
    sync_error: str | None
    odoo_task_id: int | None
    task_attempts: int
    task_next_at: datetime | None
    lease_owner: UUID | None
    lease_until: datetime | None
    requested_by_person_id: int | None
    decided_by_upn: str | None
    decided_by_name: str | None
    requested_at: datetime
    decided_at: datetime | None
    resolved_at: datetime | None
    created_at: datetime
    updated_at: datetime
    task_resolution_step: str = "none"
    task_resolution_attempts: int = 0
    task_resolution_next_at: datetime | None = None
    task_resolution_error: str | None = None


@dataclass(frozen=True)
class PersonRequestCounts:
    total: int
    unresolved: int
    actionable: int


def _positive_int(value: object, field: str) -> int:
    if type(value) is not int or value <= 0:
        raise ValueError(f"{field} must be a positive integer")
    return value


def _optional_positive_int(value: object, field: str) -> int | None:
    if value is None:
        return None
    return _positive_int(value, field)


def _nonnegative_int(value: object, field: str) -> int:
    if type(value) is not int or value < 0:
        raise ValueError(f"{field} must be a nonnegative integer")
    return value


def _required_text(value: object, field: str) -> str:
    if type(value) is not str or not value.strip():
        raise ValueError(f"{field} must not be blank")
    return value


def _optional_text(value: object, field: str) -> str | None:
    if value is None:
        return None
    if type(value) is not str:
        raise ValueError(f"{field} must be text")
    return value


def _aware_datetime(value: object, field: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")
    return value


def _optional_aware_datetime(value: object, field: str) -> datetime | None:
    if value is None:
        return None
    return _aware_datetime(value, field)


def _now(value: datetime | None) -> datetime:
    return datetime.now(UTC) if value is None else _aware_datetime(value, "now")


def _uuid(value: object, field: str) -> UUID:
    if isinstance(value, UUID):
        return value
    if isinstance(value, str):
        try:
            return UUID(value)
        except ValueError:
            pass
    raise ValueError(f"{field} must be a UUID")


def _optional_uuid(value: object, field: str) -> UUID | None:
    if value is None:
        return None
    return _uuid(value, field)


def _decimal(value: object, field: str) -> Decimal:
    if isinstance(value, bool):
        raise ValueError(f"{field} must be a nonnegative number")
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise ValueError(f"{field} must be a nonnegative number") from None
    if not result.is_finite() or result < 0:
        raise ValueError(f"{field} must be a nonnegative number")
    return result


def _state(value: object, field: str = "state") -> str:
    if type(value) is not str or value not in STATES:
        raise ValueError(f"{field} is not supported")
    return value


def _step(value: object, field: str = "conversion_step") -> str:
    if type(value) is not str or value not in CONVERSION_STEPS:
        raise ValueError(f"{field} is not supported")
    return value


def _resolution_step(value: object, field: str = "task_resolution_step") -> str:
    if type(value) is not str or value not in RESOLUTION_STEPS:
        raise ValueError(f"{field} is not supported")
    return value


def _request_from_row(row: Mapping[str, object]) -> AbsencePtoRequest:
    """Validate one database row before exposing it to domain code."""
    if not isinstance(row, Mapping):
        raise ValueError("absence PTO request row must be a mapping")
    absence_day = row.get("absence_day")
    if not isinstance(absence_day, date) or isinstance(absence_day, datetime):
        raise ValueError("absence_day must be a date")
    lease_owner = _optional_uuid(row.get("lease_owner"), "lease_owner")
    lease_until = _optional_aware_datetime(row.get("lease_until"), "lease_until")
    if (lease_owner is None) != (lease_until is None):
        raise ValueError("lease_owner and lease_until must be set together")
    return AbsencePtoRequest(
        id=_positive_int(row.get("id"), "id"),
        absence_day=absence_day,
        emp_id=_required_text(row.get("emp_id"), "emp_id"),
        person_odoo_id=_positive_int(row.get("person_odoo_id"), "person_odoo_id"),
        person_name=_required_text(row.get("person_name"), "person_name"),
        holiday_status_id=_positive_int(
            row.get("holiday_status_id"), "holiday_status_id"
        ),
        leave_type_name=_required_text(row.get("leave_type_name"), "leave_type_name"),
        balance_at_submit=_decimal(row.get("balance_at_submit"), "balance_at_submit"),
        original_absence_leave_id=_optional_positive_int(
            row.get("original_absence_leave_id"), "original_absence_leave_id"
        ),
        pto_leave_id=_optional_positive_int(row.get("pto_leave_id"), "pto_leave_id"),
        state=_state(row.get("state")),
        conversion_step=_step(row.get("conversion_step")),
        employee_note=_optional_text(row.get("employee_note"), "employee_note"),
        denial_reason=_optional_text(row.get("denial_reason"), "denial_reason"),
        manual_resolution_note=_optional_text(
            row.get("manual_resolution_note"), "manual_resolution_note"
        ),
        sync_error=_optional_text(row.get("sync_error"), "sync_error"),
        odoo_task_id=_optional_positive_int(row.get("odoo_task_id"), "odoo_task_id"),
        task_attempts=_nonnegative_int(row.get("task_attempts"), "task_attempts"),
        task_next_at=_optional_aware_datetime(row.get("task_next_at"), "task_next_at"),
        lease_owner=lease_owner,
        lease_until=lease_until,
        requested_by_person_id=_optional_positive_int(
            row.get("requested_by_person_id"), "requested_by_person_id"
        ),
        decided_by_upn=_optional_text(row.get("decided_by_upn"), "decided_by_upn"),
        decided_by_name=_optional_text(row.get("decided_by_name"), "decided_by_name"),
        requested_at=_aware_datetime(row.get("requested_at"), "requested_at"),
        decided_at=_optional_aware_datetime(row.get("decided_at"), "decided_at"),
        resolved_at=_optional_aware_datetime(row.get("resolved_at"), "resolved_at"),
        created_at=_aware_datetime(row.get("created_at"), "created_at"),
        updated_at=_aware_datetime(row.get("updated_at"), "updated_at"),
        task_resolution_step=_resolution_step(
            row.get("task_resolution_step", "none")
        ),
        task_resolution_attempts=_nonnegative_int(
            row.get("task_resolution_attempts", 0), "task_resolution_attempts"
        ),
        task_resolution_next_at=_optional_aware_datetime(
            row.get("task_resolution_next_at"), "task_resolution_next_at"
        ),
        task_resolution_error=_optional_text(
            row.get("task_resolution_error"), "task_resolution_error"
        ),
    )


def _one_request(rows: list[dict], operation: str) -> AbsencePtoRequest:
    if len(rows) != 1:
        raise RuntimeError(f"{operation} did not return exactly one request")
    return _request_from_row(rows[0])


def _optional_request(rows: list[dict]) -> AbsencePtoRequest | None:
    if not rows:
        return None
    return _one_request(rows, "request lookup")


def _lease_inputs(
    request_id: int,
    owner: UUID,
    now: datetime,
    lease_seconds: int,
) -> tuple[int, UUID, datetime, datetime]:
    safe_id = _positive_int(request_id, "request_id")
    safe_owner = _uuid(owner, "owner")
    current = _aware_datetime(now, "now")
    if type(lease_seconds) is not int or lease_seconds <= 0:
        raise ValueError("lease_seconds must be a positive integer")
    return safe_id, safe_owner, current, current + timedelta(seconds=lease_seconds)


def create_request(
    *,
    absence_day: date,
    emp_id: str,
    person_odoo_id: int,
    person_name: str,
    holiday_status_id: int,
    leave_type_name: str,
    balance_at_submit: Decimal,
    original_absence_leave_id: int | None = None,
    employee_note: str | None = None,
    requested_by_person_id: int | None = None,
    now: datetime | None = None,
) -> AbsencePtoRequest:
    """Insert an immutable submission snapshot and return its durable row."""
    if not isinstance(absence_day, date) or isinstance(absence_day, datetime):
        raise ValueError("absence_day must be a date")
    current = _now(now)
    params = (
        absence_day,
        _required_text(emp_id, "emp_id"),
        _positive_int(person_odoo_id, "person_odoo_id"),
        _required_text(person_name, "person_name"),
        _positive_int(holiday_status_id, "holiday_status_id"),
        _required_text(leave_type_name, "leave_type_name"),
        _decimal(balance_at_submit, "balance_at_submit"),
        _optional_positive_int(
            original_absence_leave_id, "original_absence_leave_id"
        ),
        _optional_text(employee_note, "employee_note"),
        _optional_positive_int(requested_by_person_id, "requested_by_person_id"),
        current,
        current,
        current,
    )
    rows = db.query(
        "INSERT INTO absence_pto_requests ("
        "absence_day, emp_id, person_odoo_id, person_name, holiday_status_id, "
        "leave_type_name, balance_at_submit, original_absence_leave_id, employee_note, "
        "requested_by_person_id, requested_at, created_at, updated_at"
        ") VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
        f"RETURNING {REQUEST_COLUMNS}",
        params,
    )
    return _one_request(rows, "request creation")


def get_request(request_id: int) -> AbsencePtoRequest | None:
    rows = db.query(
        f"SELECT {REQUEST_COLUMNS} FROM absence_pto_requests WHERE id = %s",
        (_positive_int(request_id, "request_id"),),
    )
    return _optional_request(rows)


def list_for_person(emp_id: str) -> list[AbsencePtoRequest]:
    rows = db.query(
        f"SELECT {REQUEST_COLUMNS} FROM absence_pto_requests WHERE emp_id = %s "
        "ORDER BY requested_at DESC, id DESC",
        (_required_text(emp_id, "emp_id"),),
    )
    return [_request_from_row(row) for row in rows]


def _history_limit(value: object) -> int:
    if type(value) is not int or not 1 <= value <= 100:
        raise ValueError("limit must be between 1 and 100")
    return value


def list_history_for_person(
    emp_id: str, *, limit: int = 100
) -> list[AbsencePtoRequest]:
    """Return a bounded newest-first employee history for presentation."""
    rows = db.query(
        f"SELECT {REQUEST_COLUMNS} FROM absence_pto_requests WHERE emp_id = %s "
        "ORDER BY requested_at DESC, id DESC LIMIT %s",
        (_required_text(emp_id, "emp_id"), _history_limit(limit)),
    )
    return [_request_from_row(row) for row in rows]


def request_counts_for_person(emp_id: str) -> PersonRequestCounts:
    """Return constant-size history and unresolved counts for landing badges."""
    rows = db.query(
        "SELECT COUNT(*) AS total, "
        "COUNT(*) FILTER (WHERE state IN "
        "('pending', 'converting', 'needs_review')) AS unresolved, "
        "COUNT(*) FILTER (WHERE state IN "
        "('pending', 'converting')) AS actionable "
        "FROM absence_pto_requests WHERE emp_id = %s",
        (_required_text(emp_id, "emp_id"),),
    )
    if len(rows) != 1:
        raise ValueError("request count query must return exactly one row")
    counts = PersonRequestCounts(
        total=_nonnegative_int(rows[0].get("total"), "total"),
        unresolved=_nonnegative_int(rows[0].get("unresolved"), "unresolved"),
        actionable=_nonnegative_int(rows[0].get("actionable"), "actionable"),
    )
    if counts.actionable > counts.unresolved or counts.unresolved > counts.total:
        raise ValueError("request counts are inconsistent")
    return counts


def blocking_days_for_person(
    emp_id: str, period_start: date, period_end: date
) -> set[date]:
    """Return only blocking days in the candidate period, without row history."""
    if (
        not isinstance(period_start, date)
        or isinstance(period_start, datetime)
        or not isinstance(period_end, date)
        or isinstance(period_end, datetime)
        or period_start > period_end
    ):
        raise ValueError("period bounds must be ordered dates")
    rows = db.query(
        "SELECT DISTINCT absence_day FROM absence_pto_requests "
        "WHERE emp_id = %s AND absence_day BETWEEN %s AND %s "
        "AND state IN ('pending', 'converting', 'approved', "
        "'needs_review', 'resolved_manually')",
        (_required_text(emp_id, "emp_id"), period_start, period_end),
    )
    result = set()
    for row in rows:
        day = row.get("absence_day")
        if not isinstance(day, date) or isinstance(day, datetime):
            raise ValueError("absence_day must be a date")
        result.add(day)
    return result


def list_pending() -> list[AbsencePtoRequest]:
    rows = db.query(
        f"SELECT {REQUEST_COLUMNS} FROM absence_pto_requests "
        "WHERE state IN ('pending', 'needs_review') "
        "ORDER BY requested_at, id"
    )
    return [_request_from_row(row) for row in rows]


def list_due(
    workflow_now: datetime, *, lease_now: datetime, limit: int = 25
) -> list[AbsencePtoRequest]:
    """Return bounded reconciliation candidates; callers claim each row separately."""
    workflow_current = _aware_datetime(workflow_now, "workflow_now")
    lease_current = _aware_datetime(lease_now, "lease_now")
    if type(limit) is not int or limit <= 0 or limit > 100:
        raise ValueError("limit must be between 1 and 100")
    rows = db.query(
        f"SELECT {REQUEST_COLUMNS} FROM absence_pto_requests "
        "WHERE state IN ('pending', 'converting', 'needs_review', 'approved', "
        "'resolved_manually') "
        "AND (lease_until IS NULL OR lease_until <= %s) "
        "AND ((state = 'needs_review' AND (task_next_at IS NULL OR task_next_at <= %s)) "
        "OR state IN ('pending', 'converting') OR (state IN ('approved', "
        "'resolved_manually') AND odoo_task_id IS NOT NULL "
        "AND task_resolution_step <> 'closed' AND (task_resolution_next_at IS NULL "
        "OR task_resolution_next_at <= %s))) "
        "ORDER BY COALESCE(task_resolution_next_at, task_next_at, requested_at), "
        "id LIMIT %s",
        (lease_current, workflow_current, workflow_current, limit),
    )
    return [_request_from_row(row) for row in rows]


def claim_due(
    owner: UUID,
    workflow_now: datetime,
    *,
    lease_now: datetime,
    period_start: date,
    period_end: date,
    limit: int = 25,
    lease_seconds: int = 120,
) -> list[AbsencePtoRequest]:
    """Claim bounded expired conversions and pending requests after rollover."""
    safe_owner = _uuid(owner, "owner")
    workflow_current = _aware_datetime(workflow_now, "workflow_now")
    lease_current = _aware_datetime(lease_now, "lease_now")
    if (
        not isinstance(period_start, date)
        or isinstance(period_start, datetime)
        or not isinstance(period_end, date)
        or isinstance(period_end, datetime)
        or period_start > period_end
    ):
        raise ValueError("period bounds must be ordered dates")
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("limit must be between 1 and 100")
    if type(lease_seconds) is not int or lease_seconds <= 0:
        raise ValueError("lease_seconds must be a positive integer")
    lease_until = lease_current + timedelta(seconds=lease_seconds)
    rows = db.query(
        "WITH due AS MATERIALIZED ("
        "SELECT request.id FROM absence_pto_requests AS request "
        "WHERE (request.lease_until IS NULL OR request.lease_until <= %s) "
        "AND (request.state = 'converting' OR (request.state = 'pending' "
        "AND request.absence_day NOT BETWEEN %s AND %s) OR "
        "(request.state = 'needs_review' AND (request.task_next_at IS NULL "
        "OR request.task_next_at <= %s)) OR (request.state IN ('approved', "
        "'resolved_manually') AND request.odoo_task_id IS NOT NULL "
        "AND request.task_resolution_step <> 'closed' "
        "AND (request.task_resolution_next_at IS NULL "
        "OR request.task_resolution_next_at <= %s))) "
        "ORDER BY request.requested_at, request.id LIMIT %s "
        "FOR UPDATE SKIP LOCKED"
        ") UPDATE absence_pto_requests AS request "
        "SET lease_owner = %s, lease_until = %s, updated_at = %s FROM due "
        "WHERE request.id = due.id "
        "AND (request.lease_until IS NULL OR request.lease_until <= %s) "
        f"RETURNING {QUALIFIED_REQUEST_COLUMNS}",
        (
            lease_current,
            period_start,
            period_end,
            workflow_current,
            workflow_current,
            limit,
            safe_owner,
            lease_until,
            workflow_current,
            lease_current,
        ),
    )
    return [_request_from_row(row) for row in rows]


def claim_request(
    request_id: int,
    owner: UUID,
    now: datetime,
    *,
    lease_seconds: int = 120,
) -> AbsencePtoRequest | None:
    """Atomically take or renew a bounded lease in one short transaction."""
    safe_id, safe_owner, current, lease_until = _lease_inputs(
        request_id, owner, now, lease_seconds
    )
    sql = (
        "WITH advisory AS MATERIALIZED ("
        "SELECT pg_advisory_xact_lock(%s::bigint)"
        "), locked AS MATERIALIZED ("
        "SELECT request.id FROM absence_pto_requests AS request CROSS JOIN advisory "
        "WHERE request.id = %s AND (request.lease_owner IS NULL "
        "OR request.lease_until IS NULL OR request.lease_until <= %s "
        "OR request.lease_owner = %s) FOR UPDATE"
        ") UPDATE absence_pto_requests AS request "
        "SET lease_owner = %s, lease_until = %s, updated_at = %s FROM locked "
        "WHERE request.id = locked.id AND (request.lease_owner IS NULL "
        "OR request.lease_until IS NULL OR request.lease_until <= %s "
        "OR request.lease_owner = %s) RETURNING "
        f"{QUALIFIED_REQUEST_COLUMNS}"
    )
    rows = db.query(
        sql,
        (
            safe_id,
            safe_id,
            current,
            safe_owner,
            safe_owner,
            lease_until,
            current,
            current,
            safe_owner,
        ),
    )
    return _optional_request(rows)


def renew_claim(
    request_id: int,
    owner: UUID,
    now: datetime,
    *,
    lease_seconds: int = 120,
) -> AbsencePtoRequest:
    safe_id, safe_owner, current, lease_until = _lease_inputs(
        request_id, owner, now, lease_seconds
    )
    rows = db.query(
        "UPDATE absence_pto_requests SET lease_until = %s, updated_at = %s "
        "WHERE id = %s AND lease_owner = %s AND lease_until > %s "
        f"RETURNING {REQUEST_COLUMNS}",
        (lease_until, current, safe_id, safe_owner, current),
    )
    if not rows:
        raise StaleTransition("claim renewal lost its current lease")
    return _one_request(rows, "claim renewal")


_UNSET = object()
_TRANSITION_FIELDS = {
    "original_absence_leave_id": "original_absence_leave_id",
    "pto_leave_id": "pto_leave_id",
    "employee_note": "employee_note",
    "denial_reason": "denial_reason",
    "manual_resolution_note": "manual_resolution_note",
    "sync_error": "sync_error",
    "odoo_task_id": "odoo_task_id",
    "task_attempts": "task_attempts",
    "task_next_at": "task_next_at",
    "decided_by_upn": "decided_by_upn",
    "decided_by_name": "decided_by_name",
    "decided_at": "decided_at",
    "resolved_at": "resolved_at",
}


def _validate_transition_value(field: str, value: object) -> object:
    if field in {"original_absence_leave_id", "pto_leave_id", "odoo_task_id"}:
        return _optional_positive_int(value, field)
    if field == "task_attempts":
        return _nonnegative_int(value, field)
    if field in {"task_next_at", "decided_at", "resolved_at"}:
        return _optional_aware_datetime(value, field)
    return _optional_text(value, field)


def transition(
    request_id: int,
    owner: UUID,
    *,
    expected_state: str,
    expected_step: str,
    new_state: str,
    new_step: str,
    now: datetime | None = None,
    original_absence_leave_id: object = _UNSET,
    pto_leave_id: object = _UNSET,
    employee_note: object = _UNSET,
    denial_reason: object = _UNSET,
    manual_resolution_note: object = _UNSET,
    sync_error: object = _UNSET,
    odoo_task_id: object = _UNSET,
    task_attempts: object = _UNSET,
    task_next_at: object = _UNSET,
    decided_by_upn: object = _UNSET,
    decided_by_name: object = _UNSET,
    decided_at: object = _UNSET,
    resolved_at: object = _UNSET,
) -> AbsencePtoRequest:
    """Compare-and-set one durable step for the current unexpired owner."""
    safe_id = _positive_int(request_id, "request_id")
    safe_owner = _uuid(owner, "owner")
    current = _now(now)
    assignments = ["state = %s", "conversion_step = %s", "updated_at = %s"]
    params: list[object] = [_state(new_state, "new_state"), _step(new_step, "new_step"), current]
    values = locals()
    for argument, column in _TRANSITION_FIELDS.items():
        value = values[argument]
        if value is _UNSET:
            continue
        assignments.append(f"{column} = %s")
        params.append(_validate_transition_value(argument, value))
    params.extend(
        [
            safe_id,
            safe_owner,
            current,
            _state(expected_state, "expected_state"),
            _step(expected_step, "expected_step"),
        ]
    )
    rows = db.query(
        "UPDATE absence_pto_requests SET "
        + ", ".join(assignments)
        + " WHERE id = %s AND lease_owner = %s AND lease_until > %s "
        "AND state = %s AND conversion_step = %s RETURNING "
        + REQUEST_COLUMNS,
        tuple(params),
    )
    if not rows:
        raise StaleTransition("request no longer matches its lease and expected step")
    return _one_request(rows, "request transition")


def release_claim(
    request_id: int, owner: UUID, *, now: datetime | None = None
) -> bool:
    current = _now(now)
    rows = db.query(
        "UPDATE absence_pto_requests SET lease_owner = NULL, lease_until = NULL, "
        "updated_at = %s WHERE id = %s AND lease_owner = %s AND lease_until > %s "
        "RETURNING id",
        (
            current,
            _positive_int(request_id, "request_id"),
            _uuid(owner, "owner"),
            current,
        ),
    )
    return len(rows) == 1


def mark_needs_review(
    request_id: int,
    owner: UUID,
    *,
    error: str,
    workflow_now: datetime,
    lease_now: datetime,
) -> AbsencePtoRequest:
    workflow_current = _aware_datetime(workflow_now, "workflow_now")
    lease_current = _aware_datetime(lease_now, "lease_now")
    rows = db.query(
        "UPDATE absence_pto_requests SET state = 'needs_review', sync_error = %s, "
        "task_next_at = %s, updated_at = %s WHERE id = %s AND lease_owner = %s "
        "AND lease_until > %s AND state IN ('pending', 'converting', 'needs_review') "
        f"RETURNING {REQUEST_COLUMNS}",
        (
            _required_text(error, "error"),
            workflow_current,
            workflow_current,
            _positive_int(request_id, "request_id"),
            _uuid(owner, "owner"),
            lease_current,
        ),
    )
    if not rows:
        raise StaleTransition("review transition lost its current lease")
    return _one_request(rows, "review transition")


def transition_to_pending(
    request_id: int,
    owner: UUID,
    *,
    error: str,
    now: datetime | None = None,
) -> AbsencePtoRequest:
    """Record verified compensation and make the request safely retryable."""
    current = _now(now)
    rows = db.query(
        "UPDATE absence_pto_requests SET state = 'pending', "
        "conversion_step = 'not_started', pto_leave_id = NULL, sync_error = %s, "
        "task_next_at = NULL, updated_at = %s WHERE id = %s AND lease_owner = %s "
        "AND lease_until > %s AND state = 'converting' "
        f"RETURNING {REQUEST_COLUMNS}",
        (
            _required_text(error, "error"),
            current,
            _positive_int(request_id, "request_id"),
            _uuid(owner, "owner"),
            current,
        ),
    )
    if not rows:
        raise StaleTransition("pending transition lost its current conversion lease")
    return _one_request(rows, "pending transition")


def save_task_delivery(
    request_id: int,
    owner: UUID,
    *,
    task_id: int | None,
    attempts: int,
    next_at: datetime | None,
    error: str | None,
    workflow_now: datetime,
    lease_now: datetime,
) -> AbsencePtoRequest:
    """Save task delivery progress only for the current review lease owner."""
    workflow_current = _aware_datetime(workflow_now, "workflow_now")
    lease_current = _aware_datetime(lease_now, "lease_now")
    rows = db.query(
        "UPDATE absence_pto_requests SET odoo_task_id = %s, task_attempts = %s, "
        "task_next_at = %s, sync_error = %s, updated_at = %s "
        "WHERE id = %s AND state = 'needs_review' AND lease_owner = %s "
        f"AND lease_until > %s RETURNING {REQUEST_COLUMNS}",
        (
            _optional_positive_int(task_id, "task_id"),
            _nonnegative_int(attempts, "attempts"),
            _optional_aware_datetime(next_at, "next_at"),
            _optional_text(error, "error"),
            workflow_current,
            _positive_int(request_id, "request_id"),
            _uuid(owner, "owner"),
            lease_current,
        ),
    )
    if not rows:
        raise StaleTransition("task delivery update lost its current review lease")
    return _one_request(rows, "task delivery update")


def save_resolution_delivery(
    request_id: int,
    owner: UUID,
    *,
    expected_step: str,
    new_step: str,
    attempts: int,
    next_at: datetime | None,
    error: str | None,
    workflow_now: datetime,
    lease_now: datetime,
) -> AbsencePtoRequest:
    """Checkpoint terminal task delivery for the current terminal-state owner."""
    workflow_current = _aware_datetime(workflow_now, "workflow_now")
    lease_current = _aware_datetime(lease_now, "lease_now")
    rows = db.query(
        "UPDATE absence_pto_requests SET task_resolution_step = %s, "
        "task_resolution_attempts = %s, task_resolution_next_at = %s, "
        "task_resolution_error = %s, updated_at = %s WHERE id = %s "
        "AND state IN ('approved', 'resolved_manually') AND lease_owner = %s "
        "AND lease_until > %s AND task_resolution_step = %s "
        f"RETURNING {REQUEST_COLUMNS}",
        (
            _resolution_step(new_step, "new_step"),
            _nonnegative_int(attempts, "attempts"),
            _optional_aware_datetime(next_at, "next_at"),
            _optional_text(error, "error"),
            workflow_current,
            _positive_int(request_id, "request_id"),
            _uuid(owner, "owner"),
            lease_current,
            _resolution_step(expected_step, "expected_step"),
        ),
    )
    if not rows:
        raise StaleTransition("resolution delivery update lost its current lease")
    return _one_request(rows, "resolution delivery update")


def adopt_external_pto(
    request_id: int,
    owner: UUID,
    *,
    pto_leave_id: int,
    workflow_now: datetime,
    lease_now: datetime,
) -> AbsencePtoRequest:
    """Durably bind the one verified external PTO before local finalization."""
    workflow_current = _aware_datetime(workflow_now, "workflow_now")
    lease_current = _aware_datetime(lease_now, "lease_now")
    rows = db.query(
        "UPDATE absence_pto_requests SET pto_leave_id = %s, updated_at = %s "
        "WHERE id = %s AND state = 'needs_review' AND lease_owner = %s "
        "AND lease_until > %s "
        f"RETURNING {REQUEST_COLUMNS}",
        (
            _positive_int(pto_leave_id, "pto_leave_id"),
            workflow_current,
            _positive_int(request_id, "request_id"),
            _uuid(owner, "owner"),
            lease_current,
        ),
    )
    if not rows:
        raise StaleTransition("external PTO adoption lost its current review lease")
    return _one_request(rows, "external PTO adoption")


def finalize_approved(
    request_id: int,
    owner: UUID,
    *,
    original_absence_leave_id: int | None,
    pto_leave_id: int,
    actor_upn: str | None,
    actor_name: str | None,
    source: str | None,
    workflow_now: datetime,
    lease_now: datetime,
) -> AbsencePtoRequest:
    """Atomically settle local mirrors, links, request state, and audit."""
    safe_id = _positive_int(request_id, "request_id")
    safe_owner = _uuid(owner, "owner")
    safe_original_id = _optional_positive_int(
        original_absence_leave_id, "original_absence_leave_id"
    )
    safe_pto_id = _positive_int(pto_leave_id, "pto_leave_id")
    workflow_current = _aware_datetime(workflow_now, "workflow_now")
    lease_current = _aware_datetime(lease_now, "lease_now")
    with db.cursor() as cur:
        cur.execute(
            f"SELECT {REQUEST_COLUMNS} FROM absence_pto_requests "
            "WHERE id = %s AND lease_owner = %s AND lease_until > %s AND ("
            "(state = 'converting' AND conversion_step = 'pto_approved') OR "
            "(state = 'needs_review' AND pto_leave_id = %s)) FOR UPDATE",
            (safe_id, safe_owner, lease_current, safe_pto_id),
        )
        request = _one_request(list(cur.fetchall()), "approval finalization lock")
        if (
            request.original_absence_leave_id != safe_original_id
            or request.pto_leave_id != safe_pto_id
        ):
            raise StaleTransition("approval finalization Odoo ids changed")

        cur.execute(
            "SELECT day, emp_id, odoo_leave_id FROM manual_absences "
            "WHERE day = %s AND emp_id = %s FOR UPDATE",
            (request.absence_day, request.emp_id),
        )
        absence_rows = list(cur.fetchall())
        if len(absence_rows) != 1:
            raise StaleTransition("approval finalization lost the attendance absence")
        if absence_rows[0].get("odoo_leave_id") != safe_original_id:
            raise StaleTransition("attendance absence Odoo link changed")

        if safe_original_id is not None:
            cur.execute(
                "UPDATE time_off_requests SET state = 'refuse', "
                "synced_to_odoo = TRUE, sync_error = NULL, local_record = FALSE, "
                "last_pulled_at = %s, last_pushed_at = %s, updated_at = %s "
                "WHERE odoo_leave_id = %s",
                (
                    workflow_current,
                    workflow_current,
                    workflow_current,
                    safe_original_id,
                ),
            )

        cur.execute(
            "INSERT INTO time_off_requests "
            "(person_odoo_id, originating_kiosk_user, shape, holiday_status_id, "
            "date_from, date_to, hour_from, hour_to, note, state, odoo_leave_id, "
            "synced_to_odoo, sync_error, local_record, last_pulled_at, "
            "last_pushed_at, updated_at) "
            "VALUES (%s, FALSE, 'full_day', %s, %s, %s, NULL, NULL, %s, "
            "'validate', %s, TRUE, NULL, FALSE, %s, %s, %s) "
            "ON CONFLICT (odoo_leave_id) WHERE odoo_leave_id IS NOT NULL DO UPDATE SET "
            "person_odoo_id = EXCLUDED.person_odoo_id, "
            "originating_kiosk_user = FALSE, shape = 'full_day', "
            "holiday_status_id = EXCLUDED.holiday_status_id, "
            "date_from = EXCLUDED.date_from, date_to = EXCLUDED.date_to, "
            "hour_from = NULL, hour_to = NULL, note = EXCLUDED.note, "
            "state = 'validate', synced_to_odoo = TRUE, sync_error = NULL, "
            "local_record = FALSE, last_pulled_at = EXCLUDED.last_pulled_at, "
            "last_pushed_at = EXCLUDED.last_pushed_at, updated_at = EXCLUDED.updated_at",
            (
                request.person_odoo_id,
                request.holiday_status_id,
                request.absence_day,
                request.absence_day,
                "Paid Time Off for recorded absence",
                safe_pto_id,
                workflow_current,
                workflow_current,
                workflow_current,
            ),
        )
        cur.execute(
            "UPDATE manual_absences SET odoo_leave_id = %s "
            "WHERE day = %s AND emp_id = %s "
            "AND odoo_leave_id IS NOT DISTINCT FROM %s RETURNING day",
            (safe_pto_id, request.absence_day, request.emp_id, safe_original_id),
        )
        if len(list(cur.fetchall())) != 1:
            raise StaleTransition("attendance absence link update lost its row")

        cur.execute(
            "UPDATE absence_pto_requests SET state = 'approved', "
            "conversion_step = 'pto_approved', sync_error = NULL, "
            "decided_by_upn = %s, decided_by_name = %s, decided_at = %s, "
            "task_resolution_step = CASE WHEN odoo_task_id IS NULL THEN 'closed' "
            "ELSE 'none' END, task_resolution_attempts = 0, "
            "task_resolution_next_at = CASE WHEN odoo_task_id IS NULL THEN NULL "
            "ELSE %s END, task_resolution_error = NULL, updated_at = %s "
            "WHERE id = %s AND lease_owner = %s "
            "AND lease_until > %s AND pto_leave_id = %s AND ("
            "(state = 'converting' AND conversion_step = 'pto_approved') OR "
            "state = 'needs_review') "
            f"RETURNING {REQUEST_COLUMNS}",
            (
                _optional_text(actor_upn, "actor_upn"),
                _optional_text(actor_name, "actor_name"),
                workflow_current,
                workflow_current,
                workflow_current,
                safe_id,
                safe_owner,
                lease_current,
                safe_pto_id,
            ),
        )
        approved = _one_request(list(cur.fetchall()), "approval finalization")
        request_key = f"absence_pto:{safe_id}"
        detail = {
            "original_absence_leave_id": safe_original_id,
            "pto_leave_id": safe_pto_id,
            "conversion_step": "pto_approved",
        }
        time_off_audit.record_decision_with_cursor(
            cur,
            request_id=safe_id,
            odoo_leave_id=safe_pto_id,
            person_odoo_id=request.person_odoo_id,
            person_name=request.person_name,
            leave_type=request.leave_type_name,
            date_from=request.absence_day,
            date_to=request.absence_day,
            hour_from=None,
            hour_to=None,
            action="approve",
            result_state="validate",
            reason=None,
            actor_upn=actor_upn,
            actor_name=actor_name,
            source=source,
            request_kind="absence_pto",
            request_key=request_key,
            detail=detail,
            decided_at=workflow_current,
        )
        inbox_log.record_event_with_cursor(
            cur,
            item_kind="absence_pto",
            item_key=request_key,
            person_name=request.person_name,
            category_label="Past absence PTO",
            action="approve",
            outcome="Approved",
            before_value="Absent · unpaid",
            after_value="Absent · PTO",
            actor_upn=actor_upn,
            actor_name=actor_name,
            source=source,
            reversible=False,
            detail=detail,
            resolved_at=workflow_current,
        )
    return approved


def finalize_manual(
    request_id: int,
    owner: UUID,
    *,
    actor_upn: str,
    actor_name: str,
    note: str,
    workflow_now: datetime,
    lease_now: datetime,
) -> AbsencePtoRequest:
    """Atomically record an explicit non-PTO resolution and its audit event."""
    safe_id = _positive_int(request_id, "request_id")
    safe_owner = _uuid(owner, "owner")
    safe_upn = _required_text(actor_upn, "actor_upn").strip()
    safe_name = _required_text(actor_name, "actor_name").strip()
    safe_note = _required_text(note, "note").strip()
    workflow_current = _aware_datetime(workflow_now, "workflow_now")
    lease_current = _aware_datetime(lease_now, "lease_now")
    with db.cursor() as cur:
        cur.execute(
            "UPDATE absence_pto_requests SET state = 'resolved_manually', "
            "manual_resolution_note = %s, decided_by_upn = %s, "
            "decided_by_name = %s, decided_at = %s, resolved_at = %s, "
            "task_next_at = NULL, task_resolution_step = CASE "
            "WHEN odoo_task_id IS NULL THEN 'closed' ELSE 'none' END, "
            "task_resolution_attempts = 0, task_resolution_next_at = CASE "
            "WHEN odoo_task_id IS NULL THEN NULL ELSE %s END, "
            "task_resolution_error = NULL, updated_at = %s WHERE id = %s "
            "AND state = 'needs_review' AND lease_owner = %s "
            "AND lease_until > %s "
            f"RETURNING {REQUEST_COLUMNS}",
            (
                safe_note,
                safe_upn,
                safe_name,
                workflow_current,
                workflow_current,
                workflow_current,
                workflow_current,
                safe_id,
                safe_owner,
                lease_current,
            ),
        )
        resolved = _one_request(list(cur.fetchall()), "manual review resolution")
        request_key = f"absence_pto:{safe_id}"
        inbox_log.record_event_with_cursor(
            cur,
            item_kind="absence_pto",
            item_key=request_key,
            person_name=resolved.person_name,
            category_label="Past absence PTO",
            action="handled",
            outcome="Handled manually",
            before_value="Needs payroll review",
            after_value="Handled outside Plant Manager PTO",
            reason=safe_note,
            actor_upn=safe_upn,
            actor_name=safe_name,
            source="absence_pto_review",
            reversible=False,
            detail={"request_kind": "absence_pto", "request_key": request_key},
            resolved_at=workflow_current,
        )
    return resolved


def finalize_denied(
    request_id: int,
    owner: UUID,
    *,
    actor_upn: str | None,
    actor_name: str | None,
    reason: str,
    source: str | None,
    workflow_now: datetime,
    lease_now: datetime,
) -> AbsencePtoRequest:
    """Atomically deny a pending request and append both manager audits."""
    safe_id = _positive_int(request_id, "request_id")
    safe_owner = _uuid(owner, "owner")
    safe_reason = _required_text(reason, "reason").strip()
    safe_upn = _optional_text(actor_upn, "actor_upn")
    safe_name = _optional_text(actor_name, "actor_name")
    safe_source = _optional_text(source, "source")
    workflow_current = _aware_datetime(workflow_now, "workflow_now")
    lease_current = _aware_datetime(lease_now, "lease_now")
    with db.cursor() as cur:
        cur.execute(
            f"SELECT {REQUEST_COLUMNS} FROM absence_pto_requests "
            "WHERE id = %s AND state = 'pending' AND lease_owner = %s "
            "AND lease_until > %s FOR UPDATE",
            (safe_id, safe_owner, lease_current),
        )
        request = _one_request(list(cur.fetchall()), "denial finalization lock")
        cur.execute(
            "UPDATE absence_pto_requests SET state = 'denied', "
            "denial_reason = %s, decided_by_upn = %s, decided_by_name = %s, "
            "decided_at = %s, resolved_at = %s, sync_error = NULL, "
            "task_next_at = NULL, updated_at = %s WHERE id = %s "
            "AND state = 'pending' AND lease_owner = %s AND lease_until > %s "
            f"RETURNING {REQUEST_COLUMNS}",
            (
                safe_reason,
                safe_upn,
                safe_name,
                workflow_current,
                workflow_current,
                workflow_current,
                safe_id,
                safe_owner,
                lease_current,
            ),
        )
        denied = _one_request(list(cur.fetchall()), "denial finalization")
        request_key = f"absence_pto:{safe_id}"
        detail = {
            "original_absence_leave_id": request.original_absence_leave_id,
            "pto_leave_id": request.pto_leave_id,
            "conversion_step": request.conversion_step,
        }
        time_off_audit.record_decision_with_cursor(
            cur,
            request_id=safe_id,
            odoo_leave_id=request.original_absence_leave_id,
            person_odoo_id=request.person_odoo_id,
            person_name=request.person_name,
            leave_type=request.leave_type_name,
            date_from=request.absence_day,
            date_to=request.absence_day,
            hour_from=None,
            hour_to=None,
            action="deny",
            result_state="denied",
            reason=safe_reason,
            actor_upn=safe_upn,
            actor_name=safe_name,
            source=safe_source,
            request_kind="absence_pto",
            request_key=request_key,
            detail=detail,
            decided_at=workflow_current,
        )
        inbox_log.record_event_with_cursor(
            cur,
            item_kind="absence_pto",
            item_key=request_key,
            person_name=request.person_name,
            category_label="Past absence PTO",
            action="deny",
            outcome="Denied",
            before_value="Absent · PTO pending",
            after_value="Absent · unpaid",
            reason=safe_reason,
            actor_upn=safe_upn,
            actor_name=safe_name,
            source=safe_source,
            reversible=False,
            detail=detail,
            resolved_at=workflow_current,
        )
    return denied


__all__ = [
    "AbsencePtoRequest",
    "PersonRequestCounts",
    "StaleTransition",
    "claim_due",
    "claim_request",
    "create_request",
    "adopt_external_pto",
    "blocking_days_for_person",
    "get_request",
    "finalize_approved",
    "finalize_denied",
    "finalize_manual",
    "list_due",
    "list_for_person",
    "list_history_for_person",
    "list_pending",
    "mark_needs_review",
    "release_claim",
    "renew_claim",
    "request_counts_for_person",
    "save_resolution_delivery",
    "save_task_delivery",
    "transition",
    "transition_to_pending",
]
