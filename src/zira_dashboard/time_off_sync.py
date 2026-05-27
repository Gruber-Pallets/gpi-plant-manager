"""Background reconciliation for time_off_requests <-> Odoo hr.leave.

Mirrors the kiosk_sync.py shape: every write is recorded locally first,
then pushed to Odoo asynchronously. The local row carries a state and a
``synced_to_odoo`` flag; the request handlers schedule ``push_one`` to
run off the request path, and a 60s sweep (added in a later task) picks
up anything that failed.

This module currently implements the immediate-write path only:

  - ``push_one(row_id)`` reads the row, routes by state, calls into
    ``odoo_client`` for the actual XML-RPC call, and updates the row's
    state + sync flags on success or its ``sync_error`` column on
    failure.

State routing
-------------
The kiosk UI parks requests in three pre-sync states. ``push_one``
routes each to the matching Odoo workflow:

  - ``state='draft'`` + no ``odoo_leave_id`` → create a new hr.leave
    (advances local state to ``'confirm'``)
  - ``state='draft_edit'`` + has ``odoo_leave_id`` → write changed
    fields onto the existing hr.leave (back to ``'confirm'``)
  - ``state='draft_cancel'`` + has ``odoo_leave_id`` → call
    ``action_refuse`` on the existing hr.leave (advances to ``'refuse'``)

Any other state is a no-op — the row is already in a settled state and
the next pull-poller tick will reconcile it.

Dedupe guard
------------
Every create path calls ``find_duplicate_leave`` first. This protects
against the worst-case retry scenario where the original Odoo create
succeeded but the local ``UPDATE`` flipping ``synced_to_odoo`` failed
(network blip between two successful operations). Without the guard the
sweep would create a second hr.leave on retry; with it, we adopt the
existing leave's id and mark the row synced.

Errors
------
On any exception during push, we capture a short structured prefix
(``ExceptionName: message``, trimmed) into ``sync_error`` and leave the
row at ``synced_to_odoo=FALSE`` so the sweep retries it.
"""

from __future__ import annotations

import logging
from typing import Any

from . import db, odoo_client

_log = logging.getLogger(__name__)

# Max chars from the raw exception message we copy into sync_error. The
# column itself is TEXT, but keeping it ~500 total chars (name prefix +
# message) keeps admin UI rendering predictable and bounded.
_SYNC_ERROR_MSG_LIMIT = 480


def push_one(request_id: int) -> None:
    """Sync one local row to Odoo. Called from BackgroundTasks and the sweep.

    Routes by current state + ``odoo_leave_id``:

      - No ``odoo_leave_id``, state=``'draft'`` → create (with dedupe)
      - Has ``odoo_leave_id``, state=``'draft_edit'`` → write fields
      - Has ``odoo_leave_id``, state=``'draft_cancel'`` → refuse

    On any exception the row's ``sync_error`` column is updated; the
    row stays at ``synced_to_odoo=FALSE`` for the next sweep tick.
    """
    rows = db.query(
        "SELECT id, person_odoo_id, shape, holiday_status_id, "
        "date_from, date_to, hour_from, hour_to, note, "
        "state, odoo_leave_id "
        "FROM time_off_requests WHERE id = %s",
        (request_id,),
    )
    if not rows:
        _log.warning("push_one called with unknown id=%s", request_id)
        return
    row = rows[0]
    try:
        if row["odoo_leave_id"] is None:
            _push_create(row)
        elif row["state"] == "draft_edit":
            _push_edit(row)
        elif row["state"] == "draft_cancel":
            _push_cancel(row)
        else:
            _log.info(
                "push_one no-op for row %s (state=%s, leave_id=%s)",
                row["id"], row["state"], row["odoo_leave_id"],
            )
    except Exception as e:  # noqa: BLE001 — record per-row failure and continue
        db.execute(
            "UPDATE time_off_requests SET sync_error = %s, "
            "updated_at = now() WHERE id = %s",
            (_classify_error(e), row["id"]),
        )
        _log.info("push_one failed for row %s: %s", row["id"], e)


