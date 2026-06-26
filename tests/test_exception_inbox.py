import json
from datetime import date, datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient

from zira_dashboard import db, exception_inbox, missing_wc, missed_punch_out, staffing
from zira_dashboard.app import app
from zira_dashboard.routes import exceptions as exceptions_route
from zira_dashboard.routes import staffing as staffing_routes

STATIC_DIR = Path(__file__).resolve().parents[1] / "src" / "zira_dashboard" / "static"


def _snapshot():
    row = {
        "name": "Repair 1",
        "label": "10 units",
        "detail": "7:00 AM to 8:00 AM",
        "priority": "warn",
        "badge": "Credit",
        "row_key": "assignment:Repair 1:2026-06-19T12:00:00+00:00",
        "item_key": "assignment:Repair 1:2026-06-19T12:00:00+00:00",
    }
    return {
        "today": "2026-06-19",
        "generated_at": "7:35 AM",
        "total": 1,
        "urgent_total": 0,
        "follow_up_total": 0,
        "source_errors": [],
        "work_centers": ["Repair 1"],
        "people": ["Ana", "Ben"],
        "sections": [
            {
                "id": "assignments",
                "title": "Assignments To Do",
                "count": 1,
                "tone": "warn",
                "action_key": "assignments",
                "action_label": "Manage",
                "empty": "All clear",
                "rows": [row],
            }
        ],
        "queue": [{**row, "section_id": "assignments", "category_label": "Assignments To Do", "tone": "warn"}],
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
        "plant_schedule": 0,
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
        "plant_schedule": 0,
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
        "plant_schedule",
        "late",
        "missing_wc",
        "missed_punch_out",
        "time_off",
    ]


def test_pending_time_off_section_links_to_approvals_page(monkeypatch):
    monkeypatch.setattr(exception_inbox.plant_day, "today", lambda: date(2026, 6, 19))
    monkeypatch.setattr(staffing_routes, "assignments_todo_payload", lambda: {"count": 0})
    monkeypatch.setattr(staffing_routes, "late_report_payload", lambda: {"count": 0})
    monkeypatch.setattr(missing_wc, "current_rows", lambda: [])
    monkeypatch.setattr(missed_punch_out, "current_rows", lambda: [])
    monkeypatch.setattr(exception_inbox, "_pending_time_off", lambda today: (0, []))
    monkeypatch.setattr(exception_inbox, "_work_center_names", lambda: [])

    snap = exception_inbox.build_snapshot()

    time_off = next(s for s in snap["sections"] if s["id"] == "time_off")
    assert time_off["href"] == "/staffing/time-off/approvals"


def _empty_inbox_sources(monkeypatch):
    monkeypatch.setattr(staffing_routes, "assignments_todo_payload", lambda: {"count": 0})
    monkeypatch.setattr(staffing_routes, "late_report_payload", lambda: {"count": 0})
    monkeypatch.setattr(missing_wc, "current_rows", lambda: [])
    monkeypatch.setattr(missed_punch_out, "current_rows", lambda: [])
    monkeypatch.setattr(exception_inbox, "_pending_time_off", lambda today: (0, []))
    monkeypatch.setattr(exception_inbox, "_pending_time_off_count", lambda today: 0)
    monkeypatch.setattr(exception_inbox, "_work_center_names", lambda: [])


def test_plant_schedule_reminder_waits_until_cutoff(monkeypatch):
    monkeypatch.setattr(
        exception_inbox.plant_day,
        "now",
        lambda: datetime(2026, 6, 25, 13, 29),
    )

    count, rows = exception_inbox._plant_schedule_reminder()

    assert count == 0
    assert rows == []


