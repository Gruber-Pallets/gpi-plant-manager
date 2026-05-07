"""Tests for late_report.late_people_for_day filter logic.

DB CRUD helpers are thin SQL wrappers and exercised by the live app; we
only unit-test the pure filter so the report stays in sync with what the
scheduler highlights.
"""
from datetime import date, datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from zira_dashboard import late_report


@pytest.fixture
def patch_db_empty():
    """Make absent_emp_ids_for_day and active_snoozes return empty by default."""
    with patch.object(late_report, "absent_emp_ids_for_day", return_value=set()), \
         patch.object(late_report, "active_snoozes", return_value=[]):
        yield


def _attendance(no_punch_ids=(), other=None):
    """Build an attendance dict with given no_punch ids."""
    out = {}
    for eid in no_punch_ids:
        out[eid] = {"status": "no_punch", "clocked_in_at": None, "minutes_late": 0, "transaction_type": ""}
    if other:
        out.update(other)
    return out


def _times(d, mins_past_start):
    """Return (now_local, shift_start_local) where now is mins_past_start after start."""
    shift_start = datetime(d.year, d.month, d.day, 6, 0, tzinfo=timezone.utc)
    now = shift_start + timedelta(minutes=mins_past_start)
    return now, shift_start


def test_returns_empty_before_threshold(patch_db_empty):
    """No alerts until 15 min past shift-start, even with no_punch people."""
    d = date(2026, 5, 1)
    now, start = _times(d, 10)  # 10 min past — under threshold
    att = _attendance(no_punch_ids=["100"])
    out = late_report.late_people_for_day(d, ["100"], att, now, start)
    assert out == []


def test_flags_no_punch_past_threshold(patch_db_empty):
    """16 min past start with a no_punch + scheduled person → flagged."""
    d = date(2026, 5, 1)
    now, start = _times(d, 16)
    att = _attendance(no_punch_ids=["100"])
    out = late_report.late_people_for_day(d, ["100"], att, now, start)
    assert len(out) == 1
    assert out[0]["emp_id"] == "100"
    assert out[0]["minutes_late"] == 16


def test_skips_already_clocked_in(patch_db_empty):
    """Someone who clocked in (any status other than no_punch) is not late."""
    d = date(2026, 5, 1)
    now, start = _times(d, 30)
    att = _attendance(other={
        "100": {"status": "on_time", "clocked_in_at": "06:05 AM", "minutes_late": 0, "transaction_type": "Clock In"},
        "200": {"status": "late", "clocked_in_at": "06:25 AM", "minutes_late": 25, "transaction_type": "Clock In"},
        "300": {"status": "clocked_out", "clocked_in_at": None, "minutes_late": 0, "transaction_type": "Clock Out"},
    })
    out = late_report.late_people_for_day(d, ["100", "200", "300"], att, now, start)
    assert out == []


def test_skips_unscheduled(patch_db_empty):
    """A no_punch person who isn't on today's schedule isn't flagged."""
    d = date(2026, 5, 1)
    now, start = _times(d, 30)
    att = _attendance(no_punch_ids=["100", "999"])
    out = late_report.late_people_for_day(d, ["100"], att, now, start)
    assert [r["emp_id"] for r in out] == ["100"]


def test_skips_declared_absent():
    """Manager-declared absences drop out of the list."""
    d = date(2026, 5, 1)
    now, start = _times(d, 30)
    att = _attendance(no_punch_ids=["100", "200"])
    with patch.object(late_report, "absent_emp_ids_for_day", return_value={"100"}), \
         patch.object(late_report, "active_snoozes", return_value=[]):
        out = late_report.late_people_for_day(d, ["100", "200"], att, now, start)
    assert [r["emp_id"] for r in out] == ["200"]


def test_skips_snoozed():
    """Snoozed people are silenced from the actionable list."""
    d = date(2026, 5, 1)
    now, start = _times(d, 30)
    att = _attendance(no_punch_ids=["100", "200"])
    with patch.object(late_report, "absent_emp_ids_for_day", return_value=set()), \
         patch.object(late_report, "active_snoozes", return_value=[{"emp_id": "200", "name": "Bob", "until_utc": now + timedelta(minutes=10)}]):
        out = late_report.late_people_for_day(d, ["100", "200"], att, now, start)
    assert [r["emp_id"] for r in out] == ["100"]


