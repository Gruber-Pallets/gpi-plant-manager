"""Validated dismissal for current test-only Odoo work-center exceptions."""

from datetime import UTC, date, datetime
from concurrent.futures import ThreadPoolExecutor
from threading import Lock
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from zira_dashboard import (
    attendance_exceptions,
    exception_inbox,
    inbox_log,
    missing_wc,
)
from zira_dashboard.app import app
from zira_dashboard.routes import exceptions as exceptions_route


DAY = date(2026, 9, 4)
NOW = datetime(2026, 9, 4, 14, 0, tzinfo=UTC)
client = TestClient(app)


def _unmapped_issue(
    *,
    labels=("Test Workcenter",),
    attendance_ids=(901, 902),
    odoo_work_center_ids=(),
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
        odoo_work_center_ids=odoo_work_center_ids,
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
    resolved = MagicMock(return_value=True)
    logged = MagicMock()
    monkeypatch.setattr(missing_wc, "claim_many", resolved)
    monkeypatch.setattr(inbox_log, "log_event_safe", logged)
    return resolved, logged


def _render_issue(monkeypatch, issue):
    row = {
        **exception_inbox._attendance_issue_row(issue),
        "section_id": "attendance_unmapped_location",
        "category_label": "Unknown Odoo Work Center",
        "tone": "bad",
    }
    snapshot = {
        "today": DAY.isoformat(),
        "generated_at": "9:00 AM",
        "total": 1,
        "urgent_total": 1,
        "follow_up_total": 0,
        "source_errors": [],
        "work_centers": [],
        "people": [],
        "sections": [],
        "queue": [row],
    }
    monkeypatch.setattr(exceptions_route.exception_inbox, "build_snapshot", lambda: snapshot)
    monkeypatch.setattr(
        exceptions_route.exception_inbox,
        "build_summary",
        lambda: {"total": 1, "urgent_total": 1, "source_errors": []},
    )
    monkeypatch.setattr(exceptions_route, "_active_correction_people", lambda: [])
    monkeypatch.setattr(
        exceptions_route.auth, "request_is_super_admin", lambda _request: True
    )
    return client.get("/exceptions")


def test_render_test_work_center_uses_dismiss_instead_of_map(monkeypatch):
    issue = _unmapped_issue(labels=("Test Workcenter",), odoo_work_center_ids=(17,))

    response = _render_issue(monkeypatch, issue)

    assert response.status_code == 200
    assert 'class="row-btn js-test-work-center-dismiss"' in response.text
    assert "Map this Odoo work center" not in response.text
    assert f'data-item-key="{issue.item_key}"' in response.text


@pytest.mark.parametrize(
    "labels",
    [
        ("Dismantler 1",),
        ("Test Workcenter", "Dismantler 1"),
    ],
)
def test_render_real_or_mixed_work_center_uses_map_not_dismiss(monkeypatch, labels):
    issue = _unmapped_issue(labels=labels, odoo_work_center_ids=(17,))

    response = _render_issue(monkeypatch, issue)

    assert response.status_code == 200
    assert "Map this Odoo work center" in response.text
    assert "js-test-work-center-dismiss" not in response.text


@pytest.mark.parametrize("labels", [(), ("",)])
def test_render_blank_work_center_label_uses_safe_map_link(monkeypatch, labels):
    issue = _unmapped_issue(labels=labels, odoo_work_center_ids=(17,))

    response = _render_issue(monkeypatch, issue)

    assert response.status_code == 200
    assert "js-test-work-center-dismiss" not in response.text
    assert (
        'href="/settings?section=work_centers&amp;odoo_work_center_id=17'
        '&amp;odoo_work_center_name=Unknown+Odoo+work+center"'
    ) in response.text
    assert "Map this Odoo work center" in response.text


def test_dismiss_current_test_item_suppresses_all_ids_and_audits(monkeypatch):
    issue = _unmapped_issue(labels=("Test Workcenter",), attendance_ids=(901, 902))
    resolved = MagicMock(return_value=True)
    logged = MagicMock(return_value=77)
    _install_snapshot(monkeypatch, issues=(issue,))
    monkeypatch.setattr(missing_wc, "claim_many", resolved)
    monkeypatch.setattr(inbox_log, "log_event_safe", logged)

    response = client.post(
        "/api/exceptions/attendance-unmapped-location/dismiss",
        json={"item_key": issue.item_key},
    )

    assert response.status_code == 200
    assert response.json() == {"ok": True}
    resolved.assert_called_once_with(
        issue.item_key, (901, 902), "dismissed", name=issue.employee_name
    )
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


def test_dismiss_partial_completion_audits_only_winning_completion(monkeypatch):
    issue = _unmapped_issue()
    _install_snapshot(monkeypatch, issues=(issue,))
    resolved_ids = {901}
    raw_snapshot = attendance_exceptions.AttendanceExceptionSnapshot(
        day=DAY,
        mode="strict",
        production_mode="strict",
        baseline_complete=True,
        fresh=True,
        complete=True,
        issues=(issue,),
        source_errors=(),
    )
    assert exception_inbox._without_resolved_unmapped_issues(
        raw_snapshot, resolved_ids
    ).issues == (issue,)

    def complete_partial(_item_key, attendance_ids, *_args, **_kwargs):
        missing_ids = set(attendance_ids) - resolved_ids
        if not missing_ids:
            return False
        resolved_ids.update(missing_ids)
        return True

    logged = MagicMock(return_value=77)
    monkeypatch.setattr(missing_wc, "claim_many", complete_partial)
    monkeypatch.setattr(inbox_log, "log_event_safe", logged)

    first = client.post(
        "/api/exceptions/attendance-unmapped-location/dismiss",
        json={"item_key": issue.item_key},
    )
    second = client.post(
        "/api/exceptions/attendance-unmapped-location/dismiss",
        json={"item_key": issue.item_key},
    )

    assert first.status_code == 200
    assert first.json() == {"ok": True}
    assert second.status_code == 404
    assert second.json() == {
        "ok": False,
        "error": "That inbox item is no longer open.",
    }
    assert resolved_ids == {901, 902}
    assert exception_inbox._without_resolved_unmapped_issues(
        raw_snapshot, resolved_ids
    ).issues == ()
    logged.assert_called_once()


def test_dismiss_concurrent_race_claims_and_audits_once(monkeypatch):
    issue = _unmapped_issue()
    _install_snapshot(monkeypatch, issues=(issue,))
    claim_lock = Lock()
    claimed_keys = set()

    def claim_once(item_key, *_args, **_kwargs):
        with claim_lock:
            if item_key in claimed_keys:
                return False
            claimed_keys.add(item_key)
            return True

    logged = MagicMock(return_value=77)
    monkeypatch.setattr(missing_wc, "claim_many", claim_once)
    monkeypatch.setattr(inbox_log, "log_event_safe", logged)

    def dismiss():
        return client.post(
            "/api/exceptions/attendance-unmapped-location/dismiss",
            json={"item_key": issue.item_key},
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda _index: dismiss(), range(2)))

    assert sorted(response.status_code for response in responses) == [200, 404]
    assert sum(response.json().get("ok") is True for response in responses) == 1
    logged.assert_called_once()