def test_plant_schedule_reminder_adds_unpublished_next_business_day(monkeypatch):
    loaded_days = []
    monkeypatch.setattr(
        exception_inbox.plant_day,
        "now",
        lambda: datetime(2026, 6, 25, 13, 30),
    )
    monkeypatch.setattr(
        exception_inbox.schedule_store,
        "current",
        lambda: SimpleNamespace(work_weekdays=frozenset({0, 1, 2, 3, 4})),
    )

    def fake_load_schedule(day):
        loaded_days.append(day)
        return staffing.Schedule(day=day, published=False)

    monkeypatch.setattr(exception_inbox.staffing, "load_schedule", fake_load_schedule)

    count, rows = exception_inbox._plant_schedule_reminder()

    assert loaded_days == [date(2026, 6, 26)]
    assert count == 1
    assert rows == [{
        "name": "Plant Schedule",
        "label": "Friday, Jun 26",
        "detail": "Not published",
        "priority": "warn",
        "badge": "Publish",
        "href": "/staffing?day=2026-06-26",
        "row_key": "plant_schedule:2026-06-26",
        "item_key": "plant_schedule:2026-06-26",
    }]


def test_plant_schedule_reminder_skips_published_target(monkeypatch):
    monkeypatch.setattr(
        exception_inbox.plant_day,
        "now",
        lambda: datetime(2026, 6, 25, 14, 0),
    )
    monkeypatch.setattr(
        exception_inbox.schedule_store,
        "current",
        lambda: SimpleNamespace(work_weekdays=frozenset({0, 1, 2, 3, 4})),
    )
    monkeypatch.setattr(
        exception_inbox.staffing,
        "load_schedule",
        lambda day: staffing.Schedule(day=day, published=True),
    )

    count, rows = exception_inbox._plant_schedule_reminder()

    assert count == 0
    assert rows == []


def test_plant_schedule_reminder_friday_after_cutoff_targets_monday(monkeypatch):
    loaded_days = []
    monkeypatch.setattr(
        exception_inbox.plant_day,
        "now",
        lambda: datetime(2026, 6, 26, 14, 0),
    )
    monkeypatch.setattr(
        exception_inbox.schedule_store,
        "current",
        lambda: SimpleNamespace(work_weekdays=frozenset({0, 1, 2, 3, 4})),
    )

    def fake_load_schedule(day):
        loaded_days.append(day)
        return staffing.Schedule(day=day, published=False)

    monkeypatch.setattr(exception_inbox.staffing, "load_schedule", fake_load_schedule)

    count, rows = exception_inbox._plant_schedule_reminder()

    assert loaded_days == [date(2026, 6, 29)]
    assert count == 1
    assert rows[0]["label"] == "Monday, Jun 29"


def test_snapshot_includes_unpublished_schedule_section_after_cutoff(monkeypatch):
    _empty_inbox_sources(monkeypatch)
    monkeypatch.setattr(
        exception_inbox.plant_day,
        "today",
        lambda: date(2026, 6, 25),
    )
    monkeypatch.setattr(
        exception_inbox.plant_day,
        "now",
        lambda: datetime(2026, 6, 25, 13, 45),
    )
    monkeypatch.setattr(
        exception_inbox.schedule_store,
        "current",
        lambda: SimpleNamespace(work_weekdays=frozenset({0, 1, 2, 3, 4})),
    )
    monkeypatch.setattr(
        exception_inbox.staffing,
        "load_schedule",
        lambda day: staffing.Schedule(day=day, published=False),
    )

    snap = exception_inbox.build_snapshot()

    plant_schedule = next(s for s in snap["sections"] if s["id"] == "plant_schedule")
    assert snap["total"] == 1
    assert snap["urgent_total"] == 0
    assert plant_schedule["count"] == 1
    assert plant_schedule["title"] == "Plant Schedule"
    assert plant_schedule["tone"] == "warn"
    assert plant_schedule["href"] == "/staffing?day=2026-06-26"
    assert plant_schedule["rows"][0]["badge"] == "Publish"


def test_summary_includes_unpublished_schedule_after_cutoff(monkeypatch):
    _empty_inbox_sources(monkeypatch)
    monkeypatch.setattr(
        exception_inbox.plant_day,
        "today",
        lambda: date(2026, 6, 25),
    )
    monkeypatch.setattr(
        exception_inbox.plant_day,
        "now",
        lambda: datetime(2026, 6, 25, 13, 45),
    )
    monkeypatch.setattr(
        exception_inbox.schedule_store,
        "current",
        lambda: SimpleNamespace(work_weekdays=frozenset({0, 1, 2, 3, 4})),
    )
    monkeypatch.setattr(
        exception_inbox.staffing,
        "load_schedule",
        lambda day: staffing.Schedule(day=day, published=False),
    )

    summary = exception_inbox.build_summary()

    assert summary["total"] == 1
    assert summary["sections"]["plant_schedule"] == 1


