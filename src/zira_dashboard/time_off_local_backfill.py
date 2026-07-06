"""Replay locally-recorded absences into Odoo once Odoo will accept them.

A ``time_off_requests`` row with ``local_record = TRUE`` exists because Odoo
refused to validate the leave: the requested day(s) had zero working hours —
either a holiday record covered them (e.g. the plant worked the observed 4th
of July while Odoo said "closed") or the employee's Working Schedule was
missing the weekday. The approve fallback recorded the absence here and
settled the Odoo copy as refused.

This reconciler runs on an hourly warmer and, for each approved local
record, predicts whether Odoo would accept the leave *now*: at least one day
of the span is a covered weekday not blocked by a holiday record that
applies to this employee (company-wide, or scoped to their calendar). Only
when the prediction passes does it touch Odoo: reset the refused copy to
draft, then confirm + approve. On success a *guarded* adoption hands the row
back to the poller (``local_record = FALSE``) — and if a kiosk cancel or
manager deny settled the row mid-replay, the guard misses, the human action
wins, and the Odoo copy is re-refused.

Bounded by design: prediction skips cost zero RPCs and defer the row's next
check (rotating it out of the candidate window so permanently-local rows
can't starve replayable ones); real failures re-refuse the Odoo copy and
back off exponentially (capped at a week), so a persistently-rejected leave
can never churn Odoo's workflow hourly forever. There is deliberately NO
recreate path: if HR deleted the refused copy, they touched it on purpose —
the absence stays app-only.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from typing import Any

from . import db, employee_notifications, odoo_client
from .shift_config import SITE_TZ

_log = logging.getLogger(__name__)

# Rows attempted per tick. Local records are rare; the cap bounds the Odoo
# RPC fan-out if a backlog ever accumulates.
_ATTEMPT_LIMIT = 10

# Recheck cadences (hours). Prediction skips are cheap but rotate the row
# out of the candidate window; a deleted Odoo copy is a deliberate HR action
# so it gets the slowest recheck; real failures back off exponentially up to
# the same weekly cap.
_SKIP_RECHECK_HOURS = 6
_DELETED_RECHECK_HOURS = 168
_MAX_BACKOFF_HOURS = 168


def _defer(row_id: int, hours: float, attempts: int | None = None) -> None:
    """Push the row's next backfill check ``hours`` out; optionally record
    the new failure-attempt count."""
    if attempts is None:
        db.execute(
            "UPDATE time_off_requests SET "
            "backfill_next_at = now() + (%s * interval '1 hour'), "
            "updated_at = now() WHERE id = %s",
            (hours, row_id),
        )
    else:
        db.execute(
            "UPDATE time_off_requests SET backfill_attempts = %s, "
            "backfill_next_at = now() + (%s * interval '1 hour'), "
            "updated_at = now() WHERE id = %s",
            (attempts, hours, row_id),
        )


def _holiday_scopes(start_d: date, end_d: date) -> list[tuple[int | None, set[date]]]:
    """Holiday records as (calendar scope, plant-local dates) pairs.

    Scope None = company-wide (blocks everyone); an int scopes the record to
    one working schedule. Odoo stores the bounds as naive UTC datetimes; a
    Central midnight-to-midnight holiday spills into the next UTC date, so
    the bounds are converted to plant time before taking the date range —
    otherwise every day-after-a-holiday absence would be frozen forever.
    Malformed rows (Odoo returns False for null datetimes) are skipped, not
    fatal: worst case the attempt runs and Odoo's own validation refuses it.
    """
    out: list[tuple[int | None, set[date]]] = []
    for h in odoo_client.fetch_public_holidays(start_d, end_d):
        try:
            df = datetime.fromisoformat(str(h["date_from"]))
            dt = datetime.fromisoformat(str(h["date_to"]))
        except (KeyError, TypeError, ValueError):
            _log.warning("skipping malformed holiday record %s", h.get("id"))
            continue
        first = df.replace(tzinfo=timezone.utc).astimezone(SITE_TZ).date()
        last = dt.replace(tzinfo=timezone.utc).astimezone(SITE_TZ).date()
        days: set[date] = set()
        d = first
        while d <= last:
            days.add(d)
            d += timedelta(days=1)
        # unwrap_m2o passes Odoo's False (unset) through — normalize to
        # None so "company-wide" is a single value.
        out.append((odoo_client.unwrap_m2o(h.get("calendar_id")) or None, days))
    return out


def _has_working_day(row: dict[str, Any], covered_weekdays: set[int],
                     holidays: list[tuple[int | None, set[date]]],
                     cal_id: int | None) -> bool:
    """True when at least one day of the span has working hours — Odoo's
    validation only needs ``number_of_days > 0``, not full coverage."""
    blocked: set[date] = set()
    for scope, days in holidays:
        if scope is None or scope == cal_id:
            blocked |= days
    d = row["date_from"]
    while d <= row["date_to"]:
        if d.weekday() in covered_weekdays and d not in blocked:
            return True
        d += timedelta(days=1)
    return False


def _adopt(row: dict[str, Any]) -> bool:
    """Guarded hand-back: only an approved, still-flagged row is adopted
    (``WHERE local_record AND state = 'validate'``). A kiosk cancel or
    manager deny that settled the row mid-replay makes this miss — the
    caller must then honor the human action instead. On success the
    denied-popup suppression is retired (genuine Odoo-side resolutions must
    notify again) and a pre-acknowledged 'approved' suppression is armed so
    a poll tick that photographed a stale mid-replay state can't fire a
    spurious approved popup."""
    adopted = db.query(
        "UPDATE time_off_requests SET local_record = FALSE, "
        "synced_to_odoo = TRUE, sync_error = NULL, backfill_next_at = NULL, "
        "last_pushed_at = now(), updated_at = now() "
        "WHERE id = %s AND local_record AND state = 'validate' RETURNING id",
        (row["id"],),
    )
    if not adopted:
        return False
    try:
        employee_notifications.suppress_resolution(
            row["person_odoo_id"], row, kind="time_off_approved")
        employee_notifications.unsuppress_resolution(
            row["id"], "time_off_denied")
    except Exception:  # noqa: BLE001 — cosmetic relative to the adoption
        _log.warning("notification bookkeeping failed for request %s",
                     row["id"], exc_info=True)
    return True


def _attempt(row: dict[str, Any]) -> bool:
    """Replay one local record through Odoo. Returns True when Odoo ended
    up holding the validated leave and the row was handed back."""
    leave_id = row.get("odoo_leave_id")
    state = (odoo_client.fetch_leave_state(int(leave_id))
             if leave_id is not None else None)
    if state == "validate":
        # HR already fixed the data and re-approved it directly in Odoo.
        if _adopt(row):
            _log.warning("adopted HR-validated leave %s for request %s",
                         leave_id, row["id"])
            return True
        # Settled locally mid-flight; never refuse a leave HR just approved.
        _log.warning("request %s settled locally while adopting leave %s — "
                     "left as-is", row["id"], leave_id)
        return False
    if state is None:
        # HR deleted the refused copy — a deliberate action; stay app-only.
        _log.warning("request %s: Odoo copy (leave %s) is gone; keeping the "
                     "absence app-only", row["id"], leave_id)
        _defer(row["id"], _DELETED_RECHECK_HOURS)
        return False
    try:
        if state in ("refuse", "cancel"):
            odoo_client.draft_leave(int(leave_id))
        final = odoo_client.approve_leave(int(leave_id))
        if final != "validate":
            raise RuntimeError(f"leave {leave_id} ended in state {final}")
    except Exception as e:  # noqa: BLE001 — re-settle and back off
        _log.info("backfill attempt failed for request %s (leave %s): %s",
                  row["id"], leave_id, e)
        try:
            odoo_client.refuse_leave(int(leave_id))
        except Exception:  # noqa: BLE001
            _log.warning("could not re-refuse leave %s after failed "
                         "backfill", leave_id, exc_info=True)
        attempts = int(row.get("backfill_attempts") or 0) + 1
        _defer(row["id"], min(2 ** attempts, _MAX_BACKOFF_HOURS), attempts)
        return False
    if not _adopt(row):
        # Cancel/deny won the race: their local settle stands; undo ours.
        _log.warning("request %s settled locally mid-replay — re-refusing "
                     "leave %s", row["id"], leave_id)
        try:
            odoo_client.refuse_leave(int(leave_id))
        except Exception:  # noqa: BLE001
            _log.warning("could not re-refuse leave %s after lost race",
                         leave_id, exc_info=True)
        return False
    _log.warning("backfilled local absence into Odoo: request %s -> "
                 "leave %s (person %s, %s..%s)", row["id"], leave_id,
                 row["person_odoo_id"], row["date_from"], row["date_to"])
    return True


def run_once() -> int:
    """One reconciler pass. Returns the number of rows backfilled."""
    rows = db.query(
        "SELECT id, person_odoo_id, shape, holiday_status_id, "
        "date_from, date_to, hour_from, hour_to, note, odoo_leave_id, "
        "backfill_attempts "
        "FROM time_off_requests "
        "WHERE local_record AND state = 'validate' "
        "AND (backfill_next_at IS NULL OR backfill_next_at <= now()) "
        "ORDER BY date_from, id LIMIT %s",
        (_ATTEMPT_LIMIT,),
    )
    if not rows:
        return 0
    employees = odoo_client.fetch_employees()
    cal_by_emp = {
        e["id"]: odoo_client.unwrap_m2o(e.get("resource_calendar_id"))
        for e in employees
    }
    cal_ids = sorted({c for c in cal_by_emp.values() if c})
    hours = odoo_client.fetch_calendar_hours(cal_ids) if cal_ids else {}
    holidays = _holiday_scopes(
        min(r["date_from"] for r in rows),
        max(r["date_to"] for r in rows),
    )
    done = 0
    for row in rows:
        cal_id = cal_by_emp.get(row["person_odoo_id"])
        covered = {int(k) for k in (hours.get(cal_id) or {})}
        if not _has_working_day(row, covered, holidays, cal_id):
            # Still unacceptable to Odoo (or employee archived) — recheck
            # later; the deferral rotates the row out of the LIMIT window.
            _defer(row["id"], _SKIP_RECHECK_HOURS)
            continue
        if _attempt(row):
            done += 1
    return done