def test_threshold_is_strictly_greater(patch_db_empty):
    """Exactly 15 min past start → not yet late (use > not >=)."""
    d = date(2026, 5, 1)
    now, start = _times(d, 15)
    att = _attendance(no_punch_ids=["100"])
    out = late_report.late_people_for_day(d, ["100"], att, now, start)
    assert out == []


def test_minutes_late_reflects_now_minus_start(patch_db_empty):
    """minutes_late is computed from current time, not attendance signal."""
    d = date(2026, 5, 1)
    now, start = _times(d, 47)
    att = _attendance(no_punch_ids=["100"])
    out = late_report.late_people_for_day(d, ["100"], att, now, start)
    assert out[0]["minutes_late"] == 47


def test_custom_threshold(patch_db_empty):
    """Caller can override the 15-min default (e.g., for testing)."""
    d = date(2026, 5, 1)
    now, start = _times(d, 8)
    att = _attendance(no_punch_ids=["100"])
    out = late_report.late_people_for_day(d, ["100"], att, now, start, threshold_minutes=5)
    assert len(out) == 1


# Append to tests/test_late_report.py
import os
import pytest

requires_db = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set; this test needs Postgres.",
)


@requires_db
def test_declare_absent_accepts_optional_reason():
    from datetime import date
    from zira_dashboard import db, late_report

    d = date(2026, 5, 7)
    db.execute("DELETE FROM manual_absences WHERE day = %s", (d,))
    late_report.declare_absent(d, "999", "Test Person", reason="sick")
    rows = db.query(
        "SELECT name, reason FROM manual_absences WHERE day = %s AND emp_id = %s",
        (d, "999"),
    )
    assert rows == [{"name": "Test Person", "reason": "sick"}]
    db.execute("DELETE FROM manual_absences WHERE day = %s AND emp_id = %s", (d, "999"))


@requires_db
def test_declare_absent_reason_defaults_to_none():
    from datetime import date
    from zira_dashboard import db, late_report

    d = date(2026, 5, 7)
    db.execute("DELETE FROM manual_absences WHERE day = %s", (d,))
    late_report.declare_absent(d, "998", "No-Reason Person")
    rows = db.query(
        "SELECT reason FROM manual_absences WHERE day = %s AND emp_id = %s",
        (d, "998"),
    )
    assert rows == [{"reason": None}]
    db.execute("DELETE FROM manual_absences WHERE day = %s AND emp_id = %s", (d, "998"))


@requires_db
def test_save_late_arrival_upserts():
    from datetime import date
    from zira_dashboard import db, late_report

    d = date(2026, 5, 7)
    db.execute("DELETE FROM late_arrivals WHERE day = %s AND emp_id = %s", (d, "777"))
    late_report.save_late_arrival(d, "777", "Late Person", reason="car issues")
    rows = db.query(
        "SELECT name, reason FROM late_arrivals WHERE day = %s AND emp_id = %s",
        (d, "777"),
    )
    assert rows == [{"name": "Late Person", "reason": "car issues"}]
    late_report.save_late_arrival(d, "777", "Late Person", reason="overslept")
    rows = db.query(
        "SELECT reason FROM late_arrivals WHERE day = %s AND emp_id = %s",
        (d, "777"),
    )
    assert rows == [{"reason": "overslept"}]
    db.execute("DELETE FROM late_arrivals WHERE day = %s AND emp_id = %s", (d, "777"))


@requires_db
def test_late_arrivals_for_day_returns_emp_id_set():
    from datetime import date
    from zira_dashboard import db, late_report

    d = date(2026, 5, 7)
    db.execute("DELETE FROM late_arrivals WHERE day = %s", (d,))
    late_report.save_late_arrival(d, "100", "A", reason=None)
    late_report.save_late_arrival(d, "200", "B", reason=None)
    out = late_report.late_arrivals_for_day(d)
    assert out == {"100", "200"}
    db.execute("DELETE FROM late_arrivals WHERE day = %s", (d,))


