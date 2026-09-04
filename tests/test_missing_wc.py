"""missing_wc: pure shaping (no DB) + cache/resolve round-trips (Postgres)."""

import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from threading import Barrier

import pytest

from zira_dashboard import attendance_location_policy, missing_wc


# ---- pure shaping (no DB) ----


def _people():
    return {
        7: {"name": "Maria", "wage_type": "hourly", "active": True, "excluded": False},
        8: {"name": "Boss", "wage_type": "monthly", "active": True, "excluded": False},
        9: {"name": "Gone", "wage_type": "hourly", "active": False, "excluded": False},
    }


def test_shape_keeps_only_active_hourly_unresolved():
    cached = [
        {
            "att_id": 1,
            "employee_odoo_id": 7,
            "employee_name": "Maria",
            "check_in": "2026-06-02T11:58:00+00:00",
        },
        {
            "att_id": 2,
            "employee_odoo_id": 8,
            "employee_name": "Boss",
            "check_in": "2026-06-02T08:00:00+00:00",
        },
        {
            "att_id": 3,
            "employee_odoo_id": 9,
            "employee_name": "Gone",
            "check_in": "2026-06-02T07:00:00+00:00",
        },
        {
            "att_id": 4,
            "employee_odoo_id": 7,
            "employee_name": "Maria",
            "check_in": "2026-06-01T06:00:00+00:00",
        },
    ]
    rows = missing_wc.shape_rows(cached, _people(), resolved={4})
    ids = [r["attendance_id"] for r in rows]
    assert ids == [
        1
    ]  # salaried(2) + inactive(3) dropped; 4 resolved; only hourly-active-unresolved 1
    assert rows[0]["name"] == "Maria"
    assert rows[0]["check_in_label"]  # formatted, non-empty


def test_shape_sorts_newest_first():
    cached = [
        {
            "att_id": 1,
            "employee_odoo_id": 7,
            "employee_name": "M",
            "check_in": "2026-06-01T06:00:00+00:00",
        },
        {
            "att_id": 2,
            "employee_odoo_id": 7,
            "employee_name": "M",
            "check_in": "2026-06-03T06:00:00+00:00",
        },
    ]
    rows = missing_wc.shape_rows(cached, _people(), resolved=set())
    assert [r["attendance_id"] for r in rows] == [2, 1]


def test_shape_normalizes_json_string_ids():
    cached = [
        {
            "att_id": "1",
            "employee_odoo_id": "7",
            "employee_name": "M",
            "check_in": "2026-06-01T06:00:00+00:00",
        },
        {
            "att_id": "2",
            "employee_odoo_id": "7",
            "employee_name": "M",
            "check_in": "2026-06-02T06:00:00+00:00",
        },
    ]
    rows = missing_wc.shape_rows(cached, _people(), resolved={2})
    assert [r["attendance_id"] for r in rows] == [1]
    assert rows[0]["employee_odoo_id"] == 7


def test_shape_ignores_pre_rollout_and_known_unmapped_kiosk_attendances():
    """A newly enabled Odoo WC field must not turn old punches or deliberately
    unmapped kiosk locations into urgent inbox work."""
    cached = [
        {"att_id": 1, "employee_odoo_id": 7, "check_in": "2026-08-11T15:00:00+00:00"},
        {"att_id": 2, "employee_odoo_id": 7, "check_in": "2026-08-11T17:00:00+00:00"},
        {"att_id": 3, "employee_odoo_id": 7, "check_in": "2026-08-11T17:30:00+00:00"},
    ]

    rows = missing_wc.shape_rows(
        cached,
        _people(),
        resolved=set(),
        monitoring_started_at=datetime(2026, 8, 11, 16, 0, tzinfo=timezone.utc),
        locally_unmapped_attendance_ids={2},
    )

    assert [row["attendance_id"] for row in rows] == [3]


def test_shape_suppresses_exempt_attendance_and_employee_departments():
    people = {
        7: {
            "name": "Trent",
            "wage_type": "hourly",
            "active": True,
            "excluded": False,
            "department_name": "Supervisor",
        },
        8: {
            "name": "Gerald",
            "wage_type": "hourly",
            "active": True,
            "excluded": False,
            "department_name": "Transportation",
        },
        9: {
            "name": "Producer",
            "wage_type": "hourly",
            "active": True,
            "excluded": False,
            "department_name": "Recycled",
        },
    }
    cached = [
        {"att_id": 1, "employee_odoo_id": 7, "department_name": "Maintenance"},
        {"att_id": 2, "employee_odoo_id": 7, "department_name": "Supervisor"},
        {"att_id": 3, "employee_odoo_id": 8, "department_name": None},
        {"att_id": 4, "employee_odoo_id": 9, "department_name": None},
    ]

    rows = missing_wc.shape_rows(
        cached,
        people,
        resolved=set(),
        requires_work_center=(attendance_location_policy.default_department_requires_work_center),
    )

    assert [row["attendance_id"] for row in rows] == [4]


