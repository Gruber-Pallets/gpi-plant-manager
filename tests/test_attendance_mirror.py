from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta, timezone
import os
from threading import Event, Thread

import pytest

from zira_dashboard import attendance_mirror, db


SYNCED_AT = datetime(2026, 8, 28, 15, 0, tzinfo=UTC)


def _row(
    attendance_id: int = 901,
    *,
    employee_id: int = 44,
    check_in: datetime = datetime(2026, 8, 28, 13, 0, tzinfo=UTC),
    check_out: datetime | None = None,
    write_date: datetime = datetime(2026, 8, 28, 13, 1, tzinfo=UTC),
    work_center_id: int | None = 72,
    work_center_name: str | None = "Unknown Odoo / Dismantler 1",
    department_id: int | None = 8,
    department_name: str | None = "01 Recycled",
) -> dict:
    return {
        "odoo_attendance_id": attendance_id,
        "employee_odoo_id": employee_id,
        "employee_name": "Adrian A.",
        "check_in_utc": check_in,
        "check_out_utc": check_out,
        "odoo_work_center_id": work_center_id,
        "odoo_work_center_name": work_center_name,
        "odoo_department_id": department_id,
        "odoo_department_name": department_name,
        "odoo_write_date": write_date,
    }


def test_local_days_use_plant_timezone_and_cover_cross_midnight_rows():
    days = attendance_mirror.local_days_touched(
        datetime(2026, 8, 29, 4, 30, tzinfo=UTC),
        datetime(2026, 8, 29, 6, 30, tzinfo=UTC),
    )

    assert days == {date(2026, 8, 28), date(2026, 8, 29)}


def test_local_days_do_not_count_checkout_at_local_midnight_twice():
    days = attendance_mirror.local_days_touched(
        datetime(2026, 8, 28, 22, 0, tzinfo=UTC),
        datetime(2026, 8, 29, 5, 0, tzinfo=UTC),
    )

    assert days == {date(2026, 8, 28)}


@pytest.mark.parametrize(
    "instant",
    [
        datetime(2026, 8, 28, 13, 0, tzinfo=UTC),
        datetime(2026, 8, 29, 5, 0, tzinfo=UTC),
    ],
)
def test_zero_duration_rows_touch_no_local_day_even_at_midnight(instant):
    assert attendance_mirror.local_days_touched(instant, instant) == set()


def test_active_read_queries_exclude_closed_zero_duration_rows(monkeypatch):
    queries = []
    monkeypatch.setattr(
        attendance_mirror.db,
        "query",
        lambda sql, params=None: queries.append((sql, params)) or [],
    )
    start = datetime(2026, 8, 28, 12, 0, tzinfo=UTC)
    end = datetime(2026, 8, 29, 12, 0, tzinfo=UTC)

    assert attendance_mirror.rows_overlapping(start, end) == ()
    assert attendance_mirror.rows_for_employee(44, start, end) == ()
    assert attendance_mirror.rows_for_employee(44, start, None) == ()

    assert len(queries) == 3
    assert all(
        "check_out_utc IS NULL OR check_out_utc > check_in_utc" in sql
        for sql, _params in queries
    )


def test_public_mirror_contract_rejects_naive_datetimes_before_database_access(
    monkeypatch,
):
    monkeypatch.setattr(
        attendance_mirror.db,
        "cursor",
        lambda: pytest.fail("invalid data reached the database"),
    )

    with pytest.raises(TypeError, match="sync_completed_at must be an aware datetime"):
        attendance_mirror.upsert_rows(
            [_row()], sync_completed_at=datetime(2026, 8, 28, 15, 0)
        )
    with pytest.raises(TypeError, match="start_utc must be an aware datetime"):
        attendance_mirror.rows_overlapping(
            datetime(2026, 8, 28, 12, 0), SYNCED_AT
        )


