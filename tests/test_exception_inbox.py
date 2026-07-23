import json
from datetime import date, datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient

from zira_dashboard import (
    db, exception_inbox, machine_breakdown, missing_wc, missed_punch_out,
    saturday_recruiting_store, staffing, unexpected_worker,
)
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
        "scheduled_late": [
            {
                "emp_id": 1,
                "name": "Ana",
                "minutes_late": 12,
                "scheduled_wc": "Baler",
                "scheduled_start_time": "06:00",
            }
        ],
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
    monkeypatch.setattr(machine_breakdown, "current_rows", lambda: [])
    monkeypatch.setattr(unexpected_worker, "open_events", lambda _day: [])
    monkeypatch.setattr(exception_inbox, "_work_center_names", lambda: ["Repair 1"])
    monkeypatch.setattr(exception_inbox, "_saturday_staffing_actions", lambda _today: (0, []))
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
        "saturday_recruiting": 0,
        "late": 2,
        "missing_wc": 1,
        "missed_punch_out": 1,
        "unexpected_workers": 0,
        "breakdown": 0,
        "time_off": 1,
    }
    sections = {s["id"]: s for s in snap["sections"]}
    assert sections["assignments"]["context"]["people"] == ["Ana", "Ben"]
    assert sections["assignments"]["rows"][0]["priority"] == "warn"
    assert sections["assignments"]["rows"][0]["badge"] == "Credit"
    assert sections["assignments"]["rows"][0]["row_key"].startswith("assignment:Repair 1:")
    assert sections["assignments"]["rows"][0]["action"]["start_utc"].startswith("2026")
    assert sections["late"]["rows"][0]["action"]["emp_id"] == 1
    assert sections["late"]["rows"][0]["action"]["scheduled_wc"] == "Baler"
    assert sections["late"]["rows"][0]["action"]["scheduled_start_time"] == "06:00"
    assert sections["late"]["rows"][0]["row_key"] == "late:scheduled:1"
    assert sections["late"]["rows"][-1]["label"] == "Snoozed"
    assert sections["late"]["rows"][-1]["priority"] == "muted"
    assert sections["late"]["rows"][-1]["row_key"] == "late_snoozed:3"
    assert sections["missing_wc"]["context"]["work_centers"] == ["Repair 1"]
    assert sections["missing_wc"]["rows"][0]["action"]["attendance_id"] == 10
    assert sections["missed_punch_out"]["rows"][0]["action"]["attendance_id"] == 11
    assert sections["time_off"]["rows"][0]["action"]["request_id"] == 20


def test_build_snapshot_maps_running_late_to_muted_follow_up(monkeypatch):
    monkeypatch.setattr(exception_inbox.plant_day, "today", lambda: date(2026, 7, 13))
    # The publish-tomorrow reminder fires from the REAL clock after its
    # afternoon cutoff and would pollute the totals when the suite runs
    # late in the day; it has its own dedicated tests below.
    monkeypatch.setattr(
        exception_inbox, "_plant_schedule_reminder", lambda: (0, [])
    )
    monkeypatch.setattr(staffing_routes, "assignments_todo_payload", lambda: {"count": 0})
    monkeypatch.setattr(staffing_routes, "late_report_payload", lambda: {
        "count": 0, "scheduled_late": [], "unscheduled_late": [],
        "needs_reason": [], "snoozed": [],
        "running_late": [{
            "emp_id": "7", "name": "Jesus Galindo",
            "until_iso": "2026-07-13T14:15:00+00:00", "expected_label": "9:15 AM",
        }],
    })
    monkeypatch.setattr(missing_wc, "current_rows", lambda: [])
    monkeypatch.setattr(missed_punch_out, "current_rows", lambda: [])
    monkeypatch.setattr(machine_breakdown, "current_rows", lambda: [])
    monkeypatch.setattr(exception_inbox, "_work_center_names", lambda: [])
    monkeypatch.setattr(exception_inbox, "_pending_time_off", lambda day: (0, []))

    snap = exception_inbox.build_snapshot()
    late = next(section for section in snap["sections"] if section["id"] == "late")

    assert late["rows"][-1]["label"] == "Running Late"
    assert late["rows"][-1]["detail"] == "Expected by 9:15 AM"
    assert late["rows"][-1]["priority"] == "muted"
    assert late["rows"][-1]["action"] is None
    assert snap["total"] == 0
    assert snap["urgent_total"] == 0
    assert snap["follow_up_total"] == 1


