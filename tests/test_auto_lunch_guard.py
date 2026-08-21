from zira_dashboard import auto_lunch_guard as guard
from zira_dashboard.auto_lunch_settings import Settings


def test_live_mode_has_no_alert(monkeypatch):
    monkeypatch.setattr(guard, "observe", lambda: Settings(True, False, 5.0, 30))
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


def test_audit_failure_cannot_hide_off_alert(monkeypatch, caplog):
    off = Settings(False, True, 5.0, 30)
    monkeypatch.setattr(guard.auto_lunch_settings, "reload", lambda: off)
    monkeypatch.setattr(
        guard.auto_lunch_settings,
        "reconcile_external_change",
        lambda: (_ for _ in ()).throw(RuntimeError("audit unavailable")),
    )

    assert guard.current_alert()["label"] == "Off"
    assert "external change audit failed" in caplog.text
