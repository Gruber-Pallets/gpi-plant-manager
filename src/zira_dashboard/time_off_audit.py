"""Append-only audit log for in-app time-off approve/deny decisions.

Denormalized on purpose: the leave poller hard-deletes time_off_requests
rows when a leave is deleted in Odoo, so this log snapshots person name,
leave type, and dates to stand alone. See the design spec.
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Any

from psycopg2.extras import Json

from . import db


_INSERT_DECISION_SQL = (
    "INSERT INTO time_off_decisions "
    "(request_id, odoo_leave_id, person_odoo_id, person_name, leave_type, "
    " date_from, date_to, hour_from, hour_to, action, result_state, "
    " reason, actor_upn, actor_name, source, request_kind, request_key, detail, "
    "decided_at) "
    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, "
    "%s, %s, %s, COALESCE(%s, now()))"
)


def record_decision(
    *,
    request_id: int | None,
    odoo_leave_id: int | None,
    person_odoo_id: int | None,
    person_name: str | None,
    leave_type: str | None,
    date_from: date | None,
    date_to: date | None,
    hour_from: float | None,
    hour_to: float | None,
    action: str,
    result_state: str | None,
    reason: str | None,
    actor_upn: str | None,
    actor_name: str | None,
    source: str | None,
    request_kind: str = "time_off",
    request_key: str | None = None,
    detail: dict[str, Any] | None = None,
    decided_at: datetime | None = None,
) -> None:
    """Insert one decision row. ``action`` is 'approve' or 'deny'."""
    db.execute(
        _INSERT_DECISION_SQL,
        _decision_params(
            request_id=request_id,
            odoo_leave_id=odoo_leave_id,
            person_odoo_id=person_odoo_id,
            person_name=person_name,
            leave_type=leave_type,
            date_from=date_from,
            date_to=date_to,
            hour_from=hour_from,
            hour_to=hour_to,
            action=action,
            result_state=result_state,
            reason=reason,
            actor_upn=actor_upn,
            actor_name=actor_name,
            source=source,
            request_kind=request_kind,
            request_key=request_key,
            detail=detail,
            decided_at=decided_at,
        ),
    )


def record_decision_with_cursor(cursor, **kwargs: Any) -> None:
    """Insert a decision as part of an existing caller-owned transaction."""
    cursor.execute(_INSERT_DECISION_SQL, _decision_params(**kwargs))


def _decision_params(
    *,
    request_id: int | None,
    odoo_leave_id: int | None,
    person_odoo_id: int | None,
    person_name: str | None,
    leave_type: str | None,
    date_from: date | None,
    date_to: date | None,
    hour_from: float | None,
    hour_to: float | None,
    action: str,
    result_state: str | None,
    reason: str | None,
    actor_upn: str | None,
    actor_name: str | None,
    source: str | None,
    request_kind: str = "time_off",
    request_key: str | None = None,
    detail: dict[str, Any] | None = None,
    decided_at: datetime | None = None,
) -> tuple[Any, ...]:
    return (
        request_id,
        odoo_leave_id,
        person_odoo_id,
        person_name,
        leave_type,
        date_from,
        date_to,
        hour_from,
        hour_to,
        action,
        result_state,
        reason,
        actor_upn,
        actor_name,
        source,
        request_kind,
        request_key,
        Json(detail) if detail is not None else None,
        decided_at,
    )


def recent_decisions(days: int = 30) -> list[dict[str, Any]]:
    """Decisions in the last ``days`` days, newest first."""
    return db.query(
        "SELECT id, request_id, odoo_leave_id, person_odoo_id, person_name, "
        "leave_type, date_from, date_to, hour_from, hour_to, action, "
        "result_state, reason, request_kind, request_key, detail, "
        "actor_upn, actor_name, source, decided_at "
        "FROM time_off_decisions "
        "WHERE decided_at >= now() - make_interval(days => %s) "
        "ORDER BY decided_at DESC",
        (days,),
    )
