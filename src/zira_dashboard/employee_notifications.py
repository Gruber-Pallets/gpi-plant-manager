"""Employee-facing kiosk notifications.

One row in ``employee_notifications`` == one thing to tell an employee at
their next time-clock sign-in. The only source today is time-off
resolutions (approved / denied / cancelled). ``acknowledged_at`` records
the "Got it" tap so a notification never shows twice.

Generation (``maybe_notify_resolution``) rides the time-off poller's
state-change detection in ``time_off_sync._upsert_one`` — see that module.
Display is the kiosk sign-in interstitial in ``routes/timeclock.py``.
"""
from __future__ import annotations

import os
from datetime import date, datetime, timezone
from typing import Any

from . import db, shift_config

_NOTIFY_ENV = "KIOSK_TIME_OFF_NOTIFY_ENABLED"


def notifications_enabled() -> bool:
    """Kill-switch. Default ON; set KIOSK_TIME_OFF_NOTIFY_ENABLED=0 to disable
    both the resolution popups and the day-before reminder without touching
    the rest of the time-off feature."""
    return os.environ.get(_NOTIFY_ENV, "1").strip().lower() not in (
        "0", "false", "no",
    )


def _plant_today() -> date:
    return datetime.now(timezone.utc).astimezone(shift_config.SITE_TZ).date()


def _md(d: date) -> str:
    """'Jul 1' — no leading zero on the day. Windows needs %#d for that."""
    return d.strftime("%b %#d") if os.name == "nt" else d.strftime("%b %-d")


def _date_span_label(date_from: date, date_to: date | None) -> str:
    if date_to and date_to != date_from:
        return f"{_md(date_from)} – {_md(date_to)}"
    return _md(date_from)


def _render(kind: str, req: dict[str, Any]) -> tuple[str, str]:
    """Return (title, body) for a resolution notification."""
    span = _date_span_label(req["date_from"], req.get("date_to"))
    if kind == "time_off_approved":
        return ("Time off approved",
                f"Your time off for {span} was approved. ✅")
    if kind == "time_off_denied":
        return ("Time off denied",
                f"Your time off request for {span} was denied. ❌ "
                "See a supervisor if you have questions.")
    return ("Time off cancelled",
            f"Your approved time off for {span} was cancelled. ⚠️ "
            "See a supervisor if you have questions.")


def create_time_off_notification(
    person_odoo_id: int, kind: str, req: dict[str, Any],
) -> None:
    """Insert one notification. The unique (time_off_request_id, kind) index
    + ON CONFLICT DO NOTHING make this idempotent if a poll re-processes the
    same transition."""
    title, body = _render(kind, req)
    db.execute(
        "INSERT INTO employee_notifications "
        "(person_odoo_id, kind, time_off_request_id, odoo_leave_id, "
        " title, body, leave_date_from, leave_date_to) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) "
        "ON CONFLICT (time_off_request_id, kind) DO NOTHING",
        (person_odoo_id, kind, req.get("id"), req.get("odoo_leave_id"),
         title, body, req.get("date_from"), req.get("date_to")),
    )


def has_unacknowledged(person_odoo_id: int) -> bool:
    rows = db.query(
        "SELECT 1 FROM employee_notifications "
        "WHERE person_odoo_id = %s AND acknowledged_at IS NULL LIMIT 1",
        (person_odoo_id,),
    )
    return bool(rows)


def list_unacknowledged(person_odoo_id: int) -> list[dict]:
    return db.query(
        "SELECT id, kind, title, body, leave_date_from, leave_date_to, "
        "created_at FROM employee_notifications "
        "WHERE person_odoo_id = %s AND acknowledged_at IS NULL "
        "ORDER BY created_at",
        (person_odoo_id,),
    )


def acknowledge_all(person_odoo_id: int) -> None:
    """Mark every unacknowledged notification for this person as seen. The
    single 'Got it' button clears the whole stack; person-scoped so a stale
    token can only ever clear its own person's rows."""
    db.execute(
        "UPDATE employee_notifications SET acknowledged_at = now() "
        "WHERE person_odoo_id = %s AND acknowledged_at IS NULL",
        (person_odoo_id,),
    )
