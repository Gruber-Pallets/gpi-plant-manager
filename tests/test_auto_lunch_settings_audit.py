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


class ReconcileSaveCursor:
    def __init__(self, database):
        self.database = database
        self.results = []
        self.pending_settings = None

    def execute(self, sql, params=None):
        normalized = " ".join(sql.split())
        if "FROM auto_lunch_settings WHERE id = 1 FOR UPDATE" in normalized:
            self.results.append(dict(self.database.row))
        elif "FROM auto_lunch_setting_events" in normalized:
            self.results.append(dict(self.database.latest))
        elif "INSERT INTO auto_lunch_settings " in normalized:
            self.pending_settings = dict(zip(OFF, params, strict=True))

    def fetchone(self):
        return self.results.pop(0)


class ReconcileSaveDatabase:
    def __init__(self):
        self.row = dict(OFF)
        self.latest = {
            "after_enabled": False,
            "after_observe_only": True,
            "after_flex_after_hours": 5.0,
            "after_flex_minutes": 30,
        }
        self.commits = []
        self._row_lock = Lock()

    @contextmanager
    def cursor(self):
        with self._row_lock:
            cursor = ReconcileSaveCursor(self)
            yield cursor
            if cursor.pending_settings is not None:
                self.row = cursor.pending_settings
                committed = settings.Settings(**self.row)
                self.commits.append(committed)
                self.latest = {
                    "after_enabled": committed.enabled,
                    "after_observe_only": committed.observe_only,
                    "after_flex_after_hours": committed.flex_after_hours,
                    "after_flex_minutes": committed.flex_minutes,
                }


class FailingAuditCursor(RecordingCursor):
    def execute(self, sql, params=None):
        super().execute(sql, params)
        if "INSERT INTO auto_lunch_setting_events" in sql:
            raise RuntimeError("audit unavailable")


class NoopProcessLock:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False


class MissingRowReconcileCursor:
    def __init__(self, database):
        self.database = database
        self.results = []
        self.pending_event = None
        self.advisory_locked = False

    def execute(self, sql, params=None):
        normalized = " ".join(sql.split())
        if "pg_advisory_xact_lock" in normalized:
            self.database.advisory_lock.acquire()
            self.advisory_locked = True
        elif "FROM auto_lunch_settings" in normalized:
            self.results.append(None)
        elif "FROM auto_lunch_setting_events" in normalized:
            with self.database.state_lock:
                latest = dict(self.database.latest) if self.database.latest else None
                if latest is None and not self.advisory_locked:
                    self.database.history_reads += 1
                    if self.database.history_reads == 2:
                        self.database.concurrent_history_reads.set()
            if latest is None and not self.advisory_locked:
                assert self.database.concurrent_history_reads.wait(2)
            self.results.append(latest)
        elif "INSERT INTO auto_lunch_setting_events" in normalized:
            self.pending_event = {
                "after_enabled": params[4],
                "after_observe_only": params[5],
                "after_flex_after_hours": params[6],
                "after_flex_minutes": params[7],
                "source": params[-1],
            }

    def fetchone(self):
        return self.results.pop(0)


class MissingRowReconcileDatabase:
    def __init__(self):
        self.advisory_lock = Lock()
        self.state_lock = Lock()
        self.concurrent_history_reads = Event()
        self.history_reads = 0
        self.latest = None
        self.events = []

    @contextmanager
    def cursor(self):
        cursor = MissingRowReconcileCursor(self)
        try:
            yield cursor
            if cursor.pending_event is not None:
                with self.state_lock:
                    self.events.append(cursor.pending_event)
                    self.latest = cursor.pending_event
        finally:
            if cursor.advisory_locked:
                cursor.database.advisory_lock.release()


def cursor_context(cursor, *, fail_after_yield=False):
    @contextmanager
    def opened():
        yield cursor
        if fail_after_yield:
            raise RuntimeError("commit failed")
    return opened


