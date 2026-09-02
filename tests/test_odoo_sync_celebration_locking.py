from contextlib import contextmanager, nullcontext
from threading import Event, Lock, Thread, current_thread

from zira_dashboard import db, employee_celebrations, odoo_sync


def test_roster_sync_normalizes_and_persists_feedback_work_email(monkeypatch):
    commands = []

    class FakeCursor:
        def execute(self, sql, params=None):
            commands.append((" ".join(sql.split()), params))

    @contextmanager
    def fake_cursor():
        yield FakeCursor()

    monkeypatch.setattr(db, "cursor", fake_cursor)
    monkeypatch.setattr(odoo_sync, "_read_last_sync", lambda: None)
    monkeypatch.setattr(odoo_sync, "_write_last_sync", lambda _value: None)
    monkeypatch.setattr(odoo_sync, "_set_roster_sync_alert", lambda _value: None)
    monkeypatch.setattr(odoo_sync, "_claim_celebration_source_generation", lambda: 1)
    monkeypatch.setattr(
        odoo_sync,
        "_celebration_source_generation_is_current",
        lambda _cursor, _generation: True,
    )
    monkeypatch.setattr(odoo_sync, "refresh_work_schedule_hours", lambda: None)
    employees = [
        {
            "id": 7,
            "name": "Ana Person",
            "active": True,
            "work_email": " Ana@GruberPallets.com ",
        },
        {
            "id": 8,
            "name": "Bad Email",
            "active": True,
            "work_email": "not an email",
        },
    ]
    monkeypatch.setattr(odoo_sync.odoo_client, "fetch_employees", lambda: employees)
    monkeypatch.setattr(
        odoo_sync.odoo_client,
        "fetch_employee_statuses",
        lambda: [{"id": row["id"], "active": True} for row in employees],
    )
    monkeypatch.setattr(
        odoo_sync.odoo_client,
        "fetch_employee_celebration_dates",
        lambda: odoo_sync.odoo_client.EmployeeCelebrationSource(False, False, {}),
    )
    monkeypatch.setattr(odoo_sync.odoo_client, "fetch_skills_for", lambda _ids: {})
    monkeypatch.setattr(odoo_sync.odoo_client, "fetch_spanish_skill_level_ids", lambda: {})
    monkeypatch.setattr(odoo_sync.odoo_client, "fetch_skill_columns_with_types", lambda: [])
    monkeypatch.setattr(odoo_sync.odoo_client, "fetch_skill_level_buckets", lambda: {})
    monkeypatch.setattr(odoo_sync.odoo_client, "fetch_departments", lambda: [])
    monkeypatch.setattr(odoo_sync.odoo_client, "fetch_work_schedules", lambda: [])

    assert odoo_sync.sync(force=True).ok is True

    people_writes = [item for item in commands if "INSERT INTO people" in item[0]]
    assert len(people_writes) == 2
    assert "full_name, work_email, active" in people_writes[0][0]
    assert people_writes[0][1][3] == "ana@gruberpallets.com"
    assert people_writes[1][1][3] is None


