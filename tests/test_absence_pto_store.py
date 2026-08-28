import os
from contextlib import contextmanager, nullcontext
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from uuid import UUID

import psycopg2
import pytest

from zira_dashboard._schema import SCHEMA_DDL
import zira_dashboard.absence_pto_store as store


NOW = datetime(2026, 8, 28, 14, 30, tzinfo=UTC)
OWNER = UUID("9f9de42e-1320-4da2-9fc7-e2f8b122a223")
_LOOPBACK_DATABASE_HOSTS = {"localhost", "127.0.0.1", "::1"}
_UNSAFE_DATABASE_DSN_OPTIONS = {"hostaddr", "service", "servicefile"}


def _database_integration_is_safe(
    database_url: str | None,
    explicit_opt_in: str | None,
) -> bool:
    if explicit_opt_in != "1" or not database_url:
        return False
    try:
        params = psycopg2.extensions.parse_dsn(database_url)
    except (TypeError, psycopg2.Error):
        return False
    if _UNSAFE_DATABASE_DSN_OPTIONS.intersection(params):
        return False
    return (
        params.get("host") in _LOOPBACK_DATABASE_HOSTS
        and params.get("dbname", "").endswith("_test")
    )


SAFE_TEST_DATABASE = _database_integration_is_safe(
    os.environ.get("DATABASE_URL"),
    os.environ.get("ABSENCE_PTO_TEST_DATABASE"),
)


def _row(**changes):
    row = {
        "id": 41,
        "absence_day": date(2026, 8, 20),
        "emp_id": "0044",
        "person_odoo_id": 44,
        "person_name": "Maria Example",
        "holiday_status_id": 7,
        "leave_type_name": "Paid Time Off",
        "balance_at_submit": Decimal("32.50"),
        "original_absence_leave_id": 70,
        "pto_leave_id": None,
        "state": "pending",
        "conversion_step": "not_started",
        "employee_note": "Please use PTO.",
        "denial_reason": None,
        "manual_resolution_note": None,
        "sync_error": None,
        "odoo_task_id": None,
        "task_attempts": 0,
        "task_next_at": None,
        "task_resolution_step": "none",
        "task_resolution_attempts": 0,
        "task_resolution_next_at": None,
        "task_resolution_error": None,
        "lease_owner": None,
        "lease_until": None,
        "requested_by_person_id": 14,
        "decided_by_upn": None,
        "decided_by_name": None,
        "requested_at": NOW,
        "decided_at": None,
        "resolved_at": None,
        "created_at": NOW,
        "updated_at": NOW,
    }
    row.update(changes)
    return row


def test_schema_has_linked_request_constraints_and_future_safe_columns():
    assert "CREATE TABLE IF NOT EXISTS absence_pto_requests" in SCHEMA_DDL
    assert "absence_pto_requests_state_check" in SCHEMA_DDL
    assert "absence_pto_requests_step_check" in SCHEMA_DDL
    assert "absence_pto_requests_active_uniq" in SCHEMA_DDL
    assert "lease_owner UUID" in SCHEMA_DDL
    assert "absence_pto_requests_pto_leave_uniq" in SCHEMA_DDL
    assert "absence_pto_requests_due_idx" in SCHEMA_DDL
    assert "absence_pto_requests_resolution_step_check" in SCHEMA_DDL
    assert "absence_pto_requests_resolution_due_idx" in SCHEMA_DDL
    assert "ALTER TABLE absence_pto_requests" in SCHEMA_DDL
    assert "id BIGSERIAL PRIMARY KEY" in SCHEMA_DDL
    assert "ADD COLUMN IF NOT EXISTS id BIGINT;" in SCHEMA_DDL
    alter_path = SCHEMA_DDL.split(
        "-- Keep bootstrap parity if an earlier deployment created only part of the",
        1,
    )[1]
    assert "ADD COLUMN IF NOT EXISTS id BIGSERIAL" not in alter_path
    assert "ADD COLUMN IF NOT EXISTS lease_owner UUID" in SCHEMA_DDL
    assert "absence_pto_requests_balance_check" in SCHEMA_DDL
    assert "absence_pto_requests_id_seq" in SCHEMA_DDL
    assert "ALTER COLUMN absence_day SET NOT NULL" in SCHEMA_DDL
    assert "ALTER COLUMN updated_at SET NOT NULL" in SCHEMA_DDL
    assert "FROM pg_constraint" in SCHEMA_DDL
    table_ddl = SCHEMA_DDL.split("CREATE TABLE IF NOT EXISTS absence_pto_requests", 1)[1]
    assert "REFERENCES manual_absences" not in table_ddl


