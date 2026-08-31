"""The breakdown section appears in build_summary/build_snapshot."""
import json

import pytest
from starlette.testclient import TestClient

from zira_dashboard import exception_inbox


def test_build_summary_includes_breakdown_count(monkeypatch):
    from zira_dashboard import machine_breakdown
    monkeypatch.setattr(machine_breakdown, "current_rows", lambda: [
        {"name": "Dismantler 2", "action": None},
        {"name": "Juan", "action": {"type": "breakdown"}},
    ])
    summary = exception_inbox.build_summary()
    assert summary["sections"]["breakdown"] == 2


def test_build_snapshot_includes_breakdown_section_and_rows(monkeypatch):
    from zira_dashboard import machine_breakdown
    row = {
        "name": "Dismantler 2", "label": "Stopped producing", "detail": "No output since 1:02 PM (23 min)",
        "priority": "urgent", "badge": "AUTO-DETECTED",
        "row_key": "breakdown_header:Dismantler 2:x", "item_key": "breakdown:Dismantler 2:x",
        "action": None, "dismiss_action": {"type": "breakdown_dismiss", "incident_id": 1},
    }
    monkeypatch.setattr(machine_breakdown, "current_rows", lambda: [row])
    snapshot = exception_inbox.build_snapshot()
    section = next(s for s in snapshot["sections"] if s["id"] == "breakdown")
    assert section["rows"] == [row]
    assert section["count"] == 1
    queue_item_keys = [r["item_key"] for r in snapshot["queue"]]
    assert "breakdown:Dismantler 2:x" in queue_item_keys


@pytest.mark.parametrize(
    ("live_active", "transfer_enabled"),
    [(False, True), (True, False)],
)
def test_build_snapshot_exposes_breakdown_transfer_only_after_live_activation(
    monkeypatch,
    live_active,
    transfer_enabled,
):
    from zira_dashboard import attendance_location_policy

    monkeypatch.setattr(
        attendance_location_policy,
        "live_is_active",
        lambda: live_active,
    )

    snapshot = exception_inbox.build_snapshot()

    assert snapshot["breakdown_transfer_enabled"] is transfer_enabled


def _live_breakdown_snapshot():
    operator = {
        "name": "Juan",
        "label": "Idle — Dismantler 2 is down",
        "detail": "",
        "priority": "urgent",
        "badge": "Needs decision",
        "row_key": "breakdown_op:Dismantler 2:x:Juan",
        "item_key": "breakdown:Dismantler 2:x:Juan",
        "section_id": "breakdown",
        "category_label": "Machine Breakdown",
        "tone": "bad",
        "action": {
            "type": "breakdown",
            "incident_id": 1,
            "person_name": "Juan",
            "wc_name": "Dismantler 2",
            "employee_odoo_id": 101,
        },
    }
    return {
        "today": "2026-07-08",
        "generated_at": "1:22 PM",
        "total": 1,
        "urgent_total": 1,
        "follow_up_total": 0,
        "source_errors": [],
        "work_centers": ["Repair 3", "Dismantler 2"],
        "people": [],
        "sections": [],
        "queue": [operator],
        "breakdown_transfer_enabled": False,
    }


def test_live_breakdown_ui_hides_transfer_but_keeps_snooze_and_guidance(monkeypatch):
    from zira_dashboard.app import app
    from zira_dashboard.routes import exceptions as exceptions_route

    monkeypatch.setattr(
        exceptions_route.exception_inbox,
        "build_snapshot",
        _live_breakdown_snapshot,
    )
    monkeypatch.setattr(exceptions_route, "_active_correction_people", lambda: [])

    response = TestClient(app).get("/exceptions")

    assert response.status_code == 200
    assert "js-breakdown-transfer" not in response.text
    assert 'aria-label="Work center to transfer to"' not in response.text
    assert "js-breakdown-snooze" in response.text
    assert 'data-employee-odoo-id="101"' in response.text
    assert "Use Luke's floor app to move this worker." in response.text


def test_breakdown_snooze_javascript_sends_employee_identity():
    from pathlib import Path

    js = Path("src/zira_dashboard/static/exceptions.js").read_text()

    assert "employee_odoo_id: employeeOdooId" in js


def test_live_breakdown_transfer_route_returns_410_before_delegation(monkeypatch):
    from fastapi.responses import JSONResponse

    from zira_dashboard import attendance_location_policy, breakdown_actions
    from zira_dashboard.routes import exceptions as exceptions_route

    monkeypatch.setattr(attendance_location_policy, "live_is_active", lambda: True)
    delegated = []
    monkeypatch.setattr(
        breakdown_actions,
        "transfer",
        lambda *_args, **_kwargs: delegated.append(1)
        or JSONResponse({"ok": True}),
    )

    response = exceptions_route._breakdown_transfer_sync(
        {"incident_id": 1, "person_name": "Juan", "to_wc": "Repair 3"},
        actor_upn="dale@gruberpallets.com",
        actor_name="Dale",
    )

    assert response.status_code == 410
    assert json.loads(response.body) == {
        "ok": False,
        "error": "Use Luke's floor app to move this worker.",
    }
    assert delegated == []


def test_pending_live_breakdown_transfer_route_keeps_legacy_delegation(monkeypatch):
    from fastapi.responses import JSONResponse

    from zira_dashboard import attendance_location_policy, breakdown_actions
    from zira_dashboard.routes import exceptions as exceptions_route

    monkeypatch.setattr(attendance_location_policy, "live_is_active", lambda: False)
    delegated = []
    monkeypatch.setattr(
        breakdown_actions,
        "transfer",
        lambda body, actor_upn, actor_name, friendly_error: delegated.append(
            (body, actor_upn, actor_name, friendly_error)
        )
        or JSONResponse({"ok": True}),
    )
    body = {"incident_id": 1, "person_name": "Juan", "to_wc": "Repair 3"}

    response = exceptions_route._breakdown_transfer_sync(
        body,
        actor_upn="dale@gruberpallets.com",
        actor_name="Dale",
    )

    assert response.status_code == 200
    assert delegated[0][:3] == (
        body,
        "dale@gruberpallets.com",
        "Dale",
    )