@requires_db
def test_history_for_name_returns_absent_and_late_rows():
    from datetime import date
    from zira_dashboard import db, late_report

    d1 = date(2026, 5, 5)
    d2 = date(2026, 5, 6)
    name = "Test History"
    db.execute("DELETE FROM manual_absences WHERE name = %s", (name,))
    db.execute("DELETE FROM late_arrivals WHERE name = %s", (name,))
    late_report.declare_absent(d1, "555", name, reason="sick")
    late_report.save_late_arrival(d2, "555", name, reason="overslept")

    abs_rows = late_report.absences_history_for_name(name, d1, d2)
    late_rows = late_report.late_arrivals_history_for_name(name, d1, d2)
    assert abs_rows == [{"day": d1, "reason": "sick"}]
    assert late_rows == [{"day": d2, "reason": "overslept"}]

    db.execute("DELETE FROM manual_absences WHERE name = %s", (name,))
    db.execute("DELETE FROM late_arrivals WHERE name = %s", (name,))


def test_late_people_for_day_three_sections():
    """The expanded helper returns dict with scheduled_late, unscheduled_late,
    needs_reason — derived from the same attendance dict."""
    from datetime import date, datetime
    from zira_dashboard import shift_config, late_report

    d = date(2026, 5, 7)
    shift_start_local = datetime(2026, 5, 7, 7, 0, tzinfo=shift_config.SITE_TZ)
    now_local = datetime(2026, 5, 7, 9, 0, tzinfo=shift_config.SITE_TZ)  # 2h past start

    attendance = {
        # Scheduled, no_punch — counts as scheduled_late
        "111": {"status": "no_punch", "minutes_late": 0},
        # Scheduled, late punch — counts as needs_reason
        "222": {"status": "late", "minutes_late": 31},
        # Scheduled, on time — appears nowhere
        "333": {"status": "on_time", "minutes_late": 0},
        # Unscheduled, no_punch — counts as unscheduled_late
        "444": {"status": "no_punch", "minutes_late": 0},
        # Unscheduled, late — counts as needs_reason
        "555": {"status": "late", "minutes_late": 18},
    }
    out = late_report.late_people_for_day_v2(
        day=d,
        scheduled_emp_ids=["111", "222", "333"],
        unscheduled_emp_ids=["444", "555"],
        attendance=attendance,
        now_local=now_local,
        shift_start_local=shift_start_local,
        absent_ids=set(),
        snoozed_ids=set(),
        already_recorded_late_ids=set(),
    )

    assert {r["emp_id"] for r in out["scheduled_late"]} == {"111"}
    assert {r["emp_id"] for r in out["unscheduled_late"]} == {"444"}
    assert {r["emp_id"] for r in out["needs_reason"]} == {"222", "555"}


def test_late_people_for_day_v2_suppresses_already_recorded():
    """Once a late_arrivals row exists for an emp_id, that emp_id no
    longer appears in needs_reason."""
    from datetime import date, datetime
    from zira_dashboard import shift_config, late_report

    d = date(2026, 5, 7)
    shift_start_local = datetime(2026, 5, 7, 7, 0, tzinfo=shift_config.SITE_TZ)
    now_local = datetime(2026, 5, 7, 9, 0, tzinfo=shift_config.SITE_TZ)

    attendance = {"222": {"status": "late", "minutes_late": 31}}
    out = late_report.late_people_for_day_v2(
        day=d,
        scheduled_emp_ids=["222"],
        unscheduled_emp_ids=[],
        attendance=attendance,
        now_local=now_local,
        shift_start_local=shift_start_local,
        absent_ids=set(),
        snoozed_ids=set(),
        already_recorded_late_ids={"222"},
    )
    assert out["needs_reason"] == []


def test_late_people_for_day_v2_skips_absent_and_snoozed():
    from datetime import date, datetime
    from zira_dashboard import shift_config, late_report

    d = date(2026, 5, 7)
    shift_start_local = datetime(2026, 5, 7, 7, 0, tzinfo=shift_config.SITE_TZ)
    now_local = datetime(2026, 5, 7, 9, 0, tzinfo=shift_config.SITE_TZ)

    attendance = {
        "111": {"status": "no_punch", "minutes_late": 0},
        "222": {"status": "no_punch", "minutes_late": 0},
    }
    out = late_report.late_people_for_day_v2(
        day=d,
        scheduled_emp_ids=["111", "222"],
        unscheduled_emp_ids=[],
        attendance=attendance,
        now_local=now_local,
        shift_start_local=shift_start_local,
        absent_ids={"111"},
        snoozed_ids={"222"},
        already_recorded_late_ids=set(),
    )
    assert out["scheduled_late"] == []
