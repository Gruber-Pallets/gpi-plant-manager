"""Deliver captured approvals through Odoo's normal outgoing email queue.

The approval and its outbox row commit together (see _time_off_email_schema).
Sending intent commits before XML-RPC. An uncertain create is reconciled by
Message-ID, never blindly repeated. Logs contain IDs and fixed reasons only.
"""
from __future__ import annotations

import logging
import os
import re
import xmlrpc.client
from datetime import datetime, UTC
from email.utils import formataddr, parseaddr
from html import escape

from . import db, odoo_client, shift_config, time_off_sync

_log = logging.getLogger(__name__)
_WORKER_LOCK = 7381038
_ADDRESS = re.compile(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+\Z")
_SNAPSHOT_FIELDS = ("person_odoo_id", "date_from", "date_to", "shape", "hour_from", "hour_to")


class _OneAttemptTransport:
    def request(self, host, handler, request_body, verbose=False):
        # xmlrpc.client.Transport.request silently retries a dropped response.
        # A mail create may have committed already: do not replay the mutation.
        return self.single_request(host, handler, request_body, verbose)


class _MailHttpTransport(_OneAttemptTransport, odoo_client._TimeoutTransport):
    pass


class _MailHttpsTransport(_OneAttemptTransport, odoo_client._TimeoutSafeTransport):
    pass


def _mail_transport(secure):
    return _MailHttpsTransport() if secure else _MailHttpTransport()


def _create_mail(payload):
    url, database, _login, key = odoo_client._config()
    uid = odoo_client.authenticate()
    with xmlrpc.client.ServerProxy(
        f"{url}/xmlrpc/2/object", transport=_mail_transport(url.startswith("https://"))
    ) as proxy:
        return proxy.execute_kw(database, uid, key, "mail.mail", "create", [payload], {})


def _today():
    return datetime.now(UTC).astimezone(shift_config.SITE_TZ).date()


def _address(value):
    if not isinstance(value, str):
        return None
    value = value.strip().lower()
    return value if _ADDRESS.fullmatch(value) else None


def recipient(employee: dict, policy: str) -> str | None:
    fields = {
        "personal_then_work": ("private_email", "work_email"),
        "personal": ("private_email",),
        "work": ("work_email",),
    }.get(policy)
    if fields is None:
        raise ValueError("invalid recipient policy")
    return next((address for field in fields if (address := _address(employee.get(field)))), None)


def _hour(value) -> str:
    minutes = round(float(value) * 60)
    hour, minute = divmod(minutes, 60)
    return f"{hour % 12 or 12}:{minute:02d} {'AM' if hour < 12 else 'PM'}"


def render_email(row: dict) -> tuple[str, str]:
    start, end = row["date_from"].isoformat(), row["date_to"].isoformat()
    span = start if start == end else f"{start} — {end}"
    if row["shape"] == "full_day":
        detail = "Full days off. / Días completos libres."
    else:
        detail = (
            f"Time off / Tiempo libre: {_hour(row['hour_from'])} — {_hour(row['hour_to'])} "
            "(Central time / hora central)."
        )
    body = (
        "<p>Your time off was approved.</p>"
        "<p>Tu tiempo libre fue aprobado.</p>"
        f"<p><strong>{escape(span)}</strong><br>{escape(detail)}</p>"
        "<p>You can check your request at the time clock. "
        "Please see your supervisor if you have questions.</p>"
        "<p>Puedes consultar tu solicitud en el reloj de asistencia. "
        "Si tienes preguntas, habla con tu supervisor.</p>"
        "<p>GPI Plant Manager</p>"
    )
    return "Your time off was approved / Tu tiempo libre fue aprobado", body


def message_id(row: dict) -> str:
    return f"<gpi-time-off-{row['delivery_key']}@gpiplantmanager.com>"


def _mark(row, state, *, error=None, mail_id=None, delay=60):
    db.execute(
        "UPDATE time_off_approval_email SET state = %s, last_error = %s, "
        "odoo_mail_id = COALESCE(%s, odoo_mail_id), "
        "attempts = attempts + 1, next_attempt_at = now() + %s * interval '1 second', "
        "updated_at = now() WHERE request_id = %s",
        (state, error, mail_id, delay, row["request_id"]),
    )
    if error:
        _log.warning("approval email request=%s status=%s reason=%s", row["request_id"], state, error)


def _current_request(row):
    rows = db.query("SELECT * FROM time_off_requests WHERE id = %s", (row["request_id"],))
    return rows[0] if len(rows) == 1 else None


def _still_approved(row, current) -> bool:
    if not current or current["state"] != "validate" or row["date_to"] < _today():
        return False
    if any(current.get(key) != row.get(key) for key in _SNAPSHOT_FIELDS):
        return False
    if current.get("local_record"):
        return True
    leave_id = current.get("odoo_leave_id")
    if not leave_id:
        return False
    leaves = odoo_client.execute(
        "hr.leave", "read", [leave_id],
        fields=["state", "employee_id", "request_date_from", "request_date_to",
                "request_unit_hours", "request_hour_from", "request_hour_to",
                "number_of_days", "date_from", "date_to"],
    )
    if len(leaves) != 1:
        return False
    leave = leaves[0]
    shape, hour_from, hour_to = time_off_sync._mirror_shape_and_hours(leave)
    return (
        leave.get("state") == "validate"
        and odoo_client.unwrap_m2o(leave.get("employee_id")) == row["person_odoo_id"]
        and leave.get("request_date_from") == row["date_from"].isoformat()
        and leave.get("request_date_to") == row["date_to"].isoformat()
        and shape == row["shape"]
        and time_off_sync._hours_eq(hour_from, row["hour_from"])
        and time_off_sync._hours_eq(hour_to, row["hour_to"])
    )


def _find_mail(row):
    matches = odoo_client.execute(
        "mail.mail", "search_read", [("message_id", "=", message_id(row))],
        fields=["id", "state", "message_id"], limit=2,
    )
    if len(matches) > 1:
        raise ValueError("ambiguous mail")
    if not matches:
        return None
    mail = matches[0]
    if (type(mail.get("id")) is not int or mail["id"] <= 0
            or mail.get("message_id") != message_id(row)
            or (row.get("odoo_mail_id") and row["odoo_mail_id"] != mail["id"])):
        raise ValueError("mail identity mismatch")
    return mail


def _observe_mail(row, mail):
    state = mail.get("state")
    if state == "sent":
        _mark(row, "sent", mail_id=mail["id"])
    elif state == "outgoing":
        if not _still_approved(row, _current_request(row)):
            odoo_client.execute("mail.mail", "cancel", [mail["id"]])
            _mark(row, "skipped", mail_id=mail["id"], error="request_changed")
        else:
            _mark(row, "queued", mail_id=mail["id"])
    else:
        _mark(row, "attention", mail_id=mail["id"], error="mail_delivery_failed")


def process_one(row):
    """Process under the worker lock; never raise remote text to the logger."""
    try:
        mail = _find_mail(row)
        if mail:
            _observe_mail(row, mail)
            return
        if row["state"] in {"sending", "queued"}:
            # A timeout may still commit remotely. Keep looking, without another
            # create. A human must investigate a message that never appears.
            _mark(row, "sending", error="delivery_unknown", delay=900)
            return
        if not _still_approved(row, _current_request(row)):
            _mark(row, "skipped", error="request_changed")
            return
        employees = odoo_client.execute(
            "hr.employee", "read", [row["person_odoo_id"]],
            fields=["active", "private_email", "work_email"],
        )
        if len(employees) != 1 or employees[0].get("id") != row["person_odoo_id"]:
            _mark(row, "attention", error="employee_unavailable")
            return
        if employees[0].get("active") is not True:
            _mark(row, "skipped", error="employee_inactive")
            return
        address = recipient(
            employees[0], os.environ.get("TIME_OFF_APPROVAL_EMAIL_RECIPIENT", "personal_then_work")
        )
        if not address:
            _mark(row, "pending", error="missing_email", delay=3600)
            return
        defaults = odoo_client.execute("mail.mail", "default_get", ["email_from"])
        sender = _address(parseaddr(str(defaults.get("email_from") or ""))[1])
        if not sender:
            _mark(row, "pending", error="sender_unavailable", delay=3600)
            return
        subject, body = render_email(row)
        _mark(row, "sending")  # Separate committed DB transaction BEFORE remote create.
    except ValueError:
        _mark(row, "attention", error="invalid_delivery_data")
        return
    except Exception:
        _mark(row, row["state"], error="lookup_failed", delay=300)
        return

    try:
        mail_id = _create_mail({
            "email_to": address,
            "email_from": formataddr(("GPI Plant Manager", sender)),
            "subject": subject,
            "body_html": body,
            "message_id": message_id(row),
            "auto_delete": False,
        })
        if type(mail_id) is not int or mail_id <= 0:
            raise ValueError("invalid mail id")
    except xmlrpc.client.Fault:
        # A returned Odoo application fault rolls back its create transaction.
        _mark(row, "pending", error="queue_rejected", delay=300)
    except Exception:
        _mark(row, "sending", error="delivery_unknown", delay=900)
    else:
        _mark(row, "queued", mail_id=mail_id)


def run_once() -> int:
    if os.environ.get("TIME_OFF_APPROVAL_EMAIL_ENABLED", "1").strip().lower() in {"0", "false", "no"}:
        return 0
    # The lock transaction only serializes workers. Status writes use separate
    # connections and commit before RPC so a worker crash preserves intent.
    with db.cursor() as cur:
        cur.execute("SELECT pg_try_advisory_xact_lock(%s) AS acquired", (_WORKER_LOCK,))
        if not cur.fetchone()["acquired"]:
            return 0
        rows = db.query(
            "SELECT * FROM time_off_approval_email "
            "WHERE state IN ('pending', 'sending', 'queued') AND next_attempt_at <= now() "
            "ORDER BY next_attempt_at, request_id LIMIT 5"
        )
        for row in rows:
            process_one(row)
        return len(rows)
