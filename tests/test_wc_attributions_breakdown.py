"""Pure-logic + DB tests for the breakdown exclusion extension to
wc_attributions.py. Mirrors tests/test_wc_attributions_testing.py's style."""
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from zira_dashboard import wc_attributions


def test_breakdown_identity_schema_is_additive_and_idempotent():
    from zira_dashboard._schema import SCHEMA_DDL

    ddl = " ".join(SCHEMA_DDL.split())

    assert (
        "ALTER TABLE wc_time_attributions ADD COLUMN IF NOT EXISTS "
        "employee_odoo_id INTEGER" in ddl
    )
    assert ddl.count(
        "ALTER TABLE breakdown_snoozes ADD COLUMN IF NOT EXISTS "
        "employee_odoo_id INTEGER"
    ) == 1
    assert "breakdown_snoozes_operator_identity_idx" in ddl
    assert "CREATE UNIQUE INDEX IF NOT EXISTS breakdown_snoozes_odoo_identity_uniq" not in ddl
    assert "CREATE UNIQUE INDEX IF NOT EXISTS breakdown_snoozes_legacy_identity_uniq" not in ddl
    assert "COALESCE('odoo:' || employee_odoo_id::text, 'name:' || person_name)" in ddl
    assert "wc_time_attributions_breakdown_odoo_visit_uniq" in ddl
    assert "wc_time_attributions_breakdown_legacy_visit_uniq" in ddl


def test_breakdown_source_excluded_from_people_by_wc():
    rows = [
        {"id": 1, "wc_name": "Dismantler 2", "person_name": "Juan",
         "start_utc": None, "end_utc": None, "source": "manual"},
        {"id": 2, "wc_name": "Dismantler 2", "person_name": "Benjamin",
         "start_utc": None, "end_utc": None, "source": wc_attributions.BREAKDOWN_SOURCE},
    ]
    out = wc_attributions.people_by_wc("2026-07-08", rows=rows)
    assert out["Dismantler 2"] == ["Juan"]


def test_breakdown_source_excluded_from_creditable_for_day(monkeypatch):
    rows = [
        {"id": 1, "wc_name": "Dismantler 2", "person_name": "Juan",
         "start_utc": None, "end_utc": None, "source": "manual"},
        {"id": 2, "wc_name": "Dismantler 2", "person_name": "Benjamin",
         "start_utc": None, "end_utc": None, "source": wc_attributions.BREAKDOWN_SOURCE},
    ]
    monkeypatch.setattr(wc_attributions, "for_day", lambda day: rows)
    out = wc_attributions.creditable_for_day("2026-07-08")
    assert [r["person_name"] for r in out] == ["Juan"]


def test_breakdown_windows_for_day_groups_by_person_and_wc():
    s1 = datetime(2026, 7, 8, 13, 2, tzinfo=timezone.utc)
    e1 = datetime(2026, 7, 8, 13, 30, tzinfo=timezone.utc)
    rows = [
        {"id": 1, "wc_name": "Dismantler 2", "person_name": "Juan",
         "start_utc": s1, "end_utc": e1, "source": wc_attributions.BREAKDOWN_SOURCE},
        {"id": 2, "wc_name": "Dismantler 2", "person_name": "Juan",
         "start_utc": s1, "end_utc": None, "source": "manual"},
    ]
    out = wc_attributions.breakdown_windows_for_day("2026-07-08", rows=rows)
    assert out == {("Juan", "Dismantler 2"): [(s1, e1)]}


def test_breakdown_windows_keep_same_name_odoo_identities_separate():
    start = datetime(2026, 7, 8, 13, 2, tzinfo=timezone.utc)
    rows = [
        {
            "id": 1,
            "wc_name": "Dismantler 2",
            "person_name": "Alex",
            "employee_odoo_id": 101,
            "start_utc": start,
            "end_utc": None,
            "source": wc_attributions.BREAKDOWN_SOURCE,
        },
        {
            "id": 2,
            "wc_name": "Dismantler 2",
            "person_name": "Alex",
            "employee_odoo_id": 202,
            "start_utc": start,
            "end_utc": None,
            "source": wc_attributions.BREAKDOWN_SOURCE,
        },
    ]

    out = wc_attributions.breakdown_windows_for_day("2026-07-08", rows=rows)

    assert out == {
        (101, "Alex", "Dismantler 2"): [(start, None)],
        (202, "Alex", "Dismantler 2"): [(start, None)],
    }


