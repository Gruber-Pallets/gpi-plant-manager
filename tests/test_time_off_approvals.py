from datetime import date, datetime, timezone

from fastapi.testclient import TestClient

from zira_dashboard.app import app
from zira_dashboard.routes import time_off_approvals as page


def test_pending_payload_attaches_balance_and_coverage(monkeypatch):
    monkeypatch.setattr(page, "_pending_rows", lambda today: [{
        "id": 55,
        "person_odoo_id": 7,
        "person_name": "Maria Delgado",
        "leave_type": "PTO",
        "holiday_status_id": 3,
        "date_from": date(2026, 6, 30),
        "date_to": date(2026, 7, 2),
        "hour_from": None,
        "hour_to": None,
        "state": "confirm",
    }])
    monkeypatch.setattr(
        page.time_off_context,
        "balance_for",
        lambda pid, hsid: {"remaining": 24.0, "unit": "days"},
    )
    monkeypatch.setattr(
        page.time_off_context,
        "coverage_for",
        lambda pid, df, dt: {"count": 2, "scope": "department"},
    )

    rows = page._pending_payload(date(2026, 6, 24))

    assert len(rows) == 1
    r = rows[0]
    assert r["person_name"] == "Maria Delgado"
    assert r["balance"] == {"remaining": 24.0, "unit": "days"}
    assert r["coverage"] == {"count": 2, "scope": "department"}
    assert r["state_label"] == "To approve"
    assert r["over_balance"] is False
    assert r["past_due"] is False


def test_pending_payload_flags_over_balance_and_past_due(monkeypatch):
    monkeypatch.setattr(page, "_pending_rows", lambda today: [{
        "id": 56,
        "person_odoo_id": 8,
        "person_name": "Juan Morales",
        "leave_type": "Sick",
        "holiday_status_id": 4,
        "date_from": date(2026, 6, 20),
        "date_to": date(2026, 6, 20),
        "hour_from": 8.0,
        "hour_to": 12.0,
        "state": "confirm",
    }])
    monkeypatch.setattr(
        page.time_off_context,
        "balance_for",
        lambda pid, hsid: {"remaining": 2.0, "unit": "hours"},
    )
    monkeypatch.setattr(
        page.time_off_context,
        "coverage_for",
        lambda pid, df, dt: {"count": 0, "scope": "department"},
    )

    rows = page._pending_payload(date(2026, 6, 24))

    assert rows[0]["over_balance"] is True
    assert rows[0]["past_due"] is True


def test_pending_payload_formats_partial_time_window(monkeypatch):
    monkeypatch.setattr(page, "_pending_rows", lambda today: [{
        "id": 57,
        "person_odoo_id": 9,
        "person_name": "Luis Vega",
        "leave_type": "Appointment",
        "holiday_status_id": 5,
        "date_from": date(2026, 6, 25),
        "date_to": date(2026, 6, 25),
        "hour_from": 8.5,
        "hour_to": 12.25,
        "state": "confirm",
    }])
    monkeypatch.setattr(page.time_off_context, "balance_for", lambda pid, hsid: None)
    monkeypatch.setattr(
        page.time_off_context,
        "coverage_for",
        lambda pid, df, dt: {"count": 0, "scope": "department"},
    )

    rows = page._pending_payload(date(2026, 6, 24))

    assert rows[0]["date_label"] == "2026-06-25 - 8:30 AM to 12:15 PM"


def test_recent_payload_formats_decision_time_in_plant_timezone(monkeypatch):
    monkeypatch.setattr(page.time_off_audit, "recent_decisions", lambda days=30: [{
        "person_name": "Ana Flores",
        "action": "approve",
        "leave_type": "PTO",
        "date_from": date(2026, 6, 25),
        "date_to": date(2026, 6, 25),
        "reason": None,
        "actor_name": "Dale Gruber",
        "actor_upn": "dale@gruberpallets.com",
        "decided_at": datetime(2026, 6, 24, 14, 5, tzinfo=timezone.utc),
    }])

    rows = page._recent_payload(days=30)

    assert rows[0]["decided_label"] == "6/24 9:05 AM"


def test_approvals_page_renders_200(monkeypatch):
    monkeypatch.setattr(page, "_pending_payload", lambda today: [])
    monkeypatch.setattr(page.time_off_audit, "recent_decisions", lambda days=30: [])
    client = TestClient(app)

    resp = client.get("/staffing/time-off/approvals")

    assert resp.status_code == 200
    assert "Time off approvals" in resp.text


def test_approvals_page_renders_pending_context_and_recent_decisions(monkeypatch):
    monkeypatch.setattr(page, "_pending_payload", lambda today: [{
        "id": 55,
        "person_name": "Maria Delgado",
        "leave_type": "PTO",
        "date_from": date(2026, 6, 30),
        "date_to": date(2026, 7, 2),
        "date_label": "2026-06-30 to 2026-07-02",
        "balance": {"remaining": 24.0, "unit": "hours"},
        "coverage": {"count": 2, "scope": "department"},
        "over_balance": False,
        "past_due": False,
        "awaiting_second": True,
        "state_label": "Awaiting 2nd approval",
    }])
    monkeypatch.setattr(page, "_recent_payload", lambda days=30: [{
        "person_name": "Juan Morales",
        "action": "deny",
        "leave_type": "Sick",
        "date_from": date(2026, 6, 20),
        "date_to": date(2026, 6, 20),
        "reason": "Coverage too thin",
        "actor_name": "Dale Gruber",
        "actor_upn": "dale@gruberpallets.com",
        "decided_label": "6/24 9:05 AM",
    }])
    client = TestClient(app)

    resp = client.get("/staffing/time-off/approvals")

    assert resp.status_code == 200
    assert "Maria Delgado" in resp.text
    assert "24 hours left" in resp.text
    assert "2 off" in resp.text
    assert "Awaiting 2nd approval" in resp.text
    assert "Juan Morales" in resp.text
    assert "Coverage too thin" in resp.text
    assert "6/24 9:05 AM" in resp.text
    assert "/static/time_off_approvals.js" in resp.text
    assert 'data-pending-count' in resp.text
    assert 'data-recent-decisions' in resp.text
    assert 'href="/staffing/time-off/approvals"   class="active">Approvals</a>' in resp.text


def test_approvals_js_removes_resolved_rows_and_updates_pending_counts():
    from pathlib import Path

    js = (
        Path(__file__).resolve().parents[1]
        / "src"
        / "zira_dashboard"
        / "static"
        / "time_off_approvals.js"
    ).read_text(encoding="utf-8")

    assert "function bumpPendingCount(delta)" in js
    assert "[data-pending-count]" in js
    assert "function removeResolvedRow(row)" in js
    assert "function prependDecision(decision)" in js
    assert "[data-recent-decisions]" in js
    assert "resp.decision" in js
    assert "bumpPendingCount(-1);" in js
    assert "No pending time-off requests." in js
