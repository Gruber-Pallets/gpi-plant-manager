import os
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, date, datetime
from unittest.mock import MagicMock
from uuid import UUID, uuid4

import psycopg2
import pytest

from zira_dashboard._schema import SCHEMA_DDL
from zira_dashboard.payroll_work_entry_rules import Decision
import zira_dashboard.payroll_work_entry_store as store


_LOOPBACK_DATABASE_HOSTS = {"localhost", "127.0.0.1", "::1"}
_UNSAFE_DATABASE_DSN_OPTIONS = {"hostaddr", "service", "servicefile"}


def _parse_database_dsn(database_url: str | None) -> dict[str, str] | None:
    if not database_url:
        return None
    try:
        return psycopg2.extensions.parse_dsn(database_url)
    except (TypeError, psycopg2.Error):
        return None


def _database_url_is_loopback(database_url: str | None) -> bool:
    params = _parse_database_dsn(database_url)
    return bool(
        params
        and not _UNSAFE_DATABASE_DSN_OPTIONS.intersection(params)
        and params.get("host") in _LOOPBACK_DATABASE_HOSTS
    )


def _database_integration_is_safe(
    database_url: str | None,
    explicit_opt_in: str | None,
) -> bool:
    if explicit_opt_in != "1":
        return False
    params = _parse_database_dsn(database_url)
    if not params or _UNSAFE_DATABASE_DSN_OPTIONS.intersection(params):
        return False
    return (
        params.get("host") in _LOOPBACK_DATABASE_HOSTS
        and params.get("dbname", "").endswith("_test")
    )


SAFE_TEST_DATABASE = _database_integration_is_safe(
    os.environ.get("DATABASE_URL"),
    os.environ.get("PAYROLL_GUARD_TEST_DATABASE"),
)


def correction_decision() -> Decision:
    return Decision(
        kind="correct",
        employee_id=19,
        employee_name="Isidro Moctezuma Aviles",
        work_date=date(2026, 7, 24),
        reason_codes=(),
        action="duration_update",
        work_entry_id=8502,
        attendance_id=3811,
        before_duration=3.621388889,
        after_duration=3.121355556,
        attendance_regular=3.121355556,
        attendance_overtime=5.3092,
        work_regular=3.621388889,
        work_overtime=5.309166667,
    )


def attempt_row(attempt_id=None, **changes):
    current_id = attempt_id or uuid4()
    row = {
        "attempt_id": str(current_id),
        "odoo_work_entry_id": 8502,
        "action": "duration_update",
        "employee_odoo_id": 19,
        "employee_name": "Isidro Moctezuma Aviles",
        "work_date": date(2026, 7, 24),
        "attendance_id": 3811,
        "before_duration": 3.621388889,
        "after_duration": 3.121355556,
        "attendance_regular": 3.121355556,
        "attendance_overtime": 5.3092,
        "work_regular_before": 3.621388889,
        "work_overtime": 5.309166667,
        "last_reason": "pending_correction",
        "last_detail": "correction intent saved",
        "created_at": datetime(2026, 8, 4, 12, 0, tzinfo=UTC),
        "updated_at": datetime(2026, 8, 4, 12, 0, tzinfo=UTC),
    }
    row.update(changes)
    return row


def test_schema_defines_attempt_outbox_append_only_audit_and_singleton_monitor():
    normalized_schema = " ".join(SCHEMA_DDL.split())
    assert "CREATE TABLE IF NOT EXISTS payroll_work_entry_corrections" in SCHEMA_DDL
    assert (
        "action TEXT NOT NULL CHECK (action IN ('duration_update', "
        "'delete_zero_regular'))" in SCHEMA_DDL
    )
    assert "CREATE INDEX IF NOT EXISTS payroll_work_entry_corrections_entry_idx" in SCHEMA_DDL
    assert "CREATE TABLE IF NOT EXISTS payroll_work_entry_correction_attempts" in SCHEMA_DDL
    assert "attempt_id UUID PRIMARY KEY" in normalized_schema
    assert "odoo_work_entry_id INTEGER NOT NULL" in normalized_schema
    assert "UNIQUE (odoo_work_entry_id)" in normalized_schema
    assert "attendance_id INTEGER NOT NULL" in normalized_schema
    assert "last_reason TEXT NOT NULL" in normalized_schema
    assert "last_detail TEXT NOT NULL" in normalized_schema
    assert "created_at TIMESTAMPTZ NOT NULL" in normalized_schema
    assert "updated_at TIMESTAMPTZ NOT NULL" in normalized_schema
    assert "ADD COLUMN IF NOT EXISTS attempt_id UUID" in normalized_schema
    assert (
        "CREATE UNIQUE INDEX IF NOT EXISTS payroll_work_entry_corrections_attempt_idx"
        in SCHEMA_DDL
    )
    assert "WHERE attempt_id IS NOT NULL" in SCHEMA_DDL
    assert "CREATE TABLE IF NOT EXISTS payroll_work_entry_guard_monitor" in SCHEMA_DDL
    assert "DEFAULT 1 CHECK (id = 1)" in SCHEMA_DDL
    assert "reported_issue_keys   TEXT[] NOT NULL DEFAULT '{}'" in SCHEMA_DDL
    assert "updated_at            TIMESTAMPTZ NOT NULL DEFAULT now()" in SCHEMA_DDL


