""""+ Report a breakdown" button: renders in the header, posts to the report endpoint."""
from starlette.testclient import TestClient

from zira_dashboard.app import app
from zira_dashboard.routes import exceptions as exceptions_route


def _snapshot():
    return {
        "today": "2026-07-08", "generated_at": "1:22 PM", "total": 0, "urgent_total": 0,
        "follow_up_total": 0, "source_errors": [], "work_centers": ["Dismantler 2", "Repair 3"],
        "people": [], "sections": [], "queue": [],
    }


def test_report_breakdown_button_renders_with_work_center_options(monkeypatch):
    monkeypatch.setattr(exceptions_route.exception_inbox, "build_snapshot", _snapshot)
    client = TestClient(app)
    resp = client.get("/exceptions")
    assert resp.status_code == 200
    assert "js-report-breakdown" in resp.text
    assert 'js-report-breakdown-wc' in resp.text
    assert '<option value="Dismantler 2">Dismantler 2</option>' in resp.text


def test_exceptions_js_has_report_breakdown_handler():
    from pathlib import Path
    js = (Path(__file__).resolve().parents[1] / "src" / "zira_dashboard" / "static" / "exceptions.js").read_text(encoding="utf-8")
    assert "js-report-breakdown" in js
    assert "/api/exceptions/breakdown/report" in js
