from contextlib import contextmanager

from zira_dashboard import auto_lunch_guard as guard
from zira_dashboard import db
from zira_dashboard.auto_lunch_settings import Settings


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
    assert "external change audit failed" in caplog.text


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
