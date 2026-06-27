"""Unit tests for the Odoo work-schedule conflict classifier.

The classifier is pure (no Odoo/DB), so these run locally without creds.
See docs/superpowers/specs/2026-06-27-odoo-calendar-conflict-diagnostic-design.md
"""

from types import SimpleNamespace

import zira_dashboard.odoo_client as oc
import zira_dashboard.staffing as staffing_mod
from scripts import diagnose_odoo_calendar_conflicts as diag
from scripts.diagnose_odoo_calendar_conflicts import classify_conflict

# Plant runs Mon-Fri (0=Mon .. 6=Sun, Python weekday()).
MON_FRI = frozenset({0, 1, 2, 3, 4})

_MON_FRI_HOURS = {str(d): ["6", "14"] for d in range(5)}
_MON_THU_HOURS = {str(d): ["6", "14"] for d in range(4)}


def _patch_odoo(monkeypatch, employees, schedules, cal_hours):
    monkeypatch.setattr(oc, "fetch_employees", lambda: employees)
    monkeypatch.setattr(oc, "fetch_work_schedules", lambda: schedules)
    monkeypatch.setattr(oc, "fetch_calendar_hours", lambda ids: cal_hours)


def test_covers_every_plant_weekday_is_ok():
    assert classify_conflict(MON_FRI, {0, 1, 2, 3, 4}, is_flexible=False, has_calendar=True) == "ok"


def test_extra_weekend_coverage_still_ok():
    assert classify_conflict(MON_FRI, {0, 1, 2, 3, 4, 5}, is_flexible=False, has_calendar=True) == "ok"


def test_missing_friday_is_missing_days():
    assert classify_conflict(MON_FRI, {0, 1, 2, 3}, is_flexible=False, has_calendar=True) == "missing_days"


def test_flexible_flag_is_flexible():
    # Even if the covered weekdays look complete, a flexible schedule has no
    # fixed hours Odoo can place a leave against.
    assert classify_conflict(MON_FRI, {0, 1, 2, 3, 4}, is_flexible=True, has_calendar=True) == "flexible"


def test_calendar_with_no_covered_weekdays_is_flexible():
    assert classify_conflict(MON_FRI, set(), is_flexible=False, has_calendar=True) == "flexible"


def test_no_calendar_is_no_calendar():
    assert classify_conflict(MON_FRI, set(), is_flexible=False, has_calendar=False) == "no_calendar"


def test_no_calendar_takes_precedence_over_flexible():
    # If there's no calendar at all we report that, not the flex bucket.
    assert classify_conflict(MON_FRI, set(), is_flexible=True, has_calendar=False) == "no_calendar"


def test_gather_excludes_reserves_and_non_roster(monkeypatch):
    employees = [
        {"id": 1, "name": "Ana Scheduled", "resource_calendar_id": [10, "M-F"]},
        {"id": 2, "name": "Bob Reserve", "resource_calendar_id": [10, "M-F"]},
        {"id": 3, "name": "Cara NotRostered", "resource_calendar_id": [10, "M-F"]},
        {"id": 4, "name": "Dan Missing Fri", "resource_calendar_id": [11, "M-Th"]},
    ]
    schedules = [
        {"id": 10, "name": "M-F", "is_flexible": False},
        {"id": 11, "name": "M-Th", "is_flexible": False},
    ]
    _patch_odoo(monkeypatch, employees, schedules, {10: _MON_FRI_HOURS, 11: _MON_THU_HOURS})
    roster = [
        SimpleNamespace(employee_id=1, reserve=False),
        SimpleNamespace(employee_id=2, reserve=True),   # reserve -> excluded
        SimpleNamespace(employee_id=4, reserve=False),
        # id 3 absent from roster -> excluded
    ]
    monkeypatch.setattr(staffing_mod, "load_roster", lambda: roster)

    rows, notes = diag._gather_rows(MON_FRI)

    by_id = {r["odoo_id"]: r for r in rows}
    assert set(by_id) == {1, 4}
    assert by_id[1]["verdict"] == "ok"
    assert by_id[4]["verdict"] == "missing_days"
    assert by_id[4]["missing"] == {4}  # Friday
    assert notes == []


def test_gather_falls_back_to_all_active_when_roster_unavailable(monkeypatch):
    employees = [
        {"id": 1, "name": "Ana", "resource_calendar_id": [10, "M-F"]},
        {"id": 2, "name": "Bob NoCal", "resource_calendar_id": False},
    ]
    schedules = [{"id": 10, "name": "M-F", "is_flexible": False}]
    _patch_odoo(monkeypatch, employees, schedules, {10: _MON_FRI_HOURS})

    def _boom():
        raise RuntimeError("postgres unreachable")

    monkeypatch.setattr(staffing_mod, "load_roster", _boom)

    rows, notes = diag._gather_rows(MON_FRI)

    by_id = {r["odoo_id"]: r for r in rows}
    assert set(by_id) == {1, 2}  # no reserve filter; all active included
    assert by_id[2]["verdict"] == "no_calendar"
    assert notes and "roster unavailable" in notes[0].lower()