def test_roster_writer_acquires_the_celebration_source_transaction_lock(monkeypatch):
    commands = []

    class FakeCursor:
        def execute(self, sql, params=None):
            commands.append((sql, params))

    @contextmanager
    def fake_cursor():
        yield FakeCursor()

    monkeypatch.setattr(db, "cursor", fake_cursor)
    monkeypatch.setattr(odoo_sync, "_read_last_sync", lambda: None)
    monkeypatch.setattr(odoo_sync, "_write_last_sync", lambda _value: None)
    monkeypatch.setattr(odoo_sync, "_set_roster_sync_alert", lambda _value: None)
    monkeypatch.setattr(odoo_sync, "_claim_celebration_source_generation", lambda: 1)
    monkeypatch.setattr(
        odoo_sync,
        "_celebration_source_generation_is_current",
        lambda _cursor, _generation: True,
    )
    monkeypatch.setattr(odoo_sync, "refresh_work_schedule_hours", lambda: None)
    monkeypatch.setattr(
        odoo_sync.odoo_client,
        "fetch_employees",
        lambda: [{"id": 7, "name": "Lock Test", "active": True}],
    )
    monkeypatch.setattr(
        odoo_sync.odoo_client,
        "fetch_employee_statuses",
        lambda: [{"id": 7, "active": True}],
    )
    monkeypatch.setattr(odoo_sync.odoo_client, "fetch_skills_for", lambda _ids: {})
    monkeypatch.setattr(odoo_sync.odoo_client, "fetch_spanish_skill_level_ids", lambda: {})
    monkeypatch.setattr(odoo_sync.odoo_client, "fetch_skill_columns_with_types", lambda: [])
    monkeypatch.setattr(odoo_sync.odoo_client, "fetch_skill_level_buckets", lambda: {})
    monkeypatch.setattr(odoo_sync.odoo_client, "fetch_departments", lambda: [])
    monkeypatch.setattr(odoo_sync.odoo_client, "fetch_work_schedules", lambda: [])

    assert odoo_sync.sync(force=True).ok is True
    assert commands[0] == (
        "SELECT pg_advisory_xact_lock(%s::bigint)",
        (7_243_094_217,),
    )


def test_sync_continues_roster_when_celebration_generation_claim_fails(monkeypatch):
    """The ordering guard is optional; it must not take down the roster sync."""
    commands = []

    class FakeCursor:
        def execute(self, sql, params=None):
            commands.append((sql, params))

    @contextmanager
    def fake_cursor():
        yield FakeCursor()

    def claim_failure():
        raise RuntimeError("generation storage unavailable")

    def unexpected_celebration_fetch():
        raise AssertionError("a source snapshot needs a durable generation")

    monkeypatch.setattr(db, "cursor", fake_cursor)
    monkeypatch.setattr(odoo_sync, "_read_last_sync", lambda: None)
    monkeypatch.setattr(odoo_sync, "_write_last_sync", lambda _value: None)
    monkeypatch.setattr(odoo_sync, "_set_roster_sync_alert", lambda _value: None)
    monkeypatch.setattr(
        odoo_sync, "_claim_celebration_source_generation", claim_failure
    )
    monkeypatch.setattr(odoo_sync, "refresh_work_schedule_hours", lambda: None)
    monkeypatch.setattr(
        odoo_sync.odoo_client,
        "fetch_employees",
        lambda: [{"id": 7, "name": "Lock Test", "active": True}],
    )
    monkeypatch.setattr(
        odoo_sync.odoo_client,
        "fetch_employee_statuses",
        lambda: [{"id": 7, "active": True}],
    )
    monkeypatch.setattr(
        odoo_sync.odoo_client,
        "fetch_employee_celebration_dates",
        unexpected_celebration_fetch,
    )
    monkeypatch.setattr(odoo_sync.odoo_client, "fetch_skills_for", lambda _ids: {})
    monkeypatch.setattr(odoo_sync.odoo_client, "fetch_spanish_skill_level_ids", lambda: {})
    monkeypatch.setattr(odoo_sync.odoo_client, "fetch_skill_columns_with_types", lambda: [])
    monkeypatch.setattr(odoo_sync.odoo_client, "fetch_skill_level_buckets", lambda: {})
    monkeypatch.setattr(odoo_sync.odoo_client, "fetch_departments", lambda: [])
    monkeypatch.setattr(odoo_sync.odoo_client, "fetch_work_schedules", lambda: [])

    assert odoo_sync.sync(force=True).ok is True
    assert any("INSERT INTO people" in sql for sql, _params in commands)


