from __future__ import annotations

from fastapi.testclient import TestClient

from zira_dashboard import employee_notifications
from zira_dashboard.app import app
from zira_dashboard.routes import timeclock

client = TestClient(app)

PERSON = {"id": 1, "name": "Test Person", "odoo_id": 5,
          "wage_type": "hourly", "spanish_speaker": False}


def test_start_redirects_to_notifications_when_unacked(monkeypatch):
    monkeypatch.delenv("KIOSK_TIME_OFF_NOTIFY_ENABLED", raising=False)
    monkeypatch.setattr(timeclock, "_person_by_id", lambda pid: PERSON)
    monkeypatch.setattr(employee_notifications, "has_unacknowledged",
                        lambda oid: True)

    resp = client.get("/timeclock/start/1", follow_redirects=False)

    assert resp.status_code == 303
    assert "/timeclock/notifications/" in resp.headers["location"]


def test_start_goes_to_dashboard_when_none(monkeypatch):
    monkeypatch.delenv("KIOSK_TIME_OFF_NOTIFY_ENABLED", raising=False)
    monkeypatch.setattr(timeclock, "_person_by_id", lambda pid: PERSON)
    monkeypatch.setattr(employee_notifications, "has_unacknowledged",
                        lambda oid: False)
    monkeypatch.setattr(timeclock, "_time_off_redirect_if_salaried",
                        lambda p, pid: None)

    resp = client.get("/timeclock/start/1", follow_redirects=False)

    assert resp.status_code == 303
    assert "/timeclock/dashboard/" in resp.headers["location"]


def test_notifications_screen_lists_cards(monkeypatch):
    monkeypatch.setattr(timeclock, "_person_by_id", lambda pid: PERSON)
    monkeypatch.setattr(
        employee_notifications, "list_unacknowledged",
        lambda oid: [
            {"id": 1, "kind": "time_off_approved",
             "title": "Time off approved", "body": "Your time off was approved."},
            {"id": 2, "kind": "time_off_denied",
             "title": "Time off denied", "body": "Your request was denied."},
        ],
    )
    token = timeclock._mint_token(1)

    resp = client.get(f"/timeclock/notifications/{token}")

    assert resp.status_code == 200
    assert "Time off approved" in resp.text
    assert "Your request was denied." in resp.text
    assert f"/timeclock/notifications/ack/{token}" in resp.text


def test_notifications_screen_skips_to_dashboard_when_empty(monkeypatch):
    monkeypatch.setattr(timeclock, "_person_by_id", lambda pid: PERSON)
    monkeypatch.setattr(employee_notifications, "list_unacknowledged",
                        lambda oid: [])
    token = timeclock._mint_token(1)

    resp = client.get(f"/timeclock/notifications/{token}",
                      follow_redirects=False)

    assert resp.status_code == 303
    assert "/timeclock/dashboard/" in resp.headers["location"]


def test_ack_acknowledges_and_redirects(monkeypatch):
    monkeypatch.setattr(timeclock, "_person_by_id", lambda pid: PERSON)
    seen = {}
    monkeypatch.setattr(employee_notifications, "acknowledge_all",
                        lambda oid: seen.setdefault("oid", oid))
    token = timeclock._mint_token(1)

    resp = client.post(f"/timeclock/notifications/ack/{token}",
                       follow_redirects=False)

    assert resp.status_code == 303
    assert seen["oid"] == 5  # the signing-in person's odoo id
    assert "/timeclock/dashboard/" in resp.headers["location"]


def test_notifications_screen_rejects_bad_token():
    resp = client.get("/timeclock/notifications/not-a-real-token",
                      follow_redirects=False)
    assert resp.status_code == 303
    assert "/timeclock" in resp.headers["location"]