def test_shadow_unassigned_runs_accepts_same_name_odoo_breakdown_keys(monkeypatch):
    from zira_dashboard import production_history

    start = datetime(2026, 7, 8, 13, tzinfo=timezone.utc)
    sample_at = start + timedelta(minutes=10)
    end = start + timedelta(hours=1)
    inputs = SimpleNamespace(
        samples_by_wc={"Dismantler 2": [(sample_at, 1.0)]},
        break_windows=(),
        testing_windows={},
        breakdown_windows={
            (101, "Alex", "Dismantler 2"): [(start, end)],
            (202, "Alex", "Dismantler 2"): [(start, end)],
        },
        active_intervals_by_wc={"Dismantler 2": ((start, end),)},
        segments=(),
    )
    monkeypatch.setattr(
        production_history, "_strict_inputs_for_day", lambda *_args, **_kwargs: inputs
    )
    monkeypatch.setattr(
        production_history, "_strict_shift_bounds", lambda _day: (start, end)
    )

    assert wc_attributions.shadow_unassigned_runs_for_day(
        date(2026, 7, 8), object(), now_utc=end
    ) == ()


def test_add_breakdown_and_cap_and_reopen(monkeypatch):
    from zira_dashboard import db
    calls = {}

    def fake_query(sql, params):
        calls["insert"] = (sql, params)
        return [{"id": 5}]

    monkeypatch.setattr(db, "query", fake_query)
    day = date(2026, 7, 8)
    start = datetime(2026, 7, 8, 13, 2, tzinfo=timezone.utc)
    row_id = wc_attributions.add_breakdown(day, "Dismantler 2", "Juan", start, breakdown_id=42)
    assert row_id == 5
    sql, params = calls["insert"]
    assert "source" in sql.lower()
    assert params == (
        42,
        day,
        "Dismantler 2",
        "Juan",
        start,
        None,
        wc_attributions.BREAKDOWN_SOURCE,
        None,
    )

    def fake_execute(sql, params):
        calls["cap"] = (sql, params)

    monkeypatch.setattr(db, "execute", fake_execute)
    end = datetime(2026, 7, 8, 13, 30, tzinfo=timezone.utc)
    wc_attributions.cap_breakdown(5, end)
    assert calls["cap"][1] == (end, end, 5, wc_attributions.BREAKDOWN_SOURCE)

    wc_attributions.reopen_breakdown(5)
    assert calls["cap"][1] == (5, wc_attributions.BREAKDOWN_SOURCE)  # last _execute call was reopen


def test_add_breakdown_serializes_with_open_incident_before_insert(monkeypatch):
    from zira_dashboard import db

    seen = []
    monkeypatch.setattr(
        db,
        "query",
        lambda sql, params: seen.append((sql, params)) or [],
    )
    monkeypatch.setattr(
        wc_attributions,
        "open_breakdown_row",
        lambda *_args, **_kwargs: None,
    )

    row_id = wc_attributions.add_breakdown(
        date(2026, 7, 8),
        "Dismantler 2",
        "Alex",
        datetime(2026, 7, 8, 13, 2, tzinfo=timezone.utc),
        42,
        employee_odoo_id=101,
    )

    assert row_id == 0
    insert_sql = seen[0][0]
    assert "FROM machine_breakdowns" in insert_sql
    assert "resolved_at IS NULL" in insert_sql
    assert "FOR UPDATE" in insert_sql


def test_cap_breakdown_keeps_the_earliest_concurrent_boundary(monkeypatch):
    from zira_dashboard import db

    seen = {}
    monkeypatch.setattr(
        db,
        "execute",
        lambda sql, params: seen.update(sql=sql, params=params),
    )
    earlier = datetime(2026, 7, 8, 13, 30, tzinfo=timezone.utc)

    wc_attributions.cap_breakdown(5, earlier)

    assert "LEAST(COALESCE(end_utc, %s), %s)" in " ".join(seen["sql"].split())
    assert seen["params"] == (
        earlier,
        earlier,
        5,
        wc_attributions.BREAKDOWN_SOURCE,
    )


