from datetime import date, datetime
from pathlib import Path

from fastapi.testclient import TestClient

from zira_dashboard import db, exception_inbox, missing_wc, missed_punch_out
from zira_dashboard.app import app
from zira_dashboard.routes import exceptions as exceptions_route
from zira_dashboard.routes import staffing as staffing_routes

STATIC_DIR = Path(__file__).resolve().parents[1] / "src" / "zira_dashboard" / "static"


def _snapshot():
    return {
        "today": "2026-06-19",
        "generated_at": "7:35 AM",
        "total": 1,
        "urgent_total": 0,
        "sections": [
            {
                "id": "assignments",
                "title": "Assignments To Do",
                "count": 1,
                "tone": "warn",
                "action_key": "assignments",
                "action_label": "Manage",
                "empty": "All clear",
                "rows": [{"name": "Repair 1", "label": "10 units", "detail": "7:00 AM to 8:00 AM"}],
            }
        ],
    }


def test_build_snapshot_aggregates_existing_alert_sources(monkeypatch):
    monkeypatch.setattr(exception_inbox.plant_day, "today", lambda: date(2026, 6, 19))
    monkeypatch.setattr(
        exception_inbox.plant_day,
        "now",
        lambda: datetime(2026, 6, 19, 7, 35),
    )
    monkeypatch.setattr(staffing_routes, "assignments_todo_payload", lambda: {
        "today": "2026-06-19",
        "count": 1,
        "people": ["Ana", "Ben"],
        "items": [{
            "wc_name": "Repair 1",
            "units": 10,
            "first_label": "7:00 AM",
            "last_label": "8:00 AM",
            "first_iso": "2026-06-19T12:00:00+00:00",
            "last_iso": "2026-06-19T13:00:00+00:00",
        }],
    })
    monkeypatch.setattr(staffing_routes, "late_report_payload", lambda: {
        "count": 2,
        "scheduled_late": [{"emp_id": 1, "name": "Ana", "minutes_late": 12}],
        "unscheduled_late": [{"emp_id": 2, "name": "Ben"}],
        "needs_reason": [],
        "snoozed": [{"emp_id": 3, "name": "Cal", "mins_remaining": 18}],
    })
    monkeypatch.setattr(missing_wc, "current_rows", lambda: [
        {"attendance_id": 10, "name": "Cam", "check_in_label": "7:05 AM Fri"},
    ])
    monkeypatch.setattr(missed_punch_out, "current_rows", lambda: [
        {"attendance_id": 11, "name": "Dee", "check_in_label": "1:00 PM Thu"},
    ])
    monkeypatch.setattr(exception_inbox, "_work_center_names", lambda: ["Repair 1"])
    monkeypatch.setattr(exception_inbox, "_pending_time_off", lambda today: (
        1,
        [{
            "name": "Eli",
            "label": "2026-06-20",
            "detail": "Vacation · confirm",
            "action": {"type": "time_off", "request_id": 20, "state": "confirm"},
        }],
    ))

    snap = exception_inbox.build_snapshot()

    assert snap["today"] == "2026-06-19"
    assert snap["total"] == 6
    assert snap["urgent_total"] == 4
    assert snap["follow_up_total"] == 1
    assert snap["source_errors"] == []
    counts = {s["id"]: s["count"] for s in snap["sections"]}
    assert counts == {
        "assignments": 1,
        "late": 2,
        "missing_wc": 1,
        "missed_punch_out": 1,
        "time_off": 1,
    }
    sections = {s["id"]: s for s in snap["sections"]}
    assert sections["assignments"]["context"]["people"] == ["Ana", "Ben"]
    assert sections["assignments"]["rows"][0]["priority"] == "warn"
    assert sections["assignments"]["rows"][0]["badge"] == "Credit"
    assert sections["assignments"]["rows"][0]["row_key"].startswith("assignment:Repair 1:")
    assert sections["assignments"]["rows"][0]["action"]["start_utc"].startswith("2026")
    assert sections["late"]["rows"][0]["action"]["emp_id"] == 1
    assert sections["late"]["rows"][0]["row_key"] == "late:scheduled:1"
    assert sections["late"]["rows"][-1]["label"] == "Snoozed"
    assert sections["late"]["rows"][-1]["priority"] == "muted"
    assert sections["late"]["rows"][-1]["row_key"] == "late_snoozed:3"
    assert sections["missing_wc"]["context"]["work_centers"] == ["Repair 1"]
    assert sections["missing_wc"]["rows"][0]["action"]["attendance_id"] == 10
    assert sections["missed_punch_out"]["rows"][0]["action"]["attendance_id"] == 11
    assert sections["time_off"]["rows"][0]["action"]["request_id"] == 20


