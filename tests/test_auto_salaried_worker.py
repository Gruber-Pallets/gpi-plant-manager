# tests/test_auto_salaried_worker.py
"""run_tick end-to-end against Postgres with Odoo + sync stubbed.
Mirrors tests/test_auto_lunch_worker.py hygiene. skipif Postgres."""
import os
from datetime import date, datetime, time

import pytest

from zira_dashboard import auto_salaried as asal
from zira_dashboard import company_holidays, db, shift_config

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"), reason="needs Postgres")

PID = 990888  # test person odoo_id unlikely to collide
TUE = date(2026, 9, 1)


def _at(hh, mm, day=TUE):
    return datetime.combine(day, time(hh, mm), tzinfo=shift_config.SITE_TZ)


def _cleanup():
    db.execute("DELETE FROM auto_salaried_runs WHERE person_odoo_id = %s", (PID,))
    db.execute("DELETE FROM auto_salaried_flags WHERE person_odoo_id = %s", (PID,))
    db.execute("DELETE FROM timeclock_punches_log WHERE person_odoo_id = %s", (PID,))
    db.execute("DELETE FROM time_off_requests WHERE person_odoo_id = %s", (PID,))
    db.execute("DELETE FROM people WHERE odoo_id = %s", (PID,))


@pytest.fixture(autouse=True)
def _setup(monkeypatch):
    db.bootstrap_schema()
    _cleanup()
    db.execute(
        "INSERT INTO people (odoo_id, name, active, wage_type) "
        "VALUES (%s, 'Test Salaried', TRUE, 'monthly')", (PID,))
    monkeypatch.setenv("AUTO_SALARIED_ENABLED", "1")
    monkeypatch.delenv("AUTO_SALARIED_DRY_RUN", raising=False)
    monkeypatch.setattr(company_holidays, "has_synced", lambda: True)
    monkeypatch.setattr(company_holidays, "for_day", lambda d: None)
    monkeypatch.setattr("zira_dashboard.timeclock_sync.sync_one_by_id", lambda _id: None)
    # No open Odoo attendance by default → lunch dept unread path.
    monkeypatch.setattr(
        "zira_dashboard.odoo_client.get_current_attendance", lambda pid: None)
    yield
    _cleanup()


def _punches():
    return db.query(
        "SELECT action, wc_name, occurred_at, source FROM timeclock_punches_log "
        "WHERE person_odoo_id = %s ORDER BY occurred_at, id", (PID,))


def _run_row():
    rows = db.query(
        "SELECT * FROM auto_salaried_runs WHERE person_odoo_id = %s AND day = %s",
        (PID, TUE))
    return rows[0] if rows else None


def test_full_day_four_punches(monkeypatch):
    monkeypatch.setattr(
        "zira_dashboard.odoo_client.get_current_attendance",
        lambda pid: {"id": 42, "department_id": 9, "department_name": "05 Sustaining"})
    for hh, mm in ((6, 0), (11, 0), (11, 30), (15, 30)):
        asal.run_tick(_at(hh, mm))
    rows = _punches()
    assert [(r["action"], r["wc_name"]) for r in rows] == [
        ("clock_in", "Sustaining"), ("clock_out", None),
        ("clock_in", "Sustaining"), ("clock_out", None)]
    assert all(r["source"] == "auto_salaried" for r in rows)
    run = _run_row()
    assert all(run[f"{s}_punch_id"] for s in asal.SLOT_ORDER)
    assert run["dept_patch_state"] == "none"  # already Sustaining, no patch


def test_no_double_punch_on_repeat_ticks():
    asal.run_tick(_at(6, 0))
    asal.run_tick(_at(6, 1))
    asal.run_tick(_at(6, 2))
    assert len(_punches()) == 1


def test_catch_up_backdates():
    asal.run_tick(_at(12, 10))  # app "down" until 12:10
    rows = _punches()
    assert [r["action"] for r in rows] == ["clock_in", "clock_out", "clock_in"]
    assert rows[0]["occurred_at"].astimezone(shift_config.SITE_TZ).time() == time(6, 0)
    assert rows[1]["occurred_at"].astimezone(shift_config.SITE_TZ).time() == time(11, 0)


