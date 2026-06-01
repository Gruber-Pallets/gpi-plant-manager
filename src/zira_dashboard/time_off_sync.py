"""Background reconciliation for time_off_requests <-> Odoo hr.leave.

Mirrors the timeclock_sync.py shape: every write is recorded locally first,
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
    rows missing from Odoo are HARD DELETED (reverse cascade fires first).

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

from . import db, odoo_client, time_off_balances
from .staffing import TIME_OFF_KEY

_log = logging.getLogger(__name__)

# Max chars from the raw exception message we copy into sync_error. The
# column itself is TEXT, but keeping it ~500 total chars (name prefix +
# message) keeps admin UI rendering predictable and bounded.
_SYNC_ERROR_MSG_LIMIT = 480


def find_conflicting_request(
    person_odoo_id: int,
    date_from: date,
    date_to: date,
    exclude_rid: int | None = None,
    established_only: bool = False,
) -> dict | None:
    """First non-rejected ``time_off_requests`` row for ``person_odoo_id``
    whose ``[date_from, date_to]`` overlaps the given range, else None.

    Date-level, type-agnostic overlap — mirrors Odoo's own "no two leaves on
    the same day for one employee" constraint, but caught locally *before* we
    post so an overlap never sticks in the errored state. Scoped to the same
    person only; never blocks against a coworker's time off.

    ``exclude_rid``      skip this row id (an edit can't conflict with itself).
    ``established_only``  push-path mode: only count a row as a conflict if it
                          is already synced (``synced_to_odoo = TRUE``) OR was
                          created earlier (``id < exclude_rid``). Stops two
                          simultaneous duplicate drafts from deleting each
                          other — the earlier/established one wins.
    """
    sql = (
        "SELECT id, state, synced_to_odoo, date_from, date_to "
        "FROM time_off_requests "
        "WHERE person_odoo_id = %s "
        "AND state IN ('draft','draft_edit','confirm','validate1','validate') "
        "AND date_to >= %s AND date_from <= %s"
    )
    params: list[Any] = [person_odoo_id, date_from, date_to]
    if exclude_rid is not None:
        sql += " AND id <> %s"
        params.append(exclude_rid)
    if established_only:
        # exclude_rid is always supplied in this mode (the row being pushed).
        sql += " AND (synced_to_odoo = TRUE OR id < %s)"
        params.append(exclude_rid)
    sql += " ORDER BY id LIMIT 1"
    rows = db.query(sql, tuple(params))
    return rows[0] if rows else None


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
    # Submit the leave into Odoo's approval workflow. A bare create sits in
    # "To Submit" (draft) — invisible to the manager's approval queue and not
    # deducted from balances — so confirm it into a real pending request.
    # Best-effort: if confirm fails (e.g. Odoo rejects on a balance check),
    # the leave still exists as a draft, so we keep the row synced rather than
    # looping on sync_error; the 60s poller reconciles the real state. Worst
    # case is the pre-fix behaviour (a draft in Odoo), never worse.
    try:
        odoo_client.confirm_leave(leave_id)
    except Exception as e:  # noqa: BLE001 — leave exists; log and let poll reconcile
        _log.warning(
            "confirm_leave failed for leave %s (row %s, left in draft): %s",
            leave_id, row["id"], e, exc_info=True,
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

    Local rows whose ``odoo_leave_id`` is no longer returned by Odoo are
    hard-deleted (Odoo-side deletion), after firing the reverse cascade —
    regardless of local state, so a refused/cancelled ("denied") row also
    disappears once its leave is deleted in Odoo.
    """
    # Refresh leave-types cache first so the kiosk picker stays current.
    try:
        types = odoo_client.fetch_leave_types()
    except Exception as e:  # noqa: BLE001
        # Bumped from info -> warning + exc_info so the Railway logs
        # actually show *why* the leave-types pull failed (e.g. the
        # Odoo API user lacks read perm on hr.leave.type). Without the
        # traceback, the Settings panel "no leave types" message had
        # no breadcrumb back to the root cause.
        _log.warning(
            "leave_types fetch for cache refresh failed: %s", e, exc_info=True,
        )
        types = []
    # Per-row upsert with per-row error isolation. A single bad row (e.g. a
    # type whose request_unit isn't in the cache CHECK set) must NOT abort
    # the whole refresh — that would silently freeze every *other* type's
    # cached attributes (this is exactly how a stale `requires_allocation`
    # left the kiosk showing "No allocation tracked" while Odoo was correct).
    # Mirrors the per-row tolerance in `_fallback_fetch_and_cache_leave_types`.
    for t in types:
        try:
            db.execute(
                "INSERT INTO leave_types_cache "
                "(holiday_status_id, name, request_unit, requires_allocation, "
                " color, active, last_pulled_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, now()) "
                "ON CONFLICT (holiday_status_id) DO UPDATE SET "
                "name = EXCLUDED.name, request_unit = EXCLUDED.request_unit, "
                "requires_allocation = EXCLUDED.requires_allocation, "
                "color = EXCLUDED.color, active = EXCLUDED.active, "
                "last_pulled_at = now()",
                (t["id"], t["name"], t["request_unit"],
                 t["requires_allocation"], t.get("color"), t.get("active", True)),
            )
        except Exception as e:  # noqa: BLE001
            _log.warning(
                "leave_types_cache upsert failed for type id=%s name=%r "
                "(request_unit=%r requires_allocation=%r color=%r): %s",
                t.get("id"), t.get("name"), t.get("request_unit"),
                t.get("requires_allocation"), t.get("color"), e,
                exc_info=True,
            )

    today = date.today()
    start_d = today - timedelta(days=_POLL_PAST_DAYS)
    end_d = today + timedelta(days=_POLL_FUTURE_DAYS)
    leaves = odoo_client.fetch_leaves_for_range(start_d, end_d)
    seen_ids: set[int] = set()
    for leave in leaves:
        seen_ids.add(leave["id"])
        _upsert_one(leave)
    _delete_missing_from_odoo(seen_ids, start_d, end_d)
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


def _delete_missing_from_odoo(
    seen_ids: set[int], start_d: date, end_d: date,
) -> None:
    """Rows in ``[start_d..end_d]`` with an ``odoo_leave_id`` no longer
    returned by Odoo (not in ``seen_ids``) → HARD DELETE, regardless of
    local state. Odoo is the source of truth: if the leave is gone there,
    the local mirror row is removed — including a terminal ``'refuse'`` /
    ``'cancel'`` ("denied"/cancelled) row, which must also disappear once
    its leave is deleted in Odoo. ``seen_ids`` is the sole authority on
    whether a leave still exists: ``fetch_leaves_for_range`` pulls every
    state, so a leave still present in any state is in ``seen_ids`` and is
    skipped below.

    Before deleting we fire ``cascade_on_state_change`` with a synthetic
    ``state='cancel'`` so an approved leave still logs its reverse
    ``scheduler_moves`` audit row (the breadcrumb survives the row's
    deletion) and invalidates the balance. We then invalidate the balance
    unconditionally so a deleted *pending* leave frees its in-flight
    allocation immediately rather than waiting for the 10-min balance sweep.

    Unsynced kiosk drafts (``odoo_leave_id IS NULL``) are never touched —
    the WHERE clause excludes them."""
    rows = db.query(
        "SELECT id, state, person_odoo_id, shape, holiday_status_id, "
        "date_from, date_to, hour_from, hour_to, working_hours_json, "
        "odoo_leave_id "
        "FROM time_off_requests "
        "WHERE odoo_leave_id IS NOT NULL "
        "AND date_to >= %s AND date_from <= %s",
        (start_d, end_d),
    )
    for r in rows:
        if r["odoo_leave_id"] in seen_ids:
            continue
        new_r = dict(r)
        new_r["state"] = "cancel"
        cascade_on_state_change(r, new_r)   # reverse audit + balance (if approved)
        _invalidate_balance(r["person_odoo_id"])  # also free pending allocations
        db.execute(
            "DELETE FROM time_off_requests WHERE id = %s",
            (r["id"],),
        )


def _parse_date(value: Any) -> date | None:
    """Coerce an Odoo date/datetime field into a ``date``. Tolerates
    bare ``date`` objects, ``"YYYY-MM-DD"`` strings, and
    ``"YYYY-MM-DD HH:MM:SS"`` strings (truncates time portion)."""
    if hasattr(value, "isoformat"):
        return value
    if isinstance(value, str):
        return date.fromisoformat(value[:10])
    return None


# State transitions that drive the cascade. "validate" is Odoo's
# fully-approved state; the validate1 intermediate (manager-approved but not
# HR-approved) is intentionally NOT treated as approved here so we don't
# audit the staffing side until the request is truly green-lit.
_APPROVED_STATES = {"validate"}
_REVERSED_STATES = {"refuse", "cancel"}

# scheduler_moves audit-log vocabulary. The DDL declares
# `to_bucket TEXT NOT NULL`, so reverse-direction rows can't leave it null;
# we use the sentinel "__unassigned" to signal "no longer in time-off, back to
# the regular scheduler pool". Read paths that consume scheduler_moves can
# treat "__unassigned" symmetrically with "null from_bucket". Both sentinels
# use the double-underscore prefix convention from staffing.TIME_OFF_KEY to
# mark them as pseudo-buckets rather than real work-center names.
_BUCKET_UNASSIGNED = "__unassigned"


def cascade_on_state_change(old: dict[str, Any], new: dict[str, Any]) -> None:
    """Drive scheduler-side audit + balance invalidation when a request's
    state transitions.

    The local ``time_off_requests`` table is the source of truth for what
    counts as approved on the scheduler — read paths in
    ``routes/staffing.py`` and ``routes/time_off.py`` surface approved
    rows directly (Tasks 19/20/21). This cascade is the side-effect /
    audit layer:

      - **Forward** (anything → ``validate``): logs one row to
        ``scheduler_moves`` per affected date with
        ``to_bucket=TIME_OFF_KEY`` ("__time_off") and
        ``reason='time_off_approved'``.
      - **Reverse** (``validate`` → ``refuse``/``cancel``): logs one row
        per date with ``from_bucket=TIME_OFF_KEY``,
        ``to_bucket='__unassigned'`` (sentinel — column is NOT NULL), and
        ``reason='time_off_canceled'``.

    Both directions also invalidate the person's row in
    ``time_off_balances`` so the next kiosk wizard open re-fetches fresh
    allocations from Odoo.

    No-op for any other transition (e.g. ``draft → confirm`` or
    ``confirm → validate1``). Multiple invocations on the same
    transition are safe: the resulting audit rows are append-only and
    the balance DELETE is idempotent. No XML-RPC or heavy I/O — the
    cascade runs inline on the poller's transaction path.
    """
    old_state = old.get("state")
    new_state = new.get("state")
    forward = old_state not in _APPROVED_STATES and new_state in _APPROVED_STATES
    reverse = old_state in _APPROVED_STATES and new_state in _REVERSED_STATES
    if not forward and not reverse:
        return

    person_odoo_id = new["person_odoo_id"]
    days = _date_range(new["date_from"], new["date_to"])

    if forward:
        for d in days:
            _log_scheduler_move(
                person_odoo_id, d,
                from_bucket=None, to_bucket=TIME_OFF_KEY,
                reason="time_off_approved",
            )
    else:
        for d in days:
            _log_scheduler_move(
                person_odoo_id, d,
                from_bucket=TIME_OFF_KEY, to_bucket=_BUCKET_UNASSIGNED,
                reason="time_off_canceled",
            )

    _invalidate_balance(person_odoo_id)


def _date_range(start: date, end: date) -> list[date]:
    """Inclusive list of dates from ``start`` to ``end``."""
    out: list[date] = []
    cursor = start
    while cursor <= end:
        out.append(cursor)
        cursor = cursor + timedelta(days=1)
    return out


def _log_scheduler_move(
    person_odoo_id: int,
    schedule_date: date,
    from_bucket: str | None,
    to_bucket: str,
    reason: str,
) -> None:
    """Append one audit row to ``scheduler_moves``. ``to_bucket`` is
    required by the DDL; ``from_bucket`` may be NULL."""
    db.execute(
        "INSERT INTO scheduler_moves "
        "(person_odoo_id, schedule_date, from_bucket, to_bucket, reason) "
        "VALUES (%s, %s, %s, %s, %s)",
        (person_odoo_id, schedule_date, from_bucket, to_bucket, reason),
    )


def _invalidate_balance(person_odoo_id: int) -> None:
    """Drop cached balance rows for this person so the next kiosk
    wizard refetches from Odoo.

    Delegates to ``time_off_balances.invalidate``. Wrapped in try/except
    so the cascade doesn't fail if the ``time_off_balances`` table
    hasn't been provisioned yet (Phase 1 deployment ordering: cascade
    is wired before the balance refresher in some environments). A
    swallowed error here is acceptable — the worst case is a stale
    cached balance, which the periodic refresh will correct.
    """
    try:
        time_off_balances.invalidate(person_odoo_id)
    except Exception as e:  # noqa: BLE001
        _log.info(
            "balance cache invalidation skipped for person %s: %s",
            person_odoo_id, e,
        )
