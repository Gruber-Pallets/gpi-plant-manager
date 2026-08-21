from contextlib import contextmanager
from unittest.mock import Mock

import pytest

from zira_dashboard import auto_lunch_settings as settings, db


OFF = {
    "enabled": False, "observe_only": True,
    "flex_after_hours": 5.0, "flex_minutes": 30,
}


class RecordingCursor:
    def __init__(self, rows):
        self.rows = list(rows)
        self.executed = []

    def execute(self, sql, params=None):
        self.executed.append((" ".join(sql.split()), params))

    def fetchone(self):
        return self.rows.pop(0) if self.rows else None


def cursor_context(cursor, *, fail_after_yield=False):
    @contextmanager
    def opened():
        yield cursor
        if fail_after_yield:
            raise RuntimeError("commit failed")
    return opened


def test_save_writes_setting_and_actor_audit_in_one_cursor(monkeypatch):
    cursor = RecordingCursor([OFF])
    monkeypatch.setattr(db, "cursor", cursor_context(cursor))
    cache_set = Mock()
    monkeypatch.setattr(settings._store, "set", cache_set)
    live = settings.Settings(True, False, 5.0, 30)

    assert settings.save(
        live, actor_upn="dale@gruberpallets.com", actor_name="Dale"
    ) is True

    assert len(cursor.executed) == 3
    assert "FOR UPDATE" in cursor.executed[0][0]
    assert "INSERT INTO auto_lunch_settings" in cursor.executed[1][0]
    assert "INSERT INTO auto_lunch_setting_events" in cursor.executed[2][0]
    assert cursor.executed[2][1][-3:] == (
        "dale@gruberpallets.com", "Dale", "settings"
    )
    cache_set.assert_called_once_with(live)


def test_save_of_identical_values_is_silent(monkeypatch):
    cursor = RecordingCursor([OFF])
    monkeypatch.setattr(db, "cursor", cursor_context(cursor))

    assert settings.save(settings.Settings()) is False

    assert len(cursor.executed) == 1


def test_cache_is_not_changed_when_transaction_commit_fails(monkeypatch):
    cursor = RecordingCursor([OFF])
    monkeypatch.setattr(db, "cursor", cursor_context(cursor, fail_after_yield=True))
    cache_set = Mock()
    monkeypatch.setattr(settings._store, "set", cache_set)

    with pytest.raises(RuntimeError, match="commit failed"):
        settings.save(settings.Settings(True, False, 5.0, 30))

    cache_set.assert_not_called()


def test_save_rejects_an_invalid_audit_source(monkeypatch):
    cursor = RecordingCursor([OFF])
    monkeypatch.setattr(db, "cursor", cursor_context(cursor))

    with pytest.raises(ValueError, match="invalid Auto-Lunch audit source: manual"):
        settings.save(settings.Settings(True), source="manual")

    assert cursor.executed == []


def test_recent_events_reads_newest_first_with_a_bounded_limit(monkeypatch):
    events = [{"id": 7, "source": "settings"}]
    query = Mock(return_value=events)
    monkeypatch.setattr(db, "query", query)

    assert settings.recent_events(999) == events

    sql, params = query.call_args.args
    assert "ORDER BY changed_at DESC, id DESC LIMIT %s" in sql
    assert params == (100,)