def test_monitoring_started_at_records_a_one_time_rollout_boundary(monkeypatch):
    from zira_dashboard import app_settings

    saved = {}
    monkeypatch.setattr(app_settings, "get_setting", lambda _key: None)
    monkeypatch.setattr(
        app_settings,
        "set_setting",
        lambda key, value: saved.update({"key": key, "value": value}),
    )
    started = datetime(2026, 8, 11, 19, 15, tzinfo=timezone.utc)

    assert missing_wc.monitoring_started_at(now=started) == started
    assert saved == {
        "key": "missing_wc.monitoring_started_at",
        "value": {"at": "2026-08-11T19:15:00+00:00"},
    }


def test_locally_unmapped_attendance_ids_only_returns_kiosk_rows_without_a_mapping(monkeypatch):
    from zira_dashboard import db

    captured = {}
    monkeypatch.setattr(
        db,
        "query",
        lambda sql, params: (
            captured.update({"sql": sql, "params": params})
            or [
                {"odoo_attendance_id": 22},
            ]
        ),
    )

    assert missing_wc.locally_unmapped_attendance_ids({11, 22}) == {22}
    assert captured["params"] == ([11, 22],)
    assert "clock_in" in captured["sql"]
    assert "odoo_work_center_id IS NULL" in captured["sql"]


# ---- DB-backed cache/resolve ----

pg = pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="needs Postgres")


@pg
def test_cache_write_read_round_trip():
    from zira_dashboard import db

    missing_wc.write_cache([{"att_id": 1, "employee_odoo_id": 7}])
    assert missing_wc._read_cache() == [{"att_id": 1, "employee_odoo_id": 7}]
    db.execute("UPDATE missing_wc_cache SET snapshot = '[]'::jsonb WHERE id = 1")


@pg
def test_resolve_and_resolved_ids():
    from zira_dashboard import db

    db.execute("DELETE FROM missing_wc_resolved WHERE attendance_id = %s", (999002,))
    missing_wc.resolve(999002, "assigned", name="Maria", wc_name="Dismantler 1")
    assert 999002 in missing_wc.resolved_ids()
    db.execute("DELETE FROM missing_wc_resolved WHERE attendance_id = %s", (999002,))


@pg
def test_resolve_many_suppresses_every_attendance_id():
    from zira_dashboard import db

    ids = (999012, 999013)
    db.execute("DELETE FROM missing_wc_resolved WHERE attendance_id = ANY(%s)", (list(ids),))
    try:
        missing_wc.resolve_many(ids, "dismissed", name="Luke")
        assert ids[0] in missing_wc.resolved_ids()
        assert ids[1] in missing_wc.resolved_ids()
    finally:
        db.execute("DELETE FROM missing_wc_resolved WHERE attendance_id = ANY(%s)", (list(ids),))


@pg
def test_claim_many_allows_one_winner_under_a_concurrent_race():
    from zira_dashboard import db

    ids = (999014, 999015)
    item_key = "attendance-unmapped:concurrent-test-workcenter"
    barrier = Barrier(2)
    db.execute("DELETE FROM missing_wc_resolved WHERE attendance_id = ANY(%s)", (list(ids),))

    def claim():
        barrier.wait()
        return missing_wc.claim_many(item_key, ids, "dismissed", name="Luke")

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _index: claim(), range(2)))

        assert sorted(results) == [False, True]
        rows = db.query(
            "SELECT attendance_id FROM missing_wc_resolved "
            "WHERE attendance_id = ANY(%s)",
            (list(ids),),
        )
        assert {int(row["attendance_id"]) for row in rows} == set(ids)
    finally:
        db.execute("DELETE FROM missing_wc_resolved WHERE attendance_id = ANY(%s)", (list(ids),))