def test_unexpected_workers_are_urgent_and_clear_after_published_placement(monkeypatch):
    """The inbox delegates lifecycle clearing to unexpected_worker.open_events."""
    day = date(2026, 7, 17)
    monkeypatch.setattr(exception_inbox.plant_day, "today", lambda: day)
    monkeypatch.setattr(staffing_routes, "assignments_todo_payload", lambda: {"count": 0})
    monkeypatch.setattr(staffing_routes, "late_report_payload", lambda: {"count": 0})
    monkeypatch.setattr(missing_wc, "current_rows", lambda: [])
    monkeypatch.setattr(missed_punch_out, "current_rows", lambda: [])
    monkeypatch.setattr(machine_breakdown, "current_rows", lambda: [])
    monkeypatch.setattr(exception_inbox, "_pending_time_off", lambda _day: (0, []))
    monkeypatch.setattr(exception_inbox, "_work_center_names", lambda: [])
    monkeypatch.setattr(
        unexpected_worker,
        "open_events",
        lambda _day: [{
            "id": 9,
            "day": day,
            "person_odoo_id": 7,
            "person_name": "Maria Delgado",
            "clock_in_wc": "Repair 1",
        }],
    )
    monkeypatch.setattr(
        staffing,
        "load_schedule",
        lambda _day: staffing.Schedule(
            day=day,
            assignments={"Repair 1": ["Ana"]},
            auto_enabled_work_centers=["Repair 1", "Dismantler 1"],
        ),
    )
    monkeypatch.setattr(
        staffing_routes,
        "_effective_minimum",
        lambda loc: 2 if loc.name == "Repair 1" else 1,
    )

    snap = exception_inbox.build_snapshot()
    section = next(s for s in snap["sections"] if s["id"] == "unexpected_workers")
    row = section["rows"][0]

    assert section["tone"] == "bad"
    assert row["name"] == "Maria Delgado"
    assert row["priority"] == "urgent"
    assert row["href"] == "/staffing?day=2026-07-17"
    assert row["item_key"] == "unexpected_worker:2026-07-17:7"
    assert row["detail"] == (
        "Clocked in at Repair 1 · Staffing below minimum: "
        "Repair 1 (1/2), Dismantler 1 (0/1)."
    )

    # A published assignment is cleared inside open_events, so a later inbox
    # refresh sees no row rather than trying to resolve it here.
    monkeypatch.setattr(unexpected_worker, "open_events", lambda _day: [])
    cleared = exception_inbox.build_snapshot()
    cleared_section = next(s for s in cleared["sections"] if s["id"] == "unexpected_workers")
    assert cleared_section["count"] == 0


