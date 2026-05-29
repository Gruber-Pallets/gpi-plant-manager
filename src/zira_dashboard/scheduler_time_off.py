"""Scheduler-facing time-off entries sourced from the Odoo-backed
``time_off_requests`` mirror (replacing the StratusTime feed).

Returns the same dict shape ``routes/staffing.py`` already consumes:
``{name, hours, pay_type, time_range, derived, manual_absent, pending}``.
Full-day requests use ``hours=None`` (not partial); partial shapes use the
off-window span (``hour_to - hour_from``). ``pending`` flags requests not yet
approved in Odoo (``state != 'validate'``) so the template can style them.
"""
from __future__ import annotations

from datetime import date as _date

from . import db

# Requests in these states count as "happening" on the scheduler. 'validate'
# is approved; the rest are pending. Refused/cancelled/draft-cancel excluded.
_APPROVED = "validate"
_PENDING = ("draft", "confirm", "validate1")
_VISIBLE_STATES = (_APPROVED,) + _PENDING


def _fmt_hf(h: float) -> str:
    """Decimal-hour float -> 12-hour clock, e.g. 6.5 -> '6:30am'."""
    hh = int(h)
    mm = int(round((h - hh) * 60))
    suffix = "am" if hh < 12 else "pm"
    disp = hh if hh <= 12 else hh - 12
    if disp == 0:
        disp = 12
    return f"{disp}:{mm:02d}{suffix}"


def _rows_for_day(day: _date) -> list[dict]:
    return db.query(
        "SELECT p.name AS name, r.shape AS shape, "
        "r.hour_from AS hour_from, r.hour_to AS hour_to, r.state AS state, "
        "COALESCE(lt.name, 'Time Off') AS pay_type "
        "FROM time_off_requests r "
        "JOIN people p ON p.odoo_id = r.person_odoo_id "
        "LEFT JOIN leave_types_cache lt "
        "  ON lt.holiday_status_id = r.holiday_status_id "
        "WHERE r.state = ANY(%s) "
        "AND r.date_from <= %s AND r.date_to >= %s "
        "ORDER BY p.name",
        (list(_VISIBLE_STATES), day, day),
    )


def time_off_entries_for_day(day: _date) -> list[dict]:
    """List of scheduler time-off entries for ``day`` (approved + pending)."""
    out: list[dict] = []
    for r in _rows_for_day(day):
        is_full = r["shape"] == "full_day"
        if is_full:
            hours = None
            time_range = ""
        else:
            hf = float(r["hour_from"] or 0)
            ht = float(r["hour_to"] or 0)
            hours = round(ht - hf, 2)
            time_range = f"{_fmt_hf(hf)}–{_fmt_hf(ht)}"
        out.append({
            "name": r["name"],
            "hours": hours,
            "pay_type": r["pay_type"],
            "time_range": time_range,
            "derived": False,
            "manual_absent": False,
            "pending": r["state"] != _APPROVED,
        })
    return out


def full_day_off_names(day: _date) -> set[str]:
    """Names of people who are off the WHOLE day (full_day shape). Partial-day
    people are intentionally excluded so they stay on the schedulable roster
    with a badge instead of disappearing."""
    return {
        r["name"] for r in _rows_for_day(day) if r["shape"] == "full_day"
    }