def test_schedule_source_failure_marks_inbox_degraded(monkeypatch):
    _empty_inbox_sources(monkeypatch)
    monkeypatch.setattr(
        exception_inbox.plant_day,
        "today",
        lambda: date(2026, 6, 25),
    )
    monkeypatch.setattr(
        exception_inbox.plant_day,
        "now",
        lambda: datetime(2026, 6, 25, 13, 45),
    )

    def fail_schedule():
        raise RuntimeError("schedule settings unavailable")

    monkeypatch.setattr(exception_inbox, "_plant_schedule_reminder", fail_schedule)

    snap = exception_inbox.build_snapshot()

    assert snap["total"] == 0
    assert {"source": "Plant Schedule"} in snap["source_errors"]
    plant_schedule = next(s for s in snap["sections"] if s["id"] == "plant_schedule")
    assert plant_schedule["count"] == 0
    assert plant_schedule["rows"] == []


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


def test_exceptions_page_renders_flat_queue(monkeypatch):
    snapshot = _snapshot()
    snapshot["urgent_total"] = 1
    snapshot["follow_up_total"] = 2
    monkeypatch.setattr(exceptions_route.exception_inbox, "build_snapshot", lambda: snapshot)
    client = TestClient(app)

    resp = client.get("/exceptions")

    assert resp.status_code == 200
    assert "Exception Inbox" in resp.text
    assert "Repair 1" in resp.text
    # Flat queue of card rows, not the old section/summary DOM.
    assert 'class="queue"' in resp.text
    assert "exception-row priority-warn" in resp.text
    assert '<span class="exception-name">Repair 1</span>' in resp.text
    assert 'class="category-tag tone-warn">Assignments To Do<' in resp.text
    assert 'data-item-key="assignment:Repair 1:2026-06-19T12:00:00+00:00"' in resp.text
    assert "summary-strip" not in resp.text
    assert "summary-tile" not in resp.text
    assert "Queue changed since this page loaded." in resp.text
    assert 'data-focus-mode="urgent"' in resp.text
    assert 'data-focus-count="all"' in resp.text
    assert 'data-focus-count="urgent"' in resp.text
    assert 'data-focus-count="followup"' in resp.text
    assert ">1</span> urgent" in resp.text
    assert ">2</span> follow-up" in resp.text


def test_exceptions_page_renders_archive_controls(monkeypatch):
    monkeypatch.setattr(exceptions_route.exception_inbox, "build_snapshot", _snapshot)
    client = TestClient(app)

    resp = client.get("/exceptions")

    assert resp.status_code == 200
    assert "data-archive-toggle" in resp.text
    assert "data-archive-actor" in resp.text
    assert "data-archive-hide-auto" in resp.text
    assert "data-archive-groups" in resp.text
    assert "data-archive-more" in resp.text
    assert ">Everyone</option>" in resp.text
    assert "Hide auto-resolved" in resp.text