def test_schema_enforces_correction_values_and_append_only_history():
    assert "payroll_work_entry_corrections_action_duration_check" in SCHEMA_DDL
    assert "payroll_work_entry_corrections_finite_totals_check" in SCHEMA_DDL
    assert "payroll_work_entry_corrections_verification_detail_check" in SCHEMA_DDL
    assert "CREATE OR REPLACE FUNCTION reject_payroll_correction_mutation()" in SCHEMA_DDL
    assert "CREATE TRIGGER payroll_work_entry_corrections_append_only" in SCHEMA_DDL
    assert "BEFORE UPDATE OR DELETE ON payroll_work_entry_corrections" in SCHEMA_DDL
    assert "CREATE TRIGGER payroll_work_entry_corrections_reject_truncate" in SCHEMA_DDL
    assert "BEFORE TRUNCATE ON payroll_work_entry_corrections" in SCHEMA_DDL
    assert "FOR EACH STATEMENT EXECUTE FUNCTION reject_payroll_correction_mutation()" in SCHEMA_DDL
    assert "payroll_work_entry_correction_attempts_action_duration_check" in SCHEMA_DDL
    assert "payroll_work_entry_correction_attempts_finite_totals_check" in SCHEMA_DDL


def test_append_correction_inserts_every_audit_value_in_column_order(monkeypatch):
    execute = MagicMock()
    monkeypatch.setattr(store.db, "execute", execute)
    now = datetime(2026, 8, 3, 20, 0, tzinfo=UTC)

    store.append_correction(correction_decision(), "duration reread matched", now)

    sql, params = execute.call_args.args
    normalized_sql = " ".join(sql.split())
    assert (
        "INSERT INTO payroll_work_entry_corrections "
        "(odoo_work_entry_id, action, employee_odoo_id, employee_name, work_date, "
        "before_duration, after_duration, attendance_regular, attendance_overtime, "
        "work_regular_before, work_overtime, verification_detail, corrected_at) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
        in normalized_sql
    )
    assert params == (
        8502,
        "duration_update",
        19,
        "Isidro Moctezuma Aviles",
        date(2026, 7, 24),
        3.621388889,
        3.121355556,
        3.121355556,
        5.3092,
        3.621388889,
        5.309166667,
        "duration reread matched",
        now,
    )


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"kind": "review"}, "correct"),
        ({"action": None}, "action"),
        ({"work_entry_id": None}, "Work Entry id"),
        ({"after_duration": None}, "after duration"),
    ],
)
def test_append_correction_rejects_incomplete_or_non_correction_decisions(
    monkeypatch, changes, message
):
    execute = MagicMock()
    monkeypatch.setattr(store.db, "execute", execute)
    decision = replace(correction_decision(), **changes)

    with pytest.raises(ValueError, match=message):
        store.append_correction(
            decision,
            "must not be stored",
            datetime(2026, 8, 3, 20, 0, tzinfo=UTC),
        )

    execute.assert_not_called()


def test_append_correction_accepts_zero_after_duration_for_verified_deletion(
    monkeypatch,
):
    execute = MagicMock()
    monkeypatch.setattr(store.db, "execute", execute)
    decision = replace(
        correction_decision(), action="delete_zero_regular", after_duration=0
    )

    store.append_correction(
        decision,
        "entry reread confirmed deletion",
        datetime(2026, 8, 3, 20, 0, tzinfo=UTC),
    )

    assert execute.call_args.args[1][6] == 0


