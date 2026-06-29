"""Day-before time-off reminder, computed live at clock-out.

When an employee clocks out on the last working day before approved time
off, the clock-out confirmation shows a "time off tomorrow" card. Nothing
is stored — this is recomputed on each clock-out. Only the real clock-out
endpoint calls this; transfers and auto-lunch sign-outs use other code
paths, so they never trigger it.

"Next working day" uses a simple weekend-skip rule (this is a Mon–Fri
plant). Per-person Odoo working calendars aren't cleanly available without
extra Odoo calls; this covers the plant's schedule and keeps the clock-out
hot path DB/Odoo-cheap.
"""
from __future__ import annotations

import os
from datetime import date, time as _time, timedelta
from typing import Any

from . import db
from .employee_notifications import notifications_enabled


def next_working_day(d: date) -> date:
    """The next Mon–Fri after ``d`` (skips Sat=5 / Sun=6)."""
    nxt = d + timedelta(days=1)
    while nxt.weekday() >= 5:
        nxt += timedelta(days=1)
    return nxt


def _fmt_hour(h: float | None) -> str:
    """0.0–24.0 float hour -> '9:30 AM'. None -> ''."""
    if h is None:
        return ""
    h = float(h)
    hh = int(h)
    mm = int(round((h - hh) * 60))
    if mm == 60:
        hh, mm = hh + 1, 0
    t = _time(hh % 24, mm)
    fmt = "%#I:%M %p" if os.name == "nt" else "%-I:%M %p"
    return t.strftime(fmt)


def _day_label(target: date, today: date) -> str:
    wd = target.strftime("%A")
    md = target.strftime("%b %#d") if os.name == "nt" else target.strftime("%b %-d")
    label = f"{wd}, {md}"
    if target == today + timedelta(days=1):
        return f"tomorrow ({label})"
    return label


def _body_key(shape: str | None, hf: str, ht: str) -> str:
    """The English message template (also the `t()` glossary key) for a
    partial-day reminder, chosen by shape + which hour bounds we have.

    In practice an approved (state='validate') row reaches here as
    'midday_gap': the poller's _mirror_shape_and_hours collapses partial
    leaves to that shape on sync. The late_arrival/early_leave arms are kept
    as forward-compatible handling in case that mapping ever preserves the
    finer shapes.
    """
    if shape == "late_arrival":
        return ("Heads up — {day}, you're not due in until {ht} (approved)."
                if ht else "Heads up — {day}, you have a late arrival (approved).")
    if shape == "early_leave":
        return ("Heads up — {day}, you can leave at {hf} (approved)."
                if hf else "Heads up — {day}, you have an early leave (approved).")
    # midday_gap (and any partial we can't classify)
    return ("Heads up — {day}, you're off from {hf} to {ht} (approved)."
            if hf and ht else "Heads up — {day}, you have partial time off (approved).")


def _render_reminder(row: dict[str, Any], target: date, today: date) -> dict:
    """Return the structured pieces the success template renders via ``t()``.

    Rendering happens in the template (not here) so the message can be shown
    bilingually for Spanish-speaking employees, following the kiosk's
    convention: the sentence frame is translated via ``t()`` while the
    interpolated date/hour values (``day``/``hf``/``ht``) are shared across
    both languages. ``body_key`` is the English template string AND the
    glossary key; the template calls ``t(body_key, day=…, hf=…, ht=…)``.
    """
    day = _day_label(target, today)
    if row.get("shape") == "full_day":
        return {
            "full_day": True,
            "title_key": "Time off reminder",
            "body_key": "Heads up — you have approved time off {day}. Enjoy!",
            "day": day, "hf": "", "ht": "",
        }
    hf = _fmt_hour(row.get("hour_from"))
    ht = _fmt_hour(row.get("hour_to"))
    return {
        "full_day": False,
        "title_key": "Time off reminder",
        "body_key": _body_key(row.get("shape"), hf, ht),
        "day": day, "hf": hf, "ht": ht,
    }


def reminder_for_person(person_odoo_id: int, today: date) -> dict | None:
    """Return the structured reminder dict (``full_day``/``title_key``/
    ``body_key``/``day``/``hf``/``ht``) if this person has approved time off
    (full or partial) on their next working day, else None. The success
    template renders it via ``t()`` so it localizes for bilingual employees.
    """
    if not notifications_enabled():
        return None
    target = next_working_day(today)
    rows = db.query(
        "SELECT shape, date_from, date_to, hour_from, hour_to "
        "FROM time_off_requests "
        "WHERE person_odoo_id = %s AND state = 'validate' "
        "AND date_from <= %s AND date_to >= %s "
        "ORDER BY date_from LIMIT 1",
        (person_odoo_id, target, target),
    )
    if not rows:
        return None
    return _render_reminder(rows[0], target, today)
