import os
from contextlib import contextmanager
from datetime import date

import pytest

from zira_dashboard import db
from zira_dashboard import employee_celebrations as celebrations


def test_normalize_birthday_discards_year_and_rejects_bad_dates():
    assert celebrations.normalize_birthday("1991-07-04") == (7, 4)
    assert celebrations.normalize_birthday("2026-02-29") is None
    assert celebrations.normalize_birthday(False) is None


def test_event_day_uses_feb_28_for_non_leap_years():
    assert celebrations.event_day_for(2027, 2, 29) == date(2027, 2, 28)


def test_normalize_first_contract_date_rejects_invalid_odoo_values():
    assert celebrations.normalize_first_contract_date("2021-08-20") == date(2021, 8, 20)
    assert celebrations.normalize_first_contract_date("2026-02-29") is None
    assert celebrations.normalize_first_contract_date(False) is None


def test_future_events_start_today_without_backfilling_old_events():
    events = celebrations.future_events_for_person(
        7, (7, 4), date(2021, 8, 20), date(2026, 8, 27), date(2027, 9, 1)
    )
    assert [event.event_day for event in events] == [date(2027, 7, 4), date(2027, 8, 20)]


def test_future_events_keep_first_completed_anniversary_only_after_year_one():
    events = celebrations.future_events_for_person(
        7, None, date(2026, 9, 1), date(2026, 8, 27), date(2027, 9, 1)
    )
    assert [(event.kind, event.completed_years) for event in events] == [
        ("work_anniversary", 1)
    ]


def test_future_events_observe_feb_29_and_report_completed_years():
    events = celebrations.future_events_for_person(
        7, (2, 29), date(2020, 2, 29), date(2027, 2, 28), date(2027, 2, 28)
    )
    assert [(event.kind, event.event_day, event.completed_years) for event in events] == [
        ("birthday", date(2027, 2, 28), None),
        ("work_anniversary", date(2027, 2, 28), 7),
    ]


def test_next_due_returns_only_the_oldest_row_for_the_signed_in_person(monkeypatch):
    monkeypatch.setattr(celebrations.db, "query", lambda _sql, _params: [{
        "id": 2, "person_odoo_id": 7, "kind": "birthday",
        "event_day": date(2026, 7, 4), "completed_years": None,
    }])

    assert celebrations.next_due(7, date(2026, 8, 27)).id == 2


def test_acknowledge_uses_event_and_owner_in_the_update(monkeypatch):
    seen = []
    monkeypatch.setattr(
        celebrations.db,
        "query",
        lambda sql, params: seen.append((sql, params)) or [{"id": 2}],
    )

    assert celebrations.acknowledge(2, 7) is True
    assert seen[0][1] == (2, 7)
    assert "acknowledged_at IS NULL" in seen[0][0]


def test_reconcile_future_locks_sources_and_mutates_the_queue_in_one_transaction(monkeypatch):
    commands = []

    class FakeCursor:
        def execute(self, sql, params=None):
            commands.append((sql, params))

        def fetchall(self):
            return [{
                "odoo_id": 7,
                "active": True,
                "birthday_month": 7,
                "birthday_day": 4,
                "first_contract_date": None,
            }]

    @contextmanager
    def fake_cursor():
        yield FakeCursor()

    monkeypatch.setattr(celebrations.db, "cursor", fake_cursor)
    monkeypatch.setattr(
        celebrations.db,
        "query",
        lambda *_args, **_kwargs: pytest.fail("reconciliation must use its transaction cursor"),
    )
    monkeypatch.setattr(
        celebrations.db,
        "execute",
        lambda *_args, **_kwargs: pytest.fail("reconciliation must use its transaction cursor"),
    )

    celebrations.reconcile_future(date(2026, 8, 27))

    assert "FOR UPDATE" in commands[0][0]
    assert all("employee_celebrations" in sql for sql, _params in commands[1:])


@pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="needs Postgres")
def test_reconcile_future_keeps_past_unacknowledged_events_and_removes_stale_future_ones():
    person_odoo_id = 990731
    today = date(2026, 8, 27)
    db.bootstrap_schema()
    db.execute("DELETE FROM employee_celebrations WHERE person_odoo_id = %s", (person_odoo_id,))
    db.execute("DELETE FROM people WHERE odoo_id = %s", (person_odoo_id,))
    try:
        db.execute(
            "INSERT INTO people (odoo_id, name, active, birthday_month, birthday_day) "
            "VALUES (%s, %s, TRUE, 12, 31)",
            (person_odoo_id, "__celebration_queue_reconcile__"),
        )
        db.execute(
            "INSERT INTO employee_celebrations (person_odoo_id, kind, event_day) "
            "VALUES (%s, 'birthday', %s), (%s, 'birthday', %s)",
            (person_odoo_id, date(2026, 7, 4), person_odoo_id, date(2026, 12, 31)),
        )
        db.execute(
            "UPDATE people SET birthday_month = 7, birthday_day = 4 WHERE odoo_id = %s",
            (person_odoo_id,),
        )

        celebrations.reconcile_future(today)

        rows = db.query(
            "SELECT event_day FROM employee_celebrations "
            "WHERE person_odoo_id = %s ORDER BY event_day",
            (person_odoo_id,),
        )
        assert [row["event_day"] for row in rows] == [date(2026, 7, 4), date(2027, 7, 4)]
    finally:
        db.execute("DELETE FROM employee_celebrations WHERE person_odoo_id = %s", (person_odoo_id,))
        db.execute("DELETE FROM people WHERE odoo_id = %s", (person_odoo_id,))


@pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="needs Postgres")
def test_reconcile_future_removes_only_future_unacknowledged_events_for_inactive_people():
    person_odoo_id = 990732
    today = date(2026, 8, 27)
    db.bootstrap_schema()
    db.execute("DELETE FROM employee_celebrations WHERE person_odoo_id = %s", (person_odoo_id,))
    db.execute("DELETE FROM people WHERE odoo_id = %s", (person_odoo_id,))
    try:
        db.execute(
            "INSERT INTO people (odoo_id, name, active) VALUES (%s, %s, FALSE)",
            (person_odoo_id, "__celebration_queue_inactive__"),
        )
        db.execute(
            "INSERT INTO employee_celebrations (person_odoo_id, kind, event_day, acknowledged_at) "
            "VALUES (%s, 'birthday', %s, NULL), (%s, 'birthday', %s, now())",
            (person_odoo_id, date(2026, 12, 31), person_odoo_id, date(2026, 11, 30)),
        )

        celebrations.reconcile_future(today)

        rows = db.query(
            "SELECT event_day FROM employee_celebrations "
            "WHERE person_odoo_id = %s ORDER BY event_day",
            (person_odoo_id,),
        )
        assert [row["event_day"] for row in rows] == [date(2026, 11, 30)]
    finally:
        db.execute("DELETE FROM employee_celebrations WHERE person_odoo_id = %s", (person_odoo_id,))
        db.execute("DELETE FROM people WHERE odoo_id = %s", (person_odoo_id,))