def test_build_summary_counts_open_urgent_followup_and_time_off(monkeypatch):
    monkeypatch.setattr(exception_inbox.plant_day, "today", lambda: date(2026, 6, 19))
    monkeypatch.setattr(
        exception_inbox.plant_day,
        "now",
        lambda: datetime(2026, 6, 19, 8, 10),
    )
    monkeypatch.setattr(staffing_routes, "assignments_todo_payload", lambda: {"count": 2})
    monkeypatch.setattr(staffing_routes, "late_report_payload", lambda: {
        "count": 3,
        "scheduled_late": [{"emp_id": 1}],
        "unscheduled_late": [{"emp_id": 2}],
        "needs_reason": [{"emp_id": 3}],
        "snoozed": [{"emp_id": 4}],
    })
    monkeypatch.setattr(missing_wc, "current_rows", lambda: [{"attendance_id": 10}])
    monkeypatch.setattr(missed_punch_out, "current_rows", lambda: [{"attendance_id": 11}])
    monkeypatch.setattr(exception_inbox, "_pending_time_off_count", lambda today: 4)

    summary = exception_inbox.build_summary()

    assert summary["today"] == "2026-06-19"
    assert summary["generated_at"] == "8:10 AM"
    assert summary["total"] == 11
    assert summary["urgent_total"] == 4
    assert summary["follow_up_total"] == 1
    assert summary["source_errors"] == []
    assert summary["sections"] == {
        "assignments": 2,
        "late": 3,
        "missing_wc": 1,
        "missed_punch_out": 1,
        "time_off": 4,
    }


def test_pending_time_off_uses_window_count(monkeypatch):
    captured = {"calls": 0}

    def fake_query(sql, params):
        captured["calls"] += 1
        captured["sql"] = sql
        captured["params"] = params
        return [{
            "id": 20,
            "person_odoo_id": 7,
            "odoo_leave_id": 99,
            "name": "Eli",
            "shape": "full",
            "state": "confirm",
            "date_from": date(2026, 6, 20),
            "date_to": date(2026, 6, 20),
            "hour_from": None,
            "hour_to": None,
            "sync_error": None,
            "leave_type": "Vacation",
            "total_count": 4,
        }]

    monkeypatch.setattr(db, "query", fake_query)

    count, rows = exception_inbox._pending_time_off(date(2026, 6, 19), limit=8)

    assert captured["calls"] == 1
    assert "COUNT(*) OVER () AS total_count" in captured["sql"]
    assert captured["params"] == (date(2026, 6, 19), 8)
    assert count == 4
    assert rows[0]["name"] == "Eli"
    assert rows[0]["row_key"] == "time_off:20:confirm"


def test_snapshot_marks_degraded_sources_without_hiding_page(monkeypatch):
    monkeypatch.setattr(exception_inbox.plant_day, "today", lambda: date(2026, 6, 19))
    monkeypatch.setattr(
        exception_inbox.plant_day,
        "now",
        lambda: datetime(2026, 6, 19, 8, 20),
    )

    def fail_assignments():
        raise RuntimeError("boom")

    monkeypatch.setattr(staffing_routes, "assignments_todo_payload", fail_assignments)
    monkeypatch.setattr(staffing_routes, "late_report_payload", lambda: {"count": 0})
    monkeypatch.setattr(missing_wc, "current_rows", lambda: [])
    monkeypatch.setattr(missed_punch_out, "current_rows", lambda: [])
    monkeypatch.setattr(exception_inbox, "_pending_time_off", lambda today: (0, []))
    monkeypatch.setattr(exception_inbox, "_work_center_names", lambda: [])

    snap = exception_inbox.build_snapshot()

    assert snap["total"] == 0
    assert snap["source_errors"] == [{"source": "Assignments To Do"}]
    assert [s["id"] for s in snap["sections"]] == [
        "assignments",
        "late",
        "missing_wc",
        "missed_punch_out",
        "time_off",
    ]