def test_unexpected_worker_routes_to_staffing_when_enabled_centers_are_covered(monkeypatch):
    day = date(2026, 7, 17)
    monkeypatch.setattr(exception_inbox.plant_day, "today", lambda: day)
    monkeypatch.setattr(staffing_routes, "assignments_todo_payload", lambda: {"count": 0})
    monkeypatch.setattr(staffing_routes, "late_report_payload", lambda: {"count": 0})
    monkeypatch.setattr(missing_wc, "current_rows", lambda: [])
    monkeypatch.setattr(missed_punch_out, "current_rows", lambda: [])
    monkeypatch.setattr(machine_breakdown, "current_rows", lambda: [])
    monkeypatch.setattr(exception_inbox, "_pending_time_off", lambda _day: (0, []))
    monkeypatch.setattr(exception_inbox, "_work_center_names", lambda: [])
    monkeypatch.setattr(unexpected_worker, "open_events", lambda _day: [{
        "day": day, "person_odoo_id": 7, "person_name": "Maria Delgado", "clock_in_wc": "Repair 1",
    }])
    monkeypatch.setattr(
        staffing,
        "load_schedule",
        lambda _day: staffing.Schedule(
            day=day,
            assignments={"Repair 1": ["Ana"]},
            auto_enabled_work_centers=["Repair 1"],
        ),
    )
    monkeypatch.setattr(staffing_routes, "_effective_minimum", lambda _loc: 1)

    snap = exception_inbox.build_snapshot()
    row = next(s for s in snap["sections"] if s["id"] == "unexpected_workers")["rows"][0]

    assert row["href"] == "/staffing?day=2026-07-17"
    assert row["detail"] == "Clocked in at Repair 1 · No enabled work centers are below minimum."


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
        "running_late": [{"emp_id": 5}],
    })
    monkeypatch.setattr(missing_wc, "current_rows", lambda: [{"attendance_id": 10}])
    monkeypatch.setattr(missed_punch_out, "current_rows", lambda: [{"attendance_id": 11}])
    monkeypatch.setattr(machine_breakdown, "current_rows", lambda: [])
    monkeypatch.setattr(unexpected_worker, "open_events", lambda _day: [])
    monkeypatch.setattr(exception_inbox, "_pending_time_off_counts", lambda today: (4, 2))
    monkeypatch.setattr(exception_inbox, "_saturday_staffing_actions", lambda _today: (0, []))

    summary = exception_inbox.build_summary()

    assert summary["today"] == "2026-06-19"
    assert summary["generated_at"] == "8:10 AM"
    assert summary["total"] == 11
    assert summary["urgent_total"] == 6
    assert summary["follow_up_total"] == 2
    assert summary["source_errors"] == []
    assert summary["sections"] == {
        "assignments": 2,
        "plant_schedule": 0,
        "saturday_recruiting": 0,
        "late": 3,
        "missing_wc": 1,
        "missed_punch_out": 1,
        "unexpected_workers": 0,
        "breakdown": 0,
        "time_off": 4,
    }


def test_pending_time_off_counts_include_past_due_urgent_count(monkeypatch):
    captured = {}

    def fake_query(sql, params):
        captured["sql"] = sql
        captured["params"] = params
        return [{"n": 3, "past_due_n": 2}]

    monkeypatch.setattr(db, "query", fake_query)

    counts = exception_inbox._pending_time_off_counts(date(2026, 6, 19))

    assert counts == (3, 2)
    assert "COUNT(*) AS n" in captured["sql"]
    assert "date_to < %s" in captured["sql"]
    assert captured["params"] == (date(2026, 6, 19),)


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
    monkeypatch.setattr(exception_inbox.time_off_context,
                        "coverage_breakdowns_for", lambda rows: {})

    count, rows = exception_inbox._pending_time_off(date(2026, 6, 19), limit=8)

    assert captured["calls"] == 1
    assert "COUNT(*) OVER () AS total_count" in captured["sql"]
    assert captured["params"] == (8,)
    assert count == 4
    assert rows[0]["name"] == "Eli"
    assert rows[0]["row_key"] == "time_off:20:confirm"


def test_pending_time_off_includes_and_flags_past_due_rows(monkeypatch):
    captured = {}

    def fake_query(sql, params):
        captured["sql"] = sql
        captured["params"] = params
        return [{
            "id": 20,
            "person_odoo_id": 7,
            "odoo_leave_id": 99,
            "name": "Eli",
            "shape": "full",
            "state": "confirm",
            "date_from": date(2026, 6, 17),
            "date_to": date(2026, 6, 17),
            "hour_from": None,
            "hour_to": None,
            "sync_error": None,
            "leave_type": "Vacation",
            "total_count": 1,
        }]

    monkeypatch.setattr(db, "query", fake_query)
    monkeypatch.setattr(
        exception_inbox.time_off_context,
        "coverage_breakdowns_for",
        lambda rows: {},
    )

    _count, rows = exception_inbox._pending_time_off(date(2026, 6, 19), limit=8)

    assert "date_to >= %s" not in captured["sql"]
    assert captured["params"] == (8,)
    assert rows[0]["past_due"] is True
    assert rows[0]["priority"] == "urgent"
    assert rows[0]["badge"] == "Past due"


