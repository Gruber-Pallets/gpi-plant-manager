from __future__ import annotations

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
        "raw_work_center_labels": [],
        "odoo_work_center_ids": [],
    }
    row.update(changes)
    return row


def _snapshot():
    run = _row("production_unassigned_run")
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
    assert "Choose workers and times" in html
    assert 'data-attendance-correction-open' in html
    assert 'data-correction-item-key="' + ITEM_KEY + '"' in html
    assert "Odoo status: Unknown work center" in html
    assert "Odoo work center: ODoo Only &amp; Special/Center" in html
    assert "Map this Odoo work center" in html
    assert (
        "/settings?section=work_centers&amp;odoo_work_center_id=781"
        "&amp;odoo_work_center_name=ODoo+Only+%26+Special%2FCenter"
    ) in html


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
    assert 'data-attendance-start' in html
    assert 'data-attendance-end' in html
    assert 'data-attendance-open-ended' in html
    assert "Still working" in html
    assert 'data-attendance-work-center' in html
    assert 'value="Dismantler 1"' in html
    assert 'value="Repair 1"' in html
    assert 'data-attendance-preview' in html
    assert 'data-attendance-apply' in html
    assert 'data-attendance-apply' in html and "disabled" in html
    assert 'data-attendance-refresh-confirm' in html
    assert 'data-attendance-progress' in html
    assert 'aria-live="polite"' in html
    assert 'data-attendance-preview-output' in html


def test_correction_dialog_css_supports_focus_layout_and_small_screens():
    css = open("src/zira_dashboard/static/exceptions.css", encoding="utf-8").read()

    assert ".attendance-correction-dialog" in css
    assert ".attendance-correction-dialog::backdrop" in css
    assert ".attendance-correction-dialog :focus-visible" in css
    assert ".attendance-preview-interval" in css
    assert "@media (max-width: 800px)" in css