def test_exceptions_page_shows_inbox_zero_when_queue_empty(monkeypatch):
    snapshot = _snapshot()
    snapshot["total"] = 0
    snapshot["queue"] = []
    snapshot["sections"] = []
    monkeypatch.setattr(exceptions_route.exception_inbox, "build_snapshot", lambda: snapshot)
    client = TestClient(app)

    resp = client.get("/exceptions")

    assert resp.status_code == 200
    assert "Inbox zero — nothing needs you right now." in resp.text


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
    assignment_row = {
        "name": "Repair 1",
        "label": "10 units",
        "detail": "7:00 AM to 8:00 AM",
        "priority": "warn",
        "badge": "Credit",
        "row_key": "assignment:Repair 1:2026-06-19T12:00:00+00:00",
        "item_key": "assignment:Repair 1:2026-06-19T12:00:00+00:00",
        "action": {
            "type": "assignment",
            "day": "2026-06-19",
            "wc_name": "Repair 1",
            "start_utc": "2026-06-19T12:00:00+00:00",
        },
    }
    time_off_row = {
        "name": "Eli",
        "label": "2026-06-22",
        "detail": "Vacation · confirm",
        "priority": "info",
        "badge": "Approval",
        "row_key": "time_off:20:confirm",
        "item_key": "time_off:20",
        "action": {"type": "time_off", "request_id": 20},
    }
    snapshot = {
        "today": "2026-06-19",
        "generated_at": "7:35 AM",
        "total": 2,
        "urgent_total": 1,
        "follow_up_total": 0,
        "source_errors": [],
        "work_centers": [],
        "people": ["Ana"],
        "sections": [],
        "queue": [
            {**assignment_row, "section_id": "assignments", "category_label": "Assignments To Do", "tone": "warn"},
            {**time_off_row, "section_id": "time_off", "category_label": "Pending Time Off", "tone": "info"},
        ],
    }
    monkeypatch.setattr(exceptions_route.exception_inbox, "build_snapshot", lambda: snapshot)
    client = TestClient(app)

    resp = client.get("/exceptions")

    assert resp.status_code == 200
    assert 'data-action-type="assignment"' in resp.text
    assert 'class="row-btn primary js-assign"' in resp.text
    # Assignment person options come from the top-level people var now.
    assert '<option value="Ana">Ana</option>' in resp.text
    assert 'data-urgent-open' in resp.text
    assert ">1</span> urgent" in resp.text
    assert "priority-pill warn" in resp.text
    assert 'data-row-key="assignment:Repair 1:2026-06-19T12:00:00+00:00"' in resp.text
    assert 'data-person-name="Eli"' in resp.text
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


def test_exceptions_js_drives_queue_and_archive():
    js = (STATIC_DIR / "exceptions.js").read_text(encoding="utf-8")

    # personName lookup no longer relies on a <th>; it falls back to .exception-name.
    assert "row.dataset.personName" in js
    assert "row.querySelector('.exception-name')" in js
    assert "querySelector('th')" not in js
    # Flat queue + inbox-zero handling, no section/badge bumping.
    assert "function updateQueueEmpty()" in js
    assert "[data-queue-empty]" in js
    assert "function applyFocus(mode)" in js
    assert "row.dataset.priority === 'urgent'" in js
    assert "row.dataset.priority === 'muted'" in js
    # Freshness poll compares the queue signature (item_key + row_key + total).
    assert "function snapshotSignature(snapshot)" in js
    assert "snapshot.queue" in js
    assert "hasInlineWorkInProgress()" in js
    # Archive module fetches the new endpoint and supports filters + paging.
    assert "/api/exceptions/archive" in js
    assert "include_auto=true" in js
    assert "actor=" in js
    assert "before=" in js
    assert "[data-archive-toggle]" in js
    assert "[data-archive-groups]" in js
    assert "[data-archive-more]" in js
    assert "[data-archive-hide-auto]" in js
    assert "auto-resolved" in js


def test_inbox_template_has_inline_time_off_deny_reason():
    html = (STATIC_DIR.parent / "templates" / "exceptions.html").read_text(encoding="utf-8")

    assert "js-time-off-approve" in html
    assert "js-time-off-reason" in html
    assert "js-time-off-refuse" in html
    assert 'aria-label="Person to assign"' in html
    assert 'aria-label="Late or absence reason"' in html
    assert 'aria-label="Work center to assign"' in html
    assert 'aria-label="Reason to deny time off"' in html


