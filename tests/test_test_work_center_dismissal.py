"""Validated dismissal for current test-only Odoo work-center exceptions."""

from datetime import UTC, date, datetime
from unittest.mock import MagicMock

from fastapi.testclient import TestClient

from zira_dashboard import (
    attendance_exceptions,
    inbox_log,
    missing_wc,
)
from zira_dashboard.app import app


DAY = date(2026, 9, 4)
NOW = datetime(2026, 9, 4, 14, 0, tzinfo=UTC)
client = TestClient(app)


def _unmapped_issue(
    *,
    labels=("Test Workcenter",),
    attendance_ids=(901, 902),
    item_key="attendance-unmapped:test-workcenter",
):
    return attendance_exceptions.AttendanceException(
        kind="attendance_unmapped_location",
        item_key=item_key,
        employee_odoo_id=42,
        employee_name="Luke",
        attendance_ids=attendance_ids,
        start_utc=NOW,
        end_utc=NOW,
        raw_work_center_labels=labels,
        odoo_work_center_ids=(),
        affected_workers=((42, "Luke"),),
        app_work_center_name=None,
        units=None,
        sample_count=None,
        reason="unmapped_work_center",
        priority="urgent",
        comparison_only=False,
        target_odoo_department_id=None,
    )


def _install_snapshot(monkeypatch, *, issues=()):
    snapshot = attendance_exceptions.AttendanceExceptionSnapshot(
        day=DAY,
        mode="strict",
        production_mode="strict",
        baseline_complete=True,
        fresh=True,
        complete=True,
        issues=tuple(issues),
        source_errors=(),
    )
    monkeypatch.setattr(attendance_exceptions, "build_snapshot", lambda *_a, **_k: snapshot)


def _install_no_writes(monkeypatch):
    resolved = MagicMock()
    logged = MagicMock()
    monkeypatch.setattr(missing_wc, "resolve_many", resolved)
    monkeypatch.setattr(inbox_log, "log_event_safe", logged)
    return resolved, logged


def test_dismiss_current_test_item_suppresses_all_ids_and_audits(monkeypatch):
    issue = _unmapped_issue(labels=("Test Workcenter",), attendance_ids=(901, 902))
    resolved = MagicMock()
    logged = MagicMock(return_value=77)
    _install_snapshot(monkeypatch, issues=(issue,))
    monkeypatch.setattr(missing_wc, "resolve_many", resolved)
    monkeypatch.setattr(inbox_log, "log_event_safe", logged)

    response = client.post(
        "/api/exceptions/attendance-unmapped-location/dismiss",
        json={"item_key": issue.item_key},
    )

    assert response.status_code == 200
    assert response.json() == {"ok": True}
    resolved.assert_called_once_with((901, 902), "dismissed", name=issue.employee_name)
    logged.assert_called_once_with(
        item_kind="attendance_unmapped_location",
        item_key=issue.item_key,
        person_name=issue.employee_name,
        category_label="Unknown Odoo Work Center",
        action="dismiss",
        outcome="Dismissed test work center",
        actor_upn=None,
        actor_name=None,
        source="inbox",
        reversible=False,
        detail={"raw_work_center_labels": ["Test Workcenter"]},
    )


def test_dismiss_rejects_missing_item_key_without_writing(monkeypatch):
    _install_snapshot(monkeypatch)
    resolved, logged = _install_no_writes(monkeypatch)

    response = client.post("/api/exceptions/attendance-unmapped-location/dismiss", json={})

    assert response.status_code == 400
    resolved.assert_not_called()
    logged.assert_not_called()


def test_dismiss_rejects_stale_item_without_writing(monkeypatch):
    _install_snapshot(monkeypatch)
    resolved, logged = _install_no_writes(monkeypatch)

    response = client.post(
        "/api/exceptions/attendance-unmapped-location/dismiss",
        json={"item_key": "attendance-unmapped:gone"},
    )

    assert response.status_code == 404
    resolved.assert_not_called()
    logged.assert_not_called()


