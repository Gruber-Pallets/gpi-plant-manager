"""Background reconciliation for timeclock_punches_log → Odoo hr.attendance.

The kiosk route handlers do their best to write to Odoo synchronously on
each punch. When Odoo is unreachable (transient network blip, maintenance
window), the punch is still recorded in timeclock_punches_log with
synced_to_odoo=FALSE and the kiosk shows the user a success page.

This module catches up. The app's background loop calls
`retry_unsynced_punches()` every 60s — it scans unsynced rows in
chronological order and redoes the Odoo write per row. On success the
flag flips to TRUE; on failure the sync_error column is updated but the
row stays unsynced for the next tick.

Duplicate risk: if the original Odoo write succeeded but the UPDATE that
flipped synced_to_odoo failed, the retry will create a duplicate
hr.attendance. The window for this is tiny (one UPDATE between two
successful network operations) and Phase 0 is Dale-only / low-volume —
duplicates are easy to spot in audit and the simpler retry logic is
worth the trade-off. Phase 1 can add timestamp-based dedup if it ever
matters in practice.
"""

from __future__ import annotations

import logging

from . import db, odoo_client

_log = logging.getLogger(__name__)

_BATCH_SIZE = 50


def retry_unsynced_punches() -> int:
    """Retry up to _BATCH_SIZE unsynced punches. Returns the number of
    rows successfully synced this tick.

    Order: chronological by occurred_at, then by id (so a transfer's
    transfer_out is retried before its transfer_in)."""
    rows = db.query(
        "SELECT id, person_odoo_id, action, wc_name, odoo_department_id, "
        "close_all_open_rows, "
        "COALESCE(rounded_at, occurred_at) AS occurred_at "
        "FROM timeclock_punches_log "
        "WHERE synced_to_odoo = FALSE "
        "ORDER BY occurred_at ASC, id ASC "
        "LIMIT %s",
        (_BATCH_SIZE,),
    )
    synced = 0
    for r in rows:
        try:
            _retry_one(r)
            synced += 1
        except Exception as e:  # noqa: BLE001 — record per-row failure and continue
            db.execute(
                "UPDATE timeclock_punches_log SET sync_error = %s WHERE id = %s",
                (str(e)[:500], r["id"]),
            )
            _log.info(
                "Kiosk sync still failing for row %s (%s): %s",
                r["id"], r["action"], e,
            )
    return synced


def _retry_one(r: dict) -> None:
    action = r["action"]
    person_odoo_id = r["person_odoo_id"]
    wc_name = r["wc_name"]
    odoo_department_id = r.get("odoo_department_id")
    ts = r["occurred_at"]

    if action in ("clock_in", "transfer_in"):
        # Self-correcting: if Odoo already shows an open attendance for this
        # person (a punch added by hand in Odoo, or a stale-window double-tap),
        # do NOT create a duplicate — adopt the open row so a later clock-out
        # closes the right one, and label its WC if the punch carries one.
        existing = odoo_client.get_current_attendance(person_odoo_id)
        if existing:
            # Active-live day-boundary punches carry no desired work center.
            # Adopting Luke's already-open row must not clear its location.
            if wc_name is not None:
                odoo_client.set_attendance_wc(existing["id"], wc_name)
            if odoo_department_id is not None:
                odoo_client.set_attendance_department_id(
                    existing["id"], int(odoo_department_id)
                )
            _mark_synced(r["id"], existing["id"])
            return
        att_id = odoo_client.clock_in(
            person_odoo_id,
            wc_name,
            ts,
            odoo_department_id=(
                int(odoo_department_id) if odoo_department_id is not None else None
            ),
        )
        _mark_synced(r["id"], att_id)
        return

    if action == "clock_out" and bool(r.get("close_all_open_rows")):
        # A day-end punch is authoritative even when bad overlapping rows
        # exist.  The facade closes them at this exact timestamp and performs
        # a fresh no-open-rows verification before returning.
        closed_ids = odoo_client.close_all_open_attendance_rows(person_odoo_id, ts)
        _mark_synced(r["id"], closed_ids[-1] if closed_ids else None)
        return

    if action == "clock_out":
        current = odoo_client.get_current_attendance(person_odoo_id)
        if current:
            odoo_client.clock_out(current["id"], ts)
            _mark_synced(r["id"], current["id"])
        else:
            _mark_synced(r["id"], None)
        return

    if action == "transfer_out":
        current = odoo_client.get_current_attendance(person_odoo_id)
        if current:
            odoo_client.clock_out(current["id"], ts)
            _mark_synced(r["id"], current["id"])
        else:
            # No open attendance to close — flag as a no-op success so we
            # stop retrying. Common case: the row was created by a transfer
            # whose transfer_in already synced, closing the prior record.
            _mark_synced(r["id"], None)
        return

    # Unknown action — flag synced to remove from queue, but note the issue.
    _log.warning("Unknown timeclock_punches_log.action=%r on row %s", action, r["id"])
    db.execute(
        "UPDATE timeclock_punches_log SET synced_to_odoo = TRUE, "
        "sync_error = %s, synced_at = now() WHERE id = %s",
        (f"unknown action: {action}", r["id"]),
    )


def _mark_synced(log_id: int, odoo_attendance_id: int | None) -> None:
    db.execute(
        "UPDATE timeclock_punches_log SET synced_to_odoo = TRUE, "
        "odoo_attendance_id = %s, sync_error = NULL, synced_at = now() "
        "WHERE id = %s",
        (odoo_attendance_id, log_id),
    )


def sync_one_by_id(log_id: int) -> None:
    """Sync a single log row to Odoo immediately. Called from kiosk route
    handlers via FastAPI BackgroundTasks right after the local DB write,
    so the user gets a fast response while the Odoo XML-RPC round-trip
    happens off the request path.

    On failure, the row stays at synced_to_odoo=FALSE with sync_error set;
    the periodic 60s sweep worker (`retry_unsynced_punches`) picks it up
    next tick. So this function is "best-effort fast path" — the safety
    net always runs."""
    rows = db.query(
        "SELECT id, person_odoo_id, action, wc_name, odoo_department_id, "
        "close_all_open_rows, "
        "COALESCE(rounded_at, occurred_at) AS occurred_at "
        "FROM timeclock_punches_log WHERE id = %s",
        (log_id,),
    )
    if not rows:
        _log.warning("sync_one_by_id called with unknown log_id=%s", log_id)
        return
    try:
        _retry_one(rows[0])
    except Exception as e:  # noqa: BLE001
        db.execute(
            "UPDATE timeclock_punches_log SET sync_error = %s WHERE id = %s",
            (str(e)[:500], log_id),
        )
        _log.info(
            "Immediate Odoo sync for kiosk log %s failed (will retry in 60s): %s",
            log_id, e,
        )