def test_row_maps_every_public_field_and_normalizes_uuid_string():
    lease_until = datetime(2026, 8, 28, 14, 32, tzinfo=UTC)
    request = store._request_from_row(
        _row(lease_owner=str(OWNER), lease_until=lease_until)
    )
    assert request == store.AbsencePtoRequest(
        id=41,
        absence_day=date(2026, 8, 20),
        emp_id="0044",
        person_odoo_id=44,
        person_name="Maria Example",
        holiday_status_id=7,
        leave_type_name="Paid Time Off",
        balance_at_submit=Decimal("32.50"),
        original_absence_leave_id=70,
        pto_leave_id=None,
        state="pending",
        conversion_step="not_started",
        employee_note="Please use PTO.",
        denial_reason=None,
        manual_resolution_note=None,
        sync_error=None,
        odoo_task_id=None,
        task_attempts=0,
        task_next_at=None,
        lease_owner=OWNER,
        lease_until=lease_until,
        requested_by_person_id=14,
        decided_by_upn=None,
        decided_by_name=None,
        requested_at=NOW,
        decided_at=None,
        resolved_at=None,
        created_at=NOW,
        updated_at=NOW,
    )


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"state": "lost"}, "state"),
        ({"conversion_step": "created"}, "conversion_step"),
        ({"balance_at_submit": Decimal("-1")}, "balance_at_submit"),
        ({"requested_at": datetime(2026, 8, 28, 14, 30)}, "requested_at"),
        ({"lease_owner": "not-a-uuid"}, "lease_owner"),
        ({"task_attempts": -1}, "task_attempts"),
    ],
)
def test_row_mapping_fails_closed_on_malformed_data(changes, message):
    with pytest.raises(ValueError, match=message):
        store._request_from_row(_row(**changes))


def test_create_request_inserts_a_snapshot_and_returns_typed_row(monkeypatch):
    seen = {}
    monkeypatch.setattr(
        store.db,
        "query",
        lambda sql, params: seen.update(sql=sql, params=params) or [_row()],
    )
    result = store.create_request(
        absence_day=date(2026, 8, 20),
        emp_id="0044",
        person_odoo_id=44,
        person_name="Maria Example",
        holiday_status_id=7,
        leave_type_name="Paid Time Off",
        balance_at_submit=Decimal("32.50"),
        original_absence_leave_id=70,
        employee_note="Please use PTO.",
        requested_by_person_id=14,
        now=NOW,
    )
    assert result.id == 41
    assert "INSERT INTO absence_pto_requests" in seen["sql"]
    assert "RETURNING" in seen["sql"]
    assert seen["params"][-1] == NOW


def test_load_and_list_queries_have_stable_scope_and_order(monkeypatch):
    calls = []
    monkeypatch.setattr(
        store.db,
        "query",
        lambda sql, params=None: calls.append((sql, params)) or [_row()],
    )
    assert store.get_request(41).id == 41
    assert store.list_for_person("0044")[0].emp_id == "0044"
    assert store.list_pending()[0].state == "pending"
    assert "WHERE id = %s" in calls[0][0]
    assert "WHERE emp_id = %s" in calls[1][0]
    assert "ORDER BY requested_at DESC, id DESC" in calls[1][0]
    assert "WHERE state = 'pending'" in calls[2][0]
    assert "ORDER BY requested_at, id" in calls[2][0]


def test_missing_request_returns_none(monkeypatch):
    monkeypatch.setattr(store.db, "query", lambda sql, params: [])
    assert store.get_request(404) is None