def test_mirror_reads_normalize_database_datetimes_to_aware_utc(monkeypatch):
    central = timezone(timedelta(hours=-5))
    raw = _row()
    raw.update(
        check_in_utc=datetime(2026, 8, 28, 8, 0, tzinfo=central),
        check_out_utc=datetime(2026, 8, 28, 16, 0, tzinfo=central),
        odoo_write_date=datetime(2026, 8, 28, 8, 1, tzinfo=central),
        first_seen_at=datetime(2026, 8, 28, 8, 2, tzinfo=central),
        last_seen_at=datetime(2026, 8, 28, 8, 3, tzinfo=central),
        deleted_at=None,
    )
    monkeypatch.setattr(attendance_mirror.db, "query", lambda *_a, **_k: [raw])

    stored = attendance_mirror.rows_overlapping(
        datetime(2026, 8, 28, 12, 0, tzinfo=UTC),
        datetime(2026, 8, 29, 12, 0, tzinfo=UTC),
    )[0]

    assert stored["check_in_utc"] == datetime(2026, 8, 28, 13, 0, tzinfo=UTC)
    assert stored["check_out_utc"] == datetime(2026, 8, 28, 21, 0, tzinfo=UTC)
    assert stored["odoo_write_date"].tzinfo is UTC
    assert stored["first_seen_at"].tzinfo is UTC
    assert stored["last_seen_at"].tzinfo is UTC


def test_health_and_cursor_snapshots_normalize_database_datetimes_to_utc(
    monkeypatch,
):
    central = timezone(timedelta(hours=-5))
    value = datetime(2026, 8, 28, 10, 0, tzinfo=central)
    state_row = {
        "cursor_write_date": value,
        "cursor_id": 901,
        "last_incremental_completed_at": value,
        "last_full_sweep_completed_at": value,
        "full_sweep_generation": 2,
        "baseline_completed_at": value,
    }
    health_row = {
        "last_incremental_completed_at": value,
        "last_full_sweep_completed_at": value,
        "baseline_completed_at": value,
        "oldest_recalc_requested_at": value,
        "last_error": None,
    }
    responses = [[state_row], [health_row]]
    monkeypatch.setattr(
        attendance_mirror.db, "query", lambda *_a, **_k: responses.pop(0)
    )

    state = attendance_mirror._sync_state_snapshot()
    health = attendance_mirror.health_snapshot()

    assert state.cursor_write_date.tzinfo is UTC
    assert state.last_incremental_completed_at.tzinfo is UTC
    assert state.last_full_sweep_completed_at.tzinfo is UTC
    assert state.baseline_completed_at.tzinfo is UTC
    assert health.last_incremental_completed_at.tzinfo is UTC
    assert health.last_full_sweep_completed_at.tzinfo is UTC
    assert health.baseline_completed_at.tzinfo is UTC
    assert health.oldest_recalc_requested_at.tzinfo is UTC


def test_logical_run_lock_uses_transaction_scoped_postgres_advisory_lock(
    monkeypatch,
):
    events = []

    class Cursor:
        def execute(self, sql, params=None):
            events.append(("execute", sql, params))

    @contextmanager
    def cursor():
        events.append(("enter",))
        yield Cursor()
        events.append(("commit_exit",))

    monkeypatch.setattr(attendance_mirror.db, "cursor", cursor)

    with attendance_mirror._logical_run_lock() as locked_cursor:
        assert locked_cursor.__class__ is Cursor
        events.append(("held",))

    assert events[0] == ("enter",)
    assert events[1][0] == "execute"
    assert "pg_advisory_xact_lock" in events[1][1]
    assert events[1][2] == (attendance_mirror._SYNC_ADVISORY_LOCK_KEY,)
    assert events[2:] == [("held",), ("commit_exit",)]


def test_owned_error_success_clear_preserves_foreign_failures():
    stored = attendance_mirror._error_with_failure(
        None, "incremental", "change page failed"
    )
    stored = attendance_mirror._error_with_failure(
        stored, "sweep", "ID page failed"
    )

    after_incremental_success = attendance_mirror._error_after_success(
        stored, "incremental"
    )

    assert attendance_mirror._format_error_state(after_incremental_success) == (
        "sweep: ID page failed"
    )
    assert attendance_mirror._error_after_success(
        after_incremental_success, "sweep"
    ) is None