@pytest.mark.parametrize(
    ("action", "after_duration"),
    [
        ("delete_zero_regular", 0.1),
        ("duration_update", 0.0),
        ("duration_update", -0.1),
    ],
)
def test_append_correction_rejects_action_duration_mismatches(
    monkeypatch, action, after_duration
):
    execute = MagicMock()
    monkeypatch.setattr(store.db, "execute", execute)

    with pytest.raises(ValueError, match="after duration"):
        store.append_correction(
            replace(
                correction_decision(),
                action=action,
                after_duration=after_duration,
            ),
            "must not be stored",
            datetime(2026, 8, 3, 20, 0, tzinfo=UTC),
        )

    execute.assert_not_called()


@pytest.mark.parametrize(
    "field",
    [
        "before_duration",
        "after_duration",
        "attendance_regular",
        "attendance_overtime",
        "work_regular",
        "work_overtime",
    ],
)
@pytest.mark.parametrize("invalid_value", [float("inf"), float("-inf"), float("nan")])
def test_append_correction_rejects_nonfinite_numeric_totals(
    monkeypatch, field, invalid_value
):
    execute = MagicMock()
    monkeypatch.setattr(store.db, "execute", execute)

    with pytest.raises(ValueError, match="finite"):
        store.append_correction(
            replace(correction_decision(), **{field: invalid_value}),
            "must not be stored",
            datetime(2026, 8, 3, 20, 0, tzinfo=UTC),
        )

    execute.assert_not_called()


@pytest.mark.parametrize("verification_detail", ["", "   ", "\n\t"])
def test_append_correction_rejects_blank_verification_detail(
    monkeypatch, verification_detail
):
    execute = MagicMock()
    monkeypatch.setattr(store.db, "execute", execute)

    with pytest.raises(ValueError, match="verification detail"):
        store.append_correction(
            correction_decision(),
            verification_detail,
            datetime(2026, 8, 3, 20, 0, tzinfo=UTC),
        )

    execute.assert_not_called()


def test_append_correction_rejects_naive_corrected_at(monkeypatch):
    execute = MagicMock()
    monkeypatch.setattr(store.db, "execute", execute)

    with pytest.raises(ValueError, match="timezone-aware"):
        store.append_correction(
            correction_decision(),
            "must not be stored",
            datetime(2026, 8, 3, 20, 0),
        )

    execute.assert_not_called()


def test_create_attempt_persists_complete_snapshot_before_returning(monkeypatch):
    attempt_id = UUID("1403d207-3a1d-4e50-a17e-d7fe87362fe8")
    now = datetime(2026, 8, 4, 12, 0, tzinfo=UTC)
    cursor = MagicMock()
    cursor.fetchone.return_value = attempt_row(attempt_id)

    @contextmanager
    def transaction():
        yield cursor

    monkeypatch.setattr(store.db, "cursor", transaction)

    created = store.create_attempt(attempt_id, correction_decision(), now)

    assert created.attempt_id == attempt_id
    assert created.decision == correction_decision()
    insert_sql, insert_params = cursor.execute.call_args_list[0].args
    assert "INSERT INTO payroll_work_entry_correction_attempts" in insert_sql
    assert "ON CONFLICT (attempt_id) DO NOTHING" in insert_sql
    assert insert_params == (
        str(attempt_id),
        8502,
        "duration_update",
        19,
        "Isidro Moctezuma Aviles",
        date(2026, 7, 24),
        3811,
        3.621388889,
        3.121355556,
        3.121355556,
        5.3092,
        3.621388889,
        5.309166667,
        "pending_correction",
        "correction intent saved",
        now,
        now,
    )
    select_sql, select_params = cursor.execute.call_args_list[1].args
    assert "WHERE attempt_id = %s" in select_sql
    assert select_params == (str(attempt_id),)


def test_create_attempt_is_idempotent_only_for_exact_same_snapshot(monkeypatch):
    attempt_id = uuid4()
    cursor = MagicMock()
    cursor.fetchone.return_value = attempt_row(attempt_id)

    @contextmanager
    def transaction():
        yield cursor

    monkeypatch.setattr(store.db, "cursor", transaction)

    assert store.create_attempt(
        attempt_id,
        correction_decision(),
        datetime(2026, 8, 4, 12, 0, tzinfo=UTC),
    ).decision == correction_decision()

    cursor.fetchone.return_value = attempt_row(attempt_id, after_duration=2.5)
    with pytest.raises(RuntimeError, match="snapshot mismatch"):
        store.create_attempt(
            attempt_id,
            correction_decision(),
            datetime(2026, 8, 4, 12, 0, tzinfo=UTC),
        )


