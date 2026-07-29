import os
from datetime import date

import pytest

from zira_dashboard import db, goat_notification_store as store


pytestmark = pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="needs Postgres")

_TEST_DAY = date(2026, 7, 29)


@pytest.fixture(autouse=True)
def clean_goat_notification_rows():
    db.bootstrap_schema()
    _delete_test_rows()
    yield
    _delete_test_rows()


def _delete_test_rows():
    db.execute(
        "DELETE FROM goat_slack_deliveries WHERE goat_alert_id IN "
        "(SELECT id FROM goat_alerts WHERE achieved_day = %s)",
        (_TEST_DAY,),
    )
    db.execute("DELETE FROM goat_notification_days WHERE day = %s", (_TEST_DAY,))
    db.execute("DELETE FROM goat_notification_state WHERE enabled_on = %s", (_TEST_DAY,))
    db.execute("DELETE FROM goat_alerts WHERE achieved_day = %s", (_TEST_DAY,))


def _alert(day=date(2026, 7, 29)):
    return {
        "achieved_day": day, "group_name": "Repairs", "person": "Jose O.",
        "wc_name": "Repair 3", "units": 898, "prior_record_units": 891,
        "prior_record_holder": "Jose Ochoa", "prior_record_day": date(2026, 6, 10),
    }


def test_activation_day_is_written_once():
    assert store.ensure_enabled_on(date(2026, 7, 29)) == date(2026, 7, 29)
    assert store.ensure_enabled_on(date(2026, 8, 1)) == date(2026, 7, 29)


def test_alert_and_delivery_are_single_transactional_unit():
    alert_id = store.insert_alert_and_delivery(_alert())
    assert isinstance(alert_id, int)
    assert store.insert_alert_and_delivery(_alert()) is None
    rows = db.query("SELECT goat_alert_id, status, attempts FROM goat_slack_deliveries WHERE goat_alert_id = %s", (alert_id,))
    assert rows == [{"goat_alert_id": alert_id, "status": "pending", "attempts": 0}]


def test_failure_retries_the_same_delivery_without_duplication():
    store.insert_alert_and_delivery(_alert())
    first = store.claim_delivery()
    store.return_delivery_to_pending(first["id"], "not_in_channel")
    second = store.claim_delivery()
    store.mark_delivery_sent(second["id"], "1722280000.000100")
    rows = db.query("SELECT status, attempts, slack_message_ts, last_error FROM goat_slack_deliveries WHERE id = %s", (second["id"],))
    assert rows == [{"status": "sent", "attempts": 2, "slack_message_ts": "1722280000.000100", "last_error": None}]


def test_unfinalized_workdays_exclude_days_recorded_as_finalized(monkeypatch):
    monkeypatch.setattr(store.shift_config, "work_weekdays", lambda: frozenset({0, 1, 2, 3, 4}))
    store.ensure_enabled_on(_TEST_DAY)
    store.record_finalized_day(_TEST_DAY)

    assert store.unfinalized_workdays(date(2026, 7, 31)) == [
        date(2026, 7, 30),
        date(2026, 7, 31),
    ]


def test_record_finalized_day_is_idempotent():
    store.record_finalized_day(_TEST_DAY)
    store.record_finalized_day(_TEST_DAY)

    assert db.query(
        "SELECT day FROM goat_notification_days WHERE day = %s", (_TEST_DAY,)
    ) == [{"day": _TEST_DAY}]
