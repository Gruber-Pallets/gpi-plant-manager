from __future__ import annotations

from fastapi.testclient import TestClient

from zira_dashboard import employee_notifications
from zira_dashboard.app import app
from zira_dashboard.routes import timeclock

client = TestClient(app)

PERSON = {"id": 1, "name": "Test Person", "odoo_id": 5,
          "wage_type": "hourly", "spanish_speaker": False}
PERSON_ES = {"id": 2, "name": "José", "odoo_id": 7,
             "wage_type": "hourly", "spanish_speaker": True}


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
    from datetime import date
    monkeypatch.setattr(timeclock, "_person_by_id", lambda pid: PERSON)
    monkeypatch.setattr(
        employee_notifications, "list_unacknowledged",
        lambda oid: [
            {"id": 1, "kind": "time_off_approved",
             "leave_date_from": date(2026, 7, 1), "leave_date_to": date(2026, 7, 3)},
            {"id": 2, "kind": "time_off_denied",
             "leave_date_from": date(2026, 7, 1), "leave_date_to": date(2026, 7, 1)},
        ],
    )
    token = timeclock._mint_token(1)

    resp = client.get(f"/timeclock/notifications/{token}")

    assert resp.status_code == 200
    assert "Time off approved" in resp.text          # title (t() English)
    assert "was approved" in resp.text               # approved body
    assert "was denied" in resp.text                 # denied body
    assert "Jul 1 – Jul 3" in resp.text              # span rendered into the body
    assert f"/timeclock/notifications/ack/{token}" in resp.text


def test_notifications_screen_bilingual_shows_spanish(monkeypatch):
    from datetime import date
    monkeypatch.setattr(timeclock, "_person_by_id", lambda pid: PERSON_ES)
    monkeypatch.setattr(
        employee_notifications, "list_unacknowledged",
        lambda oid: [
            {"id": 1, "kind": "time_off_approved",
             "leave_date_from": date(2026, 7, 1), "leave_date_to": date(2026, 7, 1)},
        ],
    )
    token = timeclock._mint_token(2)

    resp = client.get(f"/timeclock/notifications/{token}")

    assert resp.status_code == 200
    assert "Tiempo libre aprobado" in resp.text      # Spanish title
    assert "fue aprobado" in resp.text               # Spanish body
    assert "was approved" in resp.text               # English still stacked above


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
        lambda oid, today: {
            "full_day": True, "title_key": "Time off reminder",
            "body_key": "Heads up — you have approved time off {day}. Enjoy!",
            "day": "tomorrow (Tuesday, Jun 30)", "hf": "", "ht": ""})
    token = timeclock._mint_token(1)

    resp = client.post(f"/timeclock/clock-out/{token}")

    assert resp.status_code == 200
    assert "approved time off" in resp.text
    assert "Got it" in resp.text
    # Reminder present -> no 3s auto-redirect script. (The base template's
    # idle-timeout script always contains location.href='/timeclock', so we
    # key off the success-template's unique 3000ms delay instead.)
    assert "}, 3000)" not in resp.text


def test_clock_out_reminder_bilingual_shows_spanish(monkeypatch):
    from datetime import datetime, timezone
    from zira_dashboard import time_off_reminder, timeclock_sync, auto_lunch

    monkeypatch.delenv("KIOSK_TIME_OFF_NOTIFY_ENABLED", raising=False)
    monkeypatch.setattr(timeclock, "_person_by_id", lambda pid: PERSON_ES)
    monkeypatch.setattr(timeclock, "_time_off_redirect_if_salaried",
                        lambda p, pid: None)
    monkeypatch.setattr(
        timeclock, "_open_log_row",
        lambda *a, **k: (1, datetime(2026, 6, 29, 22, 0, tzinfo=timezone.utc)))
    monkeypatch.setattr(auto_lunch, "note_employee_clock_out", lambda oid: None)
    monkeypatch.setattr(timeclock_sync, "sync_one_by_id", lambda lid: None)
    monkeypatch.setattr(
        time_off_reminder, "reminder_for_person",
        lambda oid, today: {
            "full_day": True, "title_key": "Time off reminder",
            "body_key": "Heads up — you have approved time off {day}. Enjoy!",
            "day": "tomorrow (Tuesday, Jun 30)", "hf": "", "ht": ""})
    token = timeclock._mint_token(2)

    resp = client.post(f"/timeclock/clock-out/{token}")

    assert resp.status_code == 200
    assert "tiempo libre aprobado" in resp.text   # Spanish reminder body
    assert "Recordatorio de tiempo libre" in resp.text  # Spanish title


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
