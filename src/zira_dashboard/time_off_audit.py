"""Append-only audit log for in-app time-off approve/deny decisions.

Denormalized on purpose: the leave poller hard-deletes time_off_requests
rows when a leave is deleted in Odoo, so this log snapshots person name,
leave type, and dates to stand alone. See the design spec.
"""
from __future__ import annotations

from datetime import date
from typing import Any

from . import db


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
) -> None:
    """Insert one decision row. ``action`` is 'approve' or 'deny'."""
    db.execute(
        "INSERT INTO time_off_decisions "
        "(request_id, odoo_leave_id, person_odoo_id, person_name, leave_type, "
        " date_from, date_to, hour_from, hour_to, action, result_state, "
        " reason, actor_upn, actor_name, source) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (request_id, odoo_leave_id, person_odoo_id, person_name, leave_type,
         date_from, date_to, hour_from, hour_to, action, result_state, reason,
         actor_upn, actor_name, source),
    )


def recent_decisions(days: int = 30) -> list[dict[str, Any]]:
    """Decisions in the last ``days`` days, newest first."""
    return db.query(
        "SELECT id, request_id, odoo_leave_id, person_odoo_id, person_name, "
        "leave_type, date_from, date_to, hour_from, hour_to, action, "
        "result_state, reason, "
        "actor_upn, actor_name, source, decided_at "
        "FROM time_off_decisions "
        "WHERE decided_at >= now() - make_interval(days => %s) "
        "ORDER BY decided_at DESC",
        (days,),
    )