def test_owned_error_encoding_is_bounded_and_health_format_is_deterministic():
    stored = None
    for owner in ("baseline", "sweep", "incremental"):
        stored = attendance_mirror._error_with_failure(
            stored, owner, owner[0] * 900
        )

    assert stored is not None
    assert len(stored) <= 500
    formatted = attendance_mirror._format_error_state(stored)
    assert formatted.startswith("incremental: i")
    assert "; sweep: s" in formatted
    assert "; baseline: b" in formatted
    assert len(formatted) <= 500


def test_owned_error_mutation_preserves_legacy_unowned_error():
    stored = attendance_mirror._error_with_failure(
        "legacy failure", "sweep", "new sweep failure"
    )

    assert attendance_mirror._format_error_state(stored) == (
        "sweep: new sweep failure; legacy: legacy failure"
    )
    assert "legacy failure" in attendance_mirror._format_error_state(
        attendance_mirror._error_after_success(stored, "sweep")
    )


def test_owned_error_records_blank_exception_with_useful_fallback():
    stored = attendance_mirror._error_with_failure(None, "sweep", Exception())

    assert attendance_mirror._format_error_state(stored) == (
        "sweep: unknown error"
    )


def test_sweep_keeps_recovery_and_deletion_recalc_reasons_separate(monkeypatch):
    recovered_day = date(2026, 8, 27)
    deleted_day = date(2026, 8, 28)
    enqueue_calls = []

    class SweepCursor:
        def __init__(self):
            self.rows = []

        def execute(self, sql, params=None):
            if "deleted_at IS NOT NULL" in sql:
                self.rows = [{"odoo_attendance_id": 901}]
            elif "AND NOT (odoo_attendance_id = ANY" in sql:
                self.rows = [
                    {
                        "odoo_attendance_id": 902,
                        "check_in_utc": datetime(
                            2026, 8, 28, 13, 0, tzinfo=UTC
                        ),
                        "check_out_utc": datetime(
                            2026, 8, 28, 21, 0, tzinfo=UTC
                        ),
                    }
                ]
            else:
                self.rows = []

        def fetchall(self):
            return list(self.rows)

    state = {
        "cursor_write_date": None,
        "cursor_id": None,
        "last_incremental_completed_at": SYNCED_AT,
        "last_full_sweep_completed_at": SYNCED_AT,
        "full_sweep_generation": 1,
        "baseline_completed_at": SYNCED_AT,
        "last_error": None,
    }
    monkeypatch.setattr(
        attendance_mirror, "_locked_sync_state", lambda _cur: state
    )

    def recover_rows(_cur, _rows, **_kwargs):
        enqueue_calls.append(
            (frozenset({recovered_day}), "odoo_attendance_changed")
        )
        return {recovered_day}

    monkeypatch.setattr(attendance_mirror, "_upsert_rows_cur", recover_rows)
    monkeypatch.setattr(
        attendance_mirror,
        "_enqueue_recalc_cur",
        lambda _cur, days, reason, **_kwargs: enqueue_calls.append(
            (frozenset(days), reason)
        ),
    )

    result = attendance_mirror._store_full_sweep_cur(
        SweepCursor(),
        {901},
        recovery_rows=[_row()],
        generation=2,
        completed_at=datetime(2026, 8, 28, 22, 0, tzinfo=UTC),
    )

    assert result.affected_days == frozenset({recovered_day, deleted_day})
    assert enqueue_calls == [
        (frozenset({recovered_day}), "odoo_attendance_changed"),
        (frozenset({deleted_day}), "odoo_attendance_deleted"),
    ]


_needs_postgres = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"), reason="needs local Postgres"
)


