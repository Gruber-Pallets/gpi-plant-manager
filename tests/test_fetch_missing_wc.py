"""odoo_client.fetch_attendances_missing_wc — shaping + WC-field guard (mocked)."""

from datetime import datetime, timezone

from zira_dashboard import odoo_client


def test_returns_shaped_rows_when_wc_field_configured(monkeypatch):
    monkeypatch.setattr(odoo_client, "_kiosk_wc_field", lambda: "x_kiosk_wc")
    captured = {}

    def fake_execute(model, method, *args, **kwargs):
        captured["model"] = model
        captured["method"] = method
        captured["domain"] = args[0]
        return [{
            "id": 55, "employee_id": [7, "Maria Lopez"],
            "check_in": "2026-06-02 11:58:00", "check_out": False,
        }]

    monkeypatch.setattr(odoo_client, "execute", fake_execute)
    since = datetime(2026, 5, 21, tzinfo=timezone.utc)
    rows = odoo_client.fetch_attendances_missing_wc(since)
    assert captured["model"] == "hr.attendance"
    assert captured["method"] == "search_read"
    assert ("x_kiosk_wc", "=", False) in captured["domain"]
    assert rows == [{
        "att_id": 55, "employee_odoo_id": 7, "employee_name": "Maria Lopez",
        "check_in": rows[0]["check_in"], "check_out": None,
    }]
    assert rows[0]["check_in"]  # ISO string, non-empty


def test_returns_empty_when_wc_field_not_configured(monkeypatch):
    monkeypatch.setattr(odoo_client, "_kiosk_wc_field", lambda: None)
    called = {"execute": False}
    monkeypatch.setattr(odoo_client, "execute",
                        lambda *a, **k: called.__setitem__("execute", True) or [])
    rows = odoo_client.fetch_attendances_missing_wc(datetime.now(timezone.utc))
    assert rows == []
    assert called["execute"] is False  # never hits Odoo
