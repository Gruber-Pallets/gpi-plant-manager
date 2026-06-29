from __future__ import annotations

import json
from datetime import date
from unittest.mock import MagicMock

from zira_dashboard.routes import late_report as late_report_routes

FIXED_DAY = date(2026, 6, 17)


def test_declare_absent_sync_posts_absence_to_odoo_before_local_write(monkeypatch):
    create_absence = MagicMock(return_value={
        "holiday_status_id": 42,
        "leave_id": 777,
        "state": "validate",
    })
    declare_absent = MagicMock()
    mirror_absence = MagicMock()
    db_execute = MagicMock()
    monkeypatch.setattr(late_report_routes, "plant_today", lambda: FIXED_DAY)
    monkeypatch.setattr(late_report_routes.absence_sync, "create_absence_for_day", create_absence)
    monkeypatch.setattr(late_report_routes.absence_sync, "mirror_approved_absence", mirror_absence)
    monkeypatch.setattr(late_report_routes.late_report, "declare_absent", declare_absent)
    monkeypatch.setattr(late_report_routes.db, "execute", db_execute)
    monkeypatch.setattr(late_report_routes, "_bust_caches", lambda: None)

    response = late_report_routes._declare_absent_sync({
        "emp_id": "5",
        "name": "Test Person",
        "reason": "No call no show",
    })

    assert response.status_code == 200
    create_absence.assert_called_once_with(
        employee_odoo_id=5,
        employee_name="Test Person",
        day=FIXED_DAY,
        reason="No call no show",
    )
    declare_absent.assert_called_once_with(
        FIXED_DAY,
        "5",
        "Test Person",
        reason="No call no show",
        odoo_leave_id=777,
    )
    mirror_absence.assert_called_once()
    db_execute.assert_called_once()


def test_declare_absent_sync_mirrors_approved_absence_locally(monkeypatch):
    create_absence = MagicMock(return_value={
        "holiday_status_id": 42,
        "leave_id": 777,
        "state": "validate",
    })
    db_query = MagicMock(return_value=[])
    db_execute = MagicMock()
    monkeypatch.setattr(late_report_routes, "plant_today", lambda: FIXED_DAY)
    monkeypatch.setattr(late_report_routes.absence_sync, "create_absence_for_day", create_absence)
    monkeypatch.setattr(late_report_routes.late_report, "declare_absent", MagicMock())
    monkeypatch.setattr(late_report_routes.absence_sync.db, "query", db_query)
    monkeypatch.setattr(late_report_routes.absence_sync.db, "execute", db_execute)
    monkeypatch.setattr(late_report_routes.db, "execute", db_execute)
    monkeypatch.setattr(late_report_routes.inbox_log, "log_event_safe", lambda **k: 123)
    monkeypatch.setattr(late_report_routes, "_bust_caches", lambda: None)

    response = late_report_routes._declare_absent_sync({
        "emp_id": "5",
        "name": "Test Person",
        "reason": "No call no show",
    })

    assert response.status_code == 200
    insert_calls = [
        call for call in db_execute.call_args_list
        if "INSERT INTO time_off_requests" in call.args[0]
    ]
    assert insert_calls, "expected approved absence to be mirrored into time_off_requests"
    params = insert_calls[0].args[1]
    assert params == (
        5,
        "full_day",
        42,
        FIXED_DAY,
        FIXED_DAY,
        "Absent - Test Person: No call no show",
        "validate",
        777,
    )


def test_declare_absent_sync_records_locally_when_odoo_rejects(monkeypatch):
    """If Odoo can't represent the absence — e.g. the employee's Odoo work
    schedule shows no hours that day, raising
    'The following employees are not supposed to work during that period' —
    the manager's declaration must still succeed locally. The local
    manual_absences row is the source of truth for the scheduler/inbox; the
    Odoo Time Off sync is best-effort. Its failure surfaces as a non-fatal
    warning (HTTP 200, ok=True), NOT a 500 that blocks the whole action."""

    class _OdooFault(Exception):
        # Mirrors xmlrpc.client.Fault, whose message lives in .faultString.
        faultString = (
            "The following employees are not supposed to work during that "
            "period:\n Gerardo Vergara Quintero"
        )

    def _reject(**kwargs):
        raise _OdooFault()

    declare_absent = MagicMock()
    mirror_absence = MagicMock()
    db_execute = MagicMock()
    monkeypatch.setattr(late_report_routes, "plant_today", lambda: FIXED_DAY)
    monkeypatch.setattr(late_report_routes.absence_sync, "create_absence_for_day", _reject)
    monkeypatch.setattr(late_report_routes.absence_sync, "mirror_approved_absence", mirror_absence)
    monkeypatch.setattr(late_report_routes.late_report, "declare_absent", declare_absent)
    monkeypatch.setattr(late_report_routes.db, "execute", db_execute)
    monkeypatch.setattr(late_report_routes.inbox_log, "log_event_safe", lambda **k: 123)
    monkeypatch.setattr(late_report_routes, "_bust_caches", lambda: None)

    response = late_report_routes._declare_absent_sync({
        "emp_id": "9",
        "name": "Gerardo Vergara",
        "reason": "No call no show",
    })

    assert response.status_code == 200
    payload = json.loads(response.body)
    assert payload["ok"] is True
    assert payload.get("odoo_synced") is False
    assert "not supposed to work" in payload.get("warning", "")
    # Local record still written, with NO linked Odoo leave id.
    declare_absent.assert_called_once_with(
        FIXED_DAY,
        "9",
        "Gerardo Vergara",
        reason="No call no show",
        odoo_leave_id=None,
    )
    mirror_absence.assert_not_called()
    db_execute.assert_called_once()


def test_declare_absent_sync_rejects_non_numeric_employee_id(monkeypatch):
    create_absence = MagicMock()
    monkeypatch.setattr(late_report_routes.absence_sync, "create_absence_for_day", create_absence)

    response = late_report_routes._declare_absent_sync({
        "emp_id": "not-odoo-id",
        "name": "Test Person",
        "reason": "No call no show",
    })

    assert response.status_code == 400
    create_absence.assert_not_called()


def test_undo_absent_refuses_linked_odoo_absence_before_local_delete(monkeypatch):
    odoo_leave_id_for_absence = MagicMock(return_value=777)
    refuse_absence = MagicMock()
    undo_absent = MagicMock()
    monkeypatch.setattr(late_report_routes, "plant_today", lambda: FIXED_DAY)
    monkeypatch.setattr(
        late_report_routes.late_report,
        "odoo_leave_id_for_absence",
        odoo_leave_id_for_absence,
    )
    monkeypatch.setattr(late_report_routes.absence_sync, "refuse_absence_leave", refuse_absence)
    monkeypatch.setattr(late_report_routes.late_report, "undo_absent", undo_absent)
    monkeypatch.setattr(late_report_routes, "_bust_caches", lambda: None)

    response = late_report_routes._undo_absent_sync({"emp_id": "5"})

    assert response.status_code == 200
    odoo_leave_id_for_absence.assert_called_once_with(
        FIXED_DAY,
        "5",
    )
    refuse_absence.assert_called_once_with(777)
    undo_absent.assert_called_once_with(FIXED_DAY, "5")