def test_resolve_many_rolls_back_every_id_when_batch_fails(monkeypatch):
    from zira_dashboard import db

    committed = []

    class FailingCursor:
        def __init__(self, connection):
            self.connection = connection
            self.closed = False

        def execute(self, _sql, row):
            if self.connection.commit_count:
                raise RuntimeError("injected mid-batch failure")
            self.connection.pending.append(row)

        def executemany(self, _sql, rows):
            self.connection.pending.append(rows[0])
            raise RuntimeError("injected mid-batch failure")

        def close(self):
            self.closed = True

    class FakeConnection:
        def __init__(self):
            self.pending = []
            self.cursor_instance = FailingCursor(self)
            self.commit_count = 0
            self.rollback_count = 0

        def cursor(self, *, cursor_factory):
            assert cursor_factory is not None
            return self.cursor_instance

        def commit(self):
            self.commit_count += 1
            committed.extend(self.pending)
            self.pending.clear()

        def rollback(self):
            self.rollback_count += 1
            self.pending.clear()

    class FakePool:
        def __init__(self):
            self.connection = FakeConnection()
            self.get_count = 0
            self.returned = []

        def getconn(self):
            self.get_count += 1
            return self.connection

        def putconn(self, connection):
            self.returned.append(connection)

    pool = FakePool()
    monkeypatch.setattr(db, "_pool", pool)

    with pytest.raises(RuntimeError, match="injected mid-batch failure"):
        missing_wc.resolve_many((999012, 999013), "dismissed", name="Luke")

    assert committed == []
    assert pool.get_count == 1
    assert pool.connection.commit_count == 0
    assert pool.connection.rollback_count == 1
    assert pool.connection.pending == []
    assert pool.connection.cursor_instance.closed is True
    assert pool.returned == [pool.connection]


def test_claim_many_returns_false_without_writing_when_item_is_already_suppressed(
    monkeypatch,
):
    from zira_dashboard import db

    class Cursor:
        rowcount = 0

        def __init__(self):
            self.calls = []

        def execute(self, sql, params):
            self.calls.append((sql, params))

        def fetchone(self):
            return {"already_resolved": True}

        def executemany(self, *_args):
            raise AssertionError("losing claimant must not write")

    cursor = Cursor()

    class CursorContext:
        def __enter__(self):
            return cursor

        def __exit__(self, *_args):
            return False

    monkeypatch.setattr(db, "cursor", CursorContext)

    changed = missing_wc.claim_many(
        "attendance-unmapped:test-workcenter",
        (999012, 999013),
        "dismissed",
        name="Luke",
    )

    assert changed is False
    assert "pg_advisory_xact_lock" in cursor.calls[0][0]
    assert cursor.calls[0][1] == ("attendance-unmapped:test-workcenter",)
    assert "missing_wc_resolved" in cursor.calls[1][0]
    assert cursor.calls[1][1] == ([999012, 999013],)


def test_claim_many_rolls_back_every_id_when_batch_fails(monkeypatch):
    from zira_dashboard import db

    committed = []

    class FailingCursor:
        def __init__(self, connection):
            self.connection = connection
            self.closed = False

        def execute(self, sql, params):
            if "pg_advisory_xact_lock" in sql:
                return
            if "SELECT EXISTS" in sql:
                return
            raise AssertionError(sql)

        def fetchone(self):
            return {"already_resolved": False}

        def executemany(self, _sql, rows):
            self.connection.pending.append(rows[0])
            raise RuntimeError("injected mid-batch failure")

        def close(self):
            self.closed = True

    class FakeConnection:
        def __init__(self):
            self.pending = []
            self.cursor_instance = FailingCursor(self)
            self.commit_count = 0
            self.rollback_count = 0

        def cursor(self, *, cursor_factory):
            assert cursor_factory is not None
            return self.cursor_instance

        def commit(self):
            self.commit_count += 1
            committed.extend(self.pending)

        def rollback(self):
            self.rollback_count += 1
            self.pending.clear()

    class FakePool:
        def __init__(self):
            self.connection = FakeConnection()

        def getconn(self):
            return self.connection

        def putconn(self, _connection):
            return None

    pool = FakePool()
    monkeypatch.setattr(db, "_pool", pool)

    with pytest.raises(RuntimeError, match="injected mid-batch failure"):
        missing_wc.claim_many(
            "attendance-unmapped:test-workcenter",
            (999012, 999013),
            "dismissed",
            name="Luke",
        )

    assert committed == []
    assert pool.connection.commit_count == 0
    assert pool.connection.rollback_count == 1
    assert pool.connection.pending == []
    assert pool.connection.cursor_instance.closed is True