@pytest.mark.parametrize(
    ("content", "content_type"),
    [
        (b"{not-json", "application/json"),
        (b"[]", "application/json"),
        (b'"item"', "application/json"),
        (b"null", "application/json"),
        (b"\xff", "application/json"),
    ],
)
def test_dismiss_rejects_malformed_or_non_object_json(monkeypatch, content, content_type):
    resolved, logged = _install_no_writes(monkeypatch)

    response = client.post(
        "/api/exceptions/attendance-unmapped-location/dismiss",
        content=content,
        headers={"content-type": content_type},
    )

    assert response.status_code == 400
    assert response.json() == {"ok": False, "error": "Invalid request."}
    resolved.assert_not_called()
    logged.assert_not_called()


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
    monkeypatch.setattr(missing_wc, "claim_many", resolved)
    monkeypatch.setattr(inbox_log, "log_event_safe", logged)

    response = client.post(
        "/api/exceptions/attendance-unmapped-location/dismiss",
        json={"item_key": issue.item_key},
    )

    assert response.status_code == 500
    assert response.json() == {"ok": False, "error": "Could not dismiss this inbox item."}
    resolved.assert_called_once_with(
        issue.item_key, (901, 902), "dismissed", name=issue.employee_name
    )
    logged.assert_not_called()