def test_save_and_reconcile_take_same_advisory_lock_before_singleton_row(
    monkeypatch,
):
    save_cursor = RecordingCursor([OFF])
    monkeypatch.setattr(db, "cursor", cursor_context(save_cursor))
    assert settings.save(settings.Settings()) is False

    reconcile_cursor = RecordingCursor([
        OFF,
        {
            "after_enabled": False,
            "after_observe_only": True,
            "after_flex_after_hours": 5.0,
            "after_flex_minutes": 30,
        },
    ])
    monkeypatch.setattr(db, "cursor", cursor_context(reconcile_cursor))
    settings.reconcile_external_change()

    save_lock_sql, save_lock_params = save_cursor.executed[0]
    reconcile_lock_sql, reconcile_lock_params = reconcile_cursor.executed[0]
    assert "pg_advisory_xact_lock" in save_lock_sql
    assert "pg_advisory_xact_lock" in reconcile_lock_sql
    assert save_lock_params == reconcile_lock_params
    assert "auto_lunch_settings" in save_cursor.executed[1][0]
    assert "FOR UPDATE" in save_cursor.executed[1][0]
    assert "auto_lunch_settings" in reconcile_cursor.executed[1][0]
    assert "FOR UPDATE" in reconcile_cursor.executed[1][0]


def test_missing_singleton_concurrent_reconcilers_write_one_baseline(monkeypatch):
    database = MissingRowReconcileDatabase()
    monkeypatch.setattr(db, "cursor", database.cursor)
    monkeypatch.setattr(settings, "_save_lock", NoopProcessLock())
    monkeypatch.setattr(settings._store, "set", lambda _value: None)
    results = []
    errors = []

    def reconcile():
        try:
            results.append(settings.reconcile_external_change())
        except BaseException as exc:  # pragma: no cover - asserted below
            errors.append(exc)

    first = Thread(target=reconcile)
    second = Thread(target=reconcile)
    first.start()
    second.start()
    first.join(3)
    second.join(3)

    assert not first.is_alive()
    assert not second.is_alive()
    assert errors == []
    assert results == [settings.DEFAULT, settings.DEFAULT]
    assert [event["source"] for event in database.events] == ["baseline"]


def test_save_writes_setting_and_actor_audit_in_one_cursor(monkeypatch):
    cursor = RecordingCursor([OFF])
    monkeypatch.setattr(db, "cursor", cursor_context(cursor))
    cache_set = Mock()
    monkeypatch.setattr(settings._store, "set", cache_set)
    live = settings.Settings(True, False, 5.0, 30)

    assert settings.save(
        live, actor_upn="dale@gruberpallets.com", actor_name="Dale"
    ) is True

    assert len(cursor.executed) == 4
    assert "pg_advisory_xact_lock" in cursor.executed[0][0]
    assert "FOR UPDATE" in cursor.executed[1][0]
    assert "INSERT INTO auto_lunch_settings" in cursor.executed[2][0]
    assert "INSERT INTO auto_lunch_setting_events" in cursor.executed[3][0]
    assert cursor.executed[3][1] == (
        False, True, 5.0, 30,
        True, False, 5.0, 30,
        "dale@gruberpallets.com", "Dale", "settings",
    )
    cache_set.assert_called_once_with(live)


def test_save_of_identical_values_is_silent(monkeypatch):
    cursor = RecordingCursor([OFF])
    monkeypatch.setattr(db, "cursor", cursor_context(cursor))

    assert settings.save(settings.Settings()) is False

    assert len(cursor.executed) == 2


def test_save_of_identical_zero_flex_values_is_silent(monkeypatch):
    cursor = RecordingCursor([ZERO_FLEX])
    monkeypatch.setattr(db, "cursor", cursor_context(cursor))
    zero_flex = settings.Settings(flex_after_hours=0.0, flex_minutes=0)

    assert settings.save(zero_flex) is False

    assert len(cursor.executed) == 2


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


