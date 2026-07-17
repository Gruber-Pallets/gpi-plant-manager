"""Kiosk guard for employees reporting in during approved full-day leave."""
from __future__ import annotations

from datetime import date

from fastapi.testclient import TestClient

from zira_dashboard.app import app
from zira_dashboard.routes import timeclock


client = TestClient(app)
PERSON = {
    "id": 1,
    "name": "Test Person",
    "odoo_id": 5,
    "wage_type": "hourly",
    "spanish_speaker": False,
}
LEAVE = {"id": 42, "odoo_leave_id": 900, "person_odoo_id": 5}


def _dashboard_dependencies(monkeypatch, *, scheduled_wc="Line 1"):
    monkeypatch.setattr(timeclock, "_person_by_id", lambda _pid: PERSON)
    monkeypatch.setattr(timeclock, "_current_state", lambda _oid: {
        "is_clocked_in": False, "current_wc": None, "check_in_ts": None,
    })
    monkeypatch.setattr(timeclock, "_scheduled_wc_for", lambda _name: scheduled_wc)
    monkeypatch.setattr(timeclock, "_sync_error_warning", lambda _oid: None)
    monkeypatch.setattr(timeclock, "_saturday_commitment_context", lambda _pid: None)
    monkeypatch.setattr(timeclock, "_time_off_enabled", lambda: False)
    monkeypatch.setattr(timeclock, "_time_off_redirect_if_salaried", lambda *_args: None)


def test_dashboard_for_approved_full_day_leave_requires_work_center_choice(monkeypatch):
    _dashboard_dependencies(monkeypatch)
    monkeypatch.setattr(timeclock.unexpected_worker, "approved_full_day_leave",
                        lambda _oid, _day: LEAVE)

    response = client.get(f"/timeclock/dashboard/{timeclock._mint_token(1)}")

    assert response.status_code == 200
    assert "approved time off" in response.text.lower()
    assert "/timeclock/pick-wc/" in response.text
    assert "Confirm — Clock In" not in response.text


def test_first_clock_in_on_approved_leave_renders_confirmation_with_inputs(monkeypatch):
    monkeypatch.setattr(timeclock, "_person_by_id", lambda _pid: PERSON)
    monkeypatch.setattr(timeclock, "_time_off_redirect_if_salaried", lambda *_args: None)
    monkeypatch.setattr(timeclock.unexpected_worker, "approved_full_day_leave",
                        lambda _oid, _day: LEAVE)
    monkeypatch.setattr(timeclock, "_open_log_row",
                        lambda *_args: (_ for _ in ()).throw(AssertionError("must not punch")))

    response = client.post(
        f"/timeclock/clock-in/{timeclock._mint_token(1)}",
        data={"wc_name": "Line 2", "scheduled_wc_name": "Line 1"},
    )

    assert response.status_code == 200
    assert "approved time off" in response.text.lower()
    assert "confirming will cancel today's approved time off in odoo and clock you in." in response.text.lower()
    assert 'name="wc_name" value="Line 2"' in response.text
    assert 'name="scheduled_wc_name" value="Line 1"' in response.text
    assert "/timeclock/clock-in/confirm/" in response.text


def test_confirmed_override_refuses_then_marks_leave_records_event_and_punches(monkeypatch):
    monkeypatch.setattr(timeclock, "_person_by_id", lambda _pid: PERSON)
    monkeypatch.setattr(timeclock, "_time_off_redirect_if_salaried", lambda *_args: None)
    monkeypatch.setattr(timeclock.unexpected_worker, "approved_full_day_leave",
                        lambda _oid, _day: LEAVE)
    order = []
    monkeypatch.setattr(timeclock.odoo_client, "refuse_leave",
                        lambda leave_id: order.append(("refuse", leave_id)))
    monkeypatch.setattr(timeclock.db, "execute",
                        lambda sql, params: order.append(("mirror", params)))
    monkeypatch.setattr(timeclock.unexpected_worker, "record",
                        lambda **kwargs: order.append(("event", kwargs)) or {"id": 7})
    monkeypatch.setattr(timeclock, "_open_log_row",
                        lambda *args: order.append(("punch", args)) or (88, date(2026, 7, 17)))
    monkeypatch.setattr(timeclock, "_fmt_time", lambda _value: "7:00 AM")
    monkeypatch.setattr(timeclock, "_log_variance",
                        lambda *args: order.append(("variance", args)))
    monkeypatch.setattr(timeclock.timeclock_sync, "sync_one_by_id", lambda _id: None)

    response = client.post(
        f"/timeclock/clock-in/confirm/{timeclock._mint_token(1)}",
        data={"wc_name": "Line 2", "scheduled_wc_name": "Line 1"},
    )

    assert response.status_code == 200
    assert [step[0] for step in order[:4]] == ["refuse", "mirror", "event", "punch"]
    assert order[0] == ("refuse", 900)
    assert order[1][1][0] == "refuse"
    assert order[2][1]["clock_in_wc"] == "Line 2"
    assert order[3][1] == (5, "clock_in", "Line 2")
    assert order[4] == ("variance", (5, "Line 1", "Line 2"))


def test_refusal_failure_shows_error_without_event_or_punch(monkeypatch):
    monkeypatch.setattr(timeclock, "_person_by_id", lambda _pid: PERSON)
    monkeypatch.setattr(timeclock, "_time_off_redirect_if_salaried", lambda *_args: None)
    monkeypatch.setattr(timeclock.unexpected_worker, "approved_full_day_leave",
                        lambda _oid, _day: LEAVE)
    monkeypatch.setattr(timeclock.odoo_client, "refuse_leave",
                        lambda _leave_id: (_ for _ in ()).throw(RuntimeError("Odoo down")))
    monkeypatch.setattr(timeclock.unexpected_worker, "record",
                        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("must not event")))
    monkeypatch.setattr(timeclock, "_open_log_row",
                        lambda *_args: (_ for _ in ()).throw(AssertionError("must not punch")))
    monkeypatch.setattr(timeclock.db, "execute",
                        lambda *_args: (_ for _ in ()).throw(AssertionError("must not update mirror")))

    response = client.post(
        f"/timeclock/clock-in/confirm/{timeclock._mint_token(1)}",
        data={"wc_name": "Line 2", "scheduled_wc_name": "Line 1"},
    )

    assert response.status_code == 502
    assert "could not clear your approved time off" in response.text.lower()