def test_exceptions_api_uses_snapshot(monkeypatch):
    monkeypatch.setattr(exceptions_route.exception_inbox, "build_snapshot", _snapshot)
    client = TestClient(app)

    resp = client.get("/api/exceptions")

    assert resp.status_code == 200
    assert resp.json()["total"] == 1


def test_exceptions_summary_api_uses_summary(monkeypatch):
    monkeypatch.setattr(
        exceptions_route.exception_inbox,
        "build_summary",
        lambda: {"total": 3, "urgent_total": 2, "sections": {}},
    )
    client = TestClient(app)

    resp = client.get("/api/exceptions/summary")

    assert resp.status_code == 200
    assert resp.json()["urgent_total"] == 2


def test_exceptions_page_renders_sections(monkeypatch):
    snapshot = _snapshot()
    snapshot["urgent_total"] = 1
    snapshot["follow_up_total"] = 2
    monkeypatch.setattr(exceptions_route.exception_inbox, "build_snapshot", lambda: snapshot)
    client = TestClient(app)

    resp = client.get("/exceptions")

    assert resp.status_code == 200
    assert "Exception Inbox" in resp.text
    assert "Repair 1" in resp.text
    assert "Queue changed since this page loaded." in resp.text
    assert 'data-focus-mode="urgent"' in resp.text
    assert 'data-focus-count="all"' in resp.text
    assert 'data-focus-count="urgent"' in resp.text
    assert 'data-focus-count="followup"' in resp.text
    assert ">1</span> urgent" in resp.text
    assert ">2</span> follow-up" in resp.text


def test_exceptions_page_bootstraps_nav_summary(monkeypatch):
    monkeypatch.setattr(exceptions_route.exception_inbox, "build_snapshot", _snapshot)
    client = TestClient(app)

    resp = client.get("/exceptions")

    assert resp.status_code == 200
    assert 'id="gpi-inbox-summary-bootstrap"' in resp.text
    assert '"total": 1' in resp.text
    assert '"urgent_total": 0' in resp.text


def test_exceptions_page_renders_source_warning(monkeypatch):
    snapshot = _snapshot()
    snapshot["source_errors"] = [{"source": "Pending Time Off"}]
    monkeypatch.setattr(exceptions_route.exception_inbox, "build_snapshot", lambda: snapshot)
    client = TestClient(app)

    resp = client.get("/exceptions")

    assert resp.status_code == 200
    assert "Some checks could not load." in resp.text
    assert "Pending Time Off" in resp.text
    assert 'data-source-errors="Pending Time Off"' in resp.text


def test_exceptions_page_renders_inline_action_controls(monkeypatch):
    snapshot = {
        "today": "2026-06-19",
        "generated_at": "7:35 AM",
        "total": 2,
        "urgent_total": 1,
        "sections": [
            {
                "id": "assignments",
                "title": "Assignments To Do",
                "count": 1,
                "tone": "warn",
                "action_key": "assignments",
                "action_label": "Manage",
                "empty": "All clear",
                "context": {"people": ["Ana"]},
                "rows": [{
                    "name": "Repair 1",
                    "label": "10 units",
                    "detail": "7:00 AM to 8:00 AM",
                    "priority": "warn",
                    "badge": "Credit",
                    "row_key": "assignment:Repair 1:2026-06-19T12:00:00+00:00",
                    "action": {
                        "type": "assignment",
                        "day": "2026-06-19",
                        "wc_name": "Repair 1",
                        "start_utc": "2026-06-19T12:00:00+00:00",
                    },
                }],
            },
            {
                "id": "time_off",
                "title": "Pending Time Off",
                "count": 1,
                "tone": "info",
                "href": "/staffing/time-off",
                "empty": "All clear",
                "context": {},
                "rows": [{
                    "name": "Eli",
                    "label": "2026-06-22",
                    "detail": "Vacation · confirm",
                    "action": {"type": "time_off", "request_id": 20},
                }],
            },
        ],
    }
    monkeypatch.setattr(exceptions_route.exception_inbox, "build_snapshot", lambda: snapshot)
    client = TestClient(app)

    resp = client.get("/exceptions")

    assert resp.status_code == 200
    assert 'data-action-type="assignment"' in resp.text
    assert 'class="row-btn primary js-assign"' in resp.text
    assert 'data-urgent-open' in resp.text
    assert ">1</span> urgent" in resp.text
    assert "priority-pill warn" in resp.text
    assert 'data-row-key="assignment:Repair 1:2026-06-19T12:00:00+00:00"' in resp.text
    assert 'data-action-type="time_off"' in resp.text
    assert "js-time-off-approve" in resp.text


