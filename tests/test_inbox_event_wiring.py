"""Phase 1 wiring: each inbox-native resolve action records one inbox_events row.

Collaborators (Odoo, suppression-table writes, inbox_log itself) are
monkeypatched, so these need no Postgres. They assert each handler calls
inbox_log.log_event_safe with the right event shape + actor.
"""
import json
from datetime import date, datetime, timezone

from zira_dashboard import inbox_log
from zira_dashboard.routes import exceptions as exceptions_route


def _capture_events(monkeypatch):
    events = []
    monkeypatch.setattr(inbox_log, "log_event_safe",
                        lambda **kw: events.append(kw) or 1)
    return events


def test_time_off_approve_records_inbox_event(monkeypatch):
    from zira_dashboard import odoo_client

    events = _capture_events(monkeypatch)
    row = {
        "id": 55, "person_odoo_id": 7, "person_name": "Maria Delgado",
        "leave_type": "PTO", "date_from": date(2026, 6, 22),
        "date_to": date(2026, 6, 22), "hour_from": 8.5, "hour_to": 12.25,
        "state": "confirm", "odoo_leave_id": 99,
    }
    monkeypatch.setattr(exceptions_route, "_load_time_off_request", lambda rid: row)
    monkeypatch.setattr(odoo_client, "approve_leave", lambda leave_id: "validate")
    monkeypatch.setattr(exceptions_route, "_set_time_off_state", lambda old, state: None)
    monkeypatch.setattr(exceptions_route.time_off_audit, "record_decision",
                        lambda **kw: None)

    resp = exceptions_route._approve_time_off_sync(
        55, actor_upn="dale@gruberpallets.com",
        actor_name="Dale Gruber", source="inbox")

    assert resp.status_code == 200
    assert len(events) == 1
    e = events[0]
    assert e["item_kind"] == "time_off"
    assert e["item_key"] == "time_off:55"
    assert e["action"] == "approve"
    assert e["actor_upn"] == "dale@gruberpallets.com"
    assert e["person_name"] == "Maria Delgado"
    assert events[0]["reversible"] is False


def test_time_off_deny_records_inbox_event(monkeypatch):
    events = _capture_events(monkeypatch)
    row = {
        "id": 56, "person_odoo_id": 8, "person_name": "Carlos Ortega",
        "leave_type": "Unpaid", "date_from": date(2026, 6, 22),
        "date_to": date(2026, 6, 22), "state": "draft", "odoo_leave_id": None,
    }
    monkeypatch.setattr(exceptions_route, "_load_time_off_request", lambda rid: row)
    monkeypatch.setattr(exceptions_route, "_set_time_off_state", lambda old, state: None)
    monkeypatch.setattr(exceptions_route.time_off_audit, "record_decision",
                        lambda **kw: None)

    resp = exceptions_route._refuse_time_off_sync(
        56, reason="No coverage", actor_upn="dale@gruberpallets.com",
        actor_name="Dale Gruber", source="inbox")

    assert resp.status_code == 200
    assert len(events) == 1
    e = events[0]
    assert e["item_kind"] == "time_off"
    assert e["item_key"] == "time_off:56"
    assert e["action"] == "deny"
    assert e["reason"] == "No coverage"
    assert events[0]["reversible"] is False


def test_missing_wc_assign_records_inbox_event(monkeypatch):
    from zira_dashboard import odoo_client, missing_wc, staffing
    from zira_dashboard.routes import missing_wc as missing_wc_route

    events = _capture_events(monkeypatch)
    monkeypatch.setattr(odoo_client, "set_attendance_wc", lambda att_id, wc: None)
    monkeypatch.setattr(missing_wc, "resolve", lambda *a, **k: None)
    wc_name = staffing.LOCATIONS[0].name

    resp = missing_wc_route._assign_sync(
        {"attendance_id": 999100, "wc_name": wc_name, "name": "Maria"},
        actor_upn="dale@gruberpallets.com", actor_name="Dale Gruber")

    assert resp.status_code == 200
    e = events[0]
    assert e["item_kind"] == "missing_wc"
    assert e["item_key"] == "missing_wc:999100"
    assert e["action"] == "assign"
    assert e["after_value"] == wc_name
    assert e["actor_name"] == "Dale Gruber"
    assert json.loads(resp.body)["event_id"] == 1
    assert events[0]["reversible"] is True


