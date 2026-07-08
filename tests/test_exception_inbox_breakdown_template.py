"""Template rendering for the breakdown header + operator rows."""
from zira_dashboard.routes import exceptions as exceptions_route
from starlette.testclient import TestClient
from zira_dashboard.app import app


def _snapshot():
    header = {
        "name": "Dismantler 2", "label": "Stopped producing",
        "detail": "No output since 1:02 PM (23 min)",
        "priority": "urgent", "badge": "AUTO-DETECTED",
        "row_key": "breakdown_header:Dismantler 2:x", "item_key": "breakdown:Dismantler 2:x",
        "action": None, "dismiss_action": {"type": "breakdown_dismiss", "incident_id": 1},
    }
    operator = {
        "name": "Juan", "label": "Idle — Dismantler 2 is down", "detail": "",
        "priority": "urgent", "badge": "Needs decision",
        "row_key": "breakdown_op:Dismantler 2:x:Juan", "item_key": "breakdown:Dismantler 2:x:Juan",
        "action": {"type": "breakdown", "incident_id": 1, "person_name": "Juan", "wc_name": "Dismantler 2"},
    }
    return {
        "today": "2026-07-08", "generated_at": "1:22 PM", "total": 2, "urgent_total": 2,
        "follow_up_total": 0, "source_errors": [], "work_centers": ["Repair 3", "Dismantler 2"],
        "people": [], "sections": [],
        "queue": [
            {**header, "section_id": "breakdown", "category_label": "Machine Breakdown", "tone": "bad"},
            {**operator, "section_id": "breakdown", "category_label": "Machine Breakdown", "tone": "bad"},
        ],
    }


def test_breakdown_header_row_renders_dismiss_button(monkeypatch):
    monkeypatch.setattr(exceptions_route.exception_inbox, "build_snapshot", _snapshot)
    client = TestClient(app)
    resp = client.get("/exceptions")
    assert resp.status_code == 200
    assert 'data-action-type="breakdown_header"' in resp.text
    assert 'data-incident-id="1"' in resp.text
    assert "js-breakdown-dismiss" in resp.text
    assert "Not a breakdown" in resp.text


def test_breakdown_operator_row_renders_transfer_and_snooze(monkeypatch):
    monkeypatch.setattr(exceptions_route.exception_inbox, "build_snapshot", _snapshot)
    client = TestClient(app)
    resp = client.get("/exceptions")
    assert resp.status_code == 200
    assert 'data-action-type="breakdown"' in resp.text
    assert 'data-person-name="Juan"' in resp.text
    assert "js-breakdown-transfer" in resp.text
    assert "js-breakdown-snooze" in resp.text
    assert '<option value="Repair 3">Repair 3</option>' in resp.text
