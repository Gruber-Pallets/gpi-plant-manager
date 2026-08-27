from contextlib import contextmanager

from zira_dashboard import db, staffing


def test_save_roster_never_updates_active_on_an_existing_person(monkeypatch):
    calls = []

    class Cursor:
        def execute(self, sql, params=None):
            calls.append((" ".join(sql.split()), params))

    @contextmanager
    def cursor():
        yield Cursor()

    monkeypatch.setattr(db, "cursor", cursor)

    staffing.save_roster([
        staffing.Person(
            name="Cached Worker",
            active=False,
            reserve=True,
            employee_id=41,
        )
    ])

    people_sql, params = next(
        (sql, params) for sql, params in calls if "INSERT INTO people" in sql
    )
    insert_clause, update_clause = people_sql.split("ON CONFLICT", 1)
    assert "active" in insert_clause
    assert "active =" not in update_clause
    assert params == ("Cached Worker", False, True, 41)


def test_save_roster_acquires_celebration_source_lock_before_people_write(monkeypatch):
    calls = []

    class Cursor:
        def execute(self, sql, params=None):
            calls.append((" ".join(sql.split()), params))

    @contextmanager
    def cursor():
        yield Cursor()

    monkeypatch.setattr(db, "cursor", cursor)

    staffing.save_roster([
        staffing.Person(name="Locking Worker", active=True, reserve=False, employee_id=42)
    ])

    assert calls[0] == (
        "SELECT pg_advisory_xact_lock(%s::bigint)",
        (7_243_094_217,),
    )
    assert "INSERT INTO people" in calls[1][0]
