"""get_current_attendance department parsing + undo_transfer. odoo_client.execute
is stubbed; no Odoo needed."""
from __future__ import annotations

from unittest.mock import MagicMock

from zira_dashboard import odoo_client


def test_get_current_attendance_parses_department(monkeypatch):
    monkeypatch.setenv("ODOO_KIOSK_DEPARTMENT_FIELD", "x_kiosk_department_id")
    fake = MagicMock(return_value=[{
        "id": 55, "employee_id": [5, "Bob"], "check_in": "2026-06-02 13:00:00",
        "x_kiosk_department_id": [3, "01 Recycled"],
    }])
    monkeypatch.setattr(odoo_client, "execute", fake)

    row = odoo_client.get_current_attendance(5)

    _args, kwargs = fake.call_args
    assert "x_kiosk_department_id" in kwargs["fields"]
    assert row["department_id"] == 3
    assert row["department_name"] == "01 Recycled"


def test_get_current_attendance_no_dept_field(monkeypatch):
    monkeypatch.delenv("ODOO_KIOSK_DEPARTMENT_FIELD", raising=False)
    fake = MagicMock(return_value=[{
        "id": 55, "employee_id": [5, "Bob"], "check_in": "2026-06-02 13:00:00",
    }])
    monkeypatch.setattr(odoo_client, "execute", fake)

    row = odoo_client.get_current_attendance(5)
    assert row["department_id"] is None
    assert row["department_name"] is None


def test_get_current_attendance_none_when_clocked_out(monkeypatch):
    monkeypatch.setattr(odoo_client, "execute", MagicMock(return_value=[]))
    assert odoo_client.get_current_attendance(5) is None


def test_undo_transfer_unlinks_new_and_reopens_old(monkeypatch):
    fake = MagicMock(return_value=True)
    monkeypatch.setattr(odoo_client, "execute", fake)

    odoo_client.undo_transfer(closed_id=10, new_id=20)

    calls = [c.args for c in fake.call_args_list]
    assert ("hr.attendance", "unlink", [20]) in calls
    assert ("hr.attendance", "write", [10], {"check_out": False}) in calls


def test_undo_transfer_without_closed_id_only_unlinks(monkeypatch):
    fake = MagicMock(return_value=True)
    monkeypatch.setattr(odoo_client, "execute", fake)

    odoo_client.undo_transfer(closed_id=None, new_id=20)

    calls = [c.args for c in fake.call_args_list]
    assert ("hr.attendance", "unlink", [20]) in calls
    assert all(c[1] != "write" for c in calls)