@pytest.mark.parametrize(
    ("decision_changes", "now", "message"),
    [
        ({"kind": "review"}, datetime(2026, 8, 4, 12, 0, tzinfo=UTC), "correction"),
        ({"attendance_id": None}, datetime(2026, 8, 4, 12, 0, tzinfo=UTC), "Attendance"),
        (
            {"action": "delete_zero_regular", "after_duration": 0.1},
            datetime(2026, 8, 4, 12, 0, tzinfo=UTC),
            "after duration",
        ),
        (
            {"after_duration": float("nan")},
            datetime(2026, 8, 4, 12, 0, tzinfo=UTC),
            "finite",
        ),
        ({}, datetime(2026, 8, 4, 12, 0), "timezone-aware"),
    ],
)
def test_create_attempt_rejects_malformed_snapshot_before_db(
    monkeypatch, decision_changes, now, message
):
    cursor = MagicMock()
    monkeypatch.setattr(store.db, "cursor", cursor)

    with pytest.raises(ValueError, match=message):
        store.create_attempt(
            uuid4(), replace(correction_decision(), **decision_changes), now
        )

    cursor.assert_not_called()


def test_load_pending_attempts_returns_decisions_and_status(monkeypatch):
    attempt_id = uuid4()
    query = MagicMock(return_value=[attempt_row(attempt_id)])
    monkeypatch.setattr(store.db, "query", query)

    attempts = store.load_pending_attempts()

    assert len(attempts) == 1
    assert attempts[0].attempt_id == attempt_id
    assert attempts[0].decision == correction_decision()
    assert attempts[0].last_reason == "pending_correction"
    assert attempts[0].last_detail == "correction intent saved"
    assert "ORDER BY created_at, attempt_id" in query.call_args.args[0]


def test_load_pending_attempts_fails_closed_on_malformed_snapshot(monkeypatch):
    monkeypatch.setattr(
        store.db,
        "query",
        MagicMock(return_value=[attempt_row(after_duration=float("nan"))]),
    )

    with pytest.raises(ValueError, match="finite"):
        store.load_pending_attempts()


def test_mark_attempt_issue_updates_only_pending_status(monkeypatch):
    attempt_id = uuid4()
    now = datetime(2026, 8, 4, 12, 5, tzinfo=UTC)
    execute = MagicMock()
    monkeypatch.setattr(store.db, "execute", execute)

    store.mark_attempt_issue(
        attempt_id,
        "verification_failed",
        "fresh read failed after write",
        now,
    )

    sql, params = execute.call_args.args
    assert "UPDATE payroll_work_entry_correction_attempts" in sql
    assert "last_reason = %s, last_detail = %s, updated_at = %s" in sql
    assert "WHERE attempt_id = %s" in sql
    assert params == (
        "verification_failed",
        "fresh read failed after write",
        now,
        str(attempt_id),
    )


@pytest.mark.parametrize(
    ("reason", "detail", "now", "message"),
    [
        ("", "detail", datetime(2026, 8, 4, 12, 0, tzinfo=UTC), "reason"),
        ("write_failed", "", datetime(2026, 8, 4, 12, 0, tzinfo=UTC), "detail"),
        ("write_failed", "detail", datetime(2026, 8, 4, 12, 0), "timezone-aware"),
    ],
)
def test_mark_attempt_issue_rejects_invalid_status(
    monkeypatch, reason, detail, now, message
):
    execute = MagicMock()
    monkeypatch.setattr(store.db, "execute", execute)

    with pytest.raises(ValueError, match=message):
        store.mark_attempt_issue(uuid4(), reason, detail, now)

    execute.assert_not_called()


