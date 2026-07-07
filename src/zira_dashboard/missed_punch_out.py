"""Missed-punch-out alert: detect attendances left open past their day,
shape the badge/modal rows, and record/resolve flags.

The warmer (app._tick_missed_punch_out -> run_close) closes overdue Odoo
attendances at the midnight ending their check-in day and records each here;
the badge endpoint then does local reads only — no Odoo on the hot path.
Mirrors missing_wc.py.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, time as _time, timedelta, UTC

from .shift_config import SITE_TZ

_log = logging.getLogger(__name__)


def _parse_check_in(value):
    """ISO-8601 string (or datetime) -> tz-aware datetime, or None on bad input."""
    if not value:
        return None
    if isinstance(value, str):
        try:
            dt = datetime.fromisoformat(value)
        except (TypeError, ValueError):
            return None
    else:
        dt = value
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


def overdue_closures(open_rows: list[dict], today) -> list[dict]:
    """Pure: open attendance rows + today's site-local date -> the ones whose
    check-in (site-local) was on a day BEFORE today, each with the midnight
    ending its check-in day. Rows checked in today (normal in-progress shifts)
    and rows with bad/missing check-in are skipped."""
    out: list[dict] = []
    for r in open_rows:
        dt = _parse_check_in(r.get("check_in"))
        if dt is None:
            continue
        local_date = dt.astimezone(SITE_TZ).date()
        if local_date >= today:
            continue
        midnight = datetime.combine(local_date + timedelta(days=1), _time.min,
                                    tzinfo=SITE_TZ)
        out.append({
            "att_id": r.get("att_id"),
            "employee_odoo_id": r.get("employee_odoo_id"),
            "check_in": r.get("check_in"),
            "midnight": midnight,
        })
    return out


def _check_in_label(value) -> str:
    """ISO string or datetime -> 'H:MM AM/PM Ddd Mon D' in site-local, '' on bad input."""
    dt = _parse_check_in(value)
    if dt is None:
        return ""
    local = dt.astimezone(SITE_TZ)
    fmt = "%#I:%M %p %a %b %#d" if os.name == "nt" else "%-I:%M %p %a %b %-d"
    return local.strftime(fmt)


def _as_int(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _names_for(employee_odoo_ids) -> dict[int, str]:
    ids = sorted({
        employee_id
        for eid in employee_odoo_ids
        if (employee_id := _as_int(eid)) is not None
    })
    if not ids:
        return {}
    from . import db
    rows = db.query(
        "SELECT odoo_id, name FROM people WHERE odoo_id = ANY(%s)",
        (ids,),
    )
    return {int(r["odoo_id"]): r["name"] for r in rows if r.get("name")}


def _name_for(employee_odoo_id) -> str:
    """Person's name from `people`, or '#<odoo_id>' when not mapped."""
    names = _names_for([employee_odoo_id])
    if names:
        employee_id = _as_int(employee_odoo_id)
        if employee_id is not None:
            return names[employee_id]
    return f"#{employee_odoo_id}"


def record_close(attendance_id, employee_odoo_id, check_in, auto_closed_at,
                 name: str | None = None) -> None:
    """Flag an attendance auto-closed at midnight. Idempotent (PK conflict ->
    no-op), so re-running the warmer never duplicates a row. `check_in` may be
    an ISO string or datetime; `auto_closed_at` is the midnight datetime."""
    from . import db
    db.execute(
        "INSERT INTO missed_punch_out "
        "(attendance_id, employee_odoo_id, name, check_in, auto_closed_at) "
        "VALUES (%s, %s, %s, %s, %s) "
        "ON CONFLICT (attendance_id) DO NOTHING",
        (int(attendance_id), int(employee_odoo_id),
         name or _name_for(employee_odoo_id), check_in, auto_closed_at),
    )


def _shape_row(r: dict) -> dict:
    ci = r.get("check_in")
    check_in_date = None
    if hasattr(ci, "astimezone"):
        check_in_date = ci.astimezone(SITE_TZ).date().isoformat()
    return {
        "attendance_id": r.get("attendance_id"),
        "employee_odoo_id": r.get("employee_odoo_id"),
        "name": r.get("name") or f"#{r.get('employee_odoo_id')}",
        "check_in_label": _check_in_label(ci),
        "check_in_date": check_in_date,
    }


def current_rows() -> list[dict]:
    """Badge/modal payload: unresolved flags, newest first. All local reads."""
    from . import db
    rows = db.query(
        "SELECT attendance_id, employee_odoo_id, name, check_in, auto_closed_at "
        "FROM missed_punch_out WHERE resolved_at IS NULL "
        "ORDER BY check_in DESC")
    return [_shape_row(r) for r in rows]


def get_unresolved(attendance_id) -> dict | None:
    """The unresolved flag row (carries check_in + auto_closed_at for the route's
    bounds check), or None if unknown or already resolved."""
    from . import db
    rows = db.query(
        "SELECT attendance_id, employee_odoo_id, name, check_in, auto_closed_at "
        "FROM missed_punch_out WHERE attendance_id = %s AND resolved_at IS NULL",
        (int(attendance_id),))
    return rows[0] if rows else None


def correct(attendance_id, corrected_ts) -> None:
    """Mark a flag resolved with the manager-entered punch-out time. Guarded on
    `resolved_at IS NULL` so a double-submit can't overwrite an already-recorded
    correction (the row's resolved_at/corrected_at stay as first set)."""
    from . import db
    db.execute(
        "UPDATE missed_punch_out SET corrected_at = %s, resolved_at = now() "
        "WHERE attendance_id = %s AND resolved_at IS NULL",
        (corrected_ts, int(attendance_id)),
    )


def run_close(today) -> int:
    """One sweep: close every open attendance whose check-in was on a prior
    day at that day's midnight, and flag each. `today` is the site-local date.
    Returns how many were closed. Owns the Odoo read + writes (off the hot
    path; called by the warmer). One bad record never kills the sweep."""
    from . import odoo_client
    open_rows = odoo_client.fetch_open_attendances()
    closures = overdue_closures(open_rows, today)
    try:
        names = _names_for(c["employee_odoo_id"] for c in closures)
    except Exception as e:  # noqa: BLE001 -- name prefetch must not abort closes
        _log.warning("missed-punch name prefetch failed: %s", e)
        names = {}
    n = 0
    for c in closures:
        try:
            odoo_client.clock_out(c["att_id"], c["midnight"], mode="auto_check_out")
            employee_id = _as_int(c.get("employee_odoo_id"))
            record_close(c["att_id"], c["employee_odoo_id"],
                         c["check_in"], c["midnight"],
                         name=names.get(employee_id) if employee_id is not None else None)
            n += 1
        except Exception as e:  # noqa: BLE001 — one record never kills the sweep
            _log.warning("missed-punch close failed for att %s: %s",
                         c.get("att_id"), e)
    return n