def test_approved_leave_skips_day():
    db.execute(
        "INSERT INTO time_off_requests (person_odoo_id, shape, holiday_status_id, "
        "date_from, date_to, state) VALUES (%s, 'full_day', 1, %s, %s, 'validate')",
        (PID, TUE, TUE))
    asal.run_tick(_at(6, 0))
    assert _punches() == []
    assert _run_row()["skip_reason"] == "approved_leave"


def test_pending_leave_does_not_skip():
    db.execute(
        "INSERT INTO time_off_requests (person_odoo_id, shape, holiday_status_id, "
        "date_from, date_to, state) VALUES (%s, 'full_day', 1, %s, %s, 'confirm')",
        (PID, TUE, TUE))
    asal.run_tick(_at(6, 0))
    assert len(_punches()) == 1


def test_holiday_skips_day(monkeypatch):
    monkeypatch.setattr(company_holidays, "for_day", lambda d: object())
    asal.run_tick(_at(6, 0))
    assert _punches() == []
    assert _run_row()["skip_reason"] == "holiday"


def test_weekend_no_op():
    sat = date(2026, 9, 5)
    asal.run_tick(_at(6, 0, day=sat))
    assert _punches() == []
    assert _run_row() is None


def test_hourly_person_untouched():
    db.execute("UPDATE people SET wage_type = 'hourly' WHERE odoo_id = %s", (PID,))
    asal.run_tick(_at(6, 0))
    assert _punches() == []


def test_foreign_punches_skip_and_flag():
    db.execute(
        "INSERT INTO timeclock_punches_log (person_odoo_id, action, occurred_at, source) "
        "VALUES (%s, 'clock_in', %s, 'employee')", (PID, _at(6, 30)))
    asal.run_tick(_at(7, 0))
    own = [r for r in _punches() if r["source"] == "auto_salaried"]
    assert own == []
    assert _run_row()["skip_reason"] == "other_punches"
    flags = db.query(
        "SELECT reason FROM auto_salaried_flags WHERE person_odoo_id = %s", (PID,))
    assert [f["reason"] for f in flags] == ["other_punches"]


def test_dept_capture_pending_patch(monkeypatch):
    monkeypatch.setattr(
        "zira_dashboard.odoo_client.get_current_attendance",
        lambda pid: {"id": 42, "department_id": 31, "department_name": "Maintenance"})
    patched = []
    monkeypatch.setattr(
        "zira_dashboard.odoo_client.set_attendance_department",
        lambda att_id, dept_id: patched.append((att_id, dept_id)) or True)
    asal.run_tick(_at(6, 0))
    asal.run_tick(_at(11, 0))
    run = _run_row()
    assert run["lunch_dept_id"] == 31
    assert run["dept_patch_state"] == "pending"
    asal.run_tick(_at(11, 30))
    # Simulate the sync landing the lunch-in punch in Odoo.
    db.execute(
        "UPDATE timeclock_punches_log SET synced_to_odoo = TRUE, odoo_attendance_id = 777 "
        "WHERE id = %s", (_run_row()["lunch_in_punch_id"],))
    asal.run_tick(_at(11, 32))
    assert patched == [(777, 31)]
    assert _run_row()["dept_patch_state"] == "done"


def test_dry_run_writes_no_punches(monkeypatch):
    monkeypatch.setenv("AUTO_SALARIED_DRY_RUN", "1")
    for hh, mm in ((6, 0), (11, 0), (11, 30), (15, 30)):
        asal.run_tick(_at(hh, mm))
    assert _punches() == []
    run = _run_row()
    assert all(run[f"{s}_punch_id"] == asal.SIMULATED_PUNCH_ID for s in asal.SLOT_ORDER)


def test_off_mode_no_op(monkeypatch):
    monkeypatch.delenv("AUTO_SALARIED_ENABLED", raising=False)
    asal.run_tick(_at(6, 0))
    assert _punches() == []
