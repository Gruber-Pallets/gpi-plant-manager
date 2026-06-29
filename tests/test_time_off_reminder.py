from __future__ import annotations

from datetime import date

import pytest

from zira_dashboard import time_off_reminder as tor


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


def test_reminder_full_day(fake_db, monkeypatch):
    monkeypatch.delenv("KIOSK_TIME_OFF_NOTIFY_ENABLED", raising=False)
    fake_db["query_result"] = [{
        "shape": "full_day", "date_from": date(2026, 6, 30),
        "date_to": date(2026, 6, 30), "hour_from": None, "hour_to": None,
    }]
    out = tor.reminder_for_person(5, today=date(2026, 6, 29))
    assert out is not None
    assert "tomorrow" in out["body"].lower()
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
    assert "11:00" in out["body"] and "1:30" in out["body"]


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
