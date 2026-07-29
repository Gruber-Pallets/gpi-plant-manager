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
        "group_name": f"pytest-goat-{marker}",
        "person": f"pytest-goat-{marker}",
        "wc_name": f"pytest-goat-{marker}",
        "units": 898,
        "prior_record_units": 891,
        "prior_record_holder": "Jose Ochoa",
        "prior_record_day": date(2026, 6, 10),
    }


def test_schema_defines_durable_goat_notification_tables():
    assert "CREATE TABLE IF NOT EXISTS goat_notification_state" in SCHEMA_DDL
    assert "CREATE TABLE IF NOT EXISTS goat_notification_days" in SCHEMA_DDL
    assert "CREATE TABLE IF NOT EXISTS goat_slack_deliveries" in SCHEMA_DDL
    assert "goat_alert_id     INTEGER NOT NULL UNIQUE REFERENCES goat_alerts(id) ON DELETE CASCADE" in SCHEMA_DDL
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
    """Use one UUID-marked inert alert and never clean up shared data."""
    db.bootstrap_schema()
    alert = _alert()

    alert_id = store.insert_alert_and_delivery(alert)

    assert isinstance(alert_id, int)
    assert store.insert_alert_and_delivery(alert) is None
    rows = db.query(
        "SELECT goat_alert_id, status, attempts FROM goat_slack_deliveries "
        "WHERE goat_alert_id = %s",
        (alert_id,),
    )
    assert rows == [{"goat_alert_id": alert_id, "status": "pending", "attempts": 0}]


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
    }
    first_claim = _RecordingCursor(fetchone_results=[delivery])
    return_to_pending = _RecordingCursor()
    second_claim = _RecordingCursor(fetchone_results=[delivery])
    mark_sent = _RecordingCursor()
    _patch_cursors(
        monkeypatch, first_claim, return_to_pending, second_claim, mark_sent
    )

    first = store.claim_delivery()
    store.return_delivery_to_pending(first["id"], "not_in_channel")
    second = store.claim_delivery()
    store.mark_delivery_sent(second["id"], "1722280000.000100")

    assert first == second == delivery
    claim_sql = first_claim.executed[0][0]
    assert "attempts = delivery.attempts + 1" in claim_sql
    assert "FROM candidate, goat_alerts alert" in claim_sql
    assert "WHERE delivery.id = candidate.id AND alert.id = delivery.goat_alert_id" in claim_sql
    assert "JOIN goat_alerts" not in claim_sql
    assert return_to_pending.executed[0][1] == ("not_in_channel", delivery["id"])
    assert mark_sent.executed[0][1] == ("1722280000.000100", delivery["id"])


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
