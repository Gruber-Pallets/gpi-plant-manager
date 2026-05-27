"""Background reconciliation for time_off_requests <-> Odoo hr.leave.

Mirrors the kiosk_sync.py shape: every write is recorded locally first,
then pushed to Odoo asynchronously. The local row carries a state and a
``synced_to_odoo`` flag; the request handlers schedule ``push_one`` to
run off the request path, and a 60s sweep (added in a later task) picks
up anything that failed.

This module implements two sides of the sync:

  - **Push** (``push_one``): reads a local row, routes by state, calls
    into ``odoo_client`` for the actual XML-RPC call, and updates the
    row's state + sync flags on success or its ``sync_error`` column on
    failure.
  - **Pull** (``poll_odoo_leaves``): fetches all hr.leave in a rolling
    window from Odoo and upserts each into the local mirror. Existing
    rows whose state changed trigger ``cascade_on_state_change``; local
    rows missing from Odoo are marked ``state='cancel'``.

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
from datetime import date, timedelta
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


# Rolling window the poller pulls from Odoo. 60 days back catches recent
# retroactive HR fixes; 365 days forward covers next year's PTO requests
# already entered in Odoo. Bounded to keep one tick's payload reasonable.
_POLL_PAST_DAYS = 60
_POLL_FUTURE_DAYS = 365


def poll_odoo_leaves() -> int:
    """Pull all hr.leave for active employees in a rolling window and
    upsert into ``time_off_requests``. Returns the count of leaves
    processed.

    For each Odoo leave:

      - If we have a local row with that ``odoo_leave_id``: UPDATE if
        state differs; trigger ``cascade_on_state_change``.
      - If not: INSERT a new row with ``originating_kiosk_user=FALSE``.

    Local rows in non-terminal state whose ``odoo_leave_id`` is no
    longer returned by Odoo are marked ``state='cancel'`` (Odoo-side
    deletion).
    """
    today = date.today()
    start_d = today - timedelta(days=_POLL_PAST_DAYS)
    end_d = today + timedelta(days=_POLL_FUTURE_DAYS)
    leaves = odoo_client.fetch_leaves_for_range(start_d, end_d)
    seen_ids: set[int] = set()
    for leave in leaves:
        seen_ids.add(leave["id"])
        _upsert_one(leave)
    _mark_missing_as_cancel(seen_ids, start_d, end_d)
    return len(leaves)


def _unwrap_many2one(field: Any) -> Any:
    """Odoo XML-RPC returns Many2one fields as ``[id, display_name]``
    lists. Anywhere else (e.g. a write payload echo) they may already be
    a bare id. Normalize to just the id."""
    return field[0] if isinstance(field, list) else field


def _upsert_one(leave: dict[str, Any]) -> None:
    """Insert or update one Odoo hr.leave into the local mirror.

    On UPDATE, if state changed, fires ``cascade_on_state_change``.
    On INSERT of an already-validated leave, fires the cascade with a
    synthetic ``old`` row in ``state='draft'`` so the staffing side
    reacts as if the leave had been freshly approved.
    """
    odoo_leave_id = leave["id"]
    state = leave["state"]
    person_odoo_id = _unwrap_many2one(leave["employee_id"])
    holiday_status_id = _unwrap_many2one(leave["holiday_status_id"])
    date_from = _parse_date(leave["request_date_from"])
    date_to = _parse_date(leave["request_date_to"])
    request_unit_hours = bool(leave.get("request_unit_hours"))
    hour_from = (
        float(leave["request_hour_from"])
        if request_unit_hours and leave.get("request_hour_from")
        else None
    )
    hour_to = (
        float(leave["request_hour_to"])
        if request_unit_hours and leave.get("request_hour_to")
        else None
    )
    note = leave.get("name") or None

    rows = db.query(
        "SELECT id, person_odoo_id, state, shape, holiday_status_id, "
        "date_from, date_to, hour_from, hour_to, working_hours_json, "
        "odoo_leave_id "
        "FROM time_off_requests WHERE odoo_leave_id = %s",
        (odoo_leave_id,),
    )
    if rows:
        existing = rows[0]
        new_row = dict(existing)
        new_row["state"] = state
        new_row["date_from"] = date_from
        new_row["date_to"] = date_to
        new_row["hour_from"] = hour_from
        new_row["hour_to"] = hour_to
        db.execute(
            "UPDATE time_off_requests SET state = %s, date_from = %s, "
            "date_to = %s, hour_from = %s, hour_to = %s, "
            "last_pulled_at = now(), updated_at = now() WHERE id = %s",
            (state, date_from, date_to, hour_from, hour_to, existing["id"]),
        )
        if existing["state"] != state:
            cascade_on_state_change(existing, new_row)
    else:
        # Infer shape: full_day if no hour bounds; otherwise we can't be
        # sure of late/early/midday from Odoo alone, so call it
        # midday_gap (most permissive partial-day shape).
        shape = "midday_gap" if request_unit_hours else "full_day"
        db.execute(
            "INSERT INTO time_off_requests "
            "(person_odoo_id, originating_kiosk_user, shape, "
            "holiday_status_id, date_from, date_to, hour_from, hour_to, "
            "note, state, odoo_leave_id, synced_to_odoo, last_pulled_at) "
            "VALUES (%s, FALSE, %s, %s, %s, %s, %s, %s, %s, %s, %s, TRUE, now())",
            (person_odoo_id, shape, holiday_status_id, date_from, date_to,
             hour_from, hour_to, note, state, odoo_leave_id),
        )
        if state == "validate":
            # New HR-entered leave already approved → trigger cascade
            new_rows = db.query(
                "SELECT * FROM time_off_requests WHERE odoo_leave_id = %s",
                (odoo_leave_id,),
            )
            if new_rows:
                cascade_on_state_change({"state": "draft"}, new_rows[0])


def _mark_missing_as_cancel(
    seen_ids: set[int], start_d: date, end_d: date,
) -> None:
    """Rows in ``[start_d..end_d]`` with ``odoo_leave_id`` not in
    ``seen_ids`` and state not already terminal → mark as cancel +
    cascade-reverse."""
    rows = db.query(
        "SELECT id, state, person_odoo_id, shape, holiday_status_id, "
        "date_from, date_to, hour_from, hour_to, working_hours_json, "
        "odoo_leave_id "
        "FROM time_off_requests "
        "WHERE odoo_leave_id IS NOT NULL "
        "AND state NOT IN ('cancel', 'refuse') "
        "AND date_to >= %s AND date_from <= %s",
        (start_d, end_d),
    )
    for r in rows:
        if r["odoo_leave_id"] in seen_ids:
            continue
        new_r = dict(r)
        new_r["state"] = "cancel"
        db.execute(
            "UPDATE time_off_requests SET state = 'cancel', "
            "last_pulled_at = now(), updated_at = now() WHERE id = %s",
            (r["id"],),
        )
        cascade_on_state_change(r, new_r)


def _parse_date(value: Any) -> date | None:
    """Coerce an Odoo date/datetime field into a ``date``. Tolerates
    bare ``date`` objects, ``"YYYY-MM-DD"`` strings, and
    ``"YYYY-MM-DD HH:MM:SS"`` strings (truncates time portion)."""
    if hasattr(value, "isoformat"):
        return value
    if isinstance(value, str):
        return date.fromisoformat(value[:10])
    return None


def cascade_on_state_change(old: dict[str, Any], new: dict[str, Any]) -> None:
    """Stub — implemented in next task. For now just no-op so the test
    monkeypatching mechanism works."""
    return None