def test_dismiss_rejects_duplicate_matches_without_writing(monkeypatch):
    issue = _unmapped_issue()
    _install_snapshot(monkeypatch, issues=(issue, issue))
    resolved, logged = _install_no_writes(monkeypatch)

    response = client.post(
        "/api/exceptions/attendance-unmapped-location/dismiss",
        json={"item_key": issue.item_key},
    )

    assert response.status_code == 409
    resolved.assert_not_called()
    logged.assert_not_called()


def test_dismiss_rejects_blank_labels_without_writing(monkeypatch):
    issue = _unmapped_issue(labels=("",))
    _install_snapshot(monkeypatch, issues=(issue,))
    resolved, logged = _install_no_writes(monkeypatch)

    response = client.post(
        "/api/exceptions/attendance-unmapped-location/dismiss",
        json={"item_key": issue.item_key},
    )

    assert response.status_code == 409
    resolved.assert_not_called()
    logged.assert_not_called()


def test_dismiss_rejects_real_labels_without_writing(monkeypatch):
    issue = _unmapped_issue(labels=("Dismantler 1",))
    _install_snapshot(monkeypatch, issues=(issue,))
    resolved, logged = _install_no_writes(monkeypatch)

    response = client.post(
        "/api/exceptions/attendance-unmapped-location/dismiss",
        json={"item_key": issue.item_key},
    )

    assert response.status_code == 409
    resolved.assert_not_called()
    logged.assert_not_called()


def test_dismiss_rejects_mixed_labels_without_writing(monkeypatch):
    issue = _unmapped_issue(labels=("Test Workcenter", "Dismantler 1"))
    _install_snapshot(monkeypatch, issues=(issue,))
    resolved, logged = _install_no_writes(monkeypatch)

    response = client.post(
        "/api/exceptions/attendance-unmapped-location/dismiss",
        json={"item_key": issue.item_key},
    )

    assert response.status_code == 409
    resolved.assert_not_called()
    logged.assert_not_called()


def test_dismiss_rejects_empty_attendance_ids_without_writing(monkeypatch):
    issue = _unmapped_issue(attendance_ids=())
    _install_snapshot(monkeypatch, issues=(issue,))
    resolved, logged = _install_no_writes(monkeypatch)

    response = client.post(
        "/api/exceptions/attendance-unmapped-location/dismiss",
        json={"item_key": issue.item_key},
    )

    assert response.status_code == 409
    resolved.assert_not_called()
    logged.assert_not_called()


def test_dismiss_source_failure_returns_plain_500_without_writing(monkeypatch):
    monkeypatch.setattr(
        attendance_exceptions,
        "build_snapshot",
        MagicMock(side_effect=RuntimeError("private source detail")),
    )
    resolved, logged = _install_no_writes(monkeypatch)

    response = client.post(
        "/api/exceptions/attendance-unmapped-location/dismiss",
        json={"item_key": "attendance-unmapped:test-workcenter"},
    )

    assert response.status_code == 500
    assert response.json() == {"ok": False, "error": "Could not dismiss this inbox item."}
    resolved.assert_not_called()
    logged.assert_not_called()


def test_dismiss_write_failure_returns_plain_500_without_auditing(monkeypatch):
    issue = _unmapped_issue()
    _install_snapshot(monkeypatch, issues=(issue,))
    resolved = MagicMock(side_effect=RuntimeError("private write detail"))
    logged = MagicMock()
    monkeypatch.setattr(missing_wc, "resolve_many", resolved)
    monkeypatch.setattr(inbox_log, "log_event_safe", logged)

    response = client.post(
        "/api/exceptions/attendance-unmapped-location/dismiss",
        json={"item_key": issue.item_key},
    )

    assert response.status_code == 500
    assert response.json() == {"ok": False, "error": "Could not dismiss this inbox item."}
    resolved.assert_called_once_with((901, 902), "dismissed", name=issue.employee_name)
    logged.assert_not_called()