def test_finalize_attempt_locks_inserts_once_and_deletes_in_one_transaction(
    monkeypatch,
):
    attempt_id = uuid4()
    now = datetime(2026, 8, 4, 12, 10, tzinfo=UTC)
    cursor = MagicMock()
    cursor.fetchone.return_value = attempt_row(attempt_id)
    events = []

    @contextmanager
    def transaction():
        events.append("transaction-enter")
        try:
            yield cursor
        finally:
            events.append("transaction-exit")

    monkeypatch.setattr(store.db, "cursor", transaction)

    assert store.finalize_attempt(attempt_id, "duration reread matched", now) is True

    assert events == ["transaction-enter", "transaction-exit"]
    assert len(cursor.execute.call_args_list) == 3
    select_sql, select_params = cursor.execute.call_args_list[0].args
    assert "FROM payroll_work_entry_correction_attempts" in select_sql
    assert "WHERE attempt_id = %s FOR UPDATE" in select_sql
    assert select_params == (str(attempt_id),)
    insert_sql, insert_params = cursor.execute.call_args_list[1].args
    assert "INSERT INTO payroll_work_entry_corrections" in insert_sql
    assert "attempt_id" in insert_sql
    assert "ON CONFLICT (attempt_id) WHERE attempt_id IS NOT NULL DO NOTHING" in insert_sql
    assert insert_params[0] == str(attempt_id)
    assert insert_params[-2:] == ("duration reread matched", now)
    delete_sql, delete_params = cursor.execute.call_args_list[2].args
    assert "DELETE FROM payroll_work_entry_correction_attempts" in delete_sql
    assert delete_params == (str(attempt_id),)


def test_finalize_attempt_missing_pending_row_is_idempotent(monkeypatch):
    cursor = MagicMock()
    cursor.fetchone.return_value = None

    @contextmanager
    def transaction():
        yield cursor

    monkeypatch.setattr(store.db, "cursor", transaction)

    assert store.finalize_attempt(
        uuid4(),
        "already committed",
        datetime(2026, 8, 4, 12, 0, tzinfo=UTC),
    ) is False
    assert len(cursor.execute.call_args_list) == 1


def test_load_monitor_state_uses_singleton_and_defaults_when_absent(monkeypatch):
    query = MagicMock(return_value=[])
    monkeypatch.setattr(store.db, "query", query)

    assert store.load_monitor_state() == {
        "odoo_task_id": None,
        "reported_issue_keys": [],
    }
    assert "WHERE id = 1" in query.call_args.args[0]


def test_load_monitor_state_returns_the_persisted_values(monkeypatch):
    monkeypatch.setattr(
        store.db,
        "query",
        lambda *_: [{"odoo_task_id": 44, "reported_issue_keys": None}],
    )

    assert store.load_monitor_state() == {
        "odoo_task_id": 44,
        "reported_issue_keys": [],
    }


def test_save_monitor_state_upserts_sorted_deduplicated_issue_keys(monkeypatch):
    execute = MagicMock()
    monkeypatch.setattr(store.db, "execute", execute)
    now = datetime(2026, 8, 3, 20, 0, tzinfo=UTC)

    store.save_monitor_state(
        44,
        ["9:2026-07-24:z", "9:2026-07-24:a", "9:2026-07-24:z"],
        now,
    )

    sql, params = execute.call_args.args
    normalized_sql = " ".join(sql.split())
    assert (
        "INSERT INTO payroll_work_entry_guard_monitor "
        "(id, odoo_task_id, reported_issue_keys, updated_at) VALUES (1, %s, %s, %s) "
        "ON CONFLICT (id) DO UPDATE SET "
        "odoo_task_id = EXCLUDED.odoo_task_id, "
        "reported_issue_keys = EXCLUDED.reported_issue_keys, "
        "updated_at = EXCLUDED.updated_at"
        in normalized_sql
    )
    assert params == (44, ["9:2026-07-24:a", "9:2026-07-24:z"], now)


def test_monitor_lock_holds_transaction_through_callers_context(monkeypatch):
    events = []
    cursor = MagicMock()

    @contextmanager
    def transaction():
        events.append("transaction acquired")
        try:
            yield cursor
        finally:
            events.append("transaction released")

    monkeypatch.setattr(store.db, "cursor", transaction)

    with store.monitor_lock():
        events.append("caller lifecycle")
        cursor.execute.assert_called_once_with(
            "SELECT pg_advisory_xact_lock(%s::bigint)",
            (store.MONITOR_LOCK_KEY,),
        )

    assert events == [
        "transaction acquired",
        "caller lifecycle",
        "transaction released",
    ]
    assert -(2**63) <= store.MONITOR_LOCK_KEY < 2**63


def test_monitor_lock_releases_transaction_when_caller_raises(monkeypatch):
    events = []

    @contextmanager
    def transaction():
        events.append("transaction acquired")
        try:
            yield MagicMock()
        finally:
            events.append("transaction released")

    monkeypatch.setattr(store.db, "cursor", transaction)

    with pytest.raises(RuntimeError, match="Odoo failed"):
        with store.monitor_lock():
            raise RuntimeError("Odoo failed")

    assert events == ["transaction acquired", "transaction released"]


