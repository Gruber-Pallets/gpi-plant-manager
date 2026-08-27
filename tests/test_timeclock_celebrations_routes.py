from datetime import date

from fastapi.testclient import TestClient

from zira_dashboard import employee_celebrations, employee_notifications
from zira_dashboard.app import app
from zira_dashboard.routes import timeclock


client = TestClient(app)

PERSON = {
    "id": 1,
    "name": "Test Person",
    "odoo_id": 5,
    "wage_type": "hourly",
}


def test_start_keeps_time_off_notice_before_celebration(monkeypatch):
    celebration = employee_celebrations.Celebration(11, 5, "birthday", date(2026, 8, 27), None)
    monkeypatch.setattr(timeclock, "_person_by_id", lambda _person_id: PERSON)
    monkeypatch.setattr(employee_notifications, "has_unacknowledged", lambda _id: True)
    monkeypatch.setattr(employee_celebrations, "next_due", lambda *_: celebration)

    response = client.get("/timeclock/start/1", follow_redirects=False)

    assert response.status_code == 303
    assert "/timeclock/notifications/" in response.headers["location"]


def test_start_routes_due_celebration_before_dashboard(monkeypatch):
    celebration = employee_celebrations.Celebration(11, 5, "birthday", date(2026, 8, 27), None)
    monkeypatch.setattr(timeclock, "_person_by_id", lambda _person_id: PERSON)
    monkeypatch.setattr(employee_notifications, "has_unacknowledged", lambda _id: False)
    monkeypatch.setattr(employee_celebrations, "next_due", lambda *_: celebration)

    response = client.get("/timeclock/start/1", follow_redirects=False)

    assert response.status_code == 303
    assert "/timeclock/celebration/" in response.headers["location"]


def test_acknowledging_celebration_restarts_priority_flow(monkeypatch):
    token = timeclock._mint_token(1)
    monkeypatch.setattr(timeclock, "_person_by_id", lambda _person_id: PERSON)
    monkeypatch.setattr(employee_celebrations, "acknowledge", lambda event_id, oid: True)

    response = client.post(
        f"/timeclock/celebration/ack/{token}",
        data={"celebration_id": 11},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/timeclock/start/1"


def test_celebration_screen_rejects_bad_token():
    response = client.get("/timeclock/celebration/not-a-real-token", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"].startswith("/timeclock")


def test_celebration_ack_rejects_another_persons_event(monkeypatch):
    other_person_event = employee_celebrations.Celebration(
        12, 99, "birthday", date(2026, 8, 27), None
    )
    seen = {}

    def acknowledge(celebration_id, person_odoo_id):
        seen["acknowledged"] = (
            celebration_id == other_person_event.id
            and person_odoo_id == other_person_event.person_odoo_id
        )
        return seen["acknowledged"]

    monkeypatch.setattr(timeclock, "_person_by_id", lambda _person_id: PERSON)
    monkeypatch.setattr(employee_celebrations, "acknowledge", acknowledge)

    response = client.post(
        f"/timeclock/celebration/ack/{timeclock._mint_token(1)}",
        data={"celebration_id": other_person_event.id},
        follow_redirects=False,
    )

    assert seen["acknowledged"] is False
    assert response.status_code == 303
    assert response.headers["location"] == "/timeclock/start/1"


def test_celebration_screen_restarts_when_no_due_event_remains(monkeypatch):
    monkeypatch.setattr(timeclock, "_person_by_id", lambda _person_id: PERSON)
    monkeypatch.setattr(employee_celebrations, "next_due", lambda *_: None)

    response = client.get(
        f"/timeclock/celebration/{timeclock._mint_token(1)}",
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/timeclock/start/1"