@pytest.fixture
def clean_mirror():
    db.init_pool()
    db.bootstrap_schema()
    with db.cursor() as cur:
        cur.execute("DELETE FROM attendance_recalc_queue")
        cur.execute("DELETE FROM attendance_strict_days")
        cur.execute("DELETE FROM odoo_attendance_mirror")
        cur.execute(
            "UPDATE odoo_attendance_sync_state SET "
            "cursor_write_date = NULL, cursor_id = NULL, "
            "last_incremental_started_at = NULL, "
            "last_incremental_completed_at = NULL, "
            "last_full_sweep_completed_at = NULL, "
            "last_full_sweep_deletion_count = 0, "
            "full_sweep_generation = 0, baseline_completed_at = NULL, "
            "last_error = NULL WHERE singleton = TRUE"
        )
    yield
    with db.cursor() as cur:
        cur.execute("DELETE FROM attendance_recalc_queue")
        cur.execute("DELETE FROM attendance_strict_days")
        cur.execute("DELETE FROM odoo_attendance_mirror")


@_needs_postgres
def test_real_postgres_logical_run_lock_serializes_and_releases(clean_mirror):
    first_entered = Event()
    release_first = Event()
    second_attempting = Event()
    second_entered = Event()
    errors = []

    def hold_first_lock():
        try:
            with attendance_mirror._logical_run_lock():
                first_entered.set()
                if not release_first.wait(timeout=5):
                    raise AssertionError("timed out waiting to release first lock")
        except Exception as exc:  # pragma: no cover - reported by parent thread
            errors.append(exc)

    def wait_for_same_lock():
        try:
            if not first_entered.wait(timeout=5):
                raise AssertionError("first lock was never acquired")
            second_attempting.set()
            with attendance_mirror._logical_run_lock():
                second_entered.set()
        except Exception as exc:  # pragma: no cover - reported by parent thread
            errors.append(exc)

    first = Thread(target=hold_first_lock)
    second = Thread(target=wait_for_same_lock)
    first.start()
    second.start()
    try:
        assert second_attempting.wait(timeout=5)
        assert not second_entered.wait(timeout=0.2)
    finally:
        release_first.set()
    first.join(timeout=5)
    second.join(timeout=5)

    assert not first.is_alive()
    assert not second.is_alive()
    assert not errors
    assert second_entered.is_set()


@_needs_postgres
def test_real_postgres_locked_cursor_rolls_back_rows_and_state_together(
    clean_mirror,
):
    with pytest.raises(ConnectionError, match="lock connection lost"):
        with attendance_mirror._logical_run_lock() as cur:
            attendance_mirror._record_incremental_started_cur(cur, SYNCED_AT)
            attendance_mirror._store_incremental_cycle_cur(
                cur,
                [_row()],
                cursor_write_date=_row()["odoo_write_date"],
                cursor_id=901,
                completed_at=SYNCED_AT,
            )
            raise ConnectionError("lock connection lost")

    assert db.query("SELECT * FROM odoo_attendance_mirror") == []
    state = db.query(
        "SELECT last_incremental_started_at, "
        "last_incremental_completed_at, cursor_write_date "
        "FROM odoo_attendance_sync_state WHERE singleton = TRUE"
    )[0]
    assert state == {
        "last_incremental_started_at": None,
        "last_incremental_completed_at": None,
        "cursor_write_date": None,
    }


@_needs_postgres
def test_upsert_is_idempotent_preserves_unknown_labels_and_handles_close_reopen(
    clean_mirror,
):
    assert attendance_mirror.upsert_rows(
        [_row()], sync_completed_at=SYNCED_AT
    ) == set()
    assert attendance_mirror.upsert_rows(
        [_row(write_date=datetime(2026, 8, 28, 13, 2, tzinfo=UTC))],
        sync_completed_at=SYNCED_AT,
    ) == set()

    stored = attendance_mirror.rows_overlapping(
        datetime(2026, 8, 28, 12, 0, tzinfo=UTC),
        datetime(2026, 8, 29, 12, 0, tzinfo=UTC),
    )
    assert len(stored) == 1
    assert stored[0]["odoo_work_center_name"] == "Unknown Odoo / Dismantler 1"
    assert db.query("SELECT * FROM attendance_recalc_queue") == []

    db.execute(
        "UPDATE odoo_attendance_sync_state SET baseline_completed_at = %s "
        "WHERE singleton = TRUE",
        (SYNCED_AT,),
    )
    closed = _row(
        check_out=datetime(2026, 8, 28, 21, 0, tzinfo=UTC),
        write_date=datetime(2026, 8, 28, 21, 1, tzinfo=UTC),
    )
    assert attendance_mirror.upsert_rows(
        [closed], sync_completed_at=SYNCED_AT
    ) == {date(2026, 8, 28)}
    reopened = _row(write_date=datetime(2026, 8, 28, 21, 2, tzinfo=UTC))
    assert attendance_mirror.upsert_rows(
        [reopened], sync_completed_at=SYNCED_AT
    ) == {date(2026, 8, 28)}

    queue = db.query(
        "SELECT day, completed_at FROM attendance_recalc_queue ORDER BY day"
    )
    assert [(item["day"], item["completed_at"]) for item in queue] == [
        (date(2026, 8, 28), None)
    ]
    assert db.query("SELECT day FROM attendance_strict_days") == [
        {"day": date(2026, 8, 28)}
    ]