def test_claim_is_atomic_and_lease_bounded(monkeypatch):
    seen = {}
    monkeypatch.setattr(
        store.db,
        "query",
        lambda sql, params: seen.update(sql=sql, params=params)
        or [_row(lease_owner=OWNER, lease_until=datetime(2026, 8, 28, 14, 32, tzinfo=UTC))],
    )
    claim = store.claim_request(41, OWNER, NOW, lease_seconds=120)
    assert claim and claim.lease_owner == OWNER
    assert "SELECT pg_advisory_xact_lock(%s::bigint)" in seen["sql"]
    assert "FOR UPDATE" in seen["sql"]
    assert "lease_until <=" in seen["sql"]
    assert "lease_owner = %s" in seen["sql"]
    assert seen["sql"].rstrip().endswith(store.QUALIFIED_REQUEST_COLUMNS)
    assert "request.id AS id" in seen["sql"]
    assert "request.updated_at AS updated_at" in seen["sql"]


def test_claim_returns_none_when_an_unexpired_other_owner_holds_it(monkeypatch):
    monkeypatch.setattr(store.db, "query", lambda sql, params: [])
    assert store.claim_request(41, OWNER, NOW) is None


def test_renew_requires_current_unexpired_owner(monkeypatch):
    calls = []
    monkeypatch.setattr(
        store.db,
        "query",
        lambda sql, params: calls.append((sql, params))
        or [_row(lease_owner=OWNER, lease_until=datetime(2026, 8, 28, 14, 32, tzinfo=UTC))],
    )
    renewed = store.renew_claim(41, OWNER, NOW, lease_seconds=120)
    assert renewed.lease_owner == OWNER
    assert "lease_owner = %s" in calls[0][0]
    assert "lease_until > %s" in calls[0][0]
    assert "RETURNING" in calls[0][0]


def test_stale_renewal_fails_closed(monkeypatch):
    monkeypatch.setattr(store.db, "query", lambda sql, params: [])
    with pytest.raises(store.StaleTransition):
        store.renew_claim(41, OWNER, NOW)


def test_transition_requires_current_owner_and_expected_step(monkeypatch):
    calls = []
    monkeypatch.setattr(
        store.db,
        "query",
        lambda sql, params: calls.append((sql, params))
        or [_row(state="converting", conversion_step="absence_refused")],
    )
    out = store.transition(
        41,
        OWNER,
        expected_state="converting",
        expected_step="not_started",
        new_state="converting",
        new_step="absence_refused",
    )
    assert out.conversion_step == "absence_refused"
    assert "lease_owner = %s" in calls[0][0]
    assert "lease_until > %s" in calls[0][0]
    assert "state = %s" in calls[0][0]
    assert "conversion_step = %s" in calls[0][0]


def test_transition_can_save_known_remote_identity_and_decision_snapshot(monkeypatch):
    calls = []
    monkeypatch.setattr(
        store.db,
        "query",
        lambda sql, params: calls.append((sql, params))
        or [_row(
            state="converting",
            conversion_step="pto_created",
            pto_leave_id=71,
            decided_by_upn="manager@example.com",
            decided_by_name="Manager",
            decided_at=NOW,
        )],
    )
    result = store.transition(
        41,
        OWNER,
        expected_state="converting",
        expected_step="absence_refused",
        new_state="converting",
        new_step="pto_created",
        pto_leave_id=71,
        decided_by_upn="manager@example.com",
        decided_by_name="Manager",
        decided_at=NOW,
        now=NOW,
    )
    assert result.pto_leave_id == 71
    assert "pto_leave_id = %s" in calls[0][0]
    assert "decided_by_upn = %s" in calls[0][0]


def test_stale_transition_fails_closed(monkeypatch):
    monkeypatch.setattr(store.db, "query", lambda sql, params: [])
    with pytest.raises(store.StaleTransition):
        store.transition(
            41,
            OWNER,
            expected_state="converting",
            expected_step="not_started",
            new_state="converting",
            new_step="absence_refused",
            now=NOW,
        )


