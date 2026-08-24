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


def _late_snapshot():
    late = {
        "name": "Jesus Galindo", "label": "Scheduled late", "detail": "12 mins late",
        "priority": "urgent", "badge": "Needs decision",
        "row_key": "late:scheduled:7", "item_key": "late:7:2026-07-13",
        "action": {"type": "late_absence", "emp_id": "7", "name": "Jesus Galindo"},
    }
    return {
        "today": "2026-07-13", "generated_at": "9:00 AM", "total": 1, "urgent_total": 1,
        "follow_up_total": 0, "source_errors": [], "work_centers": [], "people": [], "sections": [],
        "queue": [{**late, "section_id": "late", "category_label": "Late / Absence", "tone": "bad"}],
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


def test_breakdown_transfer_dropdown_excludes_current_work_center(monkeypatch):
    monkeypatch.setattr(exceptions_route.exception_inbox, "build_snapshot", _snapshot)
    client = TestClient(app)
    resp = client.get("/exceptions")
    assert resp.status_code == 200

    transfer_select = resp.text.split('aria-label="Work center to transfer to"', 1)[1]
    transfer_select = transfer_select.split("</select>", 1)[0]
    assert '<option value="Repair 3">Repair 3</option>' in transfer_select
    assert '<option value="Dismantler 2">Dismantler 2</option>' not in transfer_select


def test_late_absence_row_renders_running_late_controls(monkeypatch):
    monkeypatch.setattr(exceptions_route.exception_inbox, "build_snapshot", _late_snapshot)
    response = TestClient(app).get("/exceptions")

    assert ">Absent</button>" in response.text
    assert ">Running Late — 60 min</button>" in response.text
    assert "js-reason-input" not in response.text
    assert "js-running-late-time" not in response.text
    assert "js-running-late-save" not in response.text
