# tests/test_auto_salaried_reconcile.py
"""run_reconcile against Postgres with Odoo stubbed. skipif Postgres."""
import os
from datetime import date, datetime, time, timedelta

import pytest

from zira_dashboard import auto_salaried as asal
from zira_dashboard import db, shift_config

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"), reason="needs Postgres")

PID = 990889
TUE = date(2026, 9, 1)


def _cleanup():
    db.execute("DELETE FROM auto_salaried_runs WHERE person_odoo_id = %s", (PID,))
    db.execute("DELETE FROM auto_salaried_flags WHERE person_odoo_id = %s", (PID,))
    db.execute("DELETE FROM timeclock_punches_log WHERE person_odoo_id = %s", (PID,))
    db.execute("DELETE FROM time_off_requests WHERE person_odoo_id = %s", (PID,))


@pytest.fixture(autouse=True)
def _setup(monkeypatch):
    db.bootstrap_schema()
    _cleanup()
    monkeypatch.setenv("AUTO_SALARIED_ENABLED", "1")
    monkeypatch.delenv("AUTO_SALARIED_DRY_RUN", raising=False)
    yield
    _cleanup()


def _now():
    return datetime.combine(TUE, time(12, 0), tzinfo=shift_config.SITE_TZ)


def _seed_punched_morning(odoo_att_id=501):
    """A morning-in punch (synced) + its run row, as run_tick would leave them."""
    at = datetime.combine(TUE, time(6, 0), tzinfo=shift_config.SITE_TZ)
    row = db.query(
        "INSERT INTO timeclock_punches_log (person_odoo_id, action, wc_name, "
        "occurred_at, rounded_at, source, synced_to_odoo, odoo_attendance_id) "
        "VALUES (%s, 'clock_in', 'Sustaining', %s, %s, 'auto_salaried', TRUE, %s) "
        "RETURNING id", (PID, at, at, odoo_att_id))
    db.execute(
        "INSERT INTO auto_salaried_runs (person_odoo_id, day, morning_in_punch_id) "
        "VALUES (%s, %s, %s)", (PID, TUE, row[0]["id"]))


def _approve_leave():
    db.execute(
        "INSERT INTO time_off_requests (person_odoo_id, shape, holiday_status_id, "
        "date_from, date_to, state) VALUES (%s, 'full_day', 1, %s, %s, 'validate')",
        (PID, TUE, TUE))


def _run(day=TUE):
    rows = db.query(
        "SELECT * FROM auto_salaried_runs WHERE person_odoo_id = %s AND day = %s",
        (PID, day))
    return rows[0] if rows else None


def _flags():
    return [f["reason"] for f in db.query(
        "SELECT reason FROM auto_salaried_flags WHERE person_odoo_id = %s "
        "ORDER BY reason", (PID,))]


def test_clean_day_reverted(monkeypatch):
    _seed_punched_morning(odoo_att_id=501)
    _approve_leave()
    deleted = []
    monkeypatch.setattr(
        "zira_dashboard.odoo_client.fetch_employee_attendances_for_day",
        lambda pid, day: [{"id": 501}])
    monkeypatch.setattr(
        "zira_dashboard.odoo_client.delete_attendances",
        lambda ids: deleted.append(ids))
    asal.run_reconcile(_now())
    assert deleted == [[501]]
    assert _run()["reverted"] is True
    log = db.query(
        "SELECT sync_error FROM timeclock_punches_log WHERE person_odoo_id = %s", (PID,))
    assert log[0]["sync_error"] == "reverted: approved leave"


def test_stranger_odoo_record_flags_instead(monkeypatch):
    _seed_punched_morning(odoo_att_id=501)
    _approve_leave()
    monkeypatch.setattr(
        "zira_dashboard.odoo_client.fetch_employee_attendances_for_day",
        lambda pid, day: [{"id": 501}, {"id": 999}])  # outside-app transfer
    monkeypatch.setattr(
        "zira_dashboard.odoo_client.delete_attendances",
        lambda ids: pytest.fail("must not delete on a messy day"))
    asal.run_reconcile(_now())
    assert _run()["flagged"] is True
    assert _run()["reverted"] is False
    assert "leave_conflict" in _flags()


def test_foreign_local_punch_flags_instead(monkeypatch):
    _seed_punched_morning()
    _approve_leave()
    at = datetime.combine(TUE, time(8, 0), tzinfo=shift_config.SITE_TZ)
    # NOTE: deviation from brief — the punch-log `source` CHECK constraint
    # only allows ('employee', 'auto_lunch', 'auto_salaried'), so the
    # brief's 'kiosk' source would violate it. Use 'employee' instead
    # (same change already made in the Task 4 tests).
    db.execute(
        "INSERT INTO timeclock_punches_log (person_odoo_id, action, occurred_at, source) "
        "VALUES (%s, 'transfer_in', %s, 'employee')", (PID, at))
    monkeypatch.setattr(
        "zira_dashboard.odoo_client.delete_attendances",
        lambda ids: pytest.fail("must not delete on a messy day"))
    asal.run_reconcile(_now())
    assert "leave_conflict" in _flags()


def test_no_leave_no_action(monkeypatch):
    _seed_punched_morning()
    monkeypatch.setattr(
        "zira_dashboard.odoo_client.delete_attendances",
        lambda ids: pytest.fail("no leave, no delete"))
    asal.run_reconcile(_now())
    assert _run()["reverted"] is False
    assert _flags() == []


def test_incomplete_past_day_flagged():
    yesterday = TUE - timedelta(days=1)
    at = datetime.combine(yesterday, time(6, 0), tzinfo=shift_config.SITE_TZ)
    row = db.query(
        "INSERT INTO timeclock_punches_log (person_odoo_id, action, wc_name, "
        "occurred_at, rounded_at, source) VALUES "
        "(%s, 'clock_in', 'Sustaining', %s, %s, 'auto_salaried') RETURNING id",
        (PID, at, at))
    db.execute(
        "INSERT INTO auto_salaried_runs (person_odoo_id, day, morning_in_punch_id) "
        "VALUES (%s, %s, %s)", (PID, yesterday, row[0]["id"]))
    asal.run_reconcile(_now())
    assert "incomplete_day" in _flags()
    assert _run(yesterday)["flagged"] is True


def test_today_incomplete_not_flagged():
    _seed_punched_morning()  # today, mid-day: naturally incomplete
    asal.run_reconcile(_now())
    assert _flags() == []


def test_dry_run_sentinel_row_not_reverted(monkeypatch):
    db.execute(
        "INSERT INTO auto_salaried_runs (person_odoo_id, day, morning_in_punch_id) "
        "VALUES (%s, %s, %s)", (PID, TUE, asal.SIMULATED_PUNCH_ID))
    _approve_leave()
    monkeypatch.setattr(
        "zira_dashboard.odoo_client.delete_attendances",
        lambda ids: pytest.fail("nothing real to delete"))
    asal.run_reconcile(_now())
    assert _run()["reverted"] is False
