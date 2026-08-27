import os
import logging
from datetime import UTC, date, datetime, time

import pytest
from fastapi.testclient import TestClient

from zira_dashboard import (
    db,
    employee_celebrations,
    employee_notifications,
    saturday_recruiting_store,
)
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


def test_start_skips_celebration_when_queue_lookup_fails(monkeypatch, caplog):
    def fail_due_lookup(*_args):
        raise RuntimeError("queue unavailable")

    monkeypatch.setattr(timeclock, "_person_by_id", lambda _person_id: PERSON)
    monkeypatch.setattr(employee_notifications, "has_unacknowledged", lambda _id: False)
    monkeypatch.setattr(employee_celebrations, "next_due", fail_due_lookup)
    monkeypatch.setattr(timeclock, "_time_off_redirect_if_salaried", lambda _p, _id: None)
    monkeypatch.setattr(timeclock.saturday_recruiting_store, "offer_for_person", lambda *_: None)

    with caplog.at_level(logging.ERROR, logger=timeclock.__name__):
        response = client.get("/timeclock/start/1", follow_redirects=False)

    assert response.status_code == 303
    assert "/timeclock/dashboard/" in response.headers["location"]
    assert "celebration queue lookup failed" in caplog.text.lower()


def test_start_routes_due_celebration_before_salaried_redirect(monkeypatch):
    salaried_person = {**PERSON, "wage_type": "monthly"}
    celebration = employee_celebrations.Celebration(11, 5, "birthday", date(2026, 8, 27), None)
    monkeypatch.setenv("KIOSK_TIME_OFF_ENABLED", "1")
    monkeypatch.setattr(timeclock, "_person_by_id", lambda _person_id: salaried_person)
    monkeypatch.setattr(employee_notifications, "notifications_enabled", lambda: True)
    monkeypatch.setattr(employee_notifications, "has_unacknowledged", lambda _id: False)
    monkeypatch.setattr(employee_celebrations, "next_due", lambda *_: celebration)

    response = client.get("/timeclock/start/1", follow_redirects=False)

    assert response.status_code == 303
    assert "/timeclock/celebration/" in response.headers["location"]
    assert "/timeclock/time-off/" not in response.headers["location"]


def test_start_routes_due_celebration_before_real_saturday_offer(monkeypatch):
    celebration = employee_celebrations.Celebration(11, 5, "birthday", date(2026, 8, 27), None)
    offer = saturday_recruiting_store.Offer(
        date(2026, 8, 29),
        time(7),
        time(12),
        datetime(2026, 8, 28, 7, tzinfo=UTC),
        frozenset({1}),
    )
    offer_checks = []
    monkeypatch.setattr(timeclock, "_person_by_id", lambda _person_id: PERSON)
    monkeypatch.setattr(employee_notifications, "notifications_enabled", lambda: True)
    monkeypatch.setattr(employee_notifications, "has_unacknowledged", lambda _id: False)
    monkeypatch.setattr(employee_celebrations, "next_due", lambda *_: celebration)
    monkeypatch.setattr(
        saturday_recruiting_store,
        "offer_for_person",
        lambda person_id, now: offer_checks.append((person_id, now)) or offer,
    )

    response = client.get("/timeclock/start/1", follow_redirects=False)

    assert response.status_code == 303
    assert "/timeclock/celebration/" in response.headers["location"]
    assert offer_checks == []


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


def test_celebration_ack_restarts_when_queue_acknowledgement_fails(monkeypatch, caplog):
    seen = []

    def fail_acknowledgement(celebration_id, person_odoo_id):
        seen.append((celebration_id, person_odoo_id))
        raise RuntimeError("queue unavailable")

    token = timeclock._mint_token(1)
    monkeypatch.setattr(timeclock, "_person_by_id", lambda _person_id: PERSON)
    monkeypatch.setattr(employee_celebrations, "acknowledge", fail_acknowledgement)

    with caplog.at_level(logging.ERROR, logger=timeclock.__name__):
        response = client.post(
            f"/timeclock/celebration/ack/{token}",
            data={"celebration_id": 11},
            follow_redirects=False,
        )

    assert seen == [(11, 5)]
    assert response.status_code == 303
    assert response.headers["location"] == "/timeclock/start/1"
    assert "celebration queue acknowledgement failed" in caplog.text.lower()


@pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="needs Postgres")
def test_crafted_future_celebration_ack_restarts_without_acknowledging(monkeypatch):
    person_odoo_id = 990733
    today = date(2026, 8, 27)
    db.bootstrap_schema()
    db.execute("DELETE FROM employee_celebrations WHERE person_odoo_id = %s", (person_odoo_id,))
    try:
        celebration_id = db.query(
            "INSERT INTO employee_celebrations (person_odoo_id, kind, event_day) "
            "VALUES (%s, 'birthday', %s) RETURNING id",
            (person_odoo_id, date(2026, 8, 28)),
        )[0]["id"]
        monkeypatch.setattr(timeclock, "_person_by_id", lambda _person_id: {
            **PERSON,
            "odoo_id": person_odoo_id,
        })
        monkeypatch.setattr(employee_celebrations, "plant_today", lambda: today)

        response = client.post(
            f"/timeclock/celebration/ack/{timeclock._mint_token(1)}",
            data={"celebration_id": celebration_id},
            follow_redirects=False,
        )

        assert response.status_code == 303
        assert response.headers["location"] == "/timeclock/start/1"
        row = db.query(
            "SELECT acknowledged_at FROM employee_celebrations WHERE id = %s",
            (celebration_id,),
        )[0]
        assert row["acknowledged_at"] is None
    finally:
        db.execute("DELETE FROM employee_celebrations WHERE person_odoo_id = %s", (person_odoo_id,))


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


def test_celebration_screen_restarts_when_queue_lookup_fails(monkeypatch, caplog):
    def fail_due_lookup(*_args):
        raise RuntimeError("queue unavailable")

    monkeypatch.setattr(timeclock, "_person_by_id", lambda _person_id: PERSON)
    monkeypatch.setattr(employee_celebrations, "next_due", fail_due_lookup)

    with caplog.at_level(logging.ERROR, logger=timeclock.__name__):
        response = client.get(
            f"/timeclock/celebration/{timeclock._mint_token(1)}",
            follow_redirects=False,
        )

    assert response.status_code == 303
    assert response.headers["location"] == "/timeclock/start/1"
    assert "celebration queue lookup failed" in caplog.text.lower()