def test_inbox_js_requires_time_off_deny_reason_and_sends_source():
    js = (STATIC_DIR / "exceptions.js").read_text(encoding="utf-8")

    assert "js-time-off-reason" in js
    assert "source: 'inbox'" in js
    assert "Enter a reason, then Deny again." in js
    assert "A reason is required to deny." in js
    assert "event.key !== 'Enter'" in js
    assert ".js-time-off-refuse" in js
    assert "function submitRowInput" in js
    assert ".js-reason-input" in js
    assert ".js-absent, .js-save-late" in js
    assert ".js-punch-time" in js
    assert ".js-punch-save" in js
    assert "btn.click()" in js


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
    assert "link.classList.toggle('has-open', total > 0)" in js
    assert "link.classList.toggle('is-degraded', degraded && total <= 0)" in js
    assert ".inbox-nav-count" in css
    assert ".inbox-nav-link.has-open" in css
    assert ".brand-row nav a.inbox-nav-link.has-open" in css
    assert ".inbox-nav-link.has-open .inbox-nav-count" in css
    assert ".inbox-nav-link.is-degraded .inbox-nav-count" in css


def test_time_off_approve_endpoint_updates_to_odoo_state(monkeypatch):
    from zira_dashboard import odoo_client

    monkeypatch.setattr(
        exceptions_route.plant_day,
        "now",
        lambda: datetime(2026, 6, 24, 14, 5, tzinfo=timezone.utc),
    )
    row = {
        "id": 55,
        "person_odoo_id": 7,
        "person_name": "Maria Delgado",
        "leave_type": "PTO",
        "date_from": date(2026, 6, 22),
        "date_to": date(2026, 6, 22),
        "hour_from": 8.5,
        "hour_to": 12.25,
        "state": "confirm",
        "odoo_leave_id": 99,
    }
    updates = []
    audits = []
    monkeypatch.setattr(exceptions_route, "_load_time_off_request", lambda request_id: row)
    monkeypatch.setattr(odoo_client, "approve_leave", lambda leave_id: "validate")
    monkeypatch.setattr(
        exceptions_route,
        "_set_time_off_state",
        lambda old, state: updates.append((old["id"], state)),
    )
    monkeypatch.setattr(
        exceptions_route.time_off_audit,
        "record_decision",
        lambda **kw: audits.append(kw),
    )

    resp = exceptions_route._approve_time_off_sync(
        55,
        actor_upn="dale@gruberpallets.com",
        actor_name="Dale Gruber",
        source="page",
    )

    assert resp.status_code == 200
    assert resp.body
    assert updates == [(55, "validate")]
    assert len(audits) == 1
    assert audits[0]["action"] == "approve"
    assert audits[0]["result_state"] == "validate"
    assert audits[0]["actor_upn"] == "dale@gruberpallets.com"
    assert audits[0]["person_name"] == "Maria Delgado"
    assert audits[0]["hour_from"] == 8.5
    assert audits[0]["hour_to"] == 12.25
    payload = json.loads(resp.body)
    assert payload["decision"] == {
        "action": "approve",
        "person_name": "Maria Delgado",
        "leave_type": "PTO",
        "date_from": "2026-06-22",
        "date_to": "2026-06-22",
        "hour_from": 8.5,
        "hour_to": 12.25,
        "date_label": "2026-06-22 - 8:30 AM to 12:15 PM",
        "reason": None,
        "actor_name": "Dale Gruber",
        "actor_upn": "dale@gruberpallets.com",
        "source": "page",
        "result_state": "validate",
        "decided_at": "2026-06-24T14:05:00+00:00",
        "decided_label": "6/24 9:05 AM",
    }


def test_time_off_refuse_requires_reason():
    resp = exceptions_route._refuse_time_off_sync(56, reason="")

    assert resp.status_code == 400


