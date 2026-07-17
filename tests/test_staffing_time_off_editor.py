"""Supervisor scheduler time-off editor contract tests."""

from datetime import date

from fastapi.testclient import TestClient

from zira_dashboard.app import app
from zira_dashboard.routes import staffing as staffing_routes


def test_supervisor_edit_stages_same_odoo_leave_and_queues_push(monkeypatch):
    staged, queued = [], []
    monkeypatch.setattr(staffing_routes, "_editable_time_off_for_day", lambda rid, day: {
        "id": rid, "shape": "midday_gap", "holiday_status_id": 5,
        "odoo_leave_id": 701, "date_from": date(2026, 7, 17),
        "date_to": date(2026, 7, 17),
    })
    monkeypatch.setattr(
        staffing_routes, "_stage_supervisor_time_off_edit",
        lambda **kwargs: staged.append(kwargs),
    )
    monkeypatch.setattr(staffing_routes, "_scheduler_shift_bounds", lambda _day: (6.0, 14.0))
    monkeypatch.setattr(staffing_routes, "invalidate_today_cache", lambda: None)
    monkeypatch.setattr(staffing_routes, "_queue_time_off_push", queued.append)

    response = staffing_routes._edit_scheduler_time_off(91, {
        "day": "2026-07-17", "date_from": "2026-07-18", "date_to": "2026-07-18",
        "time_from": "09:00", "time_to": "11:00",
    })

    assert response.status_code == 200
    assert staged[0]["request_id"] == 91
    assert staged[0]["holiday_status_id"] == 5
    assert queued == [91]


def test_supervisor_cancel_stages_cancel_and_queues_push(monkeypatch):
    staged, queued = [], []
    monkeypatch.setattr(
        staffing_routes, "_editable_time_off_for_day",
        lambda rid, day: {"id": rid, "odoo_leave_id": 701},
    )
    monkeypatch.setattr(staffing_routes, "_stage_supervisor_time_off_cancel", staged.append)
    monkeypatch.setattr(staffing_routes, "invalidate_today_cache", lambda: None)
    monkeypatch.setattr(staffing_routes, "_queue_time_off_push", queued.append)

    response = staffing_routes._cancel_scheduler_time_off(91, {"day": "2026-07-17"})

    assert response.status_code == 200
    assert staged == [91]
    assert queued == [91]


def test_supervisor_edit_rejects_invalid_partial_window(monkeypatch):
    monkeypatch.setattr(staffing_routes, "_editable_time_off_for_day", lambda *_: {
        "id": 91, "shape": "midday_gap", "holiday_status_id": 5,
    })
    monkeypatch.setattr(staffing_routes, "_scheduler_shift_bounds", lambda _day: (6.0, 14.0))

    response = TestClient(app).post("/api/staffing/time-off/91/edit", json={
        "day": "2026-07-17", "date_from": "2026-07-17", "date_to": "2026-07-17",
        "time_from": "12:00", "time_to": "09:00",
    })

    assert response.status_code == 422


def test_supervisor_endpoints_reject_local_or_out_of_day_record(monkeypatch):
    monkeypatch.setattr(staffing_routes, "_editable_time_off_for_day", lambda *_: None)

    response = TestClient(app).post(
        "/api/staffing/time-off/91/cancel", json={"day": "2026-07-17"},
    )

    assert response.status_code == 404
