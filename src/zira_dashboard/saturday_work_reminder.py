"""One-time optional-workday reminder claimed at the deadline-day clock-out.

Unlike the time-off reminder, an optional-workday commitment is a firm scheduling
promise.  The timestamp on the response makes this a claim: refreshing or
replaying a successful clock-out cannot display the reminder twice.
"""

from __future__ import annotations

import os
from datetime import date, datetime, time

from . import db
from .shift_config import SITE_TZ


def _fmt_time(value: time) -> str:
    fmt = "%#I:%M %p" if os.name == "nt" else "%-I:%M %p"
    return value.strftime(fmt)


def _day_label(day: date) -> str:
    return day.strftime("%A, %B %#d" if os.name == "nt" else "%A, %B %-d")


def claim_for_person(person_id: int, today: date, now: datetime) -> dict | None:
    """Atomically claim the deadline-day punch-out reminder for one volunteer.

    A row is eligible only while its optional workday remains active, it is
    still a committed response, and today is the persisted response-deadline day.
    Locking the response and writing its timestamp before returning makes a
    duplicate display impossible across concurrent/retried punch requests.
    """
    with db.cursor() as cur:
        cur.execute(
            "SELECT r.day, r.availability_start, r.availability_end, s.response_deadline, "
            "s.day_kind, s.event_name, wc.name AS wc_name "
            "FROM saturday_work_responses r "
            "JOIN saturday_recruitments s ON s.day = r.day "
            "LEFT JOIN schedule_assignments a "
            "  ON a.day = r.day AND a.person_id = r.person_id "
            "LEFT JOIN work_centers wc ON wc.id = a.wc_id "
            "WHERE r.person_id = %s "
            "  AND r.status = 'committed' "
            "  AND s.status <> 'cancelled' "
            "  AND r.punch_reminder_shown_at IS NULL "
            "  AND (s.response_deadline AT TIME ZONE %s)::date = %s "
            "ORDER BY r.day FOR UPDATE OF r",
            (person_id, SITE_TZ.key, today),
        )
        rows = cur.fetchall()
        if not rows:
            return None
        row = rows[0]
        deadline = row.get("response_deadline")
        if not isinstance(deadline, datetime) or deadline.astimezone(SITE_TZ).date() != today:
            return None
        cur.execute(
            "UPDATE saturday_work_responses SET punch_reminder_shown_at = %s, "
            "updated_at = %s WHERE day = %s AND person_id = %s "
            "AND punch_reminder_shown_at IS NULL",
            (now, now, row["day"], person_id),
        )
    is_holiday = str(row.get("day_kind") or "saturday") == "holiday"
    return {
        "day_label": _day_label(row["day"]),
        "hours": f"{_fmt_time(row['availability_start'])}–{_fmt_time(row['availability_end'])}",
        "work_center": row.get("wc_name"),
        "title_key": "Holiday work reminder" if is_holiday else "Saturday work reminder",
        "event_name": row.get("event_name") or ("Holiday" if is_holiday else "Saturday"),
    }