def test_guard_lock_uses_a_distinct_stable_signed_bigint(monkeypatch):
    events = []
    cursor = MagicMock()

    @contextmanager
    def transaction():
        events.append("guard transaction acquired")
        try:
            yield cursor
        finally:
            events.append("guard transaction released")

    monkeypatch.setattr(store.db, "cursor", transaction)

    with store.guard_lock():
        events.append("caller lifecycle")

    cursor.execute.assert_called_once_with(
        "SELECT pg_advisory_xact_lock(%s::bigint)",
        (store.GUARD_LOCK_KEY,),
    )
    assert store.GUARD_LOCK_KEY != store.MONITOR_LOCK_KEY
    assert -(2**63) <= store.GUARD_LOCK_KEY < 2**63
    assert events == [
        "guard transaction acquired",
        "caller lifecycle",
        "guard transaction released",
    ]


def test_guard_lock_releases_transaction_when_caller_raises(monkeypatch):
    events = []

    @contextmanager
    def transaction():
        events.append("guard transaction acquired")
        try:
            yield MagicMock()
        finally:
            events.append("guard transaction released")

    monkeypatch.setattr(store.db, "cursor", transaction)

    with pytest.raises(RuntimeError, match="guard failed"):
        with store.guard_lock():
            raise RuntimeError("guard failed")

    assert events == ["guard transaction acquired", "guard transaction released"]


@pytest.mark.parametrize(
    ("database_url", "expected"),
    [
        ("postgresql" + "://" + "localhost/test", True),
        ("postgresql" + "://" + "127.0.0.1:5432/test", True),
        ("postgresql" + "://" + "[::1]:5432/test", True),
        ("postgresql" + "://" + "localhost.example.com/test", False),
        ("postgresql" + "://" + "containers-us-west.railway.app/test", False),
        ("postgresql" + ":///" + "test", False),
        (None, False),
    ],
)
def test_database_integration_guard_requires_explicit_loopback_hostname(
    database_url, expected
):
    assert _database_url_is_loopback(database_url) is expected


@pytest.mark.parametrize("opt_in", [None, "", "0", "true", "yes", " 1 "])
def test_database_integration_guard_requires_exact_opt_in(opt_in):
    assert (
        _database_integration_is_safe(
            "postgresql" + "://" + "localhost/gpi_test",
            opt_in,
        )
        is False
    )


@pytest.mark.parametrize(
    "database_url",
    [
        None,
        "",
        "postgresql" + ":///" + "gpi_test",
        "postgresql" + "://" + "localhost.example.com/gpi_test",
        "postgresql" + "://" + "containers-us-west.railway.app/gpi_test",
    ],
)
def test_database_integration_guard_rejects_missing_or_nonloopback_host(database_url):
    assert _database_integration_is_safe(database_url, "1") is False


@pytest.mark.parametrize(
    "database_url",
    [
        "postgresql" + "://" + "localhost/gpi_test?host=railway.example.com",
        "postgresql" + "://" + "localhost/gpi_test?host=localhost,railway.example.com",
        "postgresql" + "://" + "localhost,railway.example.com/gpi_test",
    ],
)
def test_database_integration_guard_rejects_effective_remote_or_multi_host(
    database_url,
):
    assert _database_integration_is_safe(database_url, "1") is False


def test_database_integration_guard_rejects_effective_database_name_override():
    database_url = "postgresql" + "://" + "localhost/gpi_test?dbname=postgres"

    assert _database_integration_is_safe(database_url, "1") is False


@pytest.mark.parametrize(
    "hostaddr",
    ["203.0.113.9", "127.0.0.1"],
)
def test_database_integration_guard_rejects_any_hostaddr(hostaddr):
    database_url = (
        "postgresql" + "://" + f"localhost/gpi_test?hostaddr={hostaddr}"
    )

    assert _database_integration_is_safe(database_url, "1") is False


@pytest.mark.parametrize(
    "database_url",
    [
        "service=production",
        "postgresql" + "://" + "localhost/gpi_test?service=production",
        "servicefile=/tmp/pg_service.conf host=localhost dbname=gpi_test",
        "postgresql"
        + "://"
        + "localhost/gpi_test?servicefile=/tmp/pg_service.conf",
    ],
)
def test_database_integration_guard_rejects_service_configuration(database_url):
    assert _database_integration_is_safe(database_url, "1") is False


