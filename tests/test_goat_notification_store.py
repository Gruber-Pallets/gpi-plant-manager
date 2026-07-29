import os
from datetime import date
from uuid import uuid4

import pytest

from zira_dashboard import db, goat_notification_store as store
from zira_dashboard._schema import SCHEMA_DDL


needs_postgres = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"), reason="needs Postgres"
)


class _RecordingCursor:
    def __init__(self, *, fetchone_results=(), fetchall_results=()):
        self.executed = []
        self._fetchone_results = list(fetchone_results)
        self._fetchall_results = list(fetchall_results)

    def execute(self, sql, params=None):
        self.executed.append((sql, params))

    def fetchone(self):
        if not self._fetchone_results:
            return None
        return self._fetchone_results.pop(0)

    def fetchall(self):
        if not self._fetchall_results:
            return []
        return self._fetchall_results.pop(0)


class _CursorContext:
    def __init__(self, cursor):
        self.cursor = cursor

    def __enter__(self):
        return self.cursor

    def __exit__(self, exc_type, exc_value, traceback):
        return False


def _patch_cursors(monkeypatch, *cursors):
    remaining = iter(cursors)
    monkeypatch.setattr(db, "cursor", lambda: _CursorContext(next(remaining)))


def _alert():
    marker = uuid4().hex
    return {
        "achieved_day": date(2099, 1, 2),
        "category_key": f"pytest-goat-{marker}",
        "group_name": f"pytest-goat-{marker}",
        "person": f"pytest-goat-{marker}",
        "wc_name": f"pytest-goat-{marker}",
        "units": 898,
        "prior_record_units": 891,
        "prior_record_holder": "Jose Ochoa",
        "prior_record_day": date(2026, 6, 10),
    }


def test_schema_defines_durable_goat_notification_tables():
    assert "category_key        TEXT NOT NULL" in SCHEMA_DDL
    assert "ALTER TABLE goat_alerts ADD COLUMN IF NOT EXISTS category_key TEXT" in SCHEMA_DDL
    assert "ON goat_alerts (achieved_day, category_key) WHERE category_key IS NOT NULL" in SCHEMA_DDL
    assert "CREATE TABLE IF NOT EXISTS goat_notification_state" in SCHEMA_DDL
    assert "CREATE TABLE IF NOT EXISTS goat_notification_days" in SCHEMA_DDL
    assert "CREATE TABLE IF NOT EXISTS goat_slack_deliveries" in SCHEMA_DDL
    assert "goat_alert_id     INTEGER NOT NULL UNIQUE REFERENCES goat_alerts(id) ON DELETE CASCADE" in SCHEMA_DDL
    assert "client_msg_id     UUID NOT NULL" in SCHEMA_DDL
    assert "ALTER TABLE goat_slack_deliveries ADD COLUMN IF NOT EXISTS client_msg_id UUID" in SCHEMA_DDL
    assert "claim_token       UUID" in SCHEMA_DDL
    assert "ALTER TABLE goat_slack_deliveries ADD COLUMN IF NOT EXISTS claim_token UUID" in SCHEMA_DDL
    assert "idx_goat_slack_deliveries_claim" in SCHEMA_DDL


def test_activation_day_is_written_once_without_touching_shared_state(monkeypatch):
    enabled_on = date(2026, 7, 29)
    cursor = _RecordingCursor(fetchone_results=[{"enabled_on": enabled_on}])
    _patch_cursors(monkeypatch, cursor)

    assert store.ensure_enabled_on(date(2026, 8, 1)) == enabled_on
    assert cursor.executed == [
        (
            "INSERT INTO goat_notification_state (id, enabled_on) VALUES (1, %s) "
            "ON CONFLICT (id) DO NOTHING",
            (date(2026, 8, 1),),
        ),
        ("SELECT enabled_on FROM goat_notification_state WHERE id = 1", None),
    ]


