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


def test_clock_out_shows_reminder_card(monkeypatch):
    from datetime import datetime, timezone
    from zira_dashboard import time_off_reminder, timeclock_sync, auto_lunch

    monkeypatch.delenv("KIOSK_TIME_OFF_NOTIFY_ENABLED", raising=False)
    monkeypatch.setattr(timeclock, "_person_by_id", lambda pid: PERSON)
    monkeypatch.setattr(timeclock, "_time_off_redirect_if_salaried",
                        lambda p, pid: None)
    monkeypatch.setattr(
        timeclock, "_open_log_row",
        lambda *a, **k: (1, datetime(2026, 6, 29, 22, 0, tzinfo=timezone.utc)))
    monkeypatch.setattr(auto_lunch, "note_employee_clock_out", lambda oid: None)
    monkeypatch.setattr(timeclock_sync, "sync_one_by_id", lambda lid: None)
    monkeypatch.setattr(
        time_off_reminder, "reminder_for_person",
        lambda oid, today: {"title": "Time off reminder 🌴",
                            "body": "Heads up — you have approved time off "
                                    "tomorrow (Tuesday, Jun 30). Enjoy!"})
    token = timeclock._mint_token(1)

    resp = client.post(f"/timeclock/clock-out/{token}")

    assert resp.status_code == 200
    assert "approved time off" in resp.text
    assert "Got it" in resp.text
    # Reminder present -> no 3s auto-redirect script. (The base template's
    # idle-timeout script always contains location.href='/timeclock', so we
    # key off the success-template's unique 3000ms delay instead.)
    assert "}, 3000)" not in resp.text


def test_clock_out_no_reminder_keeps_auto_redirect(monkeypatch):
    from datetime import datetime, timezone
    from zira_dashboard import time_off_reminder, timeclock_sync, auto_lunch

    monkeypatch.delenv("KIOSK_TIME_OFF_NOTIFY_ENABLED", raising=False)
    monkeypatch.setattr(timeclock, "_person_by_id", lambda pid: PERSON)
    monkeypatch.setattr(timeclock, "_time_off_redirect_if_salaried",
                        lambda p, pid: None)
    monkeypatch.setattr(
        timeclock, "_open_log_row",
        lambda *a, **k: (1, datetime(2026, 6, 29, 22, 0, tzinfo=timezone.utc)))
    monkeypatch.setattr(auto_lunch, "note_employee_clock_out", lambda oid: None)
    monkeypatch.setattr(timeclock_sync, "sync_one_by_id", lambda lid: None)
    monkeypatch.setattr(time_off_reminder, "reminder_for_person",
                        lambda oid, today: None)
    token = timeclock._mint_token(1)

    resp = client.post(f"/timeclock/clock-out/{token}")

    assert resp.status_code == 200
    # No reminder -> success-template's 3s auto-redirect script is present.
    assert "}, 3000)" in resp.text