def test_sync_keeps_an_older_celebration_snapshot_from_committing_after_a_newer_one(
    monkeypatch,
):
    old_source_ready = Event()
    release_old_sync = Event()
    newer_attempted_sync_lock = Event()
    newer_source_started = Event()
    committed_birthdays = []
    errors = []

    old_source = odoo_sync.odoo_client.EmployeeCelebrationSource(
        True,
        False,
        {7: {"birthday": "1991-07-04"}},
    )
    new_source = odoo_sync.odoo_client.EmployeeCelebrationSource(
        True,
        False,
        {7: {"birthday": "1991-07-05"}},
    )

    class FakeCursor:
        def execute(self, sql, params=None):
            if "UPDATE people SET birthday_month" in sql:
                committed_birthdays.append(params[:2])

    @contextmanager
    def fake_cursor():
        yield FakeCursor()

    class TrackingLock:
        def __init__(self):
            self.lock = Lock()

        def __enter__(self):
            if current_thread().name == "newer-sync":
                newer_attempted_sync_lock.set()
            self.lock.acquire()
            return self

        def __exit__(self, *_args):
            self.lock.release()

    def celebration_source():
        if current_thread().name == "older-sync":
            old_source_ready.set()
            return old_source
        newer_source_started.set()
        return new_source

    def fetch_skills_for(_employee_ids):
        if current_thread().name == "older-sync":
            assert release_old_sync.wait(timeout=2)
        return {}

    monkeypatch.setattr(db, "cursor", fake_cursor)
    monkeypatch.setattr(odoo_sync, "_SNAPSHOT_SYNC_LOCK", TrackingLock())
    monkeypatch.setattr(odoo_sync, "_read_last_sync", lambda: None)
    monkeypatch.setattr(odoo_sync, "_write_last_sync", lambda _value: None)
    monkeypatch.setattr(odoo_sync, "_set_roster_sync_alert", lambda _value: None)
    monkeypatch.setattr(odoo_sync, "_claim_celebration_source_generation", lambda: 1)
    monkeypatch.setattr(
        odoo_sync,
        "_celebration_source_generation_is_current",
        lambda _cursor, _generation: True,
    )
    monkeypatch.setattr(odoo_sync, "refresh_work_schedule_hours", lambda: None)
    monkeypatch.setattr(employee_celebrations, "reconcile_future", lambda _today: None)
    monkeypatch.setattr(
        odoo_sync.odoo_client,
        "fetch_employees",
        lambda: [{"id": 7, "name": "Lock Test", "active": True}],
    )
    monkeypatch.setattr(
        odoo_sync.odoo_client,
        "fetch_employee_statuses",
        lambda: [{"id": 7, "active": True}],
    )
    monkeypatch.setattr(
        odoo_sync.odoo_client,
        "fetch_employee_celebration_dates",
        celebration_source,
    )
    monkeypatch.setattr(odoo_sync.odoo_client, "fetch_skills_for", fetch_skills_for)
    monkeypatch.setattr(odoo_sync.odoo_client, "fetch_spanish_skill_level_ids", lambda: {})
    monkeypatch.setattr(odoo_sync.odoo_client, "fetch_skill_columns_with_types", lambda: [])
    monkeypatch.setattr(odoo_sync.odoo_client, "fetch_skill_level_buckets", lambda: {})
    monkeypatch.setattr(odoo_sync.odoo_client, "fetch_departments", lambda: [])
    monkeypatch.setattr(odoo_sync.odoo_client, "fetch_work_schedules", lambda: [])

    def run_sync():
        try:
            assert odoo_sync.sync(force=True).ok is True
        except BaseException as error:
            errors.append(error)

    older = Thread(target=run_sync, name="older-sync")
    newer = Thread(target=run_sync, name="newer-sync")
    older.start()
    assert old_source_ready.wait(timeout=2)
    newer.start()
    try:
        assert newer_attempted_sync_lock.wait(timeout=2)
        assert not newer_source_started.is_set()
    finally:
        release_old_sync.set()
        older.join(timeout=2)
        newer.join(timeout=2)

    assert not older.is_alive()
    assert not newer.is_alive()
    assert errors == []
    assert committed_birthdays == [(7, 4), (7, 5)]