def test_release_requires_owner_and_does_not_clear_a_newer_lease(monkeypatch):
    calls = []
    monkeypatch.setattr(
        store.db,
        "query",
        lambda sql, params: calls.append((sql, params)) or [{"id": 41}],
    )
    assert store.release_claim(41, OWNER, now=NOW) is True
    assert "lease_owner = %s" in calls[0][0]
    assert "lease_until > %s" in calls[0][0]
    assert "lease_owner = NULL" in calls[0][0]


def test_mark_needs_review_and_task_delivery_keep_owner_guard(monkeypatch):
    calls = []
    monkeypatch.setattr(
        store.db,
        "query",
        lambda sql, params: calls.append((sql, params))
        or [_row(state="needs_review", sync_error="Needs a person")],
    )
    reviewed = store.mark_needs_review(41, OWNER, error="Needs a person", now=NOW)
    delivered = store.save_task_delivery(
        41,
        OWNER,
        task_id=501,
        attempts=1,
        next_at=NOW,
        error=None,
        now=NOW,
    )
    assert reviewed.state == "needs_review"
    assert delivered.id == 41
    assert all("lease_owner = %s" in sql for sql, _ in calls)
    assert "state = 'needs_review'" in calls[0][0]
    assert "odoo_task_id = %s" in calls[1][0]
    assert "task_attempts = %s" in calls[1][0]


def test_resolution_delivery_checkpoint_is_terminal_state_step_and_lease_guarded(
    monkeypatch,
):
    calls = []
    monkeypatch.setattr(
        store.db,
        "query",
        lambda sql, params: calls.append((sql, params))
        or [
            _row(
                state="approved",
                task_resolution_step="message_posted",
                task_resolution_attempts=0,
            )
        ],
    )

    saved = store.save_resolution_delivery(
        41,
        OWNER,
        expected_step="none",
        new_step="message_posted",
        attempts=0,
        next_at=NOW,
        error=None,
        now=NOW,
    )

    assert saved.task_resolution_step == "message_posted"
    sql = calls[0][0]
    assert "state IN ('approved', 'resolved_manually')" in sql
    assert "lease_owner = %s" in sql
    assert "lease_until > %s" in sql
    assert "task_resolution_step = %s" in sql


def test_due_query_is_bounded_and_only_returns_expired_or_due_work(monkeypatch):
    seen = {}
    monkeypatch.setattr(
        store.db,
        "query",
        lambda sql, params: seen.update(sql=sql, params=params) or [_row()],
    )
    assert store.list_due(NOW, limit=25)[0].id == 41
    assert "lease_until IS NULL OR lease_until <= %s" in seen["sql"]
    assert "task_next_at IS NULL OR task_next_at <= %s" in seen["sql"]
    assert "task_resolution_next_at IS NULL" in seen["sql"]
    assert "'resolved_manually'" in seen["sql"]
    assert "ORDER BY" in seen["sql"]
    assert "LIMIT %s" in seen["sql"]
    assert seen["params"][-1] == 25


@pytest.mark.parametrize(
    "database_url",
    [
        "postgresql://localhost/gpi_test",
        "postgresql://127.0.0.1:5432/absence_pto_test",
        "postgresql://[::1]:5432/gpi_test",
    ],
)
def test_database_guard_accepts_only_opted_in_loopback_test_database(database_url):
    assert _database_integration_is_safe(database_url, "1") is True
    assert _database_integration_is_safe(database_url, "0") is False


@pytest.mark.parametrize(
    "database_url",
    [
        "postgresql://db.example.com/gpi_test",
        "postgresql://localhost/gpi",
        "host=localhost dbname=gpi_test hostaddr=127.0.0.1",
        "service=production dbname=gpi_test host=localhost",
    ],
)
def test_database_guard_rejects_unsafe_database(database_url):
    assert _database_integration_is_safe(database_url, "1") is False


