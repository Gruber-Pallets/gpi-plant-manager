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
from dataclasses import dataclass
from datetime import date, datetime, UTC
from decimal import Decimal
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


@dataclass(frozen=True)
class AnniversaryPtoNotice:
    person_odoo_id: int
    anniversary_date: date
    balance_amount: Decimal
    balance_unit: str


def notifications_enabled() -> bool:
    """Kill-switch. Default ON; set KIOSK_TIME_OFF_NOTIFY_ENABLED=0 to disable
    both the resolution popups and the day-before reminder without touching
    the rest of the time-off feature."""
    return os.environ.get(_NOTIFY_ENV, "1").strip().lower() not in (
        "0",
        "false",
        "no",
    )


def _plant_today() -> date:
    return datetime.now(UTC).astimezone(shift_config.SITE_TZ).date()


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
        return ("Time off approved", f"Your time off for {span} was approved. ✅")
    if kind == "time_off_denied":
        return (
            "Time off denied",
            f"Your time off request for {span} was denied. ❌ "
            "See a supervisor if you have questions.",
        )
    return (
        "Time off cancelled",
        f"Your approved time off for {span} was cancelled. ⚠️ "
        "See a supervisor if you have questions.",
    )


def create_time_off_notification(
    person_odoo_id: int,
    kind: str,
    req: dict[str, Any],
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
        (
            person_odoo_id,
            kind,
            req.get("id"),
            req.get("odoo_leave_id"),
            title,
            body,
            req.get("date_from"),
            req.get("date_to"),
        ),
    )


def create_saturday_cancelled(
    person_odoo_id: int,
    day: date,
    *,
    day_kind: str = "saturday",
    event_name: str | None = None,
) -> None:
    """Notify a committed volunteer that their optional workday was cancelled.

    The partial unique index on ``(person_odoo_id, saturday_day, kind)`` keeps
    a retry of the manager cancellation endpoint from creating another kiosk
    card for the same person and date.
    """
    if day_kind == "holiday":
        title = "Holiday work cancelled"
        body = f"{event_name or 'Holiday'} work was cancelled. Do not report to work."
    else:
        title = "Saturday work cancelled"
        body = "Saturday work was cancelled. Do not report to work."
    db.execute(
        "INSERT INTO employee_notifications "
        "(person_odoo_id, kind, saturday_day, title, body) "
        "VALUES (%s, %s, %s, %s, %s) "
        "ON CONFLICT (person_odoo_id, saturday_day, kind) "
        "WHERE saturday_day IS NOT NULL DO NOTHING",
        (person_odoo_id, "saturday_work_cancelled", day, title, body),
    )


def reconcile_anniversary_pto(notices: tuple[AnniversaryPtoNotice, ...]) -> None:
    """Make unpresented anniversary reminders match the latest fresh balances.

    A row becomes an audit snapshot as soon as the kiosk presents it, so every
    cleanup and update predicate deliberately excludes presented or acknowledged
    rows.
    """
    expected = {(n.person_odoo_id, n.anniversary_date): n for n in notices}
    with db.cursor() as cursor:
        cursor.execute(
            "SELECT id, person_odoo_id, anniversary_date "
            "FROM employee_notifications "
            "WHERE kind = 'anniversary_pto_reminder' "
            "AND presented_at IS NULL AND acknowledged_at IS NULL FOR UPDATE"
        )
        for row in cursor.fetchall():
            key = (row["person_odoo_id"], row["anniversary_date"])
            if key not in expected:
                cursor.execute(
                    "DELETE FROM employee_notifications WHERE id = %s "
                    "AND kind = 'anniversary_pto_reminder' "
                    "AND presented_at IS NULL AND acknowledged_at IS NULL",
                    (row["id"],),
                )

        for notice in notices:
            amount = format(notice.balance_amount.normalize(), "f")
            title = "Your work anniversary is coming up"
            body = (
                f"Your work anniversary is {_md(notice.anniversary_date)}. "
                f"You have {amount} {notice.balance_unit} of unused Paid Time Off. "
                "Please plan to use your time or talk with your supervisor if you "
                "have questions."
            )
            cursor.execute(
                "INSERT INTO employee_notifications "
                "(person_odoo_id, kind, title, body, anniversary_date, "
                "balance_amount, balance_unit) VALUES (%s, %s, %s, %s, %s, %s, %s) "
                "ON CONFLICT (person_odoo_id, anniversary_date, kind) "
                "WHERE anniversary_date IS NOT NULL DO UPDATE SET "
                "title = EXCLUDED.title, body = EXCLUDED.body, "
                "balance_amount = EXCLUDED.balance_amount, "
                "balance_unit = EXCLUDED.balance_unit "
                "WHERE employee_notifications.presented_at IS NULL "
                "AND employee_notifications.acknowledged_at IS NULL",
                (
                    notice.person_odoo_id,
                    "anniversary_pto_reminder",
                    title,
                    body,
                    notice.anniversary_date,
                    notice.balance_amount,
                    notice.balance_unit,
                ),
            )