def test_sync_generation_guard_rejects_an_older_source_after_a_newer_process_commits(
    monkeypatch,
):
    generation_lock = Lock()
    old_source_ready = Event()
    release_old_sync = Event()
    newer_date_committed = Event()
    committed_birthdays = []
    errors = []
    latest_generation = 0

    old_source = odoo_sync.odoo_client.EmployeeCelebrationSource(
        True,
        False,
        {7: {"birthday": "1991-07-04"}},
    )
    new_source = odoo_sync.odoo_client.EmployeeCelebrationSource(
        True,
        False,
        {7: {"birthday": "1991-07-05"}},
    )

    class FakeCursor:
        def __init__(self):
            self.source_generation_is_current = False

        def execute(self, sql, params=None):
            if "SELECT (value #>> '{}'" in sql:
                self.source_generation_is_current = params[0] == latest_generation
            if "UPDATE people SET birthday_month" in sql:
                committed_birthdays.append(params[:2])
                if params[:2] == (7, 5):
                    newer_date_committed.set()

        def fetchone(self):
            return {"is_current": self.source_generation_is_current}

    @contextmanager
    def fake_cursor():
        yield FakeCursor()

    def claim_generation(_sql, _params):
        nonlocal latest_generation
        with generation_lock:
            latest_generation += 1
            return [{"generation": latest_generation}]

    def celebration_source():
        if current_thread().name == "older-process":
            old_source_ready.set()
            return old_source
        return new_source

    def fetch_skills_for(_employee_ids):
        if current_thread().name == "older-process":
            assert release_old_sync.wait(timeout=2)
        return {}

    monkeypatch.setattr(db, "cursor", fake_cursor)
    monkeypatch.setattr(db, "query", claim_generation)
    monkeypatch.setattr(odoo_sync, "_SNAPSHOT_SYNC_LOCK", nullcontext())
    monkeypatch.setattr(odoo_sync, "_read_last_sync", lambda: None)
    monkeypatch.setattr(odoo_sync, "_write_last_sync", lambda _value: None)
    monkeypatch.setattr(odoo_sync, "_set_roster_sync_alert", lambda _value: None)
    monkeypatch.setattr(odoo_sync, "refresh_work_schedule_hours", lambda: None)
    monkeypatch.setattr(employee_celebrations, "reconcile_future", lambda _today: None)
    monkeypatch.setattr(
        odoo_sync.odoo_client,
        "fetch_employees",
        lambda: [{"id": 7, "name": "Lock Test", "active": True}],
    )
    monkeypatch.setattr(
        odoo_sync.odoo_client,
        "fetch_employee_statuses",
        lambda: [{"id": 7, "active": True}],
    )
    monkeypatch.setattr(
        odoo_sync.odoo_client,
        "fetch_employee_celebration_dates",
        celebration_source,
    )
    monkeypatch.setattr(odoo_sync.odoo_client, "fetch_skills_for", fetch_skills_for)
    monkeypatch.setattr(odoo_sync.odoo_client, "fetch_spanish_skill_level_ids", lambda: {})
    monkeypatch.setattr(odoo_sync.odoo_client, "fetch_skill_columns_with_types", lambda: [])
    monkeypatch.setattr(odoo_sync.odoo_client, "fetch_skill_level_buckets", lambda: {})
    monkeypatch.setattr(odoo_sync.odoo_client, "fetch_departments", lambda: [])
    monkeypatch.setattr(odoo_sync.odoo_client, "fetch_work_schedules", lambda: [])

    def run_sync():
        try:
            assert odoo_sync.sync(force=True).ok is True
        except BaseException as error:
            errors.append(error)

    older = Thread(target=run_sync, name="older-process")
    newer = Thread(target=run_sync, name="newer-process")
    older.start()
    assert old_source_ready.wait(timeout=2)
    newer.start()
    try:
        assert newer_date_committed.wait(timeout=2)
    finally:
        release_old_sync.set()
        older.join(timeout=2)
        newer.join(timeout=2)

    assert not older.is_alive()
    assert not newer.is_alive()
    assert errors == []
    assert committed_birthdays == [(7, 5)]