@pytest.mark.skipif(
    not SAFE_TEST_DATABASE,
    reason=(
        "requires ABSENCE_PTO_TEST_DATABASE=1 and a loopback DATABASE_URL "
        "whose database name ends in _test"
    ),
)
def test_live_postgres_claims_successfully_and_enforces_active_uniqueness(monkeypatch):
    from zira_dashboard import db

    class RollBackIntegrationData(Exception):
        pass

    db.shutdown_pool()
    try:
        db.init_pool(minconn=1, maxconn=2)
        db.bootstrap_schema()
        db.bootstrap_schema()
        marker = datetime.now(tz=UTC).strftime("%Y%m%d%H%M%S%f")
        emp_id = f"pytest-absence-pto-{marker}"
        with pytest.raises(RollBackIntegrationData):
            with db.cursor() as cur:
                params = (
                    date(2099, 1, 4),
                    emp_id,
                    44,
                    "Pytest Worker",
                    7,
                    "Paid Time Off",
                    Decimal("8"),
                )
                insert_sql = """
                    INSERT INTO absence_pto_requests (
                        absence_day, emp_id, person_odoo_id, person_name,
                        holiday_status_id, leave_type_name, balance_at_submit
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s)
                    RETURNING id
                """
                cur.execute(insert_sql, params)
                first_id = cur.fetchone()["id"]
                def query_in_transaction(sql, query_params=None):
                    cur.execute(sql, query_params)
                    return list(cur.fetchall())

                with monkeypatch.context() as transaction_patch:
                    transaction_patch.setattr(store.db, "query", query_in_transaction)
                    claimed = store.claim_request(first_id, OWNER, NOW)
                assert claimed is not None
                assert claimed.id == first_id
                assert claimed.lease_owner == OWNER
                cur.execute("SAVEPOINT active_duplicate")
                with pytest.raises(psycopg2.errors.UniqueViolation):
                    cur.execute(insert_sql, params)
                cur.execute("ROLLBACK TO SAVEPOINT active_duplicate")
                cur.execute("RELEASE SAVEPOINT active_duplicate")
                cur.execute(
                    "UPDATE absence_pto_requests SET state = 'denied' WHERE id = %s",
                    (first_id,),
                )
                cur.execute(insert_sql, params)
                assert cur.fetchone()["id"] != first_id
                raise RollBackIntegrationData
    finally:
        db.shutdown_pool()