@_needs_postgres
def test_material_move_enqueues_old_and_new_days_but_version_only_does_not(
    clean_mirror,
):
    db.execute(
        "UPDATE odoo_attendance_sync_state SET baseline_completed_at = %s "
        "WHERE singleton = TRUE",
        (SYNCED_AT,),
    )
    original = _row(
        check_in=datetime(2026, 8, 29, 4, 30, tzinfo=UTC),
        check_out=datetime(2026, 8, 29, 6, 30, tzinfo=UTC),
    )
    assert attendance_mirror.upsert_rows(
        [original], sync_completed_at=SYNCED_AT
    ) == {date(2026, 8, 28), date(2026, 8, 29)}

    db.execute("DELETE FROM attendance_recalc_queue")
    db.execute("DELETE FROM attendance_strict_days")
    version_only = dict(original)
    version_only["odoo_write_date"] = datetime(
        2026, 8, 29, 6, 31, tzinfo=UTC
    )
    assert attendance_mirror.upsert_rows(
        [version_only], sync_completed_at=SYNCED_AT
    ) == set()

    moved = dict(version_only)
    moved.update(
        check_in_utc=datetime(2026, 8, 30, 13, 0, tzinfo=UTC),
        check_out_utc=datetime(2026, 8, 30, 21, 0, tzinfo=UTC),
        odoo_work_center_id=999,
        odoo_work_center_name="Brand New Odoo Work Area",
        odoo_department_id=88,
        odoo_department_name="New Department",
        odoo_write_date=datetime(2026, 8, 30, 21, 1, tzinfo=UTC),
    )
    assert attendance_mirror.upsert_rows(
        [moved], sync_completed_at=datetime(2026, 8, 30, 22, 0, tzinfo=UTC)
    ) == {
        date(2026, 8, 28),
        date(2026, 8, 29),
        date(2026, 8, 30),
    }


@_needs_postgres
def test_sweep_tombstones_are_auditable_excluded_and_enqueue_old_days(
    clean_mirror,
):
    check_out = datetime(2026, 8, 28, 14, 0, tzinfo=UTC)
    attendance_mirror.upsert_rows(
        [
            _row(901, check_out=check_out),
            _row(902, employee_id=45, check_out=check_out),
        ],
        sync_completed_at=SYNCED_AT,
    )
    db.execute(
        "UPDATE odoo_attendance_sync_state SET baseline_completed_at = %s "
        "WHERE singleton = TRUE",
        (SYNCED_AT,),
    )

    assert attendance_mirror.mark_deleted_after_successful_sweep(
        {901}, generation=1
    ) == {date(2026, 8, 28)}
    assert [
        row["odoo_attendance_id"]
        for row in attendance_mirror.rows_overlapping(
            datetime(2026, 8, 28, 12, 0, tzinfo=UTC),
            datetime(2026, 8, 29, 12, 0, tzinfo=UTC),
        )
    ] == [901]
    assert attendance_mirror.rows_for_employee(
        45, datetime(2026, 8, 28, 12, 0, tzinfo=UTC), None
    ) == ()
    deleted = db.query(
        "SELECT deleted_at FROM odoo_attendance_mirror "
        "WHERE odoo_attendance_id = 902"
    )
    assert deleted[0]["deleted_at"] is not None


