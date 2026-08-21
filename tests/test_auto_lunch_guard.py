from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
from threading import Event
import traceback

import pytest

from zira_dashboard import auto_lunch_guard as guard
from zira_dashboard import db
from zira_dashboard import inbox_keys
from zira_dashboard.auto_lunch_settings import Settings

_UNSET = getattr(guard, "_UNSET", object())


@pytest.fixture(autouse=True)
def _reset_published_alert(monkeypatch):
    monkeypatch.setattr(guard, "_published_alert", _UNSET, raising=False)
    monkeypatch.setattr(guard, "_published_failure", None, raising=False)


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


def test_concurrent_cold_failure_is_published_once_and_refresh_recovers(monkeypatch):
    observation_started = Event()
    release_observation = Event()
    waiter_one_started = Event()
    waiter_two_started = Event()
    failures = []

    def fail_observation():
        failures.append(RuntimeError("settings unavailable"))
        observation_started.set()
        assert release_observation.wait(timeout=2)
        raise failures[-1]

    def wait_for_alert(started):
        started.set()
        return guard.current_alert()

    monkeypatch.setattr(guard, "observe", fail_observation)
    with ThreadPoolExecutor(max_workers=3) as pool:
        initiating = pool.submit(guard.current_alert)
        assert observation_started.wait(timeout=2)
        waiter_one = pool.submit(wait_for_alert, waiter_one_started)
        waiter_two = pool.submit(wait_for_alert, waiter_two_started)
        assert waiter_one_started.wait(timeout=2)
        assert waiter_two_started.wait(timeout=2)
        release_observation.set()

        initiating_failure = initiating.exception(timeout=2)
        waiter_failures = [
            waiter_one.exception(timeout=2),
            waiter_two.exception(timeout=2),
        ]

    assert len(failures) == 1
    assert initiating_failure is failures[0]
    assert all(isinstance(exc, guard.AutoLunchSourceError) for exc in waiter_failures)
    assert all("settings unavailable" in str(exc) for exc in waiter_failures)

    monkeypatch.setattr(
        guard,
        "observe",
        lambda: Settings(False, True, 5.0, 30),
    )
    assert guard.refresh()["label"] == "Off"
    assert guard.current_alert()["label"] == "Off"


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


def test_failed_refresh_preserves_stale_alert_and_marks_snapshot_degraded(monkeypatch):
    monkeypatch.setattr(
        guard,
        "observe",
        lambda: Settings(False, True, 5.0, 30),
    )
    assert guard.refresh()["label"] == "Off"

    monkeypatch.setattr(
        guard,
        "observe",
        lambda: (_ for _ in ()).throw(RuntimeError("settings unavailable")),
    )

    with pytest.raises(RuntimeError, match="settings unavailable"):
        guard.refresh()

    snapshot = guard.current_snapshot()
    assert snapshot.alert["label"] == "Off"
    assert snapshot.degraded is True
    with pytest.raises(RuntimeError, match="settings unavailable"):
        guard.current_alert()


def test_failed_refresh_after_live_keeps_no_alert_and_marks_snapshot_degraded(
    monkeypatch,
):
    monkeypatch.setattr(
        guard,
        "observe",
        lambda: Settings(True, False, 5.0, 30),
    )
    assert guard.refresh() is None

    monkeypatch.setattr(
        guard,
        "observe",
        lambda: (_ for _ in ()).throw(RuntimeError("settings unavailable")),
    )

    with pytest.raises(RuntimeError, match="settings unavailable"):
        guard.refresh()

    assert guard.current_snapshot().alert is None
    assert guard.current_snapshot().degraded is True


def test_published_failure_raises_fresh_bounded_exceptions_until_recovery(monkeypatch):
    original = RuntimeError("settings unavailable")
    monkeypatch.setattr(
        guard,
        "observe",
        lambda: (_ for _ in ()).throw(original),
    )

    with pytest.raises(RuntimeError, match="settings unavailable") as initiating:
        guard.refresh()

    assert initiating.value is original
    assert guard._published_alert is _UNSET
    assert guard._published_failure is not original
    assert not isinstance(guard._published_failure, BaseException)

    published_failures = []
    traceback_lengths = []
    for _ in range(3):
        try:
            guard.current_alert()
        except Exception as exc:  # noqa: BLE001 -- inspect the published source error
            published_failures.append(exc)
            traceback_lengths.append(len(traceback.extract_tb(exc.__traceback__)))
        else:
            pytest.fail("published failure did not raise")

    assert all(isinstance(exc, guard.AutoLunchSourceError) for exc in published_failures)
    assert len({id(exc) for exc in published_failures}) == 3
    assert len(set(traceback_lengths)) == 1
    assert traceback_lengths[0] <= 3

    monkeypatch.setattr(
        guard,
        "observe",
        lambda: Settings(True, False, 5.0, 30),
    )
    assert guard.refresh() is None
    assert guard.current_alert() is None


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


def test_observe_propagates_when_reconciliation_and_reload_fail(
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

    with pytest.raises(RuntimeError, match="reload failed"):
        guard.current_alert()

    assert "settings reload failed" in caplog.text
