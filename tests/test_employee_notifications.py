from __future__ import annotations

from datetime import date

import pytest

from zira_dashboard import employee_notifications as en


@pytest.fixture
def fake_db(monkeypatch):
    captured: dict = {"queries": [], "executes": []}

    def fake_query(sql, params=None):
        captured["queries"].append((sql, params))
        return captured.get("query_result", [])

    def fake_execute(sql, params=None):
        captured["executes"].append((sql, params))

    monkeypatch.setattr(en.db, "query", fake_query)
    monkeypatch.setattr(en.db, "execute", fake_execute)
    return captured


def test_notifications_enabled_default_on(monkeypatch):
    monkeypatch.delenv("KIOSK_TIME_OFF_NOTIFY_ENABLED", raising=False)
    assert en.notifications_enabled() is True


def test_notifications_enabled_off_when_zero(monkeypatch):
    monkeypatch.setenv("KIOSK_TIME_OFF_NOTIFY_ENABLED", "0")
    assert en.notifications_enabled() is False


def test_create_inserts_with_on_conflict_do_nothing(fake_db):
    req = {
        "id": 7, "person_odoo_id": 5, "odoo_leave_id": 88,
        "date_from": date(2026, 7, 1), "date_to": date(2026, 7, 3),
    }
    en.create_time_off_notification(5, "time_off_approved", req)

    assert len(fake_db["executes"]) == 1
    sql, params = fake_db["executes"][0]
    assert "INSERT INTO employee_notifications" in sql
    assert "ON CONFLICT (time_off_request_id, kind) DO NOTHING" in sql
    assert params[0] == 5
    assert params[1] == "time_off_approved"
    assert 7 in params and 88 in params


def test_render_messages_distinct_per_kind():
    req = {"date_from": date(2026, 7, 1), "date_to": date(2026, 7, 1)}
    approved_title, approved_body = en._render("time_off_approved", req)
    denied_title, denied_body = en._render("time_off_denied", req)
    cancelled_title, cancelled_body = en._render("time_off_cancelled", req)
    assert "approved" in approved_body.lower()
    assert "denied" in denied_body.lower()
    assert "cancelled" in cancelled_body.lower()
    assert "–" not in approved_body


def test_render_multi_day_shows_span():
    req = {"date_from": date(2026, 7, 1), "date_to": date(2026, 7, 3)}
    _, body = en._render("time_off_approved", req)
    assert "–" in body  # "Jul 1 – Jul 3"


def test_has_unacknowledged_true_when_rows(fake_db):
    fake_db["query_result"] = [{"?column?": 1}]
    assert en.has_unacknowledged(5) is True
    sql, params = fake_db["queries"][0]
    assert "acknowledged_at IS NULL" in sql
    assert params == (5,)


def test_has_unacknowledged_false_when_empty(fake_db):
    fake_db["query_result"] = []
    assert en.has_unacknowledged(5) is False


def test_list_unacknowledged_filters_by_person_and_unacked(fake_db):
    fake_db["query_result"] = [{"id": 1, "title": "t", "body": "b"}]
    out = en.list_unacknowledged(5)
    assert out == [{"id": 1, "title": "t", "body": "b"}]
    sql, params = fake_db["queries"][0]
    assert "acknowledged_at IS NULL" in sql
    assert "ORDER BY created_at" in sql
    assert params == (5,)


def test_acknowledge_all_is_person_scoped(fake_db):
    en.acknowledge_all(5)
    sql, params = fake_db["executes"][0]
    assert "UPDATE employee_notifications SET acknowledged_at = now()" in sql
    assert "person_odoo_id = %s" in sql
    assert "acknowledged_at IS NULL" in sql
    assert params == (5,)
