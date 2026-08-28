import os
from dataclasses import replace
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from zira_dashboard.app import app
from zira_dashboard import absence_pto_store as absence_store
from zira_dashboard.routes import time_off_approvals as page

# The merged /staffing/time-off page reads Postgres beyond what these tests
# monkeypatch, so the two route tests need a real database (they run in CI).
_needs_db = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"), reason="needs Postgres")


@pytest.fixture(autouse=True)
def _no_linked_requests_by_default(monkeypatch):
    monkeypatch.setattr(page.absence_pto_store, "list_pending", lambda: [])


def _linked(**changes):
    row = absence_store.AbsencePtoRequest(
        id=41,
        absence_day=date(2026, 8, 20),
        emp_id="44",
        person_odoo_id=44,
        person_name="Maria Example",
        holiday_status_id=7,
        leave_type_name="Paid Time Off",
        balance_at_submit=Decimal("4"),
        original_absence_leave_id=70,
        pto_leave_id=None,
        state="pending",
        conversion_step="not_started",
        employee_note=None,
        denial_reason=None,
        manual_resolution_note=None,
        sync_error=None,
        odoo_task_id=None,
        task_attempts=0,
        task_next_at=None,
        lease_owner=None,
        lease_until=None,
        requested_by_person_id=14,
        decided_by_upn=None,
        decided_by_name=None,
        requested_at=datetime(2026, 8, 21, tzinfo=timezone.utc),
        decided_at=None,
        resolved_at=None,
        created_at=datetime(2026, 8, 21, tzinfo=timezone.utc),
        updated_at=datetime(2026, 8, 21, tzinfo=timezone.utc),
    )
    return replace(row, **changes)


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
    assert r["request_kind"] == "time_off"
    assert r["action_base"] == "/api/exceptions/time-off/55"


def test_linked_pending_payload_has_distinct_action_contract(monkeypatch):
    monkeypatch.setattr(page, "_pending_rows", lambda today: [])
    monkeypatch.setattr(page.absence_pto_store, "list_pending", lambda: [_linked()])
    monkeypatch.setattr(
        page.time_off_context,
        "balance_for",
        lambda pid, hsid: {"remaining": 3.0, "unit": "days"},
    )
    monkeypatch.setattr(
        page.time_off_context,
        "coverage_for",
        lambda pid, start, end: {"count": 0, "scope": "department"},
    )
    monkeypatch.setattr(
        page.staffing_hours,
        "current_pay_period_bounds",
        lambda today: (date(2026, 8, 16), date(2026, 8, 29)),
    )

    rows = page._pending_payload(date(2026, 8, 28))

    assert rows == [{
        "id": 41,
        "person_odoo_id": 44,
        "person_name": "Maria Example",
        "holiday_status_id": 7,
        "leave_type": "Paid Time Off",
        "date_from": date(2026, 8, 20),
        "date_to": date(2026, 8, 20),
        "date_label": "2026-08-20",
        "hour_from": None,
        "hour_to": None,
        "state": "pending",
        "state_label": "To approve",
        "balance": {"remaining": 3.0, "unit": "days"},
        "coverage": {"count": 0, "scope": "department"},
        "request_amount": 1.0,
        "request_unit": "days",
        "over_balance": False,
        "past_due": False,
        "past_absence": True,
        "treatment_label": "Absent · unpaid",
        "period_open": True,
        "period_label": "Pay period open",
        "awaiting_second": False,
        "request_kind": "absence_pto",
        "action_base": "/api/exceptions/absence-pto/41",
    }]


def test_needs_review_payload_stays_visible_with_closed_period_state(monkeypatch):
    monkeypatch.setattr(page, "_pending_rows", lambda today: [])
    monkeypatch.setattr(
        page.absence_pto_store,
        "list_pending",
        lambda: [_linked(state="needs_review", sync_error="Period closed")],
    )
    monkeypatch.setattr(page.time_off_context, "balance_for", lambda *args: None)
    monkeypatch.setattr(
        page.time_off_context,
        "coverage_for",
        lambda *args: {"count": 0, "scope": "plant"},
    )
    monkeypatch.setattr(
        page.staffing_hours,
        "current_pay_period_bounds",
        lambda today: (date(2026, 8, 23), date(2026, 9, 5)),
    )

    linked = page._pending_payload(date(2026, 8, 28))[0]

    assert linked["state"] == "needs_review"
    assert linked["state_label"] == "Needs Wendy review"
    assert linked["period_open"] is False
    assert linked["period_label"] == "Pay period closed"


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
        "hour_from": 8.5,
        "hour_to": 12.25,
        "reason": None,
        "actor_name": "Dale Gruber",
        "actor_upn": "dale@gruberpallets.com",
        "decided_at": datetime(2026, 6, 24, 14, 5, tzinfo=timezone.utc),
    }])

    rows = page._recent_payload(days=30)

    assert rows[0]["decided_label"] == "6/24 9:05 AM"
    assert rows[0]["date_label"] == "2026-06-25 - 8:30 AM to 12:15 PM"


