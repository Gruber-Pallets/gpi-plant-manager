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
from datetime import datetime, time as _time, timedelta, timezone

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
        dt = dt.replace(tzinfo=timezone.utc)
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