def test_exceptions_js_refreshes_shared_badges_after_inline_resolution():
    js = (STATIC_DIR / "exceptions.js").read_text(encoding="utf-8")

    assert "function refreshSharedBadge(row)" in js
    assert "actionType === 'assignment'" in js
    assert "actionType === 'late_absence' || actionType === 'late_reason'" in js
    assert "actionType === 'missing_wc'" in js
    assert "actionType === 'missed_punch_out'" in js
    assert "api.refreshCount()" in js
    assert "refreshSharedBadge(row);" in js
    assert "window.gpiRefreshInboxSummary" in js
    assert "resp.approved === false" in js
    assert "Moved forward; refreshing..." in js
    assert "snapshot.source_errors" in js
    assert "data-source-warning" in js
    assert "function bumpFocusCounts(row, delta)" in js
    assert "[data-focus-count=\"" in js
    assert "bumpFocusCount('urgent', delta)" in js


def test_footer_enhances_inbox_nav_with_summary_count():
    js = (STATIC_DIR / "footer.js").read_text(encoding="utf-8")
    css = (STATIC_DIR / "footer.css").read_text(encoding="utf-8")

    assert "/api/exceptions/summary" in js
    assert "startInboxSummary(ensureInboxLink())" in js
    assert "readInboxSummaryBootstrap" in js
    assert "updateInboxSummaryLink(link, initial)" in js
    assert "window.gpiRefreshInboxSummary" in js
    assert "inbox-nav-count" in js
    assert "source_errors" in js
    assert "is-degraded" in js
    assert ".inbox-nav-count" in css
    assert ".inbox-nav-link.has-urgent .inbox-nav-count" in css
    assert ".inbox-nav-link.is-degraded .inbox-nav-count" in css


def test_time_off_approve_endpoint_updates_to_odoo_state(monkeypatch):
    from zira_dashboard import odoo_client

    row = {
        "id": 55,
        "person_odoo_id": 7,
        "date_from": date(2026, 6, 22),
        "date_to": date(2026, 6, 22),
        "state": "confirm",
        "odoo_leave_id": 99,
    }
    updates = []
    monkeypatch.setattr(exceptions_route, "_load_time_off_request", lambda request_id: row)
    monkeypatch.setattr(odoo_client, "approve_leave", lambda leave_id: "validate")
    monkeypatch.setattr(
        exceptions_route,
        "_set_time_off_state",
        lambda old, state: updates.append((old["id"], state)),
    )

    resp = exceptions_route._approve_time_off_sync(55)

    assert resp.status_code == 200
    assert resp.body
    assert updates == [(55, "validate")]


def test_time_off_refuse_unsynced_draft_stays_local(monkeypatch):
    from zira_dashboard import odoo_client

    row = {
        "id": 56,
        "person_odoo_id": 8,
        "date_from": date(2026, 6, 22),
        "date_to": date(2026, 6, 22),
        "state": "draft",
        "odoo_leave_id": None,
    }
    updates = []
    refused = []
    monkeypatch.setattr(exceptions_route, "_load_time_off_request", lambda request_id: row)
    monkeypatch.setattr(odoo_client, "refuse_leave", lambda leave_id: refused.append(leave_id))
    monkeypatch.setattr(
        exceptions_route,
        "_set_time_off_state",
        lambda old, state: updates.append((old["id"], state)),
    )

    resp = exceptions_route._refuse_time_off_sync(56)

    assert resp.status_code == 200
    assert refused == []
    assert updates == [(56, "refuse")]


def test_load_time_off_request_selects_name_and_type(monkeypatch):
    captured = {}
    def fake_query(sql, params):
        captured["sql"] = sql
        return [{"id": 55, "person_name": "Maria Delgado", "leave_type": "PTO"}]
    from zira_dashboard import db as _db
    monkeypatch.setattr(_db, "query", fake_query)

    row = exceptions_route._load_time_off_request(55)

    assert row["person_name"] == "Maria Delgado"
    assert row["leave_type"] == "PTO"
    assert "COALESCE(p.name" in captured["sql"]
    assert "leave_types_cache" in captured["sql"]
