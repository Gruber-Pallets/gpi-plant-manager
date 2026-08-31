from __future__ import annotations

import re

from fastapi.testclient import TestClient

from zira_dashboard.app import app
from zira_dashboard.routes import exceptions


ITEM_KEY = "production_unassigned_run:Dismantler 1:2026-08-28T16:55:00+00:00"


def _row(kind, **changes):
    row = {
        "section_id": kind,
        "kind": kind,
        "tone": "bad",
        "priority": "urgent",
        "badge": "Needs decision",
        "category_label": "Production Without a Worker",
        "row_key": kind + ":revision",
        "item_key": ITEM_KEY,
        "name": "Dismantler 1",
        "label": "12 units",
        "detail": "Dismantler 1 · 2026-08-28T16:55:00+00:00 to 2026-08-28T17:10:00+00:00 · 3 samples",
        "action": None,
        "app_work_center_name": "Dismantler 1",
        "start_utc": "2026-08-28T16:55:00+00:00",
        "end_utc": "2026-08-28T17:10:00+00:00",
        "end_is_open": False,
        "units": 12.0,
        "sample_count": 3,
        "comparison_only": False,
        "raw_work_center_labels": [],
        "odoo_work_center_ids": [],
    }
    row.update(changes)
    return row


def _snapshot():
    run = _row(
        "production_unassigned_run",
        affected_workers=[
            {"employee_odoo_id": 44, "employee_name": "Maria Worker"},
            {"employee_odoo_id": 57, "employee_name": "José Worker"},
        ],
        reason="positive_production_has_no_valid_odoo_worker",
    )
    unmapped = _row(
        "attendance_unmapped_location",
        section_id="attendance_unmapped_location",
        category_label="Unknown Odoo Work Center",
        item_key="attendance_unmapped_location:44:501:2026-08-28T16:40:00+00:00",
        name="Maria Worker",
        label="ODoo Only & Special/Center",
        detail="2026-08-28T16:40:00+00:00 to open",
        app_work_center_name=None,
        start_utc="2026-08-28T16:40:00+00:00",
        end_utc=None,
        end_is_open=True,
        units=None,
        sample_count=None,
        raw_work_center_labels=["ODoo Only & Special/Center"],
        odoo_work_center_ids=[781],
    )
    return {
        "today": "2026-08-28",
        "generated_at": "12:15 PM",
        "total": 2,
        "urgent_total": 2,
        "follow_up_total": 0,
        "source_errors": [],
        "work_centers": ["Dismantler 1", "Repair 1"],
        "people": [],
        "sections": [],
        "queue": [run, unmapped],
    }


def test_attendance_cards_show_source_facts_and_actions(monkeypatch):
    monkeypatch.setattr(exceptions.exception_inbox, "build_snapshot", _snapshot)
    monkeypatch.setattr(
        exceptions,
        "_active_correction_people",
        lambda: [
            {"employee_odoo_id": 44, "name": "Maria Worker"},
            {"employee_odoo_id": 57, "name": "José Worker"},
        ],
    )
    monkeypatch.setattr(exceptions.auth, "request_is_super_admin", lambda request: True)

    response = TestClient(app).get("/exceptions")

    assert response.status_code == 200
    html = response.text
    assert "Odoo status: Unassigned production" in html
    assert "Production run: Dismantler 1" in html
    assert "12 units" in html
    assert "3 source readings" in html
    assert 'datetime="2026-08-28T16:55:00+00:00"' in html
    assert ">8/28/2026 11:55 AM CDT</time>" in html
    assert ">8/28/2026 12:10 PM CDT</time>" in html
    assert "Nearby attendance:" in html
    assert "Maria Worker, José Worker" in html
    assert "Reason: Positive production has no valid Odoo worker" in html
    assert "Choose workers and times" in html
    assert "data-attendance-correction-open" in html
    assert 'data-correction-item-key="' + ITEM_KEY + '"' in html
    assert "Odoo status: Unknown work center" in html
    assert "Odoo work center: ODoo Only &amp; Special/Center" in html
    assert ">8/28/2026 11:40 AM CDT</time>" in html
    assert re.search(r"11:40 AM CDT</time>\s+to\s+Still working", html)
    assert "Map this Odoo work center" in html
    assert (
        "/settings?section=work_centers&amp;odoo_work_center_id=781"
        "&amp;odoo_work_center_name=ODoo+Only+%26+Special%2FCenter"
    ) in html