@_needs_postgres
def test_rows_cursor_and_recalc_roll_back_together_on_state_write_failure(
    clean_mirror, monkeypatch
):
    real_cursor = db.cursor

    class FailStateWrite:
        def __init__(self, cursor):
            self._cursor = cursor

        def execute(self, sql, params=None):
            if sql.lstrip().startswith("UPDATE odoo_attendance_sync_state"):
                raise RuntimeError("synthetic state failure")
            return self._cursor.execute(sql, params)

        def __getattr__(self, name):
            return getattr(self._cursor, name)

    @contextmanager
    def failing_cursor():
        with real_cursor() as cur:
            yield FailStateWrite(cur)

    monkeypatch.setattr(attendance_mirror.db, "cursor", failing_cursor)
    with pytest.raises(RuntimeError, match="synthetic state failure"):
        attendance_mirror.upsert_rows([_row()], sync_completed_at=SYNCED_AT)

    monkeypatch.setattr(attendance_mirror.db, "cursor", real_cursor)
    assert db.query("SELECT * FROM odoo_attendance_mirror") == []
    state = db.query(
        "SELECT cursor_write_date, last_incremental_completed_at "
        "FROM odoo_attendance_sync_state WHERE singleton = TRUE"
    )[0]
    assert state == {
        "cursor_write_date": None,
        "last_incremental_completed_at": None,
    }


@_needs_postgres
def test_health_reports_pending_age_baseline_and_bounded_error(clean_mirror):
    db.execute(
        "UPDATE odoo_attendance_sync_state SET "
        "last_incremental_completed_at = %s, "
        "last_full_sweep_completed_at = %s, baseline_completed_at = %s, "
        "last_error = %s WHERE singleton = TRUE",
        (SYNCED_AT, SYNCED_AT, SYNCED_AT, "x" * 900),
    )
    attendance_mirror.enqueue_recalc(
        [date(2026, 8, 29), date(2026, 8, 28)],
        "source_changed",
        mark_strict=True,
    )

    health = attendance_mirror.health_snapshot()

    assert health.last_incremental_completed_at == SYNCED_AT
    assert health.last_full_sweep_completed_at == SYNCED_AT
    assert health.baseline_completed_at == SYNCED_AT
    assert health.oldest_recalc_requested_at is not None
    assert health.last_error == "x" * 500


@_needs_postgres
def test_zero_duration_row_is_auditable_but_excluded_from_active_reads(
    clean_mirror,
):
    instant = datetime(2026, 8, 29, 5, 0, tzinfo=UTC)
    attendance_mirror.upsert_rows(
        [_row(check_in=instant, check_out=instant)],
        sync_completed_at=SYNCED_AT,
    )

    assert attendance_mirror.rows_overlapping(
        instant - timedelta(hours=1), instant + timedelta(hours=1)
    ) == ()
    assert attendance_mirror.rows_for_employee(
        44, instant - timedelta(hours=1), instant + timedelta(hours=1)
    ) == ()
    assert db.query(
        "SELECT odoo_attendance_id FROM odoo_attendance_mirror"
    ) == [{"odoo_attendance_id": 901}]