@needs_postgres
def test_alert_and_delivery_are_single_transactional_unit():
    """Use one UUID-marked inert alert and delete only its returned ID."""
    db.bootstrap_schema()
    alert = _alert()
    alert_id = None

    try:
        alert_id = store.insert_alert_and_delivery(alert)

        assert isinstance(alert_id, int)
        assert store.insert_alert_and_delivery(alert) is None
        rows = db.query(
            "SELECT goat_alert_id, status, attempts FROM goat_slack_deliveries "
            "WHERE goat_alert_id = %s",
            (alert_id,),
        )
        assert rows == [{"goat_alert_id": alert_id, "status": "pending", "attempts": 0}]
    finally:
        if alert_id is not None:
            db.execute("DELETE FROM goat_alerts WHERE id = %s", (alert_id,))
            assert db.query(
                "SELECT id FROM goat_slack_deliveries WHERE goat_alert_id = %s",
                (alert_id,),
            ) == []


@needs_postgres
def test_changed_work_center_retry_creates_one_category_day_alert_and_delivery():
    """A retry with revised source rows must not publish a second category GOAT."""
    db.bootstrap_schema()
    alert = _alert()
    first_alert_id = None
    retry_alert_id = None

    try:
        first_alert_id = store.insert_alert_and_delivery(alert)
        retry_alert_id = store.insert_alert_and_delivery({**alert, "wc_name": "Repair 9"})

        assert isinstance(first_alert_id, int)
        assert retry_alert_id is None
        rows = db.query(
            "SELECT alert.id, delivery.id AS delivery_id FROM goat_alerts alert "
            "JOIN goat_slack_deliveries delivery ON delivery.goat_alert_id = alert.id "
            "WHERE alert.achieved_day = %s AND alert.category_key = %s",
            (alert["achieved_day"], alert["category_key"]),
        )
        assert len(rows) == 1
        assert rows[0]["id"] == first_alert_id
        assert isinstance(rows[0]["delivery_id"], int)
    finally:
        for alert_id in (retry_alert_id, first_alert_id):
            if alert_id is not None:
                db.execute("DELETE FROM goat_alerts WHERE id = %s", (alert_id,))


def test_claim_retry_and_sent_updates_target_the_same_delivery(monkeypatch):
    delivery = {
        "id": 41,
        "goat_alert_id": 23,
        "achieved_day": date(2026, 7, 29),
        "group_name": "Repairs",
        "person": "Jose O.",
        "wc_name": "Repair 3",
        "units": 898,
        "prior_record_units": 891,
        "prior_record_holder": "Jose Ochoa",
        "prior_record_day": date(2026, 6, 10),
        "client_msg_id": "1f7194a2-79de-4e95-a5f4-087743431fe9",
        "claim_token": "a02b5f81-2c89-4f2d-bcdf-c9f0f431838d",
    }
    first_claim = _RecordingCursor(fetchone_results=[delivery])
    return_to_pending = _RecordingCursor()
    second_claim = _RecordingCursor(fetchone_results=[delivery])
    mark_sent = _RecordingCursor()
    _patch_cursors(
        monkeypatch, first_claim, return_to_pending, second_claim, mark_sent
    )

    first = store.claim_delivery()
    store.return_delivery_to_pending(first["id"], first["claim_token"], "not_in_channel")
    second = store.claim_delivery()
    store.mark_delivery_sent(second["id"], second["claim_token"], "1722280000.000100")

    assert first == second == delivery
    claim_sql = first_claim.executed[0][0]
    assert "attempts = delivery.attempts + 1" in claim_sql
    assert "client_msg_id = COALESCE(delivery.client_msg_id, %s::uuid)" in claim_sql
    assert "claim_token = %s::uuid" in claim_sql
    assert "delivery.claim_token" in claim_sql
    assert "FROM candidate, goat_alerts alert" in claim_sql
    assert "WHERE delivery.id = candidate.id AND alert.id = delivery.goat_alert_id" in claim_sql
    assert "JOIN goat_alerts" not in claim_sql
    assert return_to_pending.executed[0][1] == ("not_in_channel", delivery["id"], delivery["claim_token"])
    assert mark_sent.executed[0][1] == ("1722280000.000100", delivery["id"], delivery["claim_token"])


