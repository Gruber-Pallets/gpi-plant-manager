from contextlib import contextmanager
from threading import Event, Lock, Thread
from unittest.mock import Mock

import pytest

from zira_dashboard import auto_lunch_settings as settings, db


OFF = {
    "enabled": False, "observe_only": True,
    "flex_after_hours": 5.0, "flex_minutes": 30,
}

ZERO_FLEX = {
    "enabled": False, "observe_only": True,
    "flex_after_hours": 0.0, "flex_minutes": 0,
}


class RecordingCursor:
    def __init__(self, rows):
        self.rows = list(rows)
        self.executed = []

    def execute(self, sql, params=None):
        self.executed.append((" ".join(sql.split()), params))

    def fetchone(self):
        return self.rows.pop(0) if self.rows else None


class TransactionalCursor:
    def __init__(self, row):
        self.row = dict(row)
        self.pending = None

    def execute(self, sql, params=None):
        normalized = " ".join(sql.split())
        if "INSERT INTO auto_lunch_settings " in normalized:
            self.pending = dict(zip(OFF, params, strict=True))

    def fetchone(self):
        return dict(self.row)


class TransactionalDatabase:
    def __init__(self):
        self.row = dict(OFF)
        self.commits = []
        self._row_lock = Lock()

    @contextmanager
    def cursor(self):
        with self._row_lock:
            cursor = TransactionalCursor(self.row)
            yield cursor
            if cursor.pending is not None:
                self.row = cursor.pending
                self.commits.append(settings.Settings(**self.row))


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
    assert cursor.executed[2][1] == (
        False, True, 5.0, 30,
        True, False, 5.0, 30,
        "dale@gruberpallets.com", "Dale", "settings",
    )
    cache_set.assert_called_once_with(live)


def test_save_of_identical_values_is_silent(monkeypatch):
    cursor = RecordingCursor([OFF])
    monkeypatch.setattr(db, "cursor", cursor_context(cursor))

    assert settings.save(settings.Settings()) is False

    assert len(cursor.executed) == 1


def test_save_of_identical_zero_flex_values_is_silent(monkeypatch):
    cursor = RecordingCursor([ZERO_FLEX])
    monkeypatch.setattr(db, "cursor", cursor_context(cursor))
    zero_flex = settings.Settings(flex_after_hours=0.0, flex_minutes=0)

    assert settings.save(zero_flex) is False

    assert len(cursor.executed) == 1


def test_cache_is_not_changed_when_transaction_commit_fails(monkeypatch):
    cursor = RecordingCursor([OFF])
    monkeypatch.setattr(db, "cursor", cursor_context(cursor, fail_after_yield=True))
    cache_set = Mock()
    monkeypatch.setattr(settings._store, "set", cache_set)

    with pytest.raises(RuntimeError, match="commit failed"):
        settings.save(settings.Settings(True, False, 5.0, 30))

    cache_set.assert_not_called()


def test_concurrent_saves_publish_cache_in_commit_order(monkeypatch):
    database = TransactionalDatabase()
    monkeypatch.setattr(db, "cursor", database.cursor)
    first = settings.Settings(True, False, 6.0, 45)
    second = settings.Settings(False, True, 7.0, 60)
    first_at_cache = Event()
    release_first_cache = Event()
    second_started = Event()
    second_published = Event()
    published = []
    errors = []

    def publish(value):
        if value == first:
            first_at_cache.set()
            if not release_first_cache.wait(5):
                raise AssertionError("first cache publication was not released")
        published.append(value)
        if value == second:
            second_published.set()

    def run_save(value, started=None):
        try:
            if started is not None:
                started.set()
            settings.save(value)
        except BaseException as exc:  # pragma: no cover - asserted in main thread
            errors.append(exc)

    monkeypatch.setattr(settings._store, "set", publish)
    first_thread = Thread(target=run_save, args=(first,))
    second_thread = Thread(target=run_save, args=(second, second_started))

    first_thread.start()
    assert first_at_cache.wait(2)
    second_thread.start()
    assert second_started.wait(2)
    try:
        second_overtook_first = second_published.wait(1)
    finally:
        release_first_cache.set()
    first_thread.join(2)
    second_thread.join(2)

    assert not first_thread.is_alive()
    assert not second_thread.is_alive()
    assert errors == []
    assert second_overtook_first is False
    assert database.commits == [first, second]
    assert published == [first, second]
    assert database.row == {
        "enabled": second.enabled,
        "observe_only": second.observe_only,
        "flex_after_hours": second.flex_after_hours,
        "flex_minutes": second.flex_minutes,
    }


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


def test_reconcile_seeds_one_baseline_when_history_is_empty(monkeypatch):
    cursor = RecordingCursor([OFF, None])
    monkeypatch.setattr(db, "cursor", cursor_context(cursor))

    assert settings.reconcile_external_change() == settings.Settings()
    assert "FOR UPDATE" in cursor.executed[0][0]
    assert cursor.executed[-1][1][-1] == "baseline"


def test_reconcile_records_one_external_change(monkeypatch):
    latest = {
        "after_enabled": True, "after_observe_only": False,
        "after_flex_after_hours": 5.0, "after_flex_minutes": 30,
    }
    cursor = RecordingCursor([OFF, latest])
    monkeypatch.setattr(db, "cursor", cursor_context(cursor))

    settings.reconcile_external_change()

    assert cursor.executed[-1][1][-3:] == (None, None, "external")


def test_reconcile_does_not_duplicate_matching_signature(monkeypatch):
    latest = {
        "after_enabled": False, "after_observe_only": True,
        "after_flex_after_hours": 5.0, "after_flex_minutes": 30,
    }
    cursor = RecordingCursor([OFF, latest])
    monkeypatch.setattr(db, "cursor", cursor_context(cursor))

    settings.reconcile_external_change()

    assert len(cursor.executed) == 2


def test_reconcile_refreshes_the_shared_cache(monkeypatch):
    latest = {
        "after_enabled": False, "after_observe_only": True,
        "after_flex_after_hours": 5.0, "after_flex_minutes": 30,
    }
    cursor = RecordingCursor([OFF, latest])
    monkeypatch.setattr(db, "cursor", cursor_context(cursor))
    cache_set = Mock()
    monkeypatch.setattr(settings._store, "set", cache_set)

    settings.reconcile_external_change()

    cache_set.assert_called_once_with(settings.Settings())