def test_reconcile_and_save_publish_cache_in_commit_order(monkeypatch):
    database = ReconcileSaveDatabase()
    monkeypatch.setattr(db, "cursor", database.cursor)
    off = settings.Settings()
    live = settings.Settings(True, False, 6.0, 45)
    reconcile_at_cache = Event()
    release_reconcile_cache = Event()
    save_started = Event()
    save_published = Event()
    published = []
    errors = []

    def publish(value):
        if value == off and not reconcile_at_cache.is_set():
            reconcile_at_cache.set()
            if not release_reconcile_cache.wait(5):
                raise AssertionError("reconciliation cache was not released")
        published.append(value)
        if value == live:
            save_published.set()

    def run_reconcile():
        try:
            settings.reconcile_external_change()
        except BaseException as exc:  # pragma: no cover - asserted in main thread
            errors.append(exc)

    def run_save():
        try:
            save_started.set()
            settings.save(live)
        except BaseException as exc:  # pragma: no cover - asserted in main thread
            errors.append(exc)

    monkeypatch.setattr(settings._store, "set", publish)
    reconcile_thread = Thread(target=run_reconcile)
    save_thread = Thread(target=run_save)

    reconcile_thread.start()
    assert reconcile_at_cache.wait(2)
    save_thread.start()
    assert save_started.wait(2)
    try:
        save_overtook_reconcile = save_published.wait(1)
    finally:
        release_reconcile_cache.set()
    reconcile_thread.join(2)
    save_thread.join(2)

    assert not reconcile_thread.is_alive()
    assert not save_thread.is_alive()
    assert errors == []
    assert save_overtook_reconcile is False
    assert database.commits == [live]
    assert published == [off, live]
    assert published[-1] == settings.Settings(**database.row)


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
    assert "pg_advisory_xact_lock" in cursor.executed[0][0]
    assert "FOR UPDATE" in cursor.executed[1][0]
    event_inserts = [
        call for call in cursor.executed
        if "INSERT INTO auto_lunch_setting_events" in call[0]
    ]
    assert event_inserts[0][1][-1] == "baseline"


def test_reconcile_records_one_external_change(monkeypatch):
    latest = {
        "after_enabled": True, "after_observe_only": False,
        "after_flex_after_hours": 5.0, "after_flex_minutes": 30,
    }
    cursor = RecordingCursor([OFF, latest])
    monkeypatch.setattr(db, "cursor", cursor_context(cursor))

    settings.reconcile_external_change()

    event_inserts = [
        call for call in cursor.executed
        if "INSERT INTO auto_lunch_setting_events" in call[0]
    ]
    assert event_inserts[0][1][-3:] == (None, None, "external")


def test_reconcile_records_a_flex_only_external_change(monkeypatch):
    persisted_live = {
        "enabled": True, "observe_only": False,
        "flex_after_hours": 6.0, "flex_minutes": 45,
    }
    previous_live = {
        "after_enabled": True, "after_observe_only": False,
        "after_flex_after_hours": 5.0, "after_flex_minutes": 30,
    }
    cursor = RecordingCursor([persisted_live, previous_live])
    monkeypatch.setattr(db, "cursor", cursor_context(cursor))

    observed = settings.reconcile_external_change()

    event_inserts = [
        call for call in cursor.executed
        if "INSERT INTO auto_lunch_setting_events" in call[0]
    ]
    assert observed == settings.Settings(True, False, 6.0, 45)
    assert event_inserts[0][1][-3:] == (None, None, "external")


def test_reconcile_does_not_duplicate_matching_signature(monkeypatch):
    latest = {
        "after_enabled": False, "after_observe_only": True,
        "after_flex_after_hours": 5.0, "after_flex_minutes": 30,
    }
    cursor = RecordingCursor([OFF, latest])
    monkeypatch.setattr(db, "cursor", cursor_context(cursor))

    settings.reconcile_external_change()

    assert all(
        "INSERT INTO auto_lunch_setting_events" not in sql
        for sql, _params in cursor.executed
    )


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


def test_reconcile_does_not_publish_cache_when_commit_fails(monkeypatch):
    latest = {
        "after_enabled": False, "after_observe_only": True,
        "after_flex_after_hours": 5.0, "after_flex_minutes": 30,
    }
    cursor = RecordingCursor([OFF, latest])
    monkeypatch.setattr(db, "cursor", cursor_context(cursor, fail_after_yield=True))
    cache_set = Mock()
    monkeypatch.setattr(settings._store, "set", cache_set)

    with pytest.raises(RuntimeError, match="commit failed"):
        settings.reconcile_external_change()

    cache_set.assert_not_called()


def test_reconcile_returns_persisted_state_when_audit_append_fails(
    monkeypatch, caplog,
):
    live = {
        "after_enabled": True, "after_observe_only": False,
        "after_flex_after_hours": 5.0, "after_flex_minutes": 30,
    }
    cursor = FailingAuditCursor([OFF, live])
    monkeypatch.setattr(db, "cursor", cursor_context(cursor))
    cache_set = Mock()
    monkeypatch.setattr(settings._store, "set", cache_set)

    assert settings.reconcile_external_change() == settings.Settings()
    assert "external change audit failed" in caplog.text
    assert any("ROLLBACK TO SAVEPOINT" in sql for sql, _ in cursor.executed)
    cache_set.assert_called_once_with(settings.Settings())