def test_normalize_breakdown_visit_locks_incident_and_converges_on_exact_row(
    monkeypatch,
):
    from contextlib import contextmanager

    from zira_dashboard import db

    start = datetime(2026, 7, 8, 13, 2, tzinfo=timezone.utc)
    end = datetime(2026, 7, 8, 14, 2, tzinfo=timezone.utc)
    calls = []
    fetched = iter(({"id": 42}, {"id": 10}))

    class Cursor:
        def execute(self, sql, params):
            calls.append((" ".join(sql.split()), params))

        def fetchone(self):
            return next(fetched)

    @contextmanager
    def cursor():
        yield Cursor()

    monkeypatch.setattr(db, "cursor", cursor)

    row_id = wc_attributions.normalize_breakdown_visit(
        9,
        42,
        "Alex Updated",
        start,
        employee_odoo_id=202,
        end_utc=end,
    )

    assert row_id == 10
    assert "machine_breakdowns" in calls[0][0]
    assert "resolved_at IS NULL FOR UPDATE" in calls[0][0]
    assert calls[0][1] == (42,)
    assert "employee_odoo_id = %s" in calls[1][0]
    assert calls[1][1] == (
        42,
        wc_attributions.BREAKDOWN_SOURCE,
        start,
        202,
        9,
    )
    assert calls[2][0].startswith("DELETE FROM wc_time_attributions")
    assert calls[2][1] == (9, 42, wc_attributions.BREAKDOWN_SOURCE, 202)
    assert "LEAST(COALESCE(end_utc, %s), %s)" in calls[3][0]
    assert calls[3][1] == (
        "Alex Updated",
        end,
        end,
        10,
        42,
        wc_attributions.BREAKDOWN_SOURCE,
    )


def test_normalize_breakdown_visit_retimes_legacy_row_when_no_exact_row(
    monkeypatch,
):
    from contextlib import contextmanager

    from zira_dashboard import db

    start = datetime(2026, 7, 8, 13, 2, tzinfo=timezone.utc)
    calls = []
    fetched = iter(({"id": 42}, None, {"id": 9}))

    class Cursor:
        def execute(self, sql, params):
            calls.append((" ".join(sql.split()), params))

        def fetchone(self):
            return next(fetched)

    @contextmanager
    def cursor():
        yield Cursor()

    monkeypatch.setattr(db, "cursor", cursor)

    row_id = wc_attributions.normalize_breakdown_visit(
        9,
        42,
        "Legacy Alex",
        start,
        employee_odoo_id=None,
        end_utc=None,
    )

    assert row_id == 9
    assert "employee_odoo_id IS NULL AND person_name = %s" in calls[1][0]
    assert "start_utc = %s, end_utc = NULL" in calls[2][0]
    assert "RETURNING id" in calls[2][0]
    assert calls[2][1] == (
        "Legacy Alex",
        start,
        9,
        42,
        wc_attributions.BREAKDOWN_SOURCE,
        "Legacy Alex",
    )