def test_pending_time_off_attaches_coverage(monkeypatch):
    monkeypatch.setattr(db, "query", lambda sql, params: [{
        "id": 20, "person_odoo_id": 7, "odoo_leave_id": 99, "name": "Eli",
        "shape": "full_day", "state": "confirm",
        "date_from": date(2026, 6, 20), "date_to": date(2026, 6, 20),
        "hour_from": None, "hour_to": None, "sync_error": None,
        "leave_type": "Vacation", "total_count": 1,
    }])
    monkeypatch.setattr(
        exception_inbox.time_off_context, "coverage_breakdowns_for",
        lambda rows: {20: {"severity": "warn", "peak_count": 3}})

    _count, rows = exception_inbox._pending_time_off(date(2026, 6, 19), limit=8)

    assert rows[0]["coverage"] == {"severity": "warn", "peak_count": 3}


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
    monkeypatch.setattr(machine_breakdown, "current_rows", lambda: [])
    monkeypatch.setattr(unexpected_worker, "open_events", lambda _day: [])
    monkeypatch.setattr(exception_inbox, "_pending_time_off", lambda today: (0, []))
    monkeypatch.setattr(exception_inbox, "_work_center_names", lambda: [])
    monkeypatch.setattr(exception_inbox, "_saturday_staffing_actions", lambda _today: (0, []))

    snap = exception_inbox.build_snapshot()

    assert snap["total"] == 0
    assert snap["source_errors"] == [{"source": "Assignments To Do"}]
    assert [s["id"] for s in snap["sections"]] == [
        "assignments",
        "plant_schedule",
        "saturday_recruiting",
        "late",
        "missing_wc",
        "missed_punch_out",
        "unexpected_workers",
        "breakdown",
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
    monkeypatch.setattr(exception_inbox, "_pending_time_off_counts", lambda today: (0, 0))
    monkeypatch.setattr(exception_inbox, "_work_center_names", lambda: [])
    monkeypatch.setattr(exception_inbox, "_saturday_staffing_actions", lambda _today: (0, []))


def test_closed_saturday_recruiting_is_an_inbox_action(monkeypatch):
    saturday = date(2026, 7, 25)
    bundle = saturday_recruiting_store.RecruitmentBundle(
        recruitment=saturday_recruiting_store.Recruitment(
            saturday, "closed", datetime.min.time(), datetime.max.time(), datetime.now(timezone.utc),
        ),
        openings=(),
        commitments=(
            saturday_recruiting_store.StoredCommitment(
                1, 10, "Ana", "committed", datetime.min.time(), datetime.max.time(), frozenset(),
            ),
        ),
    )
    monkeypatch.setattr(
        exception_inbox.saturday_recruiting_store,
        "list_closed_unprepared",
        lambda start_day: (bundle,),
    )

    count, rows = exception_inbox._saturday_staffing_actions(date(2026, 7, 20))

    assert count == 1
    assert rows == [{
        "name": "Saturday recruitment",
        "label": "Saturday, Jul 25",
        "detail": "1 committed · Ready to schedule",
        "priority": "warn",
        "badge": "Schedule",
        "href": "/staffing?day=2026-07-25",
        "row_key": "saturday_recruitment:2026-07-25",
        "item_key": "saturday_recruitment:2026-07-25",
    }]


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


def test_exceptions_api_serializes_date_bearing_coverage(monkeypatch):
    # Reproduces the regression where date objects in time-off rows / coverage
    # broke JSONResponse on /api/exceptions.
    time_off_row = {
        "name": "Maria", "label": "Jul 6 – Jul 8", "detail": "Vacation · confirm",
        "priority": "info", "badge": "Approval",
        "row_key": "time_off:55:confirm", "item_key": "time_off:55",
        "action": {"type": "time_off", "request_id": 55},
        "person_odoo_id": 7,
        "date_from": date(2026, 7, 6), "date_to": date(2026, 7, 8),
        "coverage": {
            "severity": "warn", "peak_count": 4, "peak_date": date(2026, 7, 7),
            "peak_dept_count": 2, "scope": "department", "dept_label": "Recycling",
            "has_holiday": False, "more_days": 0,
            "by_day": [{"date": date(2026, 7, 7), "count": 4, "dept_count": 2,
                        "holiday": None, "people": [
                            {"name": "Juan", "dept": "Recycling", "label": "full day",
                             "pending": False, "same_dept": True}]}],
        },
    }
    snapshot = {
        "today": "2026-07-01", "generated_at": "7:35 AM", "total": 1,
        "urgent_total": 0, "follow_up_total": 0, "source_errors": [],
        "work_centers": [], "people": [], "sections": [],
        "queue": [{**time_off_row, "section_id": "time_off",
                   "category_label": "Pending Time Off", "tone": "info"}],
    }
    monkeypatch.setattr(exceptions_route.exception_inbox, "build_snapshot",
                        lambda: snapshot)
    client = TestClient(app)

    resp = client.get("/api/exceptions")

    assert resp.status_code == 200
    body = resp.json()
    row = body["queue"][0]
    assert row["coverage"]["peak_date"] == "2026-07-07"        # date -> ISO string
    assert row["coverage"]["by_day"][0]["date"] == "2026-07-07"
    assert row["date_from"] == "2026-07-06"


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
    # The nav Inbox-count bootstrap is now rendered by _topnav.html via
    # nav_inbox_summary() -> build_summary(), not by the route. Stub that and
    # assert the page carries it exactly once (no duplicate from exceptions.html).
    monkeypatch.setattr(
        exceptions_route.exception_inbox,
        "build_summary",
        lambda: {"total": 1, "urgent_total": 0, "source_errors": []},
    )
    client = TestClient(app)

    resp = client.get("/exceptions")

    assert resp.status_code == 200
    assert resp.text.count('id="gpi-inbox-summary-bootstrap"') == 1
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


def test_exceptions_page_renders_forgot_punch_in_controls(monkeypatch):
    late_row = {
        "name": "Lauro Benitez",
        "label": "Scheduled late",
        "detail": "133 mins late",
        "priority": "urgent",
        "badge": "Needs decision",
        "row_key": "late:scheduled:5",
        "item_key": "late:5:2026-06-19",
        "action": {
            "type": "late_absence",
            "emp_id": 5,
            "name": "Lauro Benitez",
            "scheduled_wc": "Tablets",
            "scheduled_start_time": "06:00",
        },
    }
    snapshot = {
        "today": "2026-06-19",
        "generated_at": "8:13 AM",
        "total": 1,
        "urgent_total": 1,
        "follow_up_total": 0,
        "source_errors": [],
        "work_centers": ["Trim Saw", "Tablets", "Repair 1"],
        "people": [],
        "sections": [],
        "queue": [
            {**late_row, "section_id": "late", "category_label": "Late / Absence", "tone": "bad"},
        ],
    }
    monkeypatch.setattr(exceptions_route.exception_inbox, "build_snapshot", lambda: snapshot)
    client = TestClient(app)

    resp = client.get("/exceptions")

    assert resp.status_code == 200
    assert '<option value="__forgot_punch_in__">Forgot punch in</option>' not in resp.text
    assert 'class="row-btn primary js-forgot-punch-open"' in resp.text
    assert ">Missed Punch</button>" in resp.text
    assert 'class="row-btn warn js-absent"' in resp.text
    assert 'class="inline-input time js-forgot-punch-time"' in resp.text
    assert 'value="06:00"' in resp.text
    assert 'class="inline-select js-forgot-wc"' in resp.text
    assert '<option value="Trim Saw">Trim Saw</option>' in resp.text
    assert '<option value="Tablets" selected>Tablets</option>' in resp.text
    assert 'class="row-btn primary js-forgot-punch-save"' in resp.text
    assert ">Clock in</button>" in resp.text


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


def test_missed_punch_clients_show_odoo_reconciliation_message():
    exceptions_js = (STATIC_DIR / "exceptions.js").read_text()
    footer_js = (STATIC_DIR / "footer.js").read_text()

    assert "resolveRow(row, (resp && resp.message) || 'Corrected')" in exceptions_js
    assert "finishMpoRow(li, (res && res.message) || 'Corrected ✓', true, api)" in footer_js


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
    assert 'aria-label="Forgotten punch-in time"' in html
    assert 'aria-label="Forgotten punch-in work center"' in html
    assert "js-forgot-punch-open" in html
    assert ">Missed Punch</button>" in html
    assert "row-btn warn js-absent" in html


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
    assert "/api/late-report/forgot-punch-in" in js
    assert ".js-forgot-punch-time" in js
    assert ".js-forgot-punch-open" in js
    assert ".js-forgot-punch-save" in js
    assert "Missed punch: enter time and work center." in js
    assert "btn.click()" in js


def test_inbox_css_styles_absent_button_orange():
    css = (STATIC_DIR / "exceptions.css").read_text(encoding="utf-8")

    assert ".row-btn.warn" in css
    assert "background: #f97316" in css


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


def test_exceptions_page_renders_coverage_chip(monkeypatch):
    time_off_row = {
        "name": "Maria", "label": "Jul 6 – Jul 8", "detail": "Vacation · confirm",
        "priority": "info", "badge": "Approval",
        "row_key": "time_off:55:confirm", "item_key": "time_off:55",
        "action": {"type": "time_off", "request_id": 55},
        "coverage": {
            "severity": "warn", "peak_count": 4, "peak_date": date(2026, 7, 7),
            "peak_dept_count": 2, "scope": "department", "dept_label": "Recycling",
            "has_holiday": False, "more_days": 0,
            "by_day": [{
                "date": date(2026, 7, 7), "count": 4, "dept_count": 2,
                "holiday": None,
                "people": [
                    {"name": "Juan", "dept": "Recycling", "label": "full day",
                     "pending": False, "same_dept": True},
                    {"name": "Lee", "dept": None, "label": "arrives 9:00am",
                     "pending": True, "same_dept": False},
                ],
            }],
        },
    }
    snapshot = {
        "today": "2026-07-01", "generated_at": "7:35 AM", "total": 1,
        "urgent_total": 0, "follow_up_total": 0, "source_errors": [],
        "work_centers": [], "people": [], "sections": [],
        "queue": [{**time_off_row, "section_id": "time_off",
                   "category_label": "Pending Time Off", "tone": "info"}],
    }
    monkeypatch.setattr(exceptions_route.exception_inbox, "build_snapshot",
                        lambda: snapshot)
    client = TestClient(app)

    resp = client.get("/exceptions")

    assert resp.status_code == 200
    assert 'class="cov cov-warn"' in resp.text
    assert "4 off peak" in resp.text
    assert "2 in Recycling" in resp.text
    assert 'class="cov-tip"' in resp.text
    assert "Juan" in resp.text and "arrives 9:00am" in resp.text
    assert "pending" in resp.text


def test_exceptions_js_toggles_coverage_tooltip_on_tap():
    js = (STATIC_DIR / "exceptions.js").read_text(encoding="utf-8")

    assert "closest('[data-cov]')" in js
    assert "classList.toggle('cov-open')" in js
    # tapping outside closes any open coverage tooltip
    assert "cov-open" in js


def test_exceptions_css_has_coverage_chip_styles():
    css = (STATIC_DIR / "exceptions.css").read_text(encoding="utf-8")

    assert ".cov-wrap" in css
    assert ".cov-warn" in css
    assert ".cov-ok" in css
    assert ".cov-clear" in css
    assert ".cov-hol" in css
    assert ".cov-tip" in css
    # tooltip shows on hover and when tapped open
    assert ".cov-wrap:hover .cov-tip" in css
    assert ".cov-wrap.cov-open .cov-tip" in css
    assert ".cov-same" in css


def test_exceptions_js_labels_locally_recorded_approvals():
    js = (STATIC_DIR / "exceptions.js").read_text(encoding="utf-8")

    # Local-record fallback (Odoo work-schedule rejection): the resolved row
    # must be labeled as recorded-here rather than a plain "Approved".
    assert "resp.recorded_locally" in js
    assert "recorded here" in js