def test_missing_wc_dismiss_records_inbox_event(monkeypatch):
    from zira_dashboard import missing_wc
    from zira_dashboard.routes import missing_wc as missing_wc_route

    events = _capture_events(monkeypatch)
    monkeypatch.setattr(missing_wc, "resolve", lambda *a, **k: None)

    resp = missing_wc_route._dismiss_sync(
        {"attendance_id": 999100, "name": "Maria"},
        actor_upn="dale@gruberpallets.com", actor_name="Dale Gruber")

    assert resp.status_code == 200
    e = events[0]
    assert e["item_kind"] == "missing_wc"
    assert e["item_key"] == "missing_wc:999100"
    assert e["action"] == "dismiss"
    assert json.loads(resp.body)["event_id"] == 1
    assert events[0]["reversible"] is True


def test_missed_punch_correct_records_inbox_event(monkeypatch):
    from zira_dashboard import odoo_client, missed_punch_out as mpo
    from zira_dashboard.routes import missed_punch_out as mpo_route
    from zira_dashboard.shift_config import SITE_TZ

    events = _capture_events(monkeypatch)
    ci = datetime(2026, 6, 8, 14, 0, tzinfo=timezone.utc)
    midnight = datetime(2026, 6, 9, 0, 0, tzinfo=SITE_TZ)
    monkeypatch.setattr(mpo, "get_unresolved", lambda att_id: {
        "attendance_id": att_id, "employee_odoo_id": 42, "name": "Devin Park",
        "check_in": ci, "auto_closed_at": midnight,
    })
    monkeypatch.setattr(odoo_client, "clock_out", lambda att_id, ts, mode=None: None)
    monkeypatch.setattr(mpo, "correct", lambda att_id, ts: None)

    resp = mpo_route._correct_sync(
        {"attendance_id": 999500, "time": "16:30"},
        actor_upn="dale@gruberpallets.com", actor_name="Dale Gruber")

    assert resp.status_code == 200
    e = events[0]
    assert e["item_kind"] == "missed_punch_out"
    assert e["item_key"] == "missed_punch_out:999500"
    assert e["action"] == "correct"
    assert e["after_value"] == "4:30 PM"
    assert e["person_name"] == "Devin Park"


def test_late_declare_absent_records_inbox_event(monkeypatch):
    from zira_dashboard import absence_sync, db, late_report
    from zira_dashboard.routes import late_report as late_route

    events = _capture_events(monkeypatch)
    monkeypatch.setattr(absence_sync, "create_absence_for_day",
                        lambda **kw: {"leave_id": 123})
    monkeypatch.setattr(late_report, "declare_absent", lambda *a, **k: None)
    monkeypatch.setattr(db, "execute", lambda *a, **k: None)
    monkeypatch.setattr(late_route, "_bust_caches", lambda: None)

    resp = late_route._declare_absent_sync(
        {"emp_id": "42", "name": "Tomas Vela", "reason": "Sick"},
        actor_upn="dale@gruberpallets.com", actor_name="Dale Gruber")

    assert resp.status_code == 200
    e = events[0]
    assert e["item_kind"] == "late"
    assert e["action"] == "absent"
    assert e["reason"] == "Sick"
    assert e["person_name"] == "Tomas Vela"
    assert e["item_key"].startswith("late:42:")
    assert json.loads(resp.body)["event_id"] == 1
    assert events[0]["reversible"] is True


def test_late_save_reason_records_inbox_event(monkeypatch):
    from zira_dashboard import late_report
    from zira_dashboard.routes import late_report as late_route

    events = _capture_events(monkeypatch)
    monkeypatch.setattr(late_report, "save_late_arrival", lambda *a, **k: None)
    monkeypatch.setattr(late_route, "_bust_caches", lambda: None)

    resp = late_route._save_late_arrival_sync(
        {"emp_id": "42", "name": "Tomas Vela", "reason": "Overslept"},
        actor_upn="dale@gruberpallets.com", actor_name="Dale Gruber")

    assert resp.status_code == 200
    e = events[0]
    assert e["item_kind"] == "late"
    assert e["action"] == "reason"
    assert e["reason"] == "Overslept"
    assert json.loads(resp.body)["event_id"] == 1
    assert events[0]["reversible"] is True


def test_late_snooze_records_no_inbox_event(monkeypatch):
    from zira_dashboard import late_report
    from zira_dashboard.routes import late_report as late_route

    events = _capture_events(monkeypatch)
    monkeypatch.setattr(late_report, "snooze", lambda *a, **k: None)
    monkeypatch.setattr(late_route, "_bust_caches", lambda: None)

    resp = late_route._snooze_sync({"emp_id": "42", "name": "Tomas Vela", "minutes": 30})

    assert resp.status_code == 200
    assert events == []