def test_changed_work_center_retry_uses_category_day_conflict_and_one_outbox(monkeypatch):
    alert = _alert()
    first_insert = _RecordingCursor(fetchone_results=[{"id": 23}])
    retry_insert = _RecordingCursor(fetchone_results=[None])
    _patch_cursors(monkeypatch, first_insert, retry_insert)

    assert store.insert_alert_and_delivery(alert) == 23
    assert store.insert_alert_and_delivery({**alert, "wc_name": "Repair 9"}) is None

    alert_sql = first_insert.executed[0][0]
    assert "category_key" in alert_sql
    assert "ON CONFLICT DO NOTHING RETURNING id" in alert_sql
    delivery_inserts = [
        sql
        for cursor in (first_insert, retry_insert)
        for sql, _ in cursor.executed
        if "INSERT INTO goat_slack_deliveries" in sql
    ]
    assert len(delivery_inserts) == 1


def test_old_claim_failure_cannot_revert_a_newer_success(monkeypatch):
    old_claim_token = "a02b5f81-2c89-4f2d-bcdf-c9f0f431838d"
    new_claim_token = "f3d5e049-37bd-4881-bcfb-43d41c28e5a9"
    new_success = _RecordingCursor()
    late_old_failure = _RecordingCursor()
    _patch_cursors(monkeypatch, new_success, late_old_failure)

    store.mark_delivery_sent(41, new_claim_token, "1722280000.000100")
    store.return_delivery_to_pending(41, old_claim_token, "late timeout")

    for cursor in (new_success, late_old_failure):
        sql = cursor.executed[0][0]
        assert "WHERE id = %s AND status = 'sending' AND claim_token = %s::uuid" in sql
    assert late_old_failure.executed[0][1] == ("late timeout", 41, old_claim_token)


def test_insert_alert_persists_a_client_message_id(monkeypatch):
    alert_id = 23
    client_message_id = uuid4()
    insert_alert = _RecordingCursor(fetchone_results=[{"id": alert_id}])
    _patch_cursors(monkeypatch, insert_alert)
    monkeypatch.setattr(store, "uuid4", lambda: client_message_id)

    assert store.insert_alert_and_delivery(_alert()) == alert_id
    assert insert_alert.executed[1:] == [
        (
            "INSERT INTO goat_slack_deliveries (goat_alert_id, client_msg_id) VALUES (%s, %s)",
            (alert_id, str(client_message_id)),
        )
    ]


def test_unfinalized_workdays_exclude_days_recorded_as_finalized(monkeypatch):
    enabled_on = date(2026, 7, 29)
    through_day = date(2026, 7, 31)
    cursor = _RecordingCursor(
        fetchone_results=[{"enabled_on": enabled_on}],
        fetchall_results=[[{"day": enabled_on}]],
    )
    _patch_cursors(monkeypatch, cursor)
    monkeypatch.setattr(
        store.shift_config, "work_weekdays", lambda: frozenset({0, 1, 2, 3, 4})
    )

    assert store.unfinalized_workdays(through_day) == [
        date(2026, 7, 30),
        date(2026, 7, 31),
    ]
    assert cursor.executed[-1] == (
        "SELECT day FROM goat_notification_days WHERE day BETWEEN %s AND %s",
        (enabled_on, through_day),
    )


def test_unfinalized_workdays_includes_a_published_saturday(monkeypatch):
    saturday = date(2026, 8, 1)
    cursor = _RecordingCursor(
        fetchone_results=[{"enabled_on": saturday}],
        fetchall_results=[[]],
    )
    _patch_cursors(monkeypatch, cursor)
    monkeypatch.setattr(store.shift_config, "is_workday", lambda day: day == saturday)

    assert store.unfinalized_workdays(saturday) == [saturday]


def test_record_finalized_day_uses_an_idempotent_insert(monkeypatch):
    first = _RecordingCursor()
    second = _RecordingCursor()
    _patch_cursors(monkeypatch, first, second)
    finalized_day = date(2026, 7, 29)

    store.record_finalized_day(finalized_day)
    store.record_finalized_day(finalized_day)

    expected = (
        "INSERT INTO goat_notification_days (day) VALUES (%s) ON CONFLICT (day) DO NOTHING",
        (finalized_day,),
    )
    assert first.executed == [expected]
    assert second.executed == [expected]