@pytest.mark.skipif(
    not SAFE_TEST_DATABASE,
    reason=(
        "requires ABSENCE_PTO_TEST_DATABASE=1 and a loopback DATABASE_URL "
        "whose database name ends in _test"
    ),
)
def test_live_postgres_terminal_claim_and_review_finalizers_are_durable_and_atomic(
    monkeypatch,
):
    from zira_dashboard import db

    class RollBackIntegrationData(Exception):
        pass

    db.shutdown_pool()
    try:
        db.init_pool(minconn=1, maxconn=2)
        db.bootstrap_schema()
        with pytest.raises(RollBackIntegrationData):
            with db.cursor() as cur:
                marker = datetime.now(tz=UTC).strftime("%Y%m%d%H%M%S%f")

                def insert_request(day, emp_id, *, task_id=501):
                    cur.execute(
                        "INSERT INTO absence_pto_requests (absence_day, emp_id, "
                        "person_odoo_id, person_name, holiday_status_id, "
                        "leave_type_name, balance_at_submit, state, conversion_step, "
                        "original_absence_leave_id, odoo_task_id, task_next_at, "
                        "lease_owner, lease_until, requested_at, created_at, updated_at) "
                        "VALUES (%s, %s, 44, 'Pytest Worker', 7, 'Paid Time Off', 8, "
                        "'needs_review', 'absence_refused', 970, %s, %s, %s, %s, "
                        "%s, %s, %s) RETURNING id",
                        (
                            day,
                            emp_id,
                            task_id,
                            NOW,
                            OWNER,
                            NOW + timedelta(minutes=5),
                            NOW,
                            NOW,
                            NOW,
                        ),
                    )
                    return cur.fetchone()["id"]

                approval_day = date(2099, 2, 1)
                approval_id = insert_request(
                    approval_day, f"pytest-approval-{marker}"
                )
                cur.execute(
                    "INSERT INTO manual_absences (day, emp_id, name, odoo_leave_id) "
                    "VALUES (%s, %s, 'Pytest Worker', 970)",
                    (approval_day, f"pytest-approval-{marker}"),
                )

                manual_id = insert_request(
                    date(2099, 2, 2), f"pytest-manual-{marker}", task_id=502
                )
                rollback_id = insert_request(
                    date(2099, 2, 3), f"pytest-rollback-{marker}", task_id=503
                )

                def query_in_transaction(sql, params=None):
                    cur.execute(sql, params)
                    return list(cur.fetchall())

                savepoint_counter = 0

                @contextmanager
                def transaction_in_test():
                    nonlocal savepoint_counter
                    savepoint_counter += 1
                    name = f"store_call_{savepoint_counter}"
                    cur.execute(f"SAVEPOINT {name}")
                    try:
                        yield cur
                    except Exception:
                        cur.execute(f"ROLLBACK TO SAVEPOINT {name}")
                        cur.execute(f"RELEASE SAVEPOINT {name}")
                        raise
                    else:
                        cur.execute(f"RELEASE SAVEPOINT {name}")

                with monkeypatch.context() as transaction_patch:
                    transaction_patch.setattr(store.db, "query", query_in_transaction)
                    transaction_patch.setattr(store.db, "cursor", transaction_in_test)
                    adopted = store.adopt_external_pto(
                        approval_id, OWNER, pto_leave_id=971, now=NOW
                    )
                    approved = store.finalize_approved(
                        approval_id,
                        OWNER,
                        original_absence_leave_id=970,
                        pto_leave_id=adopted.pto_leave_id,
                        actor_upn="manager@example.com",
                        actor_name="Manager",
                        source="pytest",
                        now=NOW,
                    )
                    manual = store.finalize_manual(
                        manual_id,
                        OWNER,
                        actor_upn="manager@example.com",
                        actor_name="Manager",
                        note="Handled by payroll",
                        now=NOW,
                    )

                    assert approved.state == "approved"
                    assert approved.pto_leave_id == 971
                    assert approved.task_resolution_step == "none"
                    assert approved.task_resolution_next_at == NOW
                    assert manual.state == "resolved_manually"
                    assert manual.manual_resolution_note == "Handled by payroll"
                    assert manual.task_resolution_step == "none"

                    original_audit = store.inbox_log.record_event_with_cursor
                    transaction_patch.setattr(
                        store.inbox_log,
                        "record_event_with_cursor",
                        lambda *args, **kwargs: (_ for _ in ()).throw(
                            RuntimeError("injected audit failure")
                        ),
                    )
                    with pytest.raises(RuntimeError, match="injected audit failure"):
                        store.finalize_manual(
                            rollback_id,
                            OWNER,
                            actor_upn="manager@example.com",
                            actor_name="Manager",
                            note="must roll back",
                            now=NOW,
                        )
                    transaction_patch.setattr(
                        store.inbox_log,
                        "record_event_with_cursor",
                        original_audit,
                    )
                    cur.execute(
                        "SELECT state, manual_resolution_note FROM "
                        "absence_pto_requests WHERE id = %s",
                        (rollback_id,),
                    )
                    unchanged = cur.fetchone()
                    assert unchanged == {
                        "state": "needs_review",
                        "manual_resolution_note": None,
                    }

                    cur.execute(
                        "UPDATE absence_pto_requests SET lease_owner = NULL, "
                        "lease_until = NULL WHERE id IN (%s, %s)",
                        (approval_id, manual_id),
                    )
                    claimed = store.claim_due(
                        OWNER,
                        NOW,
                        period_start=date(2026, 8, 16),
                        period_end=date(2026, 8, 29),
                        limit=100,
                    )
                    claimed_ids = {request.id for request in claimed}
                    assert {approval_id, manual_id}.issubset(claimed_ids)
                raise RollBackIntegrationData
    finally:
        db.shutdown_pool()