def holiday_cancellation_event_name(row: dict[str, Any]) -> str | None:
    """Recover the escaped-at-render holiday name from stored cancellation copy."""
    if row.get("title") != "Holiday work cancelled":
        return None
    body = str(row.get("body") or "")
    suffix = " work was cancelled. Do not report to work."
    if body.endswith(suffix):
        return body[: -len(suffix)] or "Holiday"
    return "Holiday"


def suppress_resolution(
    person_odoo_id: int,
    req: dict[str, Any],
    kind: str,
) -> None:
    """Pre-insert an already-acknowledged notification of ``kind`` so the
    unique (time_off_request_id, kind) index swallows any future
    poller-generated popup of that kind for this request.

    Used when a route settles a leave in Odoo in a way the employee must
    NOT be told about — e.g. the local-record approve fallback refuses the
    Odoo copy of a leave it just approved locally; the poller observing
    that refuse must not raise a "denied" popup. Deliberately ignores the
    kill-switch: the row must exist even while popups are disabled, in
    case the feature is re-enabled later.

    ON CONFLICT DO UPDATE (not DO NOTHING): a stale UNacknowledged popup
    of the same kind may already exist — suppression must neutralize it
    too, while leaving an already-acknowledged timestamp untouched."""
    title, body = _render(kind, req)
    db.execute(
        "INSERT INTO employee_notifications "
        "(person_odoo_id, kind, time_off_request_id, odoo_leave_id, "
        " title, body, leave_date_from, leave_date_to, acknowledged_at) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, now()) "
        "ON CONFLICT (time_off_request_id, kind) DO UPDATE "
        "SET acknowledged_at = "
        "COALESCE(employee_notifications.acknowledged_at, now())",
        (
            person_odoo_id,
            kind,
            req.get("id"),
            req.get("odoo_leave_id"),
            title,
            body,
            req.get("date_from"),
            req.get("date_to"),
        ),
    )


def unsuppress_resolution(time_off_request_id: int, kind: str) -> None:
    """Undo ``suppress_resolution`` when the operation it protected was
    aborted: delete the pre-acknowledged row so a future genuine
    resolution of this kind can still notify. Only acknowledged rows are
    touched — a live (unacknowledged) popup is never deleted."""
    db.execute(
        "DELETE FROM employee_notifications "
        "WHERE time_off_request_id = %s AND kind = %s "
        "AND acknowledged_at IS NOT NULL",
        (time_off_request_id, kind),
    )


def maybe_notify_resolution(
    old: dict[str, Any],
    new: dict[str, Any],
    today: date | None = None,
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
            (new or {}).get("odoo_leave_id"),
            exc_info=True,
        )


def has_unacknowledged(person_odoo_id: int) -> bool:
    rows = db.query(
        "SELECT 1 FROM employee_notifications "
        "WHERE person_odoo_id = %s AND acknowledged_at IS NULL LIMIT 1",
        (person_odoo_id,),
    )
    return bool(rows)


def list_unacknowledged(person_odoo_id: int) -> list[dict]:
    rows = db.query(
        "SELECT id, kind, title, body, leave_date_from, leave_date_to, saturday_day, "
        "anniversary_date, balance_amount, balance_unit, created_at, presented_at "
        "FROM employee_notifications "
        "WHERE person_odoo_id = %s AND acknowledged_at IS NULL "
        "ORDER BY created_at",
        (person_odoo_id,),
    )
    ids = [row["id"] for row in rows]
    if ids:
        db.execute(
            "UPDATE employee_notifications "
            "SET presented_at = COALESCE(presented_at, now()) "
            "WHERE id = ANY(%s) AND person_odoo_id = %s "
            "AND acknowledged_at IS NULL",
            (ids, person_odoo_id),
        )
    return rows


def list_history(person_odoo_id: int) -> list[dict]:
    """Return one employee's complete kiosk-notification audit history."""
    return db.query(
        "SELECT id, kind, title, body, leave_date_from, leave_date_to, saturday_day, "
        "anniversary_date, balance_amount, balance_unit, created_at, presented_at, "
        "acknowledged_at FROM employee_notifications "
        "WHERE person_odoo_id = %s ORDER BY created_at DESC, id DESC",
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