def _push_create(row: dict[str, Any]) -> None:
    """Create a new hr.leave in Odoo for this row, with dedupe guard.

    See module docstring for why we always check for an existing leave
    before creating.
    """
    hour_from = float(row["hour_from"]) if row["hour_from"] is not None else None
    hour_to = float(row["hour_to"]) if row["hour_to"] is not None else None
    existing = odoo_client.find_duplicate_leave(
        employee_odoo_id=row["person_odoo_id"],
        holiday_status_id=row["holiday_status_id"],
        date_from=row["date_from"], date_to=row["date_to"],
    )
    if existing is not None:
        leave_id = existing
    else:
        leave_id = odoo_client.create_leave(
            employee_odoo_id=row["person_odoo_id"],
            holiday_status_id=row["holiday_status_id"],
            date_from=row["date_from"], date_to=row["date_to"],
            hour_from=hour_from, hour_to=hour_to,
            note=row["note"],
        )
    db.execute(
        "UPDATE time_off_requests SET odoo_leave_id = %s, "
        "state = 'confirm', synced_to_odoo = TRUE, sync_error = NULL, "
        "last_pushed_at = now(), updated_at = now() WHERE id = %s",
        (leave_id, row["id"]),
    )


def _push_edit(row: dict[str, Any]) -> None:
    """Write changed fields to an existing Odoo hr.leave.

    Caller staged the new values in the row before flipping state to
    ``'draft_edit'`` — we just translate them to Odoo field names.
    """
    fields: dict[str, Any] = {
        "request_date_from": row["date_from"].isoformat(),
        "request_date_to": row["date_to"].isoformat(),
    }
    if row["hour_from"] is not None and row["hour_to"] is not None:
        fields["request_unit_hours"] = True
        fields["request_hour_from"] = float(row["hour_from"])
        fields["request_hour_to"] = float(row["hour_to"])
    if row["note"]:
        fields["name"] = row["note"]
    odoo_client.write_leave(row["odoo_leave_id"], **fields)
    db.execute(
        "UPDATE time_off_requests SET state = 'confirm', "
        "synced_to_odoo = TRUE, sync_error = NULL, "
        "last_pushed_at = now(), updated_at = now() WHERE id = %s",
        (row["id"],),
    )


def _push_cancel(row: dict[str, Any]) -> None:
    """Refuse an existing Odoo hr.leave (pending-cancel or approved-cancel)."""
    odoo_client.refuse_leave(row["odoo_leave_id"])
    db.execute(
        "UPDATE time_off_requests SET state = 'refuse', "
        "synced_to_odoo = TRUE, sync_error = NULL, "
        "last_pushed_at = now(), updated_at = now() WHERE id = %s",
        (row["id"],),
    )


def _classify_error(e: Exception) -> str:
    """Wrap a raw exception in a short structured prefix for the
    ``sync_error`` column. Output is ``ExceptionName: message`` with the
    message trimmed to ``_SYNC_ERROR_MSG_LIMIT`` chars."""
    name = type(e).__name__
    msg = str(e)[:_SYNC_ERROR_MSG_LIMIT]
    return f"{name}: {msg}"


# Cap on how many unsynced rows we attempt per sweep tick. Bounds the
# blast radius if Odoo is down and the backlog has grown: one tick will
# only fire 50 XML-RPC calls instead of hammering an unbounded queue.
_SWEEP_BATCH_SIZE = 50


def retry_unsynced_requests() -> int:
    """Retry up to ``_SWEEP_BATCH_SIZE`` unsynced rows. Returns the count
    of rows attempted (success or failure recorded per row by
    ``push_one``)."""
    rows = db.query(
        "SELECT id FROM time_off_requests "
        "WHERE synced_to_odoo = FALSE "
        "ORDER BY created_at ASC, id ASC LIMIT %s",
        (_SWEEP_BATCH_SIZE,),
    )
    for r in rows:
        push_one(r["id"])
    return len(rows)
