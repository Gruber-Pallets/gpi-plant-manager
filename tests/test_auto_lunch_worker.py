"""Worker integration: run_tick drives the state machine end-to-end against
Postgres, with the open-attendance cache and Odoo sync stubbed. skipif Postgres."""
import os
from datetime import datetime, time, timedelta, timezone

import pytest

from zira_dashboard import (db, auto_lunch as al, auto_lunch_settings as als,
                            live_cache, shift_config)

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"), reason="needs Postgres")

PID = 990777  # test person odoo_id unlikely to collide


@pytest.fixture(autouse=True)
def _setup(monkeypatch):
    db.bootstrap_schema()
    db.execute("DELETE FROM auto_lunch_runs WHERE person_odoo_id = %s", (PID,))
    db.execute("DELETE FROM timeclock_punches_log WHERE person_odoo_id = %s", (PID,))
    als.save(als.Settings(enabled=True, observe_only=False,
                          flex_after_hours=5.0, flex_minutes=30))
    # Stub the Odoo sync so no XML-RPC happens; the punch row is enough.
    monkeypatch.setattr("zira_dashboard.timeclock_sync.sync_one_by_id", lambda _id: None)
    yield
    db.execute("DELETE FROM auto_lunch_runs WHERE person_odoo_id = %s", (PID,))
    db.execute("DELETE FROM timeclock_punches_log WHERE person_odoo_id = %s", (PID,))
    als.save(als.Settings())  # back to defaults (off)


def test_scheduled_auto_out_then_auto_in(monkeypatch):
    day = datetime.now(shift_config.SITE_TZ).date()
    lunch_out = datetime.combine(day, time(11, 0), tzinfo=shift_config.SITE_TZ)
    lunch_in = lunch_out + timedelta(minutes=30)
    now_ref = datetime.now(timezone.utc)
    # Force a fixed lunch window for `day`.
    from zira_dashboard.schedule_store import Break
    monkeypatch.setattr(shift_config, "is_workday", lambda d: True)
    monkeypatch.setattr(shift_config, "breaks_for",
                        lambda d: (Break(time(11, 0), time(11, 30), "Lunch"),))
    # Cache says clocked in, fresh.
    monkeypatch.setattr(live_cache, "read_open_attendance",
                        lambda: ({str(PID): {"att_id": 1, "check_in": None,
                                             "wc_name": "Bay 3"}}, now_ref))
    monkeypatch.setattr(live_cache, "is_stale", lambda _r: False)

    # Tick at lunch start -> auto clock_out written, run = auto_out.
    al.run_tick(now=lunch_out)
    outs = db.query("SELECT action, wc_name, source, "
                    "COALESCE(rounded_at, occurred_at) AS at "
                    "FROM timeclock_punches_log WHERE person_odoo_id=%s", (PID,))
    assert [(r["action"], r["source"]) for r in outs] == [("clock_out", "auto_lunch")]
    run = db.query("SELECT state, wc_name FROM auto_lunch_runs WHERE person_odoo_id=%s",
                   (PID,))[0]
    assert run["state"] == "auto_out" and run["wc_name"] == "Bay 3"

    # Now they're clocked OUT (cache empty) and it's lunch end -> auto clock_in.
    monkeypatch.setattr(live_cache, "read_open_attendance", lambda: ({}, now_ref))
    al.run_tick(now=lunch_in)
    acts = [r["action"] for r in db.query(
        "SELECT action, COALESCE(rounded_at,occurred_at) AS at FROM "
        "timeclock_punches_log WHERE person_odoo_id=%s ORDER BY at", (PID,))]
    assert acts == ["clock_out", "clock_in"]
    assert db.query("SELECT state FROM auto_lunch_runs WHERE person_odoo_id=%s",
                    (PID,))[0]["state"] == "done"


def test_clocked_out_at_lunch_is_skipped(monkeypatch):
    day = datetime.now(shift_config.SITE_TZ).date()
    lunch_out = datetime.combine(day, time(11, 0), tzinfo=shift_config.SITE_TZ)
    from zira_dashboard.schedule_store import Break
    monkeypatch.setattr(shift_config, "is_workday", lambda d: True)
    monkeypatch.setattr(shift_config, "breaks_for",
                        lambda d: (Break(time(11, 0), time(11, 30), "Lunch"),))
    monkeypatch.setattr(live_cache, "read_open_attendance",
                        lambda: ({}, datetime.now(timezone.utc)))  # nobody in
    monkeypatch.setattr(live_cache, "is_stale", lambda _r: False)
    # Seed a run row so PID is a candidate even though the cache is empty.
    db.execute("INSERT INTO auto_lunch_runs (person_odoo_id, day, kind, state) "
               "VALUES (%s,%s,'scheduled','pending')", (PID, day))
    al.run_tick(now=lunch_out)
    assert db.query("SELECT state FROM auto_lunch_runs WHERE person_odoo_id=%s",
                    (PID,))[0]["state"] == "skipped"
    assert db.query("SELECT COUNT(*) n FROM timeclock_punches_log "
                    "WHERE person_odoo_id=%s", (PID,))[0]["n"] == 0


def test_disabled_does_nothing(monkeypatch):
    als.save(als.Settings(enabled=False))
    al.run_tick(now=datetime.now(shift_config.SITE_TZ))
    assert db.query("SELECT COUNT(*) n FROM auto_lunch_runs "
                    "WHERE person_odoo_id=%s", (PID,))[0]["n"] == 0


def test_note_employee_clock_out_cancels_auto_in(monkeypatch):
    day = datetime.now(shift_config.SITE_TZ).date()
    out_at = datetime.now(shift_config.SITE_TZ).replace(microsecond=0)
    in_at = out_at + timedelta(minutes=30)
    db.execute("INSERT INTO auto_lunch_runs (person_odoo_id, day, kind, state, "
               "target_out_at, target_in_at) VALUES (%s,%s,'scheduled','auto_out',%s,%s)",
               (PID, day, out_at, in_at))
    ended = al.note_employee_clock_out(PID, now=out_at + timedelta(minutes=5))
    assert ended is True
    assert db.query("SELECT state FROM auto_lunch_runs WHERE person_odoo_id=%s",
                    (PID,))[0]["state"] == "ended_by_employee"


def test_active_lunch_run_only_inside_window():
    day = datetime.now(shift_config.SITE_TZ).date()
    out_at = datetime.now(shift_config.SITE_TZ).replace(microsecond=0)
    in_at = out_at + timedelta(minutes=30)
    db.execute("INSERT INTO auto_lunch_runs (person_odoo_id, day, kind, state, "
               "target_out_at, target_in_at) VALUES (%s,%s,'scheduled','auto_out',%s,%s)",
               (PID, day, out_at, in_at))
    assert al.active_lunch_run(PID, out_at + timedelta(minutes=5)) is not None
    assert al.active_lunch_run(PID, in_at + timedelta(minutes=1)) is None