def test_time_off_refuse_unsynced_draft_stays_local(monkeypatch):
    from zira_dashboard import odoo_client

    monkeypatch.setattr(
        exceptions_route.plant_day,
        "now",
        lambda: datetime(2026, 6, 24, 14, 10, tzinfo=timezone.utc),
    )
    row = {
        "id": 56,
        "person_odoo_id": 8,
        "person_name": "Carlos Ortega",
        "leave_type": "Unpaid",
        "date_from": date(2026, 6, 22),
        "date_to": date(2026, 6, 22),
        "state": "draft",
        "odoo_leave_id": None,
    }
    updates = []
    refused = []
    posted = []
    audits = []
    monkeypatch.setattr(exceptions_route, "_load_time_off_request", lambda request_id: row)
    monkeypatch.setattr(odoo_client, "refuse_leave", lambda leave_id: refused.append(leave_id))
    monkeypatch.setattr(
        odoo_client,
        "post_leave_message",
        lambda leave_id, body: posted.append((leave_id, body)),
    )
    monkeypatch.setattr(
        exceptions_route,
        "_set_time_off_state",
        lambda old, state: updates.append((old["id"], state)),
    )
    monkeypatch.setattr(
        exceptions_route.time_off_audit,
        "record_decision",
        lambda **kw: audits.append(kw),
    )

    resp = exceptions_route._refuse_time_off_sync(
        56,
        reason="No coverage",
        actor_upn="dale@gruberpallets.com",
        actor_name="Dale Gruber",
        source="inbox",
    )

    assert resp.status_code == 200
    assert refused == []
    assert posted == []
    assert updates == [(56, "refuse")]
    assert audits[0]["action"] == "deny"
    assert audits[0]["reason"] == "No coverage"
    assert audits[0]["source"] == "inbox"
    payload = json.loads(resp.body)
    assert payload["decision"]["action"] == "deny"
    assert payload["decision"]["reason"] == "No coverage"
    assert payload["decision"]["source"] == "inbox"
    assert payload["decision"]["person_name"] == "Carlos Ortega"
    assert payload["decision"]["decided_label"] == "6/24 9:10 AM"


def test_time_off_refuse_synced_posts_reason_to_odoo(monkeypatch):
    from zira_dashboard import odoo_client

    row = {
        "id": 57,
        "person_odoo_id": 9,
        "person_name": "Luis Vega",
        "leave_type": "PTO",
        "date_from": date(2026, 6, 25),
        "date_to": date(2026, 6, 25),
        "state": "confirm",
        "odoo_leave_id": 99,
    }
    posted = []
    monkeypatch.setattr(exceptions_route, "_load_time_off_request", lambda request_id: row)
    monkeypatch.setattr(odoo_client, "refuse_leave", lambda leave_id: None)
    monkeypatch.setattr(
        odoo_client,
        "post_leave_message",
        lambda leave_id, body: posted.append((leave_id, body)),
    )
    monkeypatch.setattr(exceptions_route, "_set_time_off_state", lambda old, state: None)
    monkeypatch.setattr(exceptions_route.time_off_audit, "record_decision", lambda **kw: None)

    resp = exceptions_route._refuse_time_off_sync(57, reason="Coverage too thin")

    assert resp.status_code == 200
    assert posted == [(99, "Coverage too thin")]


def test_time_off_refuse_survives_chatter_post_failure(monkeypatch):
    from zira_dashboard import odoo_client

    row = {
        "id": 58,
        "person_odoo_id": 9,
        "person_name": "Luis Vega",
        "leave_type": "PTO",
        "date_from": date(2026, 6, 25),
        "date_to": date(2026, 6, 25),
        "state": "confirm",
        "odoo_leave_id": 99,
    }
    updates = []
    audits = []

    def boom(leave_id, body):
        raise RuntimeError("odoo down")

    monkeypatch.setattr(exceptions_route, "_load_time_off_request", lambda request_id: row)
    monkeypatch.setattr(odoo_client, "refuse_leave", lambda leave_id: None)
    monkeypatch.setattr(odoo_client, "post_leave_message", boom)
    monkeypatch.setattr(
        exceptions_route,
        "_set_time_off_state",
        lambda old, state: updates.append(state),
    )
    monkeypatch.setattr(
        exceptions_route.time_off_audit,
        "record_decision",
        lambda **kw: audits.append(kw),
    )

    resp = exceptions_route._refuse_time_off_sync(58, reason="No coverage")

    assert resp.status_code == 200
    assert updates == ["refuse"]
    assert len(audits) == 1


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


def test_exceptions_js_has_undo_affordance():
    import pathlib
    js = pathlib.Path("src/zira_dashboard/static/exceptions.js").read_text()
    assert "/api/exceptions/undo/" in js
    assert "data-undo" in js  # the Undo control rendered in the row status
