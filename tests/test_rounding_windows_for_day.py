"""Department-driven window resolution + effective-WC selection (pure, monkeypatched)."""

from datetime import date

from zira_dashboard import staffing, rounding_store, rounding_system_store
from zira_dashboard.rounding import RoundingSettings
from zira_dashboard.routes import timeclock

MONDAY = date(2026, 6, 1)


def _sched(assignments):
    return staffing.Schedule(day=MONDAY, published=True, assignments=assignments)


# ---- _windows_for_day ----

def test_scheduled_dept_selects_system(monkeypatch):
    monkeypatch.setattr(staffing, "load_schedule", lambda d: _sched({"Dismantler 1": ["Alice"]}))
    monkeypatch.setattr(rounding_system_store, "windows_for_department",
                        lambda dept: RoundingSettings(20, 0, 0, 0) if dept == "Recycled" else None)
    monkeypatch.setattr(rounding_store, "current", lambda: RoundingSettings(0, 0, 0, 0))
    assert timeclock._windows_for_day("Alice", MONDAY, None) == RoundingSettings(20, 0, 0, 0)


def test_tablets_resolves_supervisor_system(monkeypatch):
    monkeypatch.setattr(staffing, "load_schedule", lambda d: _sched({"Tablets": ["Bob"]}))
    monkeypatch.setattr(rounding_system_store, "windows_for_department",
                        lambda dept: RoundingSettings(5, 5, 5, 5) if dept == "Supervisor" else None)
    monkeypatch.setattr(rounding_store, "current", lambda: RoundingSettings(0, 0, 0, 0))
    assert timeclock._windows_for_day("Bob", MONDAY, None) == RoundingSettings(5, 5, 5, 5)


def test_unscheduled_falls_back_to_clock_in_wc(monkeypatch):
    monkeypatch.setattr(staffing, "load_schedule", lambda d: _sched({}))
    monkeypatch.setattr(rounding_system_store, "windows_for_department",
                        lambda dept: RoundingSettings(20, 0, 0, 0) if dept == "Transportation" else None)
    monkeypatch.setattr(rounding_store, "current", lambda: RoundingSettings(0, 0, 0, 0))
    assert timeclock._windows_for_day("Carlos", MONDAY, "Truck Driver") == RoundingSettings(20, 0, 0, 0)


def test_no_schedule_no_wc_uses_plant_default(monkeypatch):
    monkeypatch.setattr(staffing, "load_schedule", lambda d: _sched({}))
    monkeypatch.setattr(rounding_system_store, "windows_for_department", lambda dept: None)
    monkeypatch.setattr(rounding_store, "current", lambda: RoundingSettings(7, 7, 7, 7))
    assert timeclock._windows_for_day("Dee", MONDAY, None) == RoundingSettings(7, 7, 7, 7)


def test_unmapped_department_uses_plant_default(monkeypatch):
    monkeypatch.setattr(staffing, "load_schedule", lambda d: _sched({"Work Orders": ["Eve"]}))
    monkeypatch.setattr(rounding_system_store, "windows_for_department", lambda dept: None)
    monkeypatch.setattr(rounding_store, "current", lambda: RoundingSettings(1, 2, 3, 4))
    assert timeclock._windows_for_day("Eve", MONDAY, None) == RoundingSettings(1, 2, 3, 4)


def test_multi_dept_first_scheduled_wins(monkeypatch):
    monkeypatch.setattr(staffing, "load_schedule",
                        lambda d: _sched({"Tablets": ["Frank"], "Dismantler 1": ["Frank"]}))
    monkeypatch.setattr(rounding_system_store, "windows_for_department",
                        lambda dept: {"Supervisor": RoundingSettings(9, 0, 0, 0),
                                      "Recycled": RoundingSettings(1, 0, 0, 0)}.get(dept))
    monkeypatch.setattr(rounding_store, "current", lambda: RoundingSettings(0, 0, 0, 0))
    assert timeclock._windows_for_day("Frank", MONDAY, None) == RoundingSettings(9, 0, 0, 0)


def test_flexible_employee_is_exempt(monkeypatch):
    # A flex-schedule employee has no fixed start/end, so they're exempt from
    # rounding even when scheduled into a department that would round (and even
    # vs. the plant default) — resolves to all-zero windows (apply_rounding no-op).
    monkeypatch.setattr(staffing, "load_schedule", lambda d: _sched({"Dismantler 1": ["Gina"]}))
    monkeypatch.setattr(rounding_system_store, "windows_for_department",
                        lambda dept: RoundingSettings(20, 0, 0, 0))
    monkeypatch.setattr(rounding_store, "current", lambda: RoundingSettings(5, 5, 5, 5))
    assert timeclock._windows_for_day(
        "Gina", MONDAY, "Dismantler 1", is_flexible=True
    ) == RoundingSettings(0, 0, 0, 0)


# ---- _effective_punch_wc ----

def test_effective_wc_clock_in_uses_form_wc():
    assert timeclock._effective_punch_wc("clock_in", "Dismantler 1", 123) == "Dismantler 1"


def test_effective_wc_clock_out_uses_current_wc(monkeypatch):
    monkeypatch.setattr(timeclock, "_current_state", lambda oid: {"current_wc": "Tablets"})
    assert timeclock._effective_punch_wc("clock_out", None, 123) == "Tablets"


def test_effective_wc_transfer_is_none():
    assert timeclock._effective_punch_wc("transfer_in", "X", 123) is None
    assert timeclock._effective_punch_wc("transfer_out", None, 123) is None


def test_effective_wc_clock_out_handles_lookup_error(monkeypatch):
    def _boom(oid):
        raise RuntimeError("db down")
    monkeypatch.setattr(timeclock, "_current_state", _boom)
    assert timeclock._effective_punch_wc("clock_out", None, 123) is None
