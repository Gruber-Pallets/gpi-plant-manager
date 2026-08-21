from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest

from zira_dashboard import auto_lunch_guard as guard
from zira_dashboard import db
from zira_dashboard import inbox_keys
from zira_dashboard.auto_lunch_settings import Settings

_UNSET = getattr(guard, "_UNSET", object())


@pytest.fixture(autouse=True)
def _reset_published_alert(monkeypatch):
    monkeypatch.setattr(guard, "_published_alert", _UNSET, raising=False)


class FailingAuditCursor:
    def __init__(self, persisted, latest):
        self.rows = [persisted, latest]

    def execute(self, sql, params=None):
        if "INSERT INTO auto_lunch_setting_events" in sql:
            raise RuntimeError("audit unavailable")

    def fetchone(self):
        return self.rows.pop(0)


def cursor_context(cursor):
    @contextmanager
    def opened():
        yield cursor
    return opened


def test_live_mode_has_no_alert_even_with_nondefault_flex(monkeypatch):
    monkeypatch.setattr(guard, "observe", lambda: Settings(True, False, 7.0, 60))
    assert guard.current_alert() is None


def test_current_alert_cold_start_observes_once_then_reuses_published_value(
    monkeypatch,
):
    calls = []
    off = Settings(False, True, 5.0, 30)
    monkeypatch.setattr(guard, "observe", lambda: calls.append("observe") or off)

    assert guard.current_alert()["label"] == "Off"
    assert guard.current_alert()["label"] == "Off"

    assert calls == ["observe"]


def test_current_alert_serializes_concurrent_cold_start(monkeypatch):
    first_observation_started = Event()
    release_first_observation = Event()
    second_observation_started = Event()
    calls = []
    off = Settings(False, True, 5.0, 30)

    def observe():
        calls.append("observe")
        if len(calls) == 1:
            first_observation_started.set()
            assert release_first_observation.wait(timeout=2)
        else:
            second_observation_started.set()
        return off

    monkeypatch.setattr(guard, "observe", observe)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(guard.current_alert)
        assert first_observation_started.wait(timeout=2)
        second = pool.submit(guard.current_alert)
        try:
            assert not second_observation_started.wait(timeout=0.2)
        finally:
            release_first_observation.set()
        assert first.result(timeout=2)["label"] == "Off"
        assert second.result(timeout=2)["label"] == "Off"

    assert calls == ["observe"]


def test_refresh_publishes_latest_alert_without_consumer_reobservation(monkeypatch):
    observed = iter([
        Settings(False, True, 5.0, 30),
        Settings(True, False, 5.0, 30),
    ])
    calls = []

    def observe():
        calls.append("observe")
        return next(observed)

    monkeypatch.setattr(guard, "observe", observe)

    assert guard.refresh()["label"] == "Off"
    assert guard.current_alert()["label"] == "Off"
    assert guard.refresh() is None
    assert guard.current_alert() is None
    assert calls == ["observe", "observe"]


def test_current_alert_returns_prior_publication_while_refresh_observes(monkeypatch):
    off = Settings(False, True, 5.0, 30)
    live = Settings(True, False, 5.0, 30)
    monkeypatch.setattr(guard, "observe", lambda: off)
    assert guard.refresh()["label"] == "Off"

    refresh_started = Event()
    release_refresh = Event()

    def blocked_observe():
        refresh_started.set()
        assert release_refresh.wait(timeout=2)
        return live

    monkeypatch.setattr(guard, "observe", blocked_observe)
    with ThreadPoolExecutor(max_workers=2) as pool:
        refresh = pool.submit(guard.refresh)
        assert refresh_started.wait(timeout=2)
        read = pool.submit(guard.current_alert)
        try:
            prior = read.result(timeout=0.2)
        finally:
            release_refresh.set()
        assert prior["label"] == "Off"
        assert refresh.result(timeout=2) is None


def test_off_mode_returns_stable_urgent_inbox_row(monkeypatch):
    monkeypatch.setattr(guard, "observe", lambda: Settings(False, True, 5.0, 30))
    assert guard.current_alert() == {
        "name": "Auto-Lunch",
        "label": "Off",
        "detail": "Lunch deductions are not being written. Restore Live mode.",
        "priority": "urgent",
        "badge": "Timeclock",
        "href": "/settings?section=timeclock#auto-lunch-form",
        "row_key": "auto_lunch:setting",
        "item_key": "auto_lunch:setting",
    }


def test_auto_lunch_setting_key_is_stable():
    assert inbox_keys.auto_lunch_setting() == "auto_lunch:setting"


def test_alert_uses_canonical_inbox_key(monkeypatch):
    monkeypatch.setattr(guard, "observe", lambda: Settings(False, True, 5.0, 30))
    monkeypatch.setattr(
        inbox_keys,
        "auto_lunch_setting",
        lambda: "auto_lunch:canonical-test",
        raising=False,
    )

    alert = guard.current_alert()

    assert alert["row_key"] == "auto_lunch:canonical-test"
    assert alert["item_key"] == "auto_lunch:canonical-test"


def test_observe_only_uses_plain_label(monkeypatch):
    monkeypatch.setattr(guard, "observe", lambda: Settings(True, True, 5.0, 30))
    assert guard.current_alert()["label"] == "Observe only"


def test_reconciliation_failure_reloads_before_falling_back(monkeypatch, caplog):
    off = Settings(False, True, 5.0, 30)
    monkeypatch.setattr(guard.auto_lunch_settings, "reload", lambda: off)
    monkeypatch.setattr(
        guard.auto_lunch_settings,
        "reconcile_external_change",
        lambda: (_ for _ in ()).throw(RuntimeError("audit unavailable")),
    )

    assert guard.current_alert()["label"] == "Off"
    assert "settings reconciliation failed" in caplog.text


def test_safe_to_unsafe_change_survives_audit_append_failure(
    monkeypatch, caplog,
):
    live = Settings(True, False, 5.0, 30)
    off_row = {
        "enabled": False, "observe_only": True,
        "flex_after_hours": 5.0, "flex_minutes": 30,
    }
    live_event = {
        "after_enabled": True, "after_observe_only": False,
        "after_flex_after_hours": 5.0, "after_flex_minutes": 30,
    }
    cursor = FailingAuditCursor(off_row, live_event)
    monkeypatch.setattr(db, "cursor", cursor_context(cursor))
    monkeypatch.setattr(guard.auto_lunch_settings._store, "_value", live)
    monkeypatch.setattr(guard.auto_lunch_settings._store, "_load", lambda: live)

    assert guard.auto_lunch_settings.current() == live

    alert = guard.current_alert()

    assert alert is not None
    assert alert["label"] == "Off"
    assert "external change audit failed" in caplog.text


def test_observe_fails_closed_when_reconciliation_and_reload_fail(
    monkeypatch, caplog,
):
    monkeypatch.setattr(
        guard.auto_lunch_settings,
        "reconcile_external_change",
        lambda: (_ for _ in ()).throw(RuntimeError("observation failed")),
    )
    monkeypatch.setattr(
        guard.auto_lunch_settings,
        "reload",
        lambda: (_ for _ in ()).throw(RuntimeError("reload failed")),
    )

    assert guard.current_alert()["label"] == "Off"
    assert "settings reload failed" in caplog.text
