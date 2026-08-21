"""Append-only activity log for the Exception Inbox — the archive + audit trail.

One row per resolution across every inbox category. Denormalized on purpose
(snapshots person/category/outcome) so it stands alone after source rows are
deleted, mirroring time_off_audit.py. A NULL actor means the item resolved
itself (auto-resolved); a human action carries the manager's UPN + name.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from . import db

_log = logging.getLogger(__name__)


def actor_from(request) -> tuple[str | None, str | None]:
    """(user_upn, user_name) for the current request; both None for system/auto.

    The auth middleware sets these on request.state for every authenticated
    request (see auth.py). Returns (None, None) when unset (e.g. AUTH_DISABLED)
    or when the request carries no ``state`` at all.
    """
    state = getattr(request, "state", None)
    return (
        getattr(state, "user_upn", None),
        getattr(state, "user_name", None),
    )


def record_event_with_cursor(
    cursor,
    *,
    item_kind: str,
    item_key: str,
    person_name: str | None,
    category_label: str | None,
    action: str,
    outcome: str | None = None,
    before_value: str | None = None,
    after_value: str | None = None,
    reason: str | None = None,
    actor_upn: str | None = None,
    actor_name: str | None = None,
    source: str | None = "inbox",
    reversible: bool = False,
    detail: Any | None = None,
) -> int:
    """Insert one event with an existing transaction cursor and return its id."""
    cursor.execute(
        "INSERT INTO inbox_events "
        "(item_kind, item_key, person_name, category_label, action, outcome, "
        " before_value, after_value, reason, actor_upn, actor_name, source, "
        " reversible, detail) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb) "
        "RETURNING id",
        (item_kind, item_key, person_name, category_label, action, outcome,
         before_value, after_value, reason, actor_upn, actor_name, source,
         reversible, json.dumps(detail, default=str) if detail is not None else None),
    )
    return int(cursor.fetchone()["id"])


def record_event(
    *,
    item_kind: str,
    item_key: str,
    person_name: str | None,
    category_label: str | None,
    action: str,
    outcome: str | None = None,
    before_value: str | None = None,
    after_value: str | None = None,
    reason: str | None = None,
    actor_upn: str | None = None,
    actor_name: str | None = None,
    source: str | None = "inbox",
    reversible: bool = False,
    detail: Any | None = None,
) -> int:
    """Insert one event row and return its id (for later undo correlation)."""
    with db.cursor() as cursor:
        return record_event_with_cursor(
            cursor,
            item_kind=item_kind,
            item_key=item_key,
            person_name=person_name,
            category_label=category_label,
            action=action,
            outcome=outcome,
            before_value=before_value,
            after_value=after_value,
            reason=reason,
            actor_upn=actor_upn,
            actor_name=actor_name,
            source=source,
            reversible=reversible,
            detail=detail,
        )


def log_event_safe(**kwargs) -> int | None:
    """record_event, but never raises. Returns the new id, or None on failure.

    The underlying action (Odoo write, suppression-table row) is the source of
    truth; a failed audit write is logged and swallowed so it can't turn a
    completed action into a 500. Mirrors the best-effort posture of the
    time-off chatter post.
    """
    try:
        return record_event(**kwargs)
    except Exception as e:  # noqa: BLE001 -- audit is best-effort
        _log.warning("inbox_log.record_event failed (%s): %s",
                     kwargs.get("item_key"), e, exc_info=True)
        return None


def recent_events(days: int = 30) -> list[dict[str, Any]]:
    """Events in the last ``days`` days, newest first (archive/audit feed)."""
    return db.query(
        "SELECT id, item_kind, item_key, person_name, category_label, action, "
        "outcome, before_value, after_value, reason, actor_upn, actor_name, "
        "source, reversible, undone_at, resolved_at "
        "FROM inbox_events "
        "WHERE resolved_at >= now() - make_interval(days => %s) "
        "ORDER BY resolved_at DESC",
        (days,),
    )


def archive(
    *,
    before=None,
    actor_upn: str | None = None,
    include_auto: bool = False,
    limit: int = 200,
) -> list[dict[str, Any]]:
    """History for the inbox archive, newest first.

    - ``before``: a ``resolved_at`` value; returns only rows strictly older
      (the "show earlier" cursor).
    - ``actor_upn``: restrict to one actor (also excludes auto-resolved, which
      have a NULL actor).
    - ``include_auto``: when False (default), hide auto-resolved rows.
    """
    clauses: list[str] = []
    params: list[Any] = []
    if before is not None:
        clauses.append("resolved_at < %s")
        params.append(before)
    if actor_upn:
        clauses.append("actor_upn = %s")
        params.append(actor_upn)
    elif not include_auto:
        clauses.append("actor_upn IS NOT NULL")
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    params.append(limit)
    return db.query(
        "SELECT id, item_kind, item_key, person_name, category_label, action, "
        "outcome, before_value, after_value, reason, actor_upn, actor_name, "
        "source, reversible, undone_at, resolved_at "
        "FROM inbox_events" + where +
        " ORDER BY resolved_at DESC LIMIT %s",
        tuple(params),
    )


def get_event(event_id: int) -> dict[str, Any] | None:
    """One event row by id, or None."""
    rows = db.query(
        "SELECT id, item_kind, item_key, person_name, category_label, action, "
        "outcome, before_value, after_value, reason, actor_upn, actor_name, "
        "source, reversible, undone_at, undo_event_id, resolved_at, detail "
        "FROM inbox_events WHERE id = %s",
        (event_id,),
    )
    return rows[0] if rows else None


def mark_undone(event_id: int, undo_event_id: int | None) -> None:
    """Stamp an event as undone, pointing at the undo event."""
    db.execute(
        "UPDATE inbox_events SET undone_at = now(), undo_event_id = %s WHERE id = %s",
        (undo_event_id, event_id),
    )


def has_human_event_since(item_key: str, since) -> bool:
    """True if a human (non-auto, non-undo) event exists for this item at or
    after ``since`` — used by the reconciler to tell a human resolution from a
    self-clearing one."""
    rows = db.query(
        "SELECT 1 FROM inbox_events WHERE item_key = %s AND resolved_at >= %s "
        "AND action NOT IN ('auto_resolved', 'undo') LIMIT 1",
        (item_key, since),
    )
    return bool(rows)
