from __future__ import annotations

from datetime import datetime, date, UTC
from decimal import Decimal
from unittest.mock import MagicMock

from fastapi.testclient import TestClient

from zira_dashboard import employee_notifications, staffing
from zira_dashboard.app import app


client = TestClient(app)


def _person(name="Carlos", employee_id=5):
    return staffing.Person(name, active=True, employee_id=employee_id)


def _history_row(**overrides):
    row = {
        "kind": "anniversary_pto_reminder",
        "title": "Your work anniversary is coming up",
        "body": "snapshot",
        "anniversary_date": date(2026, 10, 2),
        "balance_amount": Decimal("2.5"),
        "balance_unit": "days",
        "presented_at": datetime(2026, 9, 2, 11, 54, tzinfo=UTC),
        "acknowledged_at": datetime(2026, 9, 2, 11, 55, tzinfo=UTC),
    }
    row.update(overrides)
    return row


def test_employee_history_endpoint_is_person_scoped(monkeypatch):
    monkeypatch.setattr(staffing, "load_roster", lambda: [_person()])
    history = MagicMock(return_value=[_history_row()])
    monkeypatch.setattr(employee_notifications, "list_history", history)

    response = client.get("/staffing/people/Carlos/acknowledgements")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "private, no-store"
    history.assert_called_once_with(5)
    assert "Anniversary PTO reminder" in response.text
    assert "2.5 days" in response.text
    assert "Acknowledged" in response.text
    assert "Sep 2, 2026 · 6:55 AM" in response.text


def test_employee_history_shows_waiting_and_legacy_display_state(monkeypatch):
    monkeypatch.setattr(staffing, "load_roster", lambda: [_person()])
    monkeypatch.setattr(
        employee_notifications,
        "list_history",
        lambda _id: [_history_row(presented_at=None, acknowledged_at=None)],
    )

    response = client.get("/staffing/people/Carlos/acknowledgements")

    assert "Not recorded" in response.text
    assert "Waiting for acknowledgement" in response.text


def test_employee_history_empty_state(monkeypatch):
    monkeypatch.setattr(staffing, "load_roster", lambda: [_person()])
    monkeypatch.setattr(employee_notifications, "list_history", lambda _id: [])

    response = client.get("/staffing/people/Carlos/acknowledgements")

    assert "No acknowledgement history yet." in response.text


def test_employee_history_unknown_employee_is_404(monkeypatch):
    monkeypatch.setattr(staffing, "load_roster", lambda: [])

    response = client.get("/staffing/people/Missing/acknowledgements")

    assert response.status_code == 404


def test_employee_history_failure_renders_unavailable(monkeypatch):
    monkeypatch.setattr(staffing, "load_roster", lambda: [_person()])
    monkeypatch.setattr(
        employee_notifications,
        "list_history",
        MagicMock(side_effect=RuntimeError("database unavailable")),
    )

    response = client.get("/staffing/people/Carlos/acknowledgements")

    assert response.status_code == 200
    assert "Acknowledgement history is unavailable right now." in response.text


def test_employee_without_odoo_id_has_empty_history(monkeypatch):
    monkeypatch.setattr(staffing, "load_roster", lambda: [_person(employee_id=None)])
    history = MagicMock()
    monkeypatch.setattr(employee_notifications, "list_history", history)

    response = client.get("/staffing/people/Carlos/acknowledgements")

    assert response.status_code == 200
    assert "No acknowledgement history yet." in response.text
    history.assert_not_called()


def test_employee_landing_url_encodes_reserved_name_characters(monkeypatch):
    monkeypatch.setattr(staffing, "load_roster", lambda: [_person("Ana & José")])

    response = client.get("/staffing/people", follow_redirects=False)

    assert response.status_code == 307
    assert response.headers["location"] == "/staffing/people/Ana%20%26%20Jos%C3%A9"