def test_restore_breakdown_snapshot_rolls_back_partial_failure_and_retries(monkeypatch):
    from contextlib import contextmanager

    from zira_dashboard import db

    snapshot = [
        {
            "day": date(2026, 7, 8),
            "wc_name": "Dismantler 2",
            "person_name": "Alex",
            "employee_odoo_id": 101,
            "start_utc": datetime(2026, 7, 8, 13, 2, tzinfo=timezone.utc),
            "end_utc": datetime(2026, 7, 8, 13, 30, tzinfo=timezone.utc),
            "source": wc_attributions.BREAKDOWN_SOURCE,
            "breakdown_id": 42,
        },
        {
            "day": date(2026, 7, 8),
            "wc_name": "Dismantler 2",
            "person_name": "Alex",
            "employee_odoo_id": 202,
            "start_utc": datetime(2026, 7, 8, 13, 12, tzinfo=timezone.utc),
            "end_utc": None,
            "source": wc_attributions.BREAKDOWN_SOURCE,
            "breakdown_id": 42,
        },
    ]
    saved = []
    fail_second = {"enabled": True}

    class Cursor:
        def __init__(self, pending):
            self.pending = pending

        def execute(self, _sql, params):
            if fail_second["enabled"] and len(self.pending) == 1:
                raise RuntimeError("second restore failed")
            self.pending.append(params)

    @contextmanager
    def cursor():
        pending = []
        try:
            yield Cursor(pending)
        except Exception:
            raise
        else:
            saved.extend(pending)

    monkeypatch.setattr(db, "cursor", cursor)

    import pytest

    def restore():
        with db.cursor() as active_cursor:
            wc_attributions._restore_breakdown_snapshot(
                active_cursor, snapshot, 42
            )

    with pytest.raises(RuntimeError, match="second restore failed"):
        restore()
    assert saved == []

    fail_second["enabled"] = False
    restore()

    assert len(saved) == 2
    assert saved[0][:3] == (date(2026, 7, 8), "Dismantler 2", "Alex")
    assert saved[0][-2:] == (42, 101)
    assert saved[1][-2:] == (42, 202)


def test_add_and_open_breakdown_use_odoo_identity_without_changing_display_name(
    monkeypatch,
):
    from zira_dashboard import db

    calls = []
    start = datetime(2026, 7, 8, 13, 2, tzinfo=timezone.utc)

    def fake_query(sql, params):
        calls.append((sql, params))
        return [{"id": 7, "start_utc": start}]

    monkeypatch.setattr(db, "query", fake_query)

    wc_attributions.add_breakdown(
        date(2026, 7, 8),
        "Dismantler 2",
        "Alex",
        start,
        breakdown_id=42,
        employee_odoo_id=202,
    )
    row = wc_attributions.open_breakdown_row(
        date(2026, 7, 8), "Dismantler 2", "Alex", employee_odoo_id=202
    )

    assert calls[0][1][-1] == 202
    lookup_sql, lookup_params = calls[1]
    assert "employee_odoo_id = %s" in lookup_sql
    assert lookup_params == (
        date(2026, 7, 8),
        "Dismantler 2",
        202,
        wc_attributions.BREAKDOWN_SOURCE,
    )
    assert row == {"id": 7, "start_utc": start}


def test_legacy_open_breakdown_lookup_only_adopts_null_identity_rows(monkeypatch):
    from zira_dashboard import db

    seen = {}
    monkeypatch.setattr(
        db,
        "query",
        lambda sql, params: seen.update(sql=sql, params=params) or [],
    )

    wc_attributions.open_breakdown_row(
        date(2026, 7, 8), "Dismantler 2", "Alex"
    )

    assert "employee_odoo_id IS NULL" in seen["sql"]
    assert seen["params"] == (
        date(2026, 7, 8),
        "Dismantler 2",
        "Alex",
        wc_attributions.BREAKDOWN_SOURCE,
    )


def test_odoo_lookup_can_adopt_one_legacy_null_identity_row(monkeypatch):
    from zira_dashboard import db

    start = datetime(2026, 7, 8, 13, 2, tzinfo=timezone.utc)
    calls = []

    def fake_query(sql, params):
        calls.append((sql, params))
        return [] if len(calls) == 1 else [{"id": 7, "start_utc": start}]

    monkeypatch.setattr(db, "query", fake_query)

    row = wc_attributions.open_breakdown_row(
        date(2026, 7, 8),
        "Dismantler 2",
        "Alex",
        employee_odoo_id=202,
        allow_legacy_fallback=True,
    )

    assert row == {"id": 7, "start_utc": start}
    assert "employee_odoo_id = %s" in calls[0][0]
    assert "employee_odoo_id IS NULL" in calls[1][0]
    assert "UPDATE wc_time_attributions" in calls[2][0]
    assert calls[2][1] == (202, 7)


def test_add_breakdown_returns_concurrent_idempotent_insert(monkeypatch):
    from zira_dashboard import db

    start = datetime(2026, 7, 8, 13, 2, tzinfo=timezone.utc)
    calls = []

    monkeypatch.setattr(
        db,
        "query",
        lambda sql, params: calls.append((sql, params)) or [{"id": 8}],
    )

    row_id = wc_attributions.add_breakdown(
        date(2026, 7, 8),
        "Dismantler 2",
        "Alex",
        start,
        42,
        employee_odoo_id=202,
    )

    assert row_id == 8
    assert calls[0][1][-1] == 202



