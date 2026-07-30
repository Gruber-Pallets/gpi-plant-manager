from __future__ import annotations

from datetime import date

import pytest

from zira_dashboard import shift_config
from zira_dashboard import time_off_reminder as tor


@pytest.fixture(autouse=True)
def _default_operational_week(monkeypatch):
    monkeypatch.setattr(
        shift_config,
        "is_workday",
        lambda candidate: candidate.weekday() < 5,
    )


@pytest.fixture
def fake_db(monkeypatch):
    captured: dict = {"queries": []}

    def fake_query(sql, params=None):
        captured["queries"].append((sql, params))
        return captured.get("query_result", [])

    monkeypatch.setattr(tor.db, "query", fake_query)
    return captured


def test_next_working_day_skips_weekend():
    # Fri 2026-07-03 -> Mon 2026-07-06 (skip Sat/Sun).
    assert tor.next_working_day(date(2026, 7, 3)) == date(2026, 7, 6)


def test_next_working_day_midweek():
    # Mon 2026-06-29 -> Tue 2026-06-30.
    assert tor.next_working_day(date(2026, 6, 29)) == date(2026, 6, 30)


def test_next_working_day_skips_closed_weekday_holiday(monkeypatch):
    friday = date(2026, 11, 27)
    monday_holiday = date(2026, 11, 30)
    monkeypatch.setattr(
        shift_config,
        "is_workday",
        lambda candidate: candidate.weekday() < 5 and candidate != monday_holiday,
    )

    assert tor.next_working_day(friday) == date(2026, 12, 1)


def test_next_working_day_counts_published_optional_holiday(monkeypatch):
    friday = date(2026, 11, 27)
    published_holiday = date(2026, 11, 28)
    monkeypatch.setattr(
        shift_config,
        "is_workday",
        lambda candidate: candidate == published_holiday or candidate.weekday() < 5,
    )

    assert tor.next_working_day(friday) == published_holiday


def test_reminder_full_day(fake_db, monkeypatch):
    monkeypatch.delenv("KIOSK_TIME_OFF_NOTIFY_ENABLED", raising=False)
    fake_db["query_result"] = [{
        "shape": "full_day", "date_from": date(2026, 6, 30),
        "date_to": date(2026, 6, 30), "hour_from": None, "hour_to": None,
    }]
    out = tor.reminder_for_person(5, today=date(2026, 6, 29))
    assert out is not None
    # Structured pieces the template renders via t(); the date value is shared.
    assert out["full_day"] is True
    assert out["title_key"] == "Time off reminder"
    assert out["body_key"] == "Heads up — you have approved time off {day}. Enjoy!"
    assert "tomorrow" in out["day"].lower()
    sql, params = fake_db["queries"][0]
    assert "state = 'validate'" in sql
    assert params == (5, date(2026, 6, 30), date(2026, 6, 30))


def test_reminder_partial_midday_gap(fake_db, monkeypatch):
    monkeypatch.delenv("KIOSK_TIME_OFF_NOTIFY_ENABLED", raising=False)
    fake_db["query_result"] = [{
        "shape": "midday_gap", "date_from": date(2026, 6, 30),
        "date_to": date(2026, 6, 30), "hour_from": 11.0, "hour_to": 13.5,
    }]
    out = tor.reminder_for_person(5, today=date(2026, 6, 29))
    assert out is not None
    assert out["full_day"] is False
    assert out["body_key"] == "Heads up — {day}, you're off from {hf} to {ht} (approved)."
    assert out["hf"] == "11:00 AM" and out["ht"] == "1:30 PM"


def test_reminder_none_when_no_leave(fake_db, monkeypatch):
    monkeypatch.delenv("KIOSK_TIME_OFF_NOTIFY_ENABLED", raising=False)
    fake_db["query_result"] = []
    assert tor.reminder_for_person(5, today=date(2026, 6, 29)) is None


def test_reminder_none_when_disabled(fake_db, monkeypatch):
    monkeypatch.setenv("KIOSK_TIME_OFF_NOTIFY_ENABLED", "0")
    fake_db["query_result"] = [{
        "shape": "full_day", "date_from": date(2026, 6, 30),
        "date_to": date(2026, 6, 30), "hour_from": None, "hour_to": None,
    }]
    assert tor.reminder_for_person(5, today=date(2026, 6, 29)) is None
