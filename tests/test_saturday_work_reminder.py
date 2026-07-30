from __future__ import annotations

from contextlib import contextmanager
from datetime import date, datetime, time

import pytest

from zira_dashboard import saturday_work_reminder as reminder
from zira_dashboard.shift_config import SITE_TZ


NOW = datetime(2026, 7, 24, 15, 30, tzinfo=SITE_TZ)


class FakeCursor:
    def __init__(self):
        self.rows: list[dict] = []
        self.executed: list[tuple[str, tuple | None]] = []
        self.executed_update = ""

    def execute(self, sql, params=None):
        self.executed.append((sql, params))
        if "UPDATE saturday_work_responses" in sql:
            self.executed_update = sql

    def fetchall(self):
        return self.rows


@pytest.fixture
def fake_cursor(monkeypatch):
    cursor = FakeCursor()

    @contextmanager
    def fake_db_cursor():
        yield cursor

    monkeypatch.setattr(reminder.db, "cursor", fake_db_cursor)
    return cursor


def _row(**overrides):
    row = {
        "day": date(2026, 7, 25),
        "availability_start": time(7, 0),
        "availability_end": time(11, 30),
        "response_deadline": datetime(2026, 7, 24, 7, tzinfo=SITE_TZ),
        "wc_name": None,
        "day_kind": "saturday",
        "event_name": "Saturday",
    }
    row.update(overrides)
    return row


def test_claim_returns_partial_hours_and_marks_once(fake_cursor):
    fake_cursor.rows = [_row()]

    card = reminder.claim_for_person(12, date(2026, 7, 24), NOW)

    assert card["day_label"] == "Saturday, July 25"
    assert card["hours"] == "7:00 AM–11:30 AM"
    assert card["work_center"] is None
    assert card["title_key"] == "Saturday work reminder"
    assert card["event_name"] == "Saturday"
    assert "punch_reminder_shown_at" in fake_cursor.executed_update
    assert "s.day_kind" in fake_cursor.executed[0][0]
    assert "s.event_name" in fake_cursor.executed[0][0]


def test_claim_returns_persisted_holiday_reminder_metadata(fake_cursor):
    fake_cursor.rows = [
        _row(
            day=date(2026, 11, 27),
            response_deadline=datetime(2026, 11, 25, 7, tzinfo=SITE_TZ),
            day_kind="holiday",
            event_name="Black Friday",
        )
    ]

    card = reminder.claim_for_person(
        12,
        date(2026, 11, 25),
        datetime(2026, 11, 25, 15, 30, tzinfo=SITE_TZ),
    )

    assert card["day_label"] == "Friday, November 27"
    assert card["title_key"] == "Holiday work reminder"
    assert card["event_name"] == "Black Friday"


def test_cancelled_commitment_returns_none(fake_cursor):
    fake_cursor.rows = []
    assert reminder.claim_for_person(12, date(2026, 7, 24), NOW) is None


def test_already_shown_returns_none(fake_cursor):
    fake_cursor.rows = []
    assert reminder.claim_for_person(12, date(2026, 7, 24), NOW) is None


def test_day_before_deadline_returns_none(fake_cursor):
    fake_cursor.rows = [_row()]
    assert reminder.claim_for_person(12, date(2026, 7, 23), NOW) is None
    assert not fake_cursor.executed_update


def test_published_assignment_returns_work_center_name(fake_cursor):
    fake_cursor.rows = [_row(wc_name="Repair")]

    card = reminder.claim_for_person(12, date(2026, 7, 24), NOW)

    assert card["work_center"] == "Repair"
    assert "punch_reminder_shown_at" in fake_cursor.executed_update
