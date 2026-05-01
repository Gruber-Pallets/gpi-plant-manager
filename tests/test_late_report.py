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
