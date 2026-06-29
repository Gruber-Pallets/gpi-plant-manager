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

import logging
import os
from datetime import date, datetime, timezone
from typing import Any

from . import db, shift_config

_log = logging.getLogger(__name__)

_NOTIFY_ENV = "KIOSK_TIME_OFF_NOTIFY_ENABLED"

# Odoo/local state a request lands in -> the notification we raise.
_RESOLUTION_KIND = {
    "validate": "time_off_approved",
    "refuse": "time_off_denied",
    "cancel": "time_off_cancelled",
}


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


def span_label(row: dict[str, Any]) -> str:
    """Formatted date span for a stored notification row, from its snapshotted
    ``leave_date_from``/``leave_date_to``. The interstitial passes this into
    ``t()`` as the ``{span}`` value so the message localizes for bilingual
    employees while the dates stay shared across both languages. Empty string
    if the row has no start date."""
    df = row.get("leave_date_from")
    if df is None:
        return ""
    return _date_span_label(df, row.get("leave_date_to"))


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


def maybe_notify_resolution(
    old: dict[str, Any], new: dict[str, Any], today: date | None = None,
) -> None:
    """Raise a resolution notification when a request transitions into an
    approved/denied/cancelled state. Called from ``time_off_sync._upsert_one``
    on every observed state change and on insert-already-validated.

    Suppressed when:
      - the feature is off,
      - the new state isn't a resolution,
      - the change is the employee's own cancellation (local prior state
        ``draft_cancel`` — Odoo records that as a refuse/cancel, which is not
        a denial),
      - the leave is entirely in the past (date_to < today).
    """
    try:
        if not notifications_enabled():
            return
        kind = _RESOLUTION_KIND.get(new.get("state"))
        if kind is None:
            return
        if old.get("state") == "draft_cancel":
            return
        date_to = new.get("date_to")
        today = today or _plant_today()
        if date_to is None or date_to < today:
            return
        create_time_off_notification(new["person_odoo_id"], kind, new)
    except Exception:  # best-effort: a notification must never break the poll
        _log.warning(
            "resolution notification failed for leave %s",
            (new or {}).get("odoo_leave_id"), exc_info=True)


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
