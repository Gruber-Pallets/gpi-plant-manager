from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock

from zira_dashboard.routes import late_report as late_report_routes


class _FixedDateTime(datetime):
    @classmethod
    def now(cls, tz=None):
        return datetime(2026, 6, 17, 15, 30, tzinfo=timezone.utc)


def test_declare_absent_sync_posts_absence_to_odoo_before_local_write(monkeypatch):
    create_absence = MagicMock(return_value={
        "holiday_status_id": 42,
        "leave_id": 777,
        "state": "validate",
    })
    declare_absent = MagicMock()
    db_execute = MagicMock()
    monkeypatch.setattr(late_report_routes, "datetime", _FixedDateTime)
    monkeypatch.setattr(late_report_routes.absence_sync, "create_absence_for_day", create_absence)
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
        day=_FixedDateTime.now(timezone.utc).date(),
        reason="No call no show",
    )
    declare_absent.assert_called_once_with(
        _FixedDateTime.now(timezone.utc).date(),
        "5",
        "Test Person",
        reason="No call no show",
        odoo_leave_id=777,
    )
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
    monkeypatch.setattr(late_report_routes, "datetime", _FixedDateTime)
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
        _FixedDateTime.now(timezone.utc).date(),
        "5",
    )
    refuse_absence.assert_called_once_with(777)
    undo_absent.assert_called_once_with(_FixedDateTime.now(timezone.utc).date(), "5")