@_needs_postgres
def test_sweep_recovery_revives_tombstone_atomically_and_counts_deletions(
    clean_mirror,
):
    attendance_mirror.upsert_rows([_row()], sync_completed_at=SYNCED_AT)
    attendance_mirror.mark_deleted_after_successful_sweep(set(), generation=1)
    db.execute(
        "UPDATE odoo_attendance_sync_state SET baseline_completed_at = %s "
        "WHERE singleton = TRUE",
        (SYNCED_AT,),
    )
    recovered = _row(
        check_out=datetime(2026, 8, 28, 21, 0, tzinfo=UTC),
        write_date=datetime(2026, 8, 28, 21, 1, tzinfo=UTC),
    )

    result = attendance_mirror._store_full_sweep(
        {901},
        recovery_rows=[recovered],
        generation=2,
        completed_at=datetime(2026, 8, 28, 22, 0, tzinfo=UTC),
    )

    assert result.deleted_count == 0
    assert result.affected_days == frozenset({date(2026, 8, 28)})
    assert attendance_mirror.rows_for_employee(
        44,
        datetime(2026, 8, 28, 12, 0, tzinfo=UTC),
        datetime(2026, 8, 29, 12, 0, tzinfo=UTC),
    )[0]["check_out_utc"] == recovered["check_out_utc"]
    assert db.query("SELECT day FROM attendance_recalc_queue") == [
        {"day": date(2026, 8, 28)}
    ]
    assert db.query("SELECT day FROM attendance_strict_days") == [
        {"day": date(2026, 8, 28)}
    ]


@_needs_postgres
def test_sweep_recovery_and_unrelated_deletion_keep_distinct_reasons(
    clean_mirror,
):
    recovered = _row(
        attendance_id=901,
        check_in=datetime(2026, 8, 27, 13, 0, tzinfo=UTC),
        check_out=datetime(2026, 8, 27, 21, 0, tzinfo=UTC),
        write_date=datetime(2026, 8, 27, 21, 1, tzinfo=UTC),
    )
    deleted = _row(
        attendance_id=902,
        check_in=datetime(2026, 8, 28, 13, 0, tzinfo=UTC),
        check_out=datetime(2026, 8, 28, 21, 0, tzinfo=UTC),
        write_date=datetime(2026, 8, 28, 21, 1, tzinfo=UTC),
    )
    attendance_mirror.upsert_rows(
        [recovered, deleted], sync_completed_at=SYNCED_AT
    )
    attendance_mirror.mark_deleted_after_successful_sweep(
        {902}, generation=1
    )
    db.execute(
        "UPDATE odoo_attendance_sync_state SET baseline_completed_at = %s "
        "WHERE singleton = TRUE",
        (SYNCED_AT,),
    )

    attendance_mirror._store_full_sweep(
        {901},
        recovery_rows=[recovered],
        generation=2,
        completed_at=datetime(2026, 8, 28, 22, 0, tzinfo=UTC),
    )

    expected = [
        {"day": date(2026, 8, 27), "reason": "odoo_attendance_changed"},
        {"day": date(2026, 8, 28), "reason": "odoo_attendance_deleted"},
    ]
    assert db.query(
        "SELECT day, reason FROM attendance_recalc_queue ORDER BY day"
    ) == expected
    assert db.query(
        "SELECT day, reason FROM attendance_strict_days ORDER BY day"
    ) == expected


@_needs_postgres
def test_prebaseline_tombstone_recovery_does_not_recalculate_history(
    clean_mirror,
):
    attendance_mirror.upsert_rows([_row()], sync_completed_at=SYNCED_AT)
    attendance_mirror.mark_deleted_after_successful_sweep(set(), generation=1)

    result = attendance_mirror._store_full_sweep(
        {901},
        recovery_rows=[_row()],
        generation=2,
        completed_at=datetime(2026, 8, 28, 22, 0, tzinfo=UTC),
    )

    assert result.affected_days == frozenset()
    assert db.query("SELECT * FROM attendance_recalc_queue") == []
    assert db.query("SELECT * FROM attendance_strict_days") == []


@_needs_postgres
def test_owned_error_success_clears_only_matching_operation(clean_mirror):
    attendance_mirror._record_failure("incremental", "change failed")
    attendance_mirror._record_failure("sweep", "sweep failed")
    assert attendance_mirror.health_snapshot().last_error == (
        "incremental: change failed; sweep: sweep failed"
    )

    attendance_mirror.upsert_rows([], sync_completed_at=SYNCED_AT)
    assert attendance_mirror.health_snapshot().last_error == (
        "sweep: sweep failed"
    )

    attendance_mirror.mark_deleted_after_successful_sweep(set(), generation=1)
    assert attendance_mirror.health_snapshot().last_error is None