def test_attendance_action_requires_literal_false_comparison_flag(monkeypatch):
    for malformed in (None, 0, "false"):
        snapshot = _snapshot()
        snapshot["queue"] = [_row("production_unassigned_run", comparison_only=malformed)]
        monkeypatch.setattr(exceptions.exception_inbox, "build_snapshot", lambda: snapshot)
        monkeypatch.setattr(exceptions, "_active_correction_people", lambda: [])

        html = TestClient(app).get("/exceptions").text

        assert "data-attendance-correction-open" not in html

    snapshot = _snapshot()
    snapshot["queue"] = [_row("production_unassigned_run")]
    snapshot["queue"][0].pop("comparison_only")
    monkeypatch.setattr(exceptions.exception_inbox, "build_snapshot", lambda: snapshot)
    assert "data-attendance-correction-open" not in TestClient(app).get("/exceptions").text


def test_correction_dialog_has_accessible_people_time_target_and_confirmation_controls(
    monkeypatch,
):
    monkeypatch.setattr(exceptions.exception_inbox, "build_snapshot", _snapshot)
    monkeypatch.setattr(
        exceptions,
        "_active_correction_people",
        lambda: [
            {"employee_odoo_id": 44, "name": "Maria Worker"},
            {"employee_odoo_id": 57, "name": "José Worker"},
            {"employee_odoo_id": 0, "name": "Invalid"},
        ],
    )
    monkeypatch.setattr(exceptions.auth, "request_is_super_admin", lambda request: False)

    html = TestClient(app).get("/exceptions").text

    assert '<dialog id="attendance-correction-dialog"' in html
    assert 'aria-labelledby="attendance-correction-title"' in html
    assert 'data-plant-timezone="America/Chicago"' in html
    assert 'name="employee_odoo_ids" value="44"' in html
    assert 'name="employee_odoo_ids" value="57"' in html
    assert 'name="employee_odoo_ids" value="0"' not in html
    assert "data-attendance-start" in html
    assert "data-attendance-end" in html
    assert "<legend>Which start time?</legend>" in html
    assert "data-attendance-start-occurrence" in html
    assert "<legend>Which end time?</legend>" in html
    assert "data-attendance-end-occurrence" in html
    assert "data-attendance-open-ended" in html
    assert "Still working" in html
    assert "data-attendance-work-center" in html
    assert 'value="Dismantler 1"' in html
    assert 'value="Repair 1"' in html
    assert "data-attendance-preview" in html
    assert "data-attendance-apply" in html
    assert "data-attendance-apply" in html and "disabled" in html
    assert "data-attendance-refresh-confirm" in html
    assert "data-attendance-progress" in html
    assert 'aria-live="polite"' in html
    assert "data-attendance-preview-output" in html


def test_attendance_card_distinguishes_both_fall_back_occurrences(monkeypatch):
    snapshot = _snapshot()
    snapshot["queue"] = [
        _row(
            "production_unassigned_run",
            start_utc="2026-11-01T06:30:00+00:00",
            end_utc="2026-11-01T07:30:00+00:00",
            detail="raw UTC values must not be visible",
        )
    ]
    monkeypatch.setattr(exceptions.exception_inbox, "build_snapshot", lambda: snapshot)
    monkeypatch.setattr(exceptions, "_active_correction_people", lambda: [])

    html = TestClient(app).get("/exceptions").text

    assert 'datetime="2026-11-01T06:30:00+00:00">11/1/2026 1:30 AM CDT' in html
    assert 'datetime="2026-11-01T07:30:00+00:00">11/1/2026 1:30 AM CST' in html
    assert "raw UTC values must not be visible" not in html


def test_attendance_card_bounds_nearby_workers_and_reason():
    row = _row(
        "production_unassigned_run",
        affected_workers=[
            {"employee_odoo_id": value, "employee_name": f"Worker {value}"}
            for value in range(1, 15)
        ],
        reason="r" * 500,
    )

    displayed = exceptions._display_exception_queue([row])[0]

    assert len(displayed["affected_workers"]) == 12
    assert displayed["affected_workers_truncated"] is True
    assert displayed["affected_workers"][-1]["employee_name"] == "Worker 12"
    assert len(displayed["reason_label"]) == 200


def test_correction_dialog_css_supports_focus_layout_and_small_screens():
    css = open("src/zira_dashboard/static/exceptions.css", encoding="utf-8").read()

    assert ".attendance-correction-dialog" in css
    assert ".attendance-correction-dialog::backdrop" in css
    assert ".attendance-correction-dialog :focus-visible" in css
    assert ".attendance-preview-interval" in css
    assert "@media (max-width: 800px)" in css