@pytest.mark.parametrize(
    "database_url",
    [
        "not a postgres dsn",
        "postgresql" + "://" + "[::1/gpi_test",
        "postgresql" + "://" + "localhost/%ZZ_test",
    ],
)
def test_database_integration_guard_rejects_malformed_dsn(database_url):
    assert _database_integration_is_safe(database_url, "1") is False


@pytest.mark.parametrize(
    "database_url",
    [
        "postgresql" + "://" + "localhost/postgres",
        "postgresql" + "://" + "127.0.0.1/gpi_test_copy",
        "postgresql" + "://" + "[::1]/gpi_test/extra",
    ],
)
def test_database_integration_guard_requires_test_database_suffix(database_url):
    assert _database_integration_is_safe(database_url, "1") is False


@pytest.mark.parametrize(
    "database_url",
    [
        "postgresql" + "://" + "localhost/gpi_test",
        "postgresql" + "://" + "127.0.0.1:5432/payroll_guard_test",
        "postgresql" + "://" + "[::1]:5432/gpi_test",
    ],
)
def test_database_integration_guard_accepts_opted_in_loopback_test_database(database_url):
    assert _database_integration_is_safe(database_url, "1") is True


def _assert_postgres_rejects(cur, sql, params, expected_message):
    cur.execute("SAVEPOINT expected_rejection")
    with pytest.raises(psycopg2.Error, match=expected_message):
        cur.execute(sql, params)
    cur.execute("ROLLBACK TO SAVEPOINT expected_rejection")
    cur.execute("RELEASE SAVEPOINT expected_rejection")


