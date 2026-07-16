"""Custom-hours round-trip via the public load_schedule / save_schedule API.

These tests exercise the Postgres-backed Schedule storage. They skip
when DATABASE_URL is not set (e.g., CI without a Postgres available).
"""

import os
from datetime import date

import pytest

from zira_dashboard.staffing import Schedule, load_schedule, save_schedule


def test_schedule_custom_hours_defaults_to_none():
    s = Schedule(day=date(2026, 4, 28))
    assert s.custom_hours is None


def test_schedule_delivery_defaults_to_empty_mapping():
    assert Schedule(day=date(2026, 4, 28)).published_delivery == {}


pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="needs Postgres",
)


@pytest.fixture(autouse=True)
def _clean_schedule(request):
    """Wipe the test day's schedule before/after so tests are isolated."""
    if request.node.get_closest_marker("skipif"):
        # Setup: skip the cleanup if the test is skipped via marker.
        pass
    from zira_dashboard import db
    d = date(2099, 12, 31)
    db.execute("DELETE FROM schedules WHERE day = %s", (d,))
    yield
    db.execute("DELETE FROM schedules WHERE day = %s", (d,))


def test_load_schedule_reads_custom_hours():
    d = date(2099, 12, 31)
    sched_in = Schedule(
        day=d,
        published=True,
        assignments={},
        custom_hours={
            "start": "09:00",
            "end": "13:00",
            "breaks": [{"start": "11:00", "end": "11:15", "name": "Stand-up"}],
        },
    )
    save_schedule(sched_in)
    sched = load_schedule(d)
    assert sched.custom_hours == {
        "start": "09:00",
        "end": "13:00",
        "breaks": [{"start": "11:00", "end": "11:15", "name": "Stand-up"}],
    }


def test_load_schedule_treats_missing_custom_hours_as_none():
    d = date(2099, 12, 31)
    save_schedule(Schedule(day=d, published=False, assignments={}, custom_hours=None))
    sched = load_schedule(d)
    assert sched.custom_hours is None


def test_save_schedule_writes_custom_hours():
    d = date(2099, 12, 31)
    sched = Schedule(
        day=d,
        published=False,
        assignments={},
        custom_hours={"start": "09:00", "end": "13:00", "breaks": []},
    )
    save_schedule(sched)
    out = load_schedule(d)
    assert out.custom_hours == {"start": "09:00", "end": "13:00", "breaks": []}


def test_save_schedule_omits_custom_hours_when_none():
    d = date(2099, 12, 31)
    sched = Schedule(day=d, published=False, assignments={}, custom_hours=None)
    save_schedule(sched)
    out = load_schedule(d)
    assert out.custom_hours is None