@pytest.mark.skipif(
    not SAFE_TEST_DATABASE,
    reason=(
        "requires ABSENCE_PTO_TEST_DATABASE=1 and a loopback DATABASE_URL "
        "whose database name ends in _test"
    ),
)
def test_live_postgres_recovers_a_valid_partial_table_and_rejects_invalid_rows(
    monkeypatch,
):
    from zira_dashboard import db

    class RollBackIntegrationData(Exception):
        pass

    partial_table_sql = """
        CREATE TABLE absence_pto_requests (
            id BIGINT,
            absence_day DATE,
            emp_id TEXT,
            person_odoo_id INTEGER,
            person_name TEXT,
            holiday_status_id INTEGER,
            leave_type_name TEXT,
            balance_at_submit NUMERIC,
            state TEXT,
            conversion_step TEXT,
            task_attempts INTEGER,
            requested_at TIMESTAMPTZ,
            created_at TIMESTAMPTZ,
            updated_at TIMESTAMPTZ
        )
    """
    insert_partial_sql = """
        INSERT INTO absence_pto_requests (
            id, absence_day, emp_id, person_odoo_id, person_name,
            holiday_status_id, leave_type_name, balance_at_submit,
            state, conversion_step, task_attempts, requested_at,
            created_at, updated_at
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
    """

    db.shutdown_pool()
    try:
        db.init_pool(minconn=1, maxconn=2)
        db.bootstrap_schema()
        with pytest.raises(RollBackIntegrationData):
            with db.cursor() as cur:
                cur.execute("DROP TABLE absence_pto_requests")
                cur.execute(partial_table_sql)
                original = (
                    91,
                    date(2099, 1, 5),
                    "pytest-partial-valid",
                    44,
                    "Partial Worker",
                    7,
                    "Paid Time Off",
                    Decimal("8.5"),
                    "pending",
                    "not_started",
                    0,
                    NOW,
                    NOW,
                    NOW,
                )
                cur.execute(insert_partial_sql, original)

                with monkeypatch.context() as transaction_patch:
                    transaction_patch.setattr(db, "cursor", lambda: nullcontext(cur))
                    db.bootstrap_schema()
                    db.bootstrap_schema()

                cur.execute(
                    "SELECT conname FROM pg_constraint "
                    "WHERE conrelid = 'absence_pto_requests'::regclass"
                )
                constraint_names = {row["conname"] for row in cur.fetchall()}
                assert {
                    "absence_pto_requests_pkey",
                    "absence_pto_requests_state_check",
                    "absence_pto_requests_step_check",
                    "absence_pto_requests_balance_check",
                }.issubset(constraint_names)

                required_columns = {
                    "id",
                    "absence_day",
                    "emp_id",
                    "person_odoo_id",
                    "person_name",
                    "holiday_status_id",
                    "leave_type_name",
                    "balance_at_submit",
                    "state",
                    "conversion_step",
                    "task_attempts",
                    "requested_at",
                    "created_at",
                    "updated_at",
                }
                cur.execute(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_schema = current_schema() "
                    "AND table_name = 'absence_pto_requests' "
                    "AND is_nullable = 'NO'"
                )
                not_null_columns = {row["column_name"] for row in cur.fetchall()}
                assert required_columns.issubset(not_null_columns)

                cur.execute(
                    "SELECT id, absence_day, emp_id, person_odoo_id, person_name, "
                    "holiday_status_id, leave_type_name, balance_at_submit, state, "
                    "conversion_step, task_attempts, requested_at, created_at, updated_at "
                    "FROM absence_pto_requests WHERE id = 91"
                )
                recovered = cur.fetchone()
                assert tuple(recovered.values()) == original

                def query_in_transaction(sql, query_params=None):
                    cur.execute(sql, query_params)
                    return list(cur.fetchall())

                with monkeypatch.context() as transaction_patch:
                    transaction_patch.setattr(store.db, "query", query_in_transaction)
                    created = store.create_request(
                        absence_day=date(2099, 1, 6),
                        emp_id="pytest-partial-next-id",
                        person_odoo_id=45,
                        person_name="Next Worker",
                        holiday_status_id=7,
                        leave_type_name="Paid Time Off",
                        balance_at_submit=Decimal("16"),
                        now=NOW,
                    )
                assert created.id == 92

                cur.execute("DROP TABLE absence_pto_requests")
                cur.execute(partial_table_sql)
                cur.execute(
                    insert_partial_sql,
                    original[:7]
                    + (Decimal("-1"),)
                    + original[8:],
                )
                cur.execute("SAVEPOINT invalid_partial_bootstrap")
                with monkeypatch.context() as transaction_patch:
                    transaction_patch.setattr(db, "cursor", lambda: nullcontext(cur))
                    with pytest.raises(psycopg2.errors.CheckViolation):
                        db.bootstrap_schema()
                cur.execute("ROLLBACK TO SAVEPOINT invalid_partial_bootstrap")
                cur.execute("RELEASE SAVEPOINT invalid_partial_bootstrap")
                raise RollBackIntegrationData
    finally:
        db.shutdown_pool()


