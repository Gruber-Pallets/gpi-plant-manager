"""Finalize daily production GOAT records and deliver their Slack celebrations."""

from __future__ import annotations

import logging
import os
from datetime import date, datetime, timedelta

from . import (
    awards,
    goat_categories,
    goat_notification_store as store,
    precompute,
    production_history,
    shift_config,
    slack_client,
)


logger = logging.getLogger(__name__)


def winner_for_day(category, day: date, records: list[dict]) -> dict | None:
    wc_names = goat_categories.work_center_names(category)
    rows = awards.person_days_in_wc_names(wc_names, day, day, records=records)
    winner = awards.best_person_day(rows)
    if winner is None:
        return None
    contributions = [
        record for record in records
        if record["day"] == day and record["person"] == winner["name"] and record["wc"] in wc_names
    ]
    if not contributions:
        return None
    location = sorted(contributions, key=lambda record: (-float(record["units"]), record["wc"]))[0]
    return {"person": winner["name"], "wc_name": location["wc"], "units": int(round(float(winner["units"])))}


def _eligible_categories():
    return tuple(category for category in goat_categories.all_categories() if goat_categories.has_metered_source(category))


def _records_through(day: date) -> list[dict]:
    return production_history.daily_records(awards.AWARDS_DATA_FLOOR, day)


def finalize_day(day: date, client) -> list[dict]:
    """Persist notifications for category records set on one completed workday."""
    precompute.precompute_day(day, client)
    records = _records_through(day)
    alerts: list[dict] = []

    for category in _eligible_categories():
        winner = winner_for_day(category, day, records)
        if winner is None:
            continue

        prior_rows = [
            row
            for row in awards.person_days_in_wc_names(
                goat_categories.work_center_names(category),
                awards.AWARDS_DATA_FLOOR,
                day,
                records=records,
            )
            if row["day"] != day
        ]
        prior = awards.best_person_day(prior_rows)
        if prior is None:
            continue

        prior_units = int(round(float(prior["units"])))
        if winner["units"] <= prior_units:
            continue

        alert = {
            "achieved_day": day,
            "category_key": category.key,
            "group_name": category.label,
            "person": winner["person"],
            "wc_name": winner["wc_name"],
            "units": winner["units"],
            "prior_record_units": prior_units,
            "prior_record_holder": prior["name"],
            "prior_record_day": prior["day"],
        }
        if store.insert_alert_and_delivery(alert) is not None:
            alerts.append(alert)

    return alerts


def _short_date(value: date) -> str:
    return f"{value.strftime('%b')} {value.day}, {value.year}"


def _first_name(person: str) -> str:
    return person.strip().split(maxsplit=1)[0] if person.strip() else "them"


def message_payload(alert: dict) -> tuple[str, list[dict]]:
    headline = f"🏆 NEW {str(alert['group_name']).upper()} GOAT!"
    record_line = f"*{alert['person']}* — *{int(alert['units']):,} pallets* at {alert['wc_name']} on {_short_date(alert['achieved_day'])}"
    previous = f"previous = {alert['prior_record_holder']} · {int(alert['prior_record_units'])} · {_short_date(alert['prior_record_day'])}"
    closing = f"Congratulate {_first_name(str(alert['person']))} when you see them! 🎉"
    text = f"{headline}\n{str(alert['person'])} — {int(alert['units']):,} pallets at {alert['wc_name']} on {_short_date(alert['achieved_day'])}\n({previous})\n{closing}"
    blocks = [
        {"type": "header", "text": {"type": "plain_text", "text": headline}},
        {"type": "section", "text": {"type": "mrkdwn", "text": record_line}},
        {"type": "context", "elements": [{"type": "mrkdwn", "text": f"_({previous})_"}]},
        {"type": "section", "text": {"type": "mrkdwn", "text": closing}},
    ]
    return text, blocks


def drain_deliveries() -> int:
    channel_id = os.environ.get("GOAT_SLACK_CHANNEL_ID")
    if not channel_id:
        logger.warning("GOAT_SLACK_CHANNEL_ID is not configured; GOAT deliveries remain pending")
        return 0
    sent = 0
    while delivery := store.claim_delivery():
        text, blocks = message_payload(delivery)
        try:
            result = slack_client.post_message(
                channel_id=channel_id,
                text=text,
                blocks=blocks,
                client_msg_id=str(delivery["client_msg_id"]),
            )
        except slack_client.SlackError as exc:
            store.return_delivery_to_pending(delivery["id"], delivery["claim_token"], str(exc))
            break
        store.mark_delivery_sent(delivery["id"], delivery["claim_token"], result["message_ts"])
        sent += 1
    return sent


def _prior_configured_workday(day: date) -> date:
    candidate = day - timedelta(days=1)
    while not shift_config.is_workday(candidate):
        candidate -= timedelta(days=1)
    return candidate


def run_due(now_utc: datetime, client) -> None:
    """Finalize outstanding completed workdays, then send their durable outbox."""
    local_now = now_utc.astimezone(shift_config.SITE_TZ)
    today = local_now.date()
    store.ensure_enabled_on(today)
    latest_completed_day = (
        today
        if local_now.time() >= shift_config.shift_end_for(today)
        else _prior_configured_workday(today)
    )
    for day in store.unfinalized_workdays(latest_completed_day):
        finalize_day(day, client)
        store.record_finalized_day(day)
    drain_deliveries()
