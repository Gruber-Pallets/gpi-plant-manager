"""PostgreSQL persistence for finalized GOAT Slack notifications."""

from __future__ import annotations

from datetime import date, timedelta
from uuid import uuid4

from . import shift_config


def ensure_enabled_on(day: date) -> date:
    from . import db

    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO goat_notification_state (id, enabled_on) VALUES (1, %s) "
            "ON CONFLICT (id) DO NOTHING",
            (day,),
        )
        cur.execute("SELECT enabled_on FROM goat_notification_state WHERE id = 1")
        return cur.fetchone()["enabled_on"]


def unfinalized_workdays(through_day: date) -> list[date]:
    """Return enabled workdays through ``through_day`` not yet finalized."""
    from . import db

    with db.cursor() as cur:
        cur.execute("SELECT enabled_on FROM goat_notification_state WHERE id = 1")
        state = cur.fetchone()
        if state is None:
            return []
        enabled_on = state["enabled_on"]
        cur.execute(
            "SELECT day FROM goat_notification_days WHERE day BETWEEN %s AND %s",
            (enabled_on, through_day),
        )
        finalized_days = {row["day"] for row in cur.fetchall()}

    days: list[date] = []
    current = enabled_on
    while current <= through_day:
        if shift_config.is_workday(current) and current not in finalized_days:
            days.append(current)
        current += timedelta(days=1)
    return days


def record_finalized_day(day: date) -> None:
    from . import db

    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO goat_notification_days (day) VALUES (%s) ON CONFLICT (day) DO NOTHING",
            (day,),
        )


def insert_alert_and_delivery(alert: dict) -> int | None:
    from . import db

    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO goat_alerts (achieved_day, category_key, group_name, person, wc_name, units, prior_record_units, prior_record_holder, prior_record_day) "
            "VALUES (%(achieved_day)s, %(category_key)s, %(group_name)s, %(person)s, %(wc_name)s, %(units)s, %(prior_record_units)s, %(prior_record_holder)s, %(prior_record_day)s) "
            "ON CONFLICT DO NOTHING RETURNING id",
            alert,
        )
        row = cur.fetchone()
        if row is None:
            return None
        alert_id = int(row["id"])
        cur.execute(
            "INSERT INTO goat_slack_deliveries (goat_alert_id, client_msg_id) VALUES (%s, %s)",
            (alert_id, str(uuid4())),
        )
        return alert_id


def claim_delivery() -> dict | None:
    from . import db

    with db.cursor() as cur:
        cur.execute(
            "WITH candidate AS (SELECT id FROM goat_slack_deliveries "
            "WHERE status = 'pending' OR (status = 'sending' AND attempted_at < now() - interval '5 minutes') "
            "ORDER BY id FOR UPDATE SKIP LOCKED LIMIT 1) "
            "UPDATE goat_slack_deliveries delivery SET status = 'sending', attempts = delivery.attempts + 1, attempted_at = now(), "
            "client_msg_id = COALESCE(delivery.client_msg_id, %s::uuid), claim_token = %s::uuid "
            "FROM candidate, goat_alerts alert "
            "WHERE delivery.id = candidate.id AND alert.id = delivery.goat_alert_id "
            "RETURNING delivery.id, delivery.goat_alert_id, delivery.client_msg_id, delivery.claim_token, alert.achieved_day, alert.group_name, alert.person, alert.wc_name, alert.units, alert.prior_record_units, alert.prior_record_holder, alert.prior_record_day",
            (str(uuid4()), str(uuid4())),
        )
        return cur.fetchone()


def mark_delivery_sent(delivery_id: int, claim_token: str, message_ts: str) -> None:
    from . import db

    with db.cursor() as cur:
        cur.execute(
            "UPDATE goat_slack_deliveries SET status = 'sent', sent_at = now(), "
            "slack_message_ts = %s, last_error = NULL "
            "WHERE id = %s AND status = 'sending' AND claim_token = %s::uuid",
            (message_ts, delivery_id, str(claim_token)),
        )


def return_delivery_to_pending(delivery_id: int, claim_token: str, error: str) -> None:
    from . import db

    with db.cursor() as cur:
        cur.execute(
            "UPDATE goat_slack_deliveries SET status = 'pending', last_error = %s, claim_token = NULL "
            "WHERE id = %s AND status = 'sending' AND claim_token = %s::uuid",
            (error, delivery_id, str(claim_token)),
        )