@pytest.mark.skipif(
    not SAFE_TEST_DATABASE,
    reason=(
        "requires ABSENCE_PTO_TEST_DATABASE=1 and a loopback DATABASE_URL "
        "whose database name ends in _test"
    ),
)
def test_live_postgres_rejects_nonempty_partial_table_without_inventing_ids(
    monkeypatch,
):
    from zira_dashboard import db

    class RollBackIntegrationData(Exception):
        pass

    db.shutdown_pool()
    try:
        db.init_pool(minconn=1, maxconn=2)
        db.bootstrap_schema()
        with pytest.raises(RollBackIntegrationData):
            with db.cursor() as cur:
                cur.execute("DROP TABLE absence_pto_requests")
                cur.execute(
                    """
                    CREATE TABLE absence_pto_requests (
                        absence_day DATE,
                        emp_id TEXT,
                        person_odoo_id INTEGER,
                        person_name TEXT,
                        holiday_status_id INTEGER,
                        leave_type_name TEXT,
                        balance_at_submit NUMERIC,
                        state TEXT,
                        conversion_step TEXT,
                        task_attempts INTEGER,
                        requested_at TIMESTAMPTZ,
                        created_at TIMESTAMPTZ,
                        updated_at TIMESTAMPTZ
                    )
                    """
                )
                original = (
                    date(2099, 1, 7),
                    "pytest-partial-no-id",
                    44,
                    "No ID Worker",
                    7,
                    "Paid Time Off",
                    Decimal("8"),
                    "pending",
                    "not_started",
                    0,
                    NOW,
                    NOW,
                    NOW,
                )
                cur.execute(
                    """
                    INSERT INTO absence_pto_requests (
                        absence_day, emp_id, person_odoo_id, person_name,
                        holiday_status_id, leave_type_name, balance_at_submit,
                        state, conversion_step, task_attempts, requested_at,
                        created_at, updated_at
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    original,
                )
                cur.execute("SAVEPOINT missing_id_bootstrap")
                with monkeypatch.context() as transaction_patch:
                    transaction_patch.setattr(db, "cursor", lambda: nullcontext(cur))
                    with pytest.raises(psycopg2.errors.NotNullViolation):
                        db.bootstrap_schema()
                cur.execute("ROLLBACK TO SAVEPOINT missing_id_bootstrap")
                cur.execute("RELEASE SAVEPOINT missing_id_bootstrap")

                cur.execute(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_schema = current_schema() "
                    "AND table_name = 'absence_pto_requests'"
                )
                columns = {row["column_name"] for row in cur.fetchall()}
                assert "id" not in columns
                cur.execute(
                    "SELECT absence_day, emp_id, person_odoo_id, person_name, "
                    "holiday_status_id, leave_type_name, balance_at_submit, state, "
                    "conversion_step, task_attempts, requested_at, created_at, updated_at "
                    "FROM absence_pto_requests"
                )
                unchanged = cur.fetchone()
                assert tuple(unchanged.values()) == original
                raise RollBackIntegrationData
    finally:
        db.shutdown_pool()