@_needs_db
def test_approvals_url_redirects_to_merged_time_off_page(monkeypatch):
    # The standalone approvals page merged into /staffing/time-off
    # (2026-07-22); the old URL 301s so bookmarks keep working.
    monkeypatch.setattr(page, "_pending_payload", lambda today: [])
    monkeypatch.setattr(page.time_off_audit, "recent_decisions", lambda days=30: [])
    client = TestClient(app)

    bare = client.get("/staffing/time-off/approvals", follow_redirects=False)
    assert bare.status_code == 301
    assert bare.headers["location"] == "/staffing/time-off"

    resp = client.get("/staffing/time-off/approvals")
    assert resp.status_code == 200
    assert "Time off approvals" in resp.text
    assert 'data-recent-decisions' in resp.text
    assert 'data-recent-empty' in resp.text


@_needs_db
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
        "date_label": "2026-06-20 - 8:30 AM to 12:15 PM",
        "reason": "Coverage too thin",
        "actor_name": "Dale Gruber",
        "actor_upn": "dale@gruberpallets.com",
        "decided_label": "6/24 9:05 AM",
    }])
    client = TestClient(app)

    resp = client.get("/staffing/time-off")

    assert resp.status_code == 200
    assert "Maria Delgado" in resp.text
    assert "24 hours left" in resp.text
    assert "2 off" in resp.text
    assert "Awaiting 2nd approval" in resp.text
    assert "Juan Morales" in resp.text
    assert "Coverage too thin" in resp.text
    assert "2026-06-20 - 8:30 AM to 12:15 PM" in resp.text
    assert "6/24 9:05 AM" in resp.text
    assert "/static/time_off_approvals.js" in resp.text
    assert 'data-pending-count' in resp.text
    assert 'data-recent-decisions' in resp.text
    assert 'aria-label="Reason to deny time off"' in resp.text
    # The Approvals tab is gone; the merged page lives under Time Off.
    assert "Approvals</a>" not in resp.text
    assert 'href="/staffing/time-off"   class="active">Time Off</a>' in resp.text


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
    assert "[data-recent-empty]" in js
    assert "resp.decision" in js
    assert "decision.date_label" in js
    assert "decision.decided_label" in js
    assert "event.key !== 'Enter'" in js
    assert ".js-refuse" in js
    assert "btn.click()" in js
    assert "bumpPendingCount(-1);" in js
    assert "No pending time-off requests." in js


def test_approvals_js_labels_locally_recorded_approvals():
    from pathlib import Path

    js = (
        Path(__file__).resolve().parents[1]
        / "src"
        / "zira_dashboard"
        / "static"
        / "time_off_approvals.js"
    ).read_text(encoding="utf-8")

    # Approvals that Odoo rejected for a Working Schedule conflict resolve
    # as local records — the row label must say so, not a plain "Approved".
    assert "resp.recorded_locally" in js
    assert "recorded here" in js


def test_approvals_markup_and_js_dispatch_by_server_action_metadata():
    from pathlib import Path

    root = Path(__file__).resolve().parents[1] / "src" / "zira_dashboard"
    template = (root / "templates" / "_time_off_approvals_panel.html").read_text(
        encoding="utf-8"
    )
    js = (root / "static" / "time_off_approvals.js").read_text(encoding="utf-8")

    assert 'data-request-kind="{{ r.request_kind }}"' in template
    assert 'data-action-base="{{ r.action_base }}"' in template
    assert "Past absence" in template
    assert "Absent · unpaid" in template
    assert "1 PTO day" in template
    assert "Mark handled" in template
    assert "js-handled-note" in template
    assert "row.dataset.actionBase" in js
    assert "row.dataset.requestKind === 'absence_pto' ? '/deny' : '/refuse'" in js
    assert "base + '/approve'" in js
    assert "base + '/handled'" in js
    assert "resp.warning" in js
    assert "resp.error" in js
