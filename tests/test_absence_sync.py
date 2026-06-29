from __future__ import annotations

from datetime import date
from unittest.mock import MagicMock

import pytest

from zira_dashboard import absence_sync


def test_resolve_absence_leave_type_matches_active_type_case_insensitively(monkeypatch):
    monkeypatch.setattr(
        absence_sync.odoo_client,
        "fetch_leave_types",
        lambda: [
            {"id": 1, "name": "Paid Time Off", "active": True},
            {"id": 42, "name": "absence", "active": True},
        ],
    )

    assert absence_sync.resolve_absence_leave_type_id() == 42


def test_resolve_absence_leave_type_raises_when_missing(monkeypatch):
    monkeypatch.setattr(
        absence_sync.odoo_client,
        "fetch_leave_types",
        lambda: [{"id": 1, "name": "Paid Time Off", "active": True}],
    )

    with pytest.raises(absence_sync.AbsenceSyncError, match="Absence"):
        absence_sync.resolve_absence_leave_type_id()


def test_create_absence_creates_and_approves_full_day_odoo_leave(monkeypatch):
    monkeypatch.setattr(absence_sync, "resolve_absence_leave_type_id", lambda: 42)
    find_duplicate = MagicMock(return_value=None)
    create_leave = MagicMock(return_value=777)
    confirm_leave = MagicMock()
    approve_leave = MagicMock(return_value="validate")
    monkeypatch.setattr(absence_sync.odoo_client, "find_duplicate_leave", find_duplicate)
    monkeypatch.setattr(absence_sync.odoo_client, "create_leave", create_leave)
    monkeypatch.setattr(absence_sync.odoo_client, "confirm_leave", confirm_leave)
    monkeypatch.setattr(absence_sync.odoo_client, "approve_leave", approve_leave)

    result = absence_sync.create_absence_for_day(
        employee_odoo_id=5,
        employee_name="Test Person",
        day=date(2026, 6, 17),
        reason="No call no show",
    )

    find_duplicate.assert_called_once_with(
        employee_odoo_id=5,
        holiday_status_id=42,
        date_from=date(2026, 6, 17),
        date_to=date(2026, 6, 17),
    )
    create_leave.assert_called_once_with(
        employee_odoo_id=5,
        holiday_status_id=42,
        date_from=date(2026, 6, 17),
        date_to=date(2026, 6, 17),
        hour_from=None,
        hour_to=None,
        note="Absent - Test Person: No call no show",
    )
    confirm_leave.assert_called_once_with(777)
    approve_leave.assert_called_once_with(777)
    assert result == {
        "holiday_status_id": 42,
        "leave_id": 777,
        "state": "validate",
    }


def test_create_absence_reuses_duplicate_leave(monkeypatch):
    monkeypatch.setattr(absence_sync, "resolve_absence_leave_type_id", lambda: 42)
    monkeypatch.setattr(
        absence_sync.odoo_client,
        "find_duplicate_leave",
        MagicMock(return_value=888),
    )
    create_leave = MagicMock()
    confirm_leave = MagicMock()
    approve_leave = MagicMock(return_value="validate")
    monkeypatch.setattr(absence_sync.odoo_client, "create_leave", create_leave)
    monkeypatch.setattr(absence_sync.odoo_client, "confirm_leave", confirm_leave)
    monkeypatch.setattr(absence_sync.odoo_client, "approve_leave", approve_leave)

    result = absence_sync.create_absence_for_day(
        employee_odoo_id=5,
        employee_name="Test Person",
        day=date(2026, 6, 17),
        reason="Already entered",
    )

    create_leave.assert_not_called()
    confirm_leave.assert_called_once_with(888)
    approve_leave.assert_called_once_with(888)
    assert result["leave_id"] == 888


def test_refuse_absence_ignores_missing_leave_id(monkeypatch):
    refuse_leave = MagicMock()
    monkeypatch.setattr(absence_sync.odoo_client, "refuse_leave", refuse_leave)

    absence_sync.refuse_absence_leave(None)

    refuse_leave.assert_not_called()


def test_mirror_approved_absence_updates_existing_pending_row(monkeypatch):
    existing = {
        "id": 12,
        "state": "confirm",
        "person_odoo_id": 5,
        "shape": "full_day",
        "holiday_status_id": 42,
        "date_from": date(2026, 6, 17),
        "date_to": date(2026, 6, 17),
        "hour_from": None,
        "hour_to": None,
        "working_hours_json": None,
        "odoo_leave_id": None,
    }
    query = MagicMock(return_value=[existing])
    execute = MagicMock()
    cascade = MagicMock()
    monkeypatch.setattr(absence_sync.db, "query", query)
    monkeypatch.setattr(absence_sync.db, "execute", execute)
    monkeypatch.setattr(
        "zira_dashboard.time_off_sync.cascade_on_state_change",
        cascade,
    )

    absence_sync.mirror_approved_absence(
        employee_odoo_id=5,
        holiday_status_id=42,
        leave_id=777,
        day=date(2026, 6, 17),
        employee_name="Test Person",
        reason="No call no show",
    )

    update_sql, update_params = execute.call_args.args
    assert "UPDATE time_off_requests" in update_sql
    assert update_params == (
        42,
        date(2026, 6, 17),
        date(2026, 6, 17),
        "Absent - Test Person: No call no show",
        777,
        12,
    )
    old, new = cascade.call_args.args
    assert old is existing
    assert new["id"] == 12
    assert new["state"] == "validate"
    assert new["odoo_leave_id"] == 777