@pytest.mark.skipif(
    not SAFE_TEST_DATABASE,
    reason=(
        "requires PAYROLL_GUARD_TEST_DATABASE=1 and a loopback DATABASE_URL "
        "whose database name ends in _test"
    ),
)
def test_local_postgres_schema_and_store_guarantees(monkeypatch):
    """Exercise only an explicitly opted-in loopback test DB; all data is rolled back."""
    from zira_dashboard import db

    class RollBackIntegrationData(Exception):
        pass

    db.shutdown_pool()
    try:
        db.init_pool(minconn=1, maxconn=2)
        db.bootstrap_schema()
        db.bootstrap_schema()

        with pytest.raises(RollBackIntegrationData):
            with db.cursor() as cur:
                cur.execute(
                    "SELECT conname FROM pg_constraint "
                    "WHERE conrelid IN ("
                    "'payroll_work_entry_corrections'::regclass, "
                    "'payroll_work_entry_correction_attempts'::regclass, "
                    "'payroll_work_entry_guard_monitor'::regclass)"
                )
                constraint_names = {row["conname"] for row in cur.fetchall()}
                assert {
                    "payroll_work_entry_corrections_action_duration_check",
                    "payroll_work_entry_corrections_finite_totals_check",
                    "payroll_work_entry_corrections_verification_detail_check",
                    "payroll_work_entry_correction_attempts_entry_unique",
                    "payroll_work_entry_correction_attempts_action_duration_check",
                    "payroll_work_entry_correction_attempts_finite_totals_check",
                    "payroll_work_entry_correction_attempts_reason_check",
                    "payroll_work_entry_correction_attempts_detail_check",
                    "payroll_work_entry_guard_monitor_id_check",
                }.issubset(constraint_names)

                cur.execute(
                    "SELECT indexname FROM pg_indexes "
                    "WHERE schemaname = 'public' "
                    "AND tablename = 'payroll_work_entry_corrections'"
                )
                index_names = {row["indexname"] for row in cur.fetchall()}
                assert "payroll_work_entry_corrections_entry_idx" in index_names
                assert "payroll_work_entry_corrections_attempt_idx" in index_names

                cur.execute(
                    "SELECT tgname FROM pg_trigger "
                    "WHERE tgrelid = 'payroll_work_entry_corrections'::regclass "
                    "AND NOT tgisinternal"
                )
                assert {row["tgname"] for row in cur.fetchall()} == {
                    "payroll_work_entry_corrections_append_only",
                    "payroll_work_entry_corrections_reject_truncate",
                }

                def execute_in_transaction(sql, params=None):
                    cur.execute(sql, params)

                def query_in_transaction(sql, params=None):
                    cur.execute(sql, params)
                    return list(cur.fetchall())

                @contextmanager
                def cursor_in_transaction():
                    yield cur

                with monkeypatch.context() as transaction_patch:
                    transaction_patch.setattr(store.db, "execute", execute_in_transaction)
                    transaction_patch.setattr(store.db, "query", query_in_transaction)
                    transaction_patch.setattr(store.db, "cursor", cursor_in_transaction)
                    now = datetime(2026, 8, 4, 12, 0, tzinfo=UTC)
                    store.append_correction(
                        correction_decision(), "local integration round trip", now
                    )
                    cur.execute(
                        "SELECT action, after_duration, verification_detail, corrected_at "
                        "FROM payroll_work_entry_corrections "
                        "WHERE odoo_work_entry_id = %s ORDER BY id DESC LIMIT 1",
                        (8502,),
                    )
                    inserted = cur.fetchone()
                    assert inserted["action"] == "duration_update"
                    assert inserted["after_duration"] == pytest.approx(3.121355556)
                    assert inserted["verification_detail"] == "local integration round trip"
                    assert inserted["corrected_at"] == now

                    store.save_monitor_state(44, ["z", "a", "z"], now)
                    store.save_monitor_state(45, ["b"], now)
                    assert store.load_monitor_state() == {
                        "odoo_task_id": 45,
                        "reported_issue_keys": ["b"],
                    }

                    attempt_id = UUID("73d06909-e287-4ff7-84d8-c92a6398640a")
                    created = store.create_attempt(
                        attempt_id, correction_decision(), now
                    )
                    assert created.attempt_id == attempt_id
                    cur.execute("SAVEPOINT unique_pending_attempt")
                    with pytest.raises(psycopg2.errors.UniqueViolation):
                        store.create_attempt(
                            uuid4(), correction_decision(), now
                        )
                    cur.execute("ROLLBACK TO SAVEPOINT unique_pending_attempt")
                    cur.execute("RELEASE SAVEPOINT unique_pending_attempt")
                    assert store.finalize_attempt(
                        attempt_id, "attempt integration verified", now
                    )
                    assert not store.finalize_attempt(
                        attempt_id, "already finalized", now
                    )
                    cur.execute(
                        "SELECT count(*) AS count FROM payroll_work_entry_corrections "
                        "WHERE attempt_id = %s",
                        (str(attempt_id),),
                    )
                    assert cur.fetchone()["count"] == 1
                    cur.execute(
                        "SELECT count(*) AS count "
                        "FROM payroll_work_entry_correction_attempts "
                        "WHERE attempt_id = %s",
                        (str(attempt_id),),
                    )
                    assert cur.fetchone()["count"] == 0

                correction_insert = (
                    "INSERT INTO payroll_work_entry_corrections "
                    "(odoo_work_entry_id, action, employee_odoo_id, employee_name, "
                    "work_date, before_duration, after_duration, attendance_regular, "
                    "attendance_overtime, work_regular_before, work_overtime, "
                    "verification_detail, corrected_at) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
                )
                valid_params = (
                    9502,
                    "duration_update",
                    19,
                    "Local Test",
                    date(2026, 8, 4),
                    1.5,
                    1.0,
                    1.0,
                    0.5,
                    1.5,
                    0.5,
                    "verified",
                    datetime(2026, 8, 4, 12, 0, tzinfo=UTC),
                )
                _assert_postgres_rejects(
                    cur,
                    correction_insert,
                    valid_params[:1]
                    + ("delete_zero_regular",)
                    + valid_params[2:6]
                    + (1.0,)
                    + valid_params[7:],
                    "action_duration_check",
                )
                _assert_postgres_rejects(
                    cur,
                    correction_insert,
                    valid_params[:5] + (float("inf"),) + valid_params[6:],
                    "finite_totals_check",
                )
                _assert_postgres_rejects(
                    cur,
                    "INSERT INTO payroll_work_entry_guard_monitor (id) VALUES (2)",
                    None,
                    "payroll_work_entry_guard_monitor_id_check",
                )
                _assert_postgres_rejects(
                    cur,
                    "UPDATE payroll_work_entry_corrections SET employee_name = %s "
                    "WHERE verification_detail = %s",
                    ("Changed", "local integration round trip"),
                    "append-only",
                )
                _assert_postgres_rejects(
                    cur,
                    "DELETE FROM payroll_work_entry_corrections "
                    "WHERE verification_detail = %s",
                    ("local integration round trip",),
                    "append-only",
                )
                _assert_postgres_rejects(
                    cur,
                    "TRUNCATE payroll_work_entry_corrections",
                    None,
                    "append-only",
                )

                raise RollBackIntegrationData
    finally:
        db.shutdown_pool()