def test_open_breakdown_row(monkeypatch):
    from zira_dashboard import db
    day = date(2026, 7, 8)
    start = datetime(2026, 7, 8, 13, 2, tzinfo=timezone.utc)
    monkeypatch.setattr(db, "query", lambda sql, params: [{"id": 7, "start_utc": start}])
    row = wc_attributions.open_breakdown_row(day, "Dismantler 2", "Juan")
    assert row == {"id": 7, "start_utc": start}

    monkeypatch.setattr(db, "query", lambda sql, params: [])
    assert wc_attributions.open_breakdown_row(day, "Dismantler 2", "Juan") is None


def _open_test_incident(db, day, wc_name, stop_utc):
    db.execute("DELETE FROM machine_breakdowns WHERE wc_name = %s", (wc_name,))
    return db.query(
        "INSERT INTO machine_breakdowns (wc_name, day, detected_stop_utc) "
        "VALUES (%s, %s, %s) RETURNING id",
        (wc_name, day, stop_utc),
    )[0]["id"]


@pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="needs Postgres")
def test_add_breakdown_is_atomic_for_concurrent_warmers():
    from zira_dashboard import db

    db.bootstrap_schema()
    day = date(2098, 8, 31)
    wc_name = "Task 12 Race WC"
    start = datetime(2098, 8, 31, 13, 2, tzinfo=timezone.utc)
    db.execute("DELETE FROM wc_time_attributions WHERE wc_name = %s", (wc_name,))
    incident_id = _open_test_incident(db, day, wc_name, start)

    def add(_index):
        return wc_attributions.add_breakdown(
            day, wc_name, "Alex", start, incident_id, employee_odoo_id=101
        )

    try:
        with ThreadPoolExecutor(max_workers=8) as pool:
            ids = list(pool.map(add, range(24)))
        rows = db.query(
            "SELECT id, employee_odoo_id FROM wc_time_attributions "
            "WHERE wc_name = %s AND source = %s",
            (wc_name, wc_attributions.BREAKDOWN_SOURCE),
        )
        assert len(set(ids)) == 1
        assert rows == [{"id": ids[0], "employee_odoo_id": 101}]
    finally:
        db.execute("DELETE FROM wc_time_attributions WHERE wc_name = %s", (wc_name,))
        db.execute("DELETE FROM machine_breakdowns WHERE id = %s", (incident_id,))


@pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="needs Postgres")
def test_same_name_workers_cap_only_the_departed_employee():
    from zira_dashboard import db

    db.bootstrap_schema()
    day = date(2098, 8, 31)
    wc_name = "Task 12 Same Name WC"
    start = datetime(2098, 8, 31, 13, 2, tzinfo=timezone.utc)
    departure = datetime(2098, 8, 31, 13, 23, tzinfo=timezone.utc)
    db.execute("DELETE FROM wc_time_attributions WHERE wc_name = %s", (wc_name,))
    incident_id = _open_test_incident(db, day, wc_name, start)

    try:
        first = wc_attributions.add_breakdown(
            day, wc_name, "Alex", start, incident_id, employee_odoo_id=101
        )
        second = wc_attributions.add_breakdown(
            day, wc_name, "Alex", start, incident_id, employee_odoo_id=202
        )

        wc_attributions.cap_breakdown(first, departure)

        rows = db.query(
            "SELECT employee_odoo_id, end_utc FROM wc_time_attributions "
            "WHERE wc_name = %s AND source = %s ORDER BY employee_odoo_id",
            (wc_name, wc_attributions.BREAKDOWN_SOURCE),
        )
        assert rows == [
            {"employee_odoo_id": 101, "end_utc": departure},
            {"employee_odoo_id": 202, "end_utc": None},
        ]
        assert first != second
    finally:
        db.execute("DELETE FROM wc_time_attributions WHERE wc_name = %s", (wc_name,))
        db.execute("DELETE FROM machine_breakdowns WHERE id = %s", (incident_id,))


@pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="needs Postgres")
def test_breakdown_cap_keeps_the_earliest_resolution_time():
    from zira_dashboard import db

    db.bootstrap_schema()
    day = date(2098, 8, 31)
    wc_name = "Task 12 Earliest Cap WC"
    start = datetime(2098, 8, 31, 13, 2, tzinfo=timezone.utc)
    recovery = datetime(2098, 8, 31, 13, 20, tzinfo=timezone.utc)
    late_departure = datetime(2098, 8, 31, 13, 25, tzinfo=timezone.utc)
    db.execute("DELETE FROM wc_time_attributions WHERE wc_name = %s", (wc_name,))
    incident_id = _open_test_incident(db, day, wc_name, start)

    try:
        row_id = wc_attributions.add_breakdown(
            day, wc_name, "Alex", start, incident_id, employee_odoo_id=101
        )

        wc_attributions.cap_breakdown(row_id, recovery)
        wc_attributions.cap_breakdown(row_id, late_departure)

        saved = db.query(
            "SELECT end_utc FROM wc_time_attributions WHERE id = %s",
            (row_id,),
        )
        assert saved == [{"end_utc": recovery}]
    finally:
        db.execute("DELETE FROM wc_time_attributions WHERE wc_name = %s", (wc_name,))
        db.execute("DELETE FROM machine_breakdowns WHERE id = %s", (incident_id,))


@pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="needs Postgres")
def test_dismiss_serializes_with_concurrent_breakdown_adoption():
    from zira_dashboard import db, machine_breakdown

    db.bootstrap_schema()
    day = date(2098, 8, 31)
    wc_name = "Task 12 Dismiss Race WC"
    start = datetime(2098, 8, 31, 13, 2, tzinfo=timezone.utc)
    incident_id = _open_test_incident(db, day, wc_name, start)

    def add(_index):
        return wc_attributions.add_breakdown(
            day,
            wc_name,
            "Alex",
            start,
            incident_id,
            employee_odoo_id=101,
        )

    try:
        with ThreadPoolExecutor(max_workers=8) as pool:
            futures = [pool.submit(add, index) for index in range(24)]
            futures.append(pool.submit(machine_breakdown.dismiss_incident, incident_id))
            for future in futures:
                future.result()
        assert db.query(
            "SELECT id FROM wc_time_attributions WHERE breakdown_id = %s",
            (incident_id,),
        ) == []
    finally:
        db.execute("DELETE FROM wc_time_attributions WHERE breakdown_id = %s", (incident_id,))
        db.execute("DELETE FROM machine_breakdowns WHERE id = %s", (incident_id,))


@pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="needs Postgres")
def test_dismiss_undo_snapshot_replay_is_idempotent_under_race():
    from zira_dashboard import db, machine_breakdown

    db.bootstrap_schema()
    day = date(2098, 8, 31)
    wc_name = "Task 12 Undo Race WC"
    start = datetime(2098, 8, 31, 13, 2, tzinfo=timezone.utc)
    incident_id = _open_test_incident(db, day, wc_name, start)
    snapshot = [{
        "day": day,
        "wc_name": wc_name,
        "person_name": "Alex",
        "employee_odoo_id": 101,
        "start_utc": start,
        "end_utc": None,
        "source": wc_attributions.BREAKDOWN_SOURCE,
        "breakdown_id": incident_id,
    }]
    machine_breakdown.dismiss_incident(incident_id)

    try:
        with ThreadPoolExecutor(max_workers=8) as pool:
            list(
                pool.map(
                    lambda _index: machine_breakdown.undo_dismiss_incident(
                        incident_id, snapshot
                    ),
                    range(24),
                )
            )
        rows = db.query(
            "SELECT employee_odoo_id, start_utc FROM wc_time_attributions "
            "WHERE breakdown_id = %s",
            (incident_id,),
        )
        assert rows == [{"employee_odoo_id": 101, "start_utc": start}]
    finally:
        db.execute("DELETE FROM wc_time_attributions WHERE breakdown_id = %s", (incident_id,))
        db.execute("DELETE FROM machine_breakdowns WHERE id = %s", (incident_id,))
