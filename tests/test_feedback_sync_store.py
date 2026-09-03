"""Durable feedback claim and immutable-attempt state-machine tests."""

from __future__ import annotations

import hashlib
import json
import os
from concurrent.futures import ThreadPoolExecutor
from contextlib import AbstractContextManager
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta
from threading import Barrier
from unittest.mock import MagicMock
from uuid import UUID, uuid4

import psycopg2
import pytest
from psycopg2 import sql

from zira_dashboard import feedback_sync_store as store
from zira_dashboard.feedback_projection import BinaryEvidence, Projection


CLAIM_TOKEN = UUID("11111111-1111-1111-1111-111111111111")
ATTEMPT_ID = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
MAX_SIGNED_64 = 9_223_372_036_854_775_807


def test_pre_attempt_release_api_is_public():
    assert "PreAttemptReleaseResult" in store.__all__
    assert "release_pre_attempt_quarantine" in store.__all__


class RecordingCursor(AbstractContextManager):
    """Cursor with one scripted fetch result per execute call."""

    def __init__(
        self,
        results: list[list[dict[str, object]]] | None = None,
        *,
        errors: dict[int, Exception] | None = None,
    ):
        self._results = list(results or [])
        self._errors = dict(errors or {})
        self._current: list[dict[str, object]] = []
        self.executions: list[tuple[str, object]] = []
        self.exit_exception: type[BaseException] | None = None
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        self.exit_exception = exc_type
        return False

    def execute(self, statement, params=None):
        if params is not None:
            assert str(statement).count("%s") == len(params)
        index = len(self.executions)
        self.executions.append((str(statement), params))
        if index in self._errors:
            raise self._errors[index]
        self._current = self._results[index] if index < len(self._results) else []

    def fetchall(self):
        return list(self._current)

    def fetchone(self):
        return self._current[0] if self._current else None

    def close(self):
        self.closed = True


class FakeConnection:
    def __init__(self, cursor: RecordingCursor):
        self.scripted_cursor = cursor
        self.cursor_factory = None
        self.commits = 0
        self.rollbacks = 0

    def cursor(self, *, cursor_factory=None):
        self.cursor_factory = cursor_factory
        return self.scripted_cursor

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


class FakePool:
    def __init__(self, connection: FakeConnection):
        self.connection = connection
        self.returned: list[FakeConnection] = []

    def getconn(self):
        return self.connection

    def putconn(self, connection):
        self.returned.append(connection)


def use_cursor(monkeypatch, *results, errors=None) -> RecordingCursor:
    cursor = RecordingCursor(list(results), errors=errors)
    monkeypatch.setattr(store.db, "cursor", lambda: cursor)
    return cursor


def normalized_sql(cursor: RecordingCursor, index: int) -> str:
    return " ".join(cursor.executions[index][0].split())


def aware_now() -> datetime:
    return datetime(2026, 8, 20, 18, 0, tzinfo=UTC)


def manifest_values(
    *,
    feedback_id: int = 17,
    name: str = "Safe",
    binaries: dict[str, BinaryEvidence] | None = None,
):
    fields = {
        "x_name": name,
        "x_studio_source_id": f"GPI-PM-FB-{feedback_id}",
        "x_studio_source": "GPI Plant Manager",
        "x_studio_date_start": "2026-08-20",
        "x_studio_type": "Digital",
        "x_studio_status": "Requested",
    }
    binary_values = {} if binaries is None else binaries
    manifest = {
        "fields": fields,
        "binary_evidence": {
            field: {
                "sha256": evidence.sha256,
                "byte_length": evidence.byte_length,
            }
            for field, evidence in sorted(binary_values.items())
        },
    }
    encoded = json.dumps(
        manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode()
    return manifest, hashlib.sha256(encoded).hexdigest(), binary_values


def claim(
    *,
    version: int = 1,
    last_synced_version: int = 0,
    remote_id: int | None = None,
    active_attempt_id: UUID | None = None,
    attempt_count: int = 0,
    owner: str = "worker-a",
) -> store.Claim:
    return store.Claim(
        feedback_id=17,
        desired_version=version,
        last_synced_version=last_synced_version,
        odoo_improvement_id=remote_id,
        claim_owner=owner,
        claim_token=CLAIM_TOKEN,
        claim_expires_at=aware_now() + timedelta(minutes=5),
        active_attempt_id=active_attempt_id,
        attempt_count=attempt_count,
    )


def claimed_row(**changes) -> dict[str, object]:
    row: dict[str, object] = {
        "feedback_id": 17,
        "desired_version": 1,
        "last_synced_version": 0,
        "odoo_improvement_id": None,
        "claim_owner": "worker-a",
        "claim_token": CLAIM_TOKEN,
        "claim_expires_at": aware_now() + timedelta(minutes=5),
        "active_attempt_id": None,
        "attempt_count": 0,
    }
    row.update(changes)
    return row


def selected_sync_row(**changes) -> dict[str, object]:
    row = {
        "feedback_id": 17,
        "desired_version": 1,
        "last_synced_version": 0,
        "odoo_improvement_id": None,
        "active_attempt_id": None,
        "attempt_count": 0,
    }
    row.update(changes)
    return row


def attempt_row(**changes) -> dict[str, object]:
    manifest, digest, _ = manifest_values()
    row: dict[str, object] = {
        "attempt_id": ATTEMPT_ID,
        "feedback_id": 17,
        "projection_version": 1,
        "mutation_kind": "update",
        "remote_id": 77,
        "manifest": manifest,
        "manifest_digest": digest,
        "before_sha256": None,
        "before_byte_length": None,
        "after_sha256": None,
        "after_byte_length": None,
        "state": "prepared",
        "dispatch_marked_at": None,
        "rpc_succeeded_at": None,
        "readback_at": None,
        "settled_at": None,
        "outcome_detail": None,
        "created_at": aware_now(),
        "updated_at": aware_now(),
    }
    row.update(changes)
    return row


def verified_canary_row(**changes) -> dict[str, object]:
    settled = aware_now()
    row = attempt_row(
        state="verified",
        projection_version=3,
        remote_id=901,
        dispatch_marked_at=settled - timedelta(seconds=3),
        rpc_succeeded_at=settled - timedelta(seconds=2),
        readback_at=settled,
        settled_at=settled,
        sync_desired_version=3,
        sync_last_synced_version=3,
        sync_remote_id=901,
        sync_state="idle",
        sync_claim_owner=None,
        sync_claim_token=None,
        sync_claim_expires_at=None,
        sync_active_attempt_id=None,
        sync_attempt_count=0,
        sync_quarantine_reason=None,
        sync_quarantined_at=None,
        sync_last_error_class=None,
        sync_last_error_summary=None,
        feedback_projection_version=3,
    )
    row.update(changes)
    return row


def owned_attempt_row(*, state: str, desired_version: int = 1, **changes):
    row = attempt_row(state=state)
    row.update(
        {
            "sync_desired_version": desired_version,
            "sync_last_synced_version": 0,
            "sync_remote_id": row["remote_id"] if state == "rpc_succeeded" else None,
            "sync_attempt_count": 0,
        }
    )
    row.update(changes)
    return row


def attempt(
    *,
    state: str = "prepared",
    remote_id: int | None = 77,
    projection_version: int = 1,
    mutation_kind: str = "update",
    feedback_id: int = 17,
    attempt_id: UUID = ATTEMPT_ID,
) -> store.Attempt:
    manifest, digest, _ = manifest_values(feedback_id=feedback_id)
    return store.Attempt(
        attempt_id=attempt_id,
        feedback_id=feedback_id,
        projection_version=projection_version,
        mutation_kind=mutation_kind,
        remote_id=remote_id,
        manifest=manifest,
        manifest_digest=digest,
        binaries={},
        state=state,
    )


def assert_rollback(cursor: RecordingCursor) -> None:
    assert cursor.exit_exception is store.StateTransitionError


def test_claim_and_attempt_expose_frozen_detached_authority():
    item = claim()
    with pytest.raises(FrozenInstanceError):
        item.feedback_id = 99

    manifest, digest, _ = manifest_values()
    saved = store.Attempt(
        attempt_id=ATTEMPT_ID,
        feedback_id=17,
        projection_version=1,
        mutation_kind="update",
        remote_id=77,
        manifest=manifest,
        manifest_digest=digest,
        binaries={},
        state="prepared",
    )
    manifest["fields"]["x_name"] = "caller changed input"
    detached = saved.manifest
    detached["fields"]["x_name"] = "caller changed output"
    assert saved.manifest["fields"]["x_name"] == "Safe"
    with pytest.raises(FrozenInstanceError):
        saved.state = "verified"


@pytest.mark.parametrize("value", [True, 1.0, 0, -1, MAX_SIGNED_64 + 1])
def test_claim_rejects_non_positive_exact_signed_64_ids(value):
    with pytest.raises(ValueError):
        store.Claim(
            feedback_id=value,
            desired_version=1,
            last_synced_version=0,
            odoo_improvement_id=None,
            claim_owner="worker-a",
            claim_token=CLAIM_TOKEN,
            claim_expires_at=aware_now(),
            active_attempt_id=None,
            attempt_count=0,
        )


def test_claim_rejects_naive_time_wrong_uuid_and_unbounded_worker():
    values = claimed_row()
    values.pop("feedback_id")
    values.pop("claim_expires_at")
    with pytest.raises(ValueError):
        store.Claim(
            feedback_id=17,
            **values,
            claim_expires_at=aware_now().replace(tzinfo=None),
        )
    values["claim_expires_at"] = aware_now()
    values.pop("claim_token")
    with pytest.raises(ValueError):
        store.Claim(
            feedback_id=17,
            **values,
            claim_token=str(CLAIM_TOKEN),
        )
    values["claim_token"] = CLAIM_TOKEN
    values.pop("claim_owner")
    with pytest.raises(ValueError):
        store.Claim(feedback_id=17, **values, claim_owner="w" * 129)


def test_retry_due_uses_exact_bounded_schedule():
    expected_minutes = [1, 2, 4, 8, 16, 32, 60, 60]
    assert [store.retry_due(aware_now(), count) - aware_now() for count in range(1, 9)] == [
        timedelta(minutes=value) for value in expected_minutes
    ]
    for invalid in (True, 0, -1, 1.5):
        with pytest.raises(ValueError):
            store.retry_due(aware_now(), invalid)


def test_claim_due_uses_skip_locked_canary_and_fixed_python_uuid(monkeypatch):
    monkeypatch.setattr(store, "uuid4", lambda: CLAIM_TOKEN)
    selected = selected_sync_row()
    returned = claimed_row()
    cursor = use_cursor(monkeypatch, [selected], [returned])

    result = store.claim_due(now=aware_now(), worker_id="worker-a", limit=10, canary_feedback_id=17)

    assert result == [claim()]
    select_sql = normalized_sql(cursor, 0)
    update_sql = normalized_sql(cursor, 1)
    assert "FOR UPDATE SKIP LOCKED" in select_sql
    assert "s.feedback_id = %s" in select_sql
    assert "LIMIT %s" in select_sql
    assert "uuid_generate" not in update_sql.lower()
    assert "claim_token = %s" in update_sql
    assert CLAIM_TOKEN in cursor.executions[1][1]
    assert aware_now() + timedelta(minutes=5) in cursor.executions[1][1]


def test_claim_due_select_and_guarded_updates_share_one_transaction(monkeypatch):
    monkeypatch.setattr(store, "uuid4", lambda: CLAIM_TOKEN)
    cursor = use_cursor(monkeypatch, [selected_sync_row()], [claimed_row()])
    store.claim_due(now=aware_now(), worker_id="worker-a", limit=1)
    assert cursor.exit_exception is None
    update_sql = normalized_sql(cursor, 1)
    assert "state = 'idle'" in update_sql
    assert "desired_version = %s" in update_sql
    assert "active_attempt_id IS NOT DISTINCT FROM %s" in update_sql


@pytest.mark.parametrize("limit", [True, 0, -1, 11, 1.0])
def test_claim_due_rejects_invalid_limit_before_database(monkeypatch, limit):
    database = MagicMock(side_effect=AssertionError("database must not be touched"))
    monkeypatch.setattr(store.db, "cursor", database)
    with pytest.raises(ValueError):
        store.claim_due(now=aware_now(), worker_id="worker-a", limit=limit)
    database.assert_not_called()


def test_claim_due_invalid_selected_row_fails_before_update_and_rolls_back(monkeypatch):
    cursor = use_cursor(monkeypatch, [selected_sync_row(feedback_id=True)])
    with pytest.raises(store.StateTransitionError):
        store.claim_due(now=aware_now(), worker_id="worker-a", limit=1)
    assert len(cursor.executions) == 1
    assert_rollback(cursor)


@pytest.mark.parametrize("returned", [[], [claimed_row(), claimed_row()]])
def test_claim_due_zero_or_multiple_guarded_updates_roll_back(monkeypatch, returned):
    cursor = use_cursor(monkeypatch, [selected_sync_row()], returned)
    with pytest.raises(store.StateTransitionError):
        store.claim_due(now=aware_now(), worker_id="worker-a", limit=1)
    assert_rollback(cursor)


def test_load_active_attempt_validates_owner_and_returns_detached_manifest(monkeypatch):
    active_claim = claim(active_attempt_id=ATTEMPT_ID)
    cursor = use_cursor(monkeypatch, [attempt_row()])
    loaded = store.load_active_attempt(active_claim)
    assert loaded.attempt_id == attempt().attempt_id
    assert loaded.manifest == attempt().manifest
    assert loaded.state == "prepared"
    changed = loaded.manifest
    changed["fields"]["x_name"] = "changed"
    assert loaded.manifest["fields"]["x_name"] == "Safe"
    statement = normalized_sql(cursor, 0)
    assert "s.claim_owner = %s" in statement
    assert "s.claim_token = %s" in statement
    assert "s.active_attempt_id IS NOT DISTINCT FROM %s" in statement
    assert "a.projection_version > s.last_synced_version" in statement
    assert "a.projection_version <= s.desired_version" in statement


def test_load_active_attempt_returns_none_only_for_owned_claim_without_attempt(monkeypatch):
    cursor = use_cursor(monkeypatch, [{"attempt_id": None}])
    assert store.load_active_attempt(claim()) is None
    assert cursor.exit_exception is None


@pytest.mark.parametrize("rows", [[], [attempt_row(), attempt_row()]])
def test_load_active_attempt_wrong_owner_or_duplicate_rows_fail_closed(monkeypatch, rows):
    cursor = use_cursor(monkeypatch, rows)
    with pytest.raises(store.StateTransitionError):
        store.load_active_attempt(claim(active_attempt_id=ATTEMPT_ID))
    assert_rollback(cursor)


def test_load_active_attempt_rejects_noncanonical_persisted_manifest(monkeypatch):
    cursor = use_cursor(monkeypatch, [attempt_row(manifest_digest="a" * 64)])
    with pytest.raises(store.StateTransitionError):
        store.load_active_attempt(claim(active_attempt_id=ATTEMPT_ID))
    assert_rollback(cursor)


def test_newer_claim_loads_and_dispatches_saved_prepared_attempt(monkeypatch):
    newer_claim = claim(version=2, active_attempt_id=ATTEMPT_ID)
    cursor = use_cursor(
        monkeypatch,
        [attempt_row(projection_version=1)],
        [owned_attempt_row(state="prepared", desired_version=2)],
        [{"attempt_id": ATTEMPT_ID}],
    )

    loaded = store.load_active_attempt(newer_claim)
    assert loaded is not None
    changed = store.mark_dispatch(newer_claim, loaded, aware_now())

    assert changed.projection_version == 1
    assert changed.state == "dispatch_marked"
    load_sql = normalized_sql(cursor, 0)
    assert "a.projection_version > s.last_synced_version" in load_sql
    assert "a.projection_version <= s.desired_version" in load_sql
    lock_sql = normalized_sql(cursor, 1)
    assert "a.projection_version > s.last_synced_version" in lock_sql
    assert "a.projection_version <= s.desired_version" in lock_sql


def test_newer_claim_schedules_readback_for_saved_rpc_succeeded_attempt(monkeypatch):
    newer_claim = claim(
        version=2,
        remote_id=77,
        active_attempt_id=ATTEMPT_ID,
    )
    cursor = use_cursor(
        monkeypatch,
        [attempt_row(state="rpc_succeeded", projection_version=1)],
        [owned_attempt_row(state="rpc_succeeded", desired_version=2)],
        [{"feedback_id": 17}],
    )

    loaded = store.load_active_attempt(newer_claim)
    assert loaded is not None
    assert store.schedule_readback(newer_claim, loaded, aware_now()) is True
    assert loaded.projection_version == 1
    assert "a.projection_version > s.last_synced_version" in normalized_sql(cursor, 0)


@pytest.mark.parametrize(
    ("newer_claim", "saved_attempt"),
    [
        (
            claim(
                version=2,
                last_synced_version=1,
                active_attempt_id=ATTEMPT_ID,
            ),
            attempt(projection_version=1),
        ),
        (
            claim(version=1, active_attempt_id=ATTEMPT_ID),
            attempt(projection_version=2),
        ),
        (
            claim(version=2, active_attempt_id=ATTEMPT_ID),
            attempt(feedback_id=18),
        ),
        (
            claim(
                version=2,
                active_attempt_id=UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"),
            ),
            attempt(),
        ),
    ],
)
def test_active_attempt_projection_must_be_unsynced_and_not_newer_than_truth(
    monkeypatch, newer_claim, saved_attempt
):
    database = MagicMock(side_effect=AssertionError("database must not be touched"))
    monkeypatch.setattr(store.db, "cursor", database)
    with pytest.raises(ValueError):
        store.mark_dispatch(newer_claim, saved_attempt, aware_now())
    database.assert_not_called()


def test_prepare_attempt_persists_canonical_manifest_then_guard_sets_active(monkeypatch):
    manifest, digest, binaries = manifest_values()
    cursor = use_cursor(
        monkeypatch,
        [claimed_row()],
        [],
        [{"feedback_id": 17}],
    )
    saved = store.prepare_attempt(
        claim=claim(),
        attempt_id=ATTEMPT_ID,
        mutation_kind="create",
        remote_id=None,
        manifest=manifest,
        manifest_digest=digest,
        binaries=binaries,
        now=aware_now(),
    )
    assert saved.attempt_id == ATTEMPT_ID
    assert saved.state == "prepared"
    assert "INSERT INTO feedback_odoo_attempts" in normalized_sql(cursor, 1)
    update_sql = normalized_sql(cursor, 2)
    assert "active_attempt_id = %s" in update_sql
    assert "state = 'in_flight'" in update_sql
    assert "claim_owner = %s" in update_sql
    assert "claim_token = %s" in update_sql


def test_prepare_attempt_persists_review_task_link_in_create_manifest(monkeypatch):
    manifest, _digest, binaries = manifest_values()
    manifest["fields"]["x_studio_type"] = "Physical - Issue"
    manifest["fields"]["x_studio_linked_task"] = 902
    encoded = json.dumps(
        manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode()
    digest = hashlib.sha256(encoded).hexdigest()
    use_cursor(
        monkeypatch,
        [claimed_row()],
        [],
        [{"feedback_id": 17}],
    )

    saved = store.prepare_attempt(
        claim=claim(),
        attempt_id=ATTEMPT_ID,
        mutation_kind="create",
        remote_id=None,
        manifest=manifest,
        manifest_digest=digest,
        binaries=binaries,
        now=aware_now(),
    )

    assert saved.manifest["fields"]["x_studio_linked_task"] == 902


def test_prepare_attempt_validates_binary_evidence_against_manifest(monkeypatch):
    raw = b"safe jpeg bytes"
    evidence = BinaryEvidence(raw, hashlib.sha256(raw).hexdigest(), len(raw))
    manifest, digest, binaries = manifest_values(binaries={"x_studio_image": evidence})
    cursor = use_cursor(
        monkeypatch,
        [claimed_row()],
        [],
        [{"feedback_id": 17}],
    )
    saved = store.prepare_attempt(
        claim=claim(),
        attempt_id=ATTEMPT_ID,
        mutation_kind="create",
        remote_id=None,
        manifest=manifest,
        manifest_digest=digest,
        binaries=binaries,
        now=aware_now(),
    )
    params = cursor.executions[1][1]
    assert evidence.sha256 in params
    assert evidence.byte_length in params
    assert raw not in params
    assert saved.manifest["binary_evidence"]["x_studio_image"] == {
        "sha256": evidence.sha256,
        "byte_length": len(raw),
    }


@pytest.mark.parametrize("bad_digest", ["a" * 64, b"a" * 64, "A" * 64])
def test_prepare_attempt_recomputes_digest_before_database(monkeypatch, bad_digest):
    manifest, _digest, binaries = manifest_values()
    database = MagicMock(side_effect=AssertionError("database must not be touched"))
    monkeypatch.setattr(store.db, "cursor", database)
    with pytest.raises(ValueError):
        store.prepare_attempt(
            claim=claim(),
            attempt_id=ATTEMPT_ID,
            mutation_kind="create",
            remote_id=None,
            manifest=manifest,
            manifest_digest=bad_digest,
            binaries=binaries,
            now=aware_now(),
        )
    database.assert_not_called()


@pytest.mark.parametrize(
    "manifest_change",
    [
        lambda value: value["fields"].update({"claim_token": str(CLAIM_TOKEN)}),
        lambda value: value["fields"].update({"x_studio_image": "c2VjcmV0"}),
        lambda value: value["binary_evidence"].update(
            {"x_studio_image": {"sha256": "a" * 64, "byte_length": 4}}
        ),
    ],
)
def test_prepare_attempt_rejects_token_binary_or_unmatched_evidence_before_database(
    monkeypatch, manifest_change
):
    manifest, _digest, binaries = manifest_values()
    manifest_change(manifest)
    digest = hashlib.sha256(
        json.dumps(manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()
    database = MagicMock(side_effect=AssertionError("database must not be touched"))
    monkeypatch.setattr(store.db, "cursor", database)
    with pytest.raises(ValueError):
        store.prepare_attempt(
            claim=claim(),
            attempt_id=ATTEMPT_ID,
            mutation_kind="create",
            remote_id=None,
            manifest=manifest,
            manifest_digest=digest,
            binaries=binaries,
            now=aware_now(),
        )
    database.assert_not_called()


def test_prepare_attempt_rejects_claim_token_in_field_value(monkeypatch):
    manifest, digest, binaries = manifest_values(name=f"unsafe {CLAIM_TOKEN}")
    database = MagicMock(side_effect=AssertionError("database must not be touched"))
    monkeypatch.setattr(store.db, "cursor", database)
    with pytest.raises(ValueError):
        store.prepare_attempt(
            claim=claim(),
            attempt_id=ATTEMPT_ID,
            mutation_kind="create",
            remote_id=None,
            manifest=manifest,
            manifest_digest=digest,
            binaries=binaries,
            now=aware_now(),
        )
    database.assert_not_called()


def test_prepare_attempt_guard_failure_rolls_back_insert(monkeypatch):
    manifest, digest, binaries = manifest_values()
    cursor = use_cursor(monkeypatch, [claimed_row()], [], [])
    with pytest.raises(store.StateTransitionError):
        store.prepare_attempt(
            claim=claim(),
            attempt_id=ATTEMPT_ID,
            mutation_kind="create",
            remote_id=None,
            manifest=manifest,
            manifest_digest=digest,
            binaries=binaries,
            now=aware_now(),
        )
    assert "INSERT INTO feedback_odoo_attempts" in normalized_sql(cursor, 1)
    assert_rollback(cursor)


def test_prepare_attempt_second_write_cardinality_failure_rolls_back_real_db_cursor(
    monkeypatch,
):
    manifest, digest, binaries = manifest_values()
    cursor = RecordingCursor([[claimed_row()], [], []])
    connection = FakeConnection(cursor)
    pool = FakePool(connection)
    monkeypatch.setattr(store.db, "_pool", pool)

    with pytest.raises(store.StateTransitionError):
        store.prepare_attempt(
            claim=claim(),
            attempt_id=ATTEMPT_ID,
            mutation_kind="create",
            remote_id=None,
            manifest=manifest,
            manifest_digest=digest,
            binaries=binaries,
            now=aware_now(),
        )

    assert len(cursor.executions) == 3
    assert connection.commits == 0
    assert connection.rollbacks == 1
    assert cursor.closed is True
    assert pool.returned == [connection]


@pytest.mark.parametrize("lock_rows", [[], [claimed_row(), claimed_row()]])
def test_prepare_attempt_wrong_owner_or_multiple_lock_rows_write_nothing(monkeypatch, lock_rows):
    manifest, digest, binaries = manifest_values()
    cursor = use_cursor(monkeypatch, lock_rows)
    with pytest.raises(store.StateTransitionError):
        store.prepare_attempt(
            claim=claim(),
            attempt_id=ATTEMPT_ID,
            mutation_kind="create",
            remote_id=None,
            manifest=manifest,
            manifest_digest=digest,
            binaries=binaries,
            now=aware_now(),
        )
    assert len(cursor.executions) == 1
    assert_rollback(cursor)


def test_prepare_attempt_replay_rolls_back_without_replacing_evidence(monkeypatch):
    manifest, digest, binaries = manifest_values()
    cursor = use_cursor(
        monkeypatch,
        [claimed_row()],
        errors={1: psycopg2.IntegrityError("duplicate attempt")},
    )
    with pytest.raises(psycopg2.IntegrityError):
        store.prepare_attempt(
            claim=claim(),
            attempt_id=ATTEMPT_ID,
            mutation_kind="create",
            remote_id=None,
            manifest=manifest,
            manifest_digest=digest,
            binaries=binaries,
            now=aware_now(),
        )
    assert cursor.exit_exception is psycopg2.IntegrityError


def test_defer_prepared_releases_claim_but_retains_attempt_and_count(monkeypatch):
    cursor = use_cursor(
        monkeypatch,
        [owned_attempt_row(state="prepared")],
        [{"feedback_id": 17}],
    )
    assert store.defer_prepared_for_closed_gate(claim(), attempt(), aware_now()) is True
    statement = normalized_sql(cursor, 1)
    assert "state = 'idle'" in statement
    assert "active_attempt_id = %s" in statement
    assert "attempt_count" not in statement.split("SET", 1)[1].split("WHERE", 1)[0]
    assert "claim_owner = NULL" in statement


def test_mark_dispatch_guards_prepared_attempt_and_owner(monkeypatch):
    cursor = use_cursor(
        monkeypatch,
        [owned_attempt_row(state="prepared")],
        [{"attempt_id": ATTEMPT_ID}],
    )
    changed = store.mark_dispatch(claim(), attempt(), aware_now())
    assert changed.state == "dispatch_marked"
    statement = normalized_sql(cursor, 1)
    assert "state = 'dispatch_marked'" in statement
    assert "state = %s" in statement
    assert "projection_version = %s" in statement


@pytest.mark.parametrize("rows", [[], [owned_attempt_row(state="prepared")] * 2])
def test_mark_dispatch_wrong_owner_zero_or_multiple_rolls_back(monkeypatch, rows):
    cursor = use_cursor(monkeypatch, rows)
    with pytest.raises(store.StateTransitionError):
        store.mark_dispatch(claim(), attempt(), aware_now())
    assert len(cursor.executions) == 1
    assert_rollback(cursor)


def test_mark_dispatch_replay_from_wrong_state_fails_closed(monkeypatch):
    cursor = use_cursor(monkeypatch, [])
    with pytest.raises(store.StateTransitionError):
        store.mark_dispatch(claim(), attempt(), aware_now())
    assert_rollback(cursor)


def _invoke_owned_transition(operation: str):
    if operation == "defer":
        return store.defer_prepared_for_closed_gate(claim(), attempt(), aware_now())
    if operation == "mark_dispatch":
        return store.mark_dispatch(claim(), attempt(), aware_now())
    if operation == "mark_rpc_succeeded":
        return store.mark_rpc_succeeded(
            claim(remote_id=77), attempt(), remote_id=77, now=aware_now()
        )
    if operation == "schedule_readback":
        return store.schedule_readback(claim(remote_id=77), attempt(), aware_now())
    if operation == "settle_verified":
        return store.settle_verified(claim(remote_id=77), attempt(), remote_id=77, now=aware_now())
    if operation == "record_definitive_failure":
        return store.record_definitive_failure(
            claim(remote_id=77),
            attempt(),
            "odoo_fault",
            aware_now(),
        )
    if operation == "quarantine":
        return store.quarantine(
            claim(remote_id=77),
            "ambiguous_mutation",
            aware_now(),
            attempt=attempt(),
        )
    raise AssertionError(f"unknown transition {operation}")


@pytest.mark.parametrize(
    "operation",
    [
        "defer",
        "mark_dispatch",
        "mark_rpc_succeeded",
        "schedule_readback",
        "settle_verified",
        "record_definitive_failure",
        "quarantine",
    ],
)
@pytest.mark.parametrize("lock_rows", [[], [attempt_row(), attempt_row()]])
def test_every_owned_transition_rejects_wrong_owner_or_multiple_lock_rows(
    monkeypatch, operation, lock_rows
):
    cursor = use_cursor(monkeypatch, lock_rows)
    with pytest.raises(store.StateTransitionError):
        _invoke_owned_transition(operation)
    assert len(cursor.executions) == 1
    assert_rollback(cursor)


@pytest.mark.parametrize(
    ("operation", "expected_state"),
    [
        ("defer", "prepared"),
        ("mark_dispatch", "prepared"),
        ("mark_rpc_succeeded", "dispatch_marked"),
        ("schedule_readback", "rpc_succeeded"),
        ("settle_verified", "rpc_succeeded"),
        ("record_definitive_failure", ("prepared", "dispatch_marked")),
    ],
)
def test_every_owned_transition_fences_exact_expected_attempt_state(
    monkeypatch, operation, expected_state
):
    cursor = use_cursor(monkeypatch, [])
    with pytest.raises(store.StateTransitionError):
        _invoke_owned_transition(operation)
    statement, params = cursor.executions[0]
    assert "a.state = ANY(%s)" in " ".join(statement.split())
    expected = list(expected_state) if type(expected_state) is tuple else [expected_state]
    assert expected in params
    assert_rollback(cursor)


@pytest.mark.parametrize(
    ("operation", "database_state", "sync_remote_id"),
    [
        ("defer", "prepared", None),
        ("mark_dispatch", "prepared", None),
        ("mark_rpc_succeeded", "dispatch_marked", 77),
        ("schedule_readback", "rpc_succeeded", 77),
        ("settle_verified", "rpc_succeeded", 77),
        ("record_definitive_failure", "dispatch_marked", 77),
        ("quarantine", "dispatch_marked", 77),
    ],
)
@pytest.mark.parametrize("updated_rows", [[], [{"feedback_id": 17}, {"feedback_id": 17}]])
def test_every_owned_transition_rolls_back_zero_or_multiple_first_updates(
    monkeypatch, operation, database_state, sync_remote_id, updated_rows
):
    cursor = use_cursor(
        monkeypatch,
        [owned_attempt_row(state=database_state, sync_remote_id=sync_remote_id)],
        updated_rows,
    )
    with pytest.raises(store.StateTransitionError):
        _invoke_owned_transition(operation)
    assert len(cursor.executions) == 2
    assert_rollback(cursor)


def test_mark_rpc_succeeded_atomically_records_evidence_and_adopts_remote(monkeypatch):
    create_attempt = store.Attempt(
        attempt_id=ATTEMPT_ID,
        feedback_id=17,
        projection_version=1,
        mutation_kind="create",
        remote_id=None,
        manifest=manifest_values()[0],
        manifest_digest=manifest_values()[1],
        binaries={},
        state="prepared",
    )
    cursor = use_cursor(
        monkeypatch,
        [
            owned_attempt_row(
                state="dispatch_marked",
                mutation_kind="create",
                remote_id=None,
                sync_remote_id=None,
            )
        ],
        [{"attempt_id": ATTEMPT_ID}],
        [{"feedback_id": 17}],
    )
    changed = store.mark_rpc_succeeded(claim(), create_attempt, remote_id=901, now=aware_now())
    assert changed.state == "rpc_succeeded"
    assert changed.remote_id == 901
    assert "rpc_succeeded_at = %s" in normalized_sql(cursor, 1)
    assert "odoo_improvement_id = %s" in normalized_sql(cursor, 2)


@pytest.mark.parametrize("remote_id", [None, True, 0, -1, 1.5, MAX_SIGNED_64 + 1])
def test_mark_rpc_succeeded_rejects_invalid_remote_id_before_database(monkeypatch, remote_id):
    database = MagicMock(side_effect=AssertionError("database must not be touched"))
    monkeypatch.setattr(store.db, "cursor", database)
    with pytest.raises(ValueError):
        store.mark_rpc_succeeded(claim(), attempt(), remote_id=remote_id, now=aware_now())
    database.assert_not_called()


def test_mark_rpc_succeeded_sync_guard_failure_rolls_back_attempt(monkeypatch):
    cursor = use_cursor(
        monkeypatch,
        [owned_attempt_row(state="dispatch_marked", sync_remote_id=77)],
        [{"attempt_id": ATTEMPT_ID}],
        [],
    )
    with pytest.raises(store.StateTransitionError):
        store.mark_rpc_succeeded(claim(remote_id=77), attempt(), remote_id=77, now=aware_now())
    assert_rollback(cursor)


def test_schedule_readback_releases_claim_for_same_rpc_succeeded_attempt(monkeypatch):
    cursor = use_cursor(
        monkeypatch,
        [owned_attempt_row(state="rpc_succeeded")],
        [{"feedback_id": 17}],
    )
    assert store.schedule_readback(claim(), attempt(), aware_now()) is True
    statement = normalized_sql(cursor, 1)
    assert "state = 'idle'" in statement
    assert "active_attempt_id = %s" in statement
    assert "due_at = %s" in statement


def test_settle_verified_saves_only_attempt_projection_when_desired_is_newer(monkeypatch):
    cursor = use_cursor(
        monkeypatch,
        [owned_attempt_row(state="rpc_succeeded", desired_version=2)],
        [{"attempt_id": ATTEMPT_ID}],
        [
            {
                "feedback_id": 17,
                "desired_version": 2,
                "last_synced_version": 1,
            }
        ],
    )
    assert (
        store.settle_verified(
            claim(version=1, remote_id=77), attempt(), remote_id=77, now=aware_now()
        )
        is True
    )
    statement = normalized_sql(cursor, 2)
    assert "last_synced_version = %s" in statement
    assert "last_synced_version = desired_version" not in statement
    assert "desired_version >= %s" in statement
    assert "active_attempt_id = NULL" in statement
    assert "due_at = CASE" in statement


def test_settle_verified_rolls_back_attempt_when_sync_fence_misses(monkeypatch):
    cursor = use_cursor(
        monkeypatch,
        [owned_attempt_row(state="rpc_succeeded")],
        [{"attempt_id": ATTEMPT_ID}],
        [],
    )
    with pytest.raises(store.StateTransitionError):
        store.settle_verified(claim(remote_id=77), attempt(), remote_id=77, now=aware_now())
    assert_rollback(cursor)


def test_settle_verified_replay_is_rejected(monkeypatch):
    cursor = use_cursor(monkeypatch, [])
    with pytest.raises(store.StateTransitionError):
        store.settle_verified(claim(remote_id=77), attempt(), remote_id=77, now=aware_now())
    assert_rollback(cursor)


@pytest.mark.parametrize(
    ("attempt_count", "minutes", "quarantined"),
    [
        (0, 1, False),
        (1, 2, False),
        (2, 4, False),
        (3, 8, False),
        (4, 16, False),
        (5, 32, False),
        (6, 60, False),
        (7, None, True),
    ],
)
def test_definitive_failure_exact_delays_and_eighth_exhaustion(
    monkeypatch, attempt_count, minutes, quarantined
):
    owned = owned_attempt_row(
        state="dispatch_marked",
        sync_attempt_count=attempt_count,
        sync_remote_id=77,
    )
    sync_result = {
        "feedback_id": 17,
        "attempt_count": attempt_count + 1,
        "state": "quarantined" if quarantined else "idle",
    }
    cursor = use_cursor(
        monkeypatch,
        [owned],
        [{"attempt_id": ATTEMPT_ID}],
        [sync_result],
    )
    outcome = store.record_definitive_failure(
        claim(remote_id=77, attempt_count=attempt_count),
        attempt(),
        "odoo_fault",
        aware_now(),
    )
    assert outcome == ("quarantined" if quarantined else "retry_scheduled")
    statement, params = cursor.executions[2]
    assert "Odoo refused the feedback change." in params
    if quarantined:
        assert "state = 'quarantined'" in " ".join(statement.split())
        assert "quarantine_reason = 'retry_exhausted'" in " ".join(statement.split())
        assert ATTEMPT_ID in params
    else:
        assert aware_now() + timedelta(minutes=minutes) in params
        assert "active_attempt_id = NULL" in " ".join(statement.split())


@pytest.mark.parametrize("error_class", ["UPPER", "has-dash", "a" * 65])
def test_definitive_failure_rejects_malformed_error_classes(monkeypatch, error_class):
    database = MagicMock(side_effect=AssertionError("database must not be touched"))
    monkeypatch.setattr(store.db, "cursor", database)
    with pytest.raises(ValueError):
        store.record_definitive_failure(claim(remote_id=77), attempt(), error_class, aware_now())
    database.assert_not_called()


def test_definitive_failure_replay_does_not_increment_twice(monkeypatch):
    cursor = use_cursor(monkeypatch, [])
    with pytest.raises(store.StateTransitionError):
        store.record_definitive_failure(
            claim(remote_id=77),
            attempt(),
            "odoo_fault",
            aware_now(),
        )
    assert len(cursor.executions) == 1
    assert_rollback(cursor)


def test_pre_dispatch_identity_read_failure_is_definitive_and_retryable(monkeypatch):
    cursor = use_cursor(
        monkeypatch,
        [owned_attempt_row(state="prepared")],
        [{"attempt_id": ATTEMPT_ID}],
        [{"feedback_id": 17, "attempt_count": 1, "state": "idle"}],
    )
    assert (
        store.record_definitive_failure(
            claim(),
            attempt(),
            "identity_read_failed",
            aware_now(),
        )
        == "retry_scheduled"
    )
    assert "state = %s" in normalized_sql(cursor, 1)
    assert "prepared" in cursor.executions[1][1]
    assert "The Odoo identity check could not finish." in cursor.executions[2][1]


def test_quarantine_without_attempt_fences_empty_active_attempt(monkeypatch):
    cursor = use_cursor(
        monkeypatch,
        [claimed_row()],
        [{"feedback_id": 17}],
    )
    assert store.quarantine(claim(), "duplicate_compound_identity", aware_now()) is True
    assert "active_attempt_id IS NULL" in normalized_sql(cursor, 0)
    statement = normalized_sql(cursor, 1)
    assert "state = 'quarantined'" in statement
    assert "quarantine_reason = %s" in statement
    assert "More than one Odoo record has this feedback number." in cursor.executions[1][1]


def test_quarantine_with_attempt_atomically_preserves_manifest_evidence(monkeypatch):
    cursor = use_cursor(
        monkeypatch,
        [owned_attempt_row(state="dispatch_marked", sync_remote_id=77)],
        [{"attempt_id": ATTEMPT_ID}],
        [{"feedback_id": 17}],
    )
    assert (
        store.quarantine(
            claim(remote_id=77),
            "ambiguous_mutation",
            aware_now(),
            attempt=attempt(),
        )
        is True
    )
    attempt_update = normalized_sql(cursor, 1)
    assert "state = 'ambiguous'" in attempt_update
    assert "manifest" not in attempt_update.split("SET", 1)[1].split("WHERE", 1)[0]
    assert "state = %s" in attempt_update
    assert "state = 'quarantined'" in normalized_sql(cursor, 2)
    assert "The Odoo result is unclear, so this feedback stopped." in cursor.executions[2][1]


def test_quarantine_sync_failure_rolls_back_attempt_update(monkeypatch):
    cursor = use_cursor(
        monkeypatch,
        [owned_attempt_row(state="dispatch_marked", sync_remote_id=77)],
        [{"attempt_id": ATTEMPT_ID}],
        [],
    )
    with pytest.raises(store.StateTransitionError):
        store.quarantine(
            claim(remote_id=77),
            "ambiguous_mutation",
            aware_now(),
            attempt=attempt(),
        )
    assert_rollback(cursor)


@pytest.mark.parametrize("mutation_kind", ["create", "update"])
def test_quarantine_after_new_remote_adoption_fences_persisted_rpc_remote(
    monkeypatch, mutation_kind
):
    initial_attempt_remote = None if mutation_kind == "create" else 901
    dispatch_attempt = attempt(
        state="dispatch_marked",
        mutation_kind=mutation_kind,
        remote_id=initial_attempt_remote,
    )
    mark_cursor = use_cursor(
        monkeypatch,
        [
            owned_attempt_row(
                state="dispatch_marked",
                mutation_kind=mutation_kind,
                remote_id=initial_attempt_remote,
                sync_remote_id=None,
            )
        ],
        [{"attempt_id": ATTEMPT_ID}],
        [{"feedback_id": 17}],
    )
    rpc_attempt = store.mark_rpc_succeeded(
        claim(), dispatch_attempt, remote_id=901, now=aware_now()
    )
    assert claim().odoo_improvement_id is None
    assert 901 in mark_cursor.executions[2][1]

    quarantine_cursor = use_cursor(
        monkeypatch,
        [
            owned_attempt_row(
                state="rpc_succeeded",
                mutation_kind=mutation_kind,
                remote_id=901,
                sync_remote_id=901,
            )
        ],
        [{"attempt_id": ATTEMPT_ID}],
        [{"feedback_id": 17}],
    )

    assert (
        store.quarantine(
            claim(),
            "ambiguous_mutation",
            aware_now(),
            attempt=rpc_attempt,
        )
        is True
    )
    assert 901 in quarantine_cursor.executions[2][1]


@pytest.mark.parametrize(
    "payload",
    [
        "pass" + "word=hunter2",
        "https://remote.example/private?id=901",
        '{"remote_result":"secret"}',
        "caller supplied text",
        str(CLAIM_TOKEN),
    ],
)
def test_definitive_failure_rejects_all_caller_supplied_summary_text(monkeypatch, payload):
    database = MagicMock(side_effect=AssertionError("database must not be touched"))
    monkeypatch.setattr(store.db, "cursor", database)
    with pytest.raises((TypeError, ValueError)):
        store.record_definitive_failure(
            claim(remote_id=77),
            attempt(),
            "odoo_fault",
            payload,
            aware_now(),
        )
    database.assert_not_called()


@pytest.mark.parametrize(
    "payload",
    [
        "pass" + "word=hunter2",
        "https://remote.example/private?id=901",
        '{"remote_result":"secret"}',
        "caller supplied text",
        str(CLAIM_TOKEN),
    ],
)
def test_quarantine_rejects_all_caller_supplied_detail_text(monkeypatch, payload):
    database = MagicMock(side_effect=AssertionError("database must not be touched"))
    monkeypatch.setattr(store.db, "cursor", database)
    with pytest.raises(TypeError):
        store.quarantine(
            claim(),
            "ambiguous_mutation",
            payload,
            aware_now(),
        )
    database.assert_not_called()


@pytest.mark.parametrize("unknown_class", ["caller_text", "password", "remote_error"])
def test_definitive_failure_rejects_unknown_error_classes_before_database(
    monkeypatch, unknown_class
):
    database = MagicMock(side_effect=AssertionError("database must not be touched"))
    monkeypatch.setattr(store.db, "cursor", database)
    with pytest.raises(ValueError):
        store.record_definitive_failure(claim(remote_id=77), attempt(), unknown_class, aware_now())
    database.assert_not_called()


@pytest.mark.parametrize("unknown_reason", ["caller_text", "password", "remote_error"])
def test_quarantine_rejects_unknown_reasons_before_database(monkeypatch, unknown_reason):
    database = MagicMock(side_effect=AssertionError("database must not be touched"))
    monkeypatch.setattr(store.db, "cursor", database)
    with pytest.raises(ValueError):
        store.quarantine(claim(), unknown_reason, aware_now())
    database.assert_not_called()


def expired_row(*, attempt_state=None, **changes):
    row: dict[str, object] = {
        "feedback_id": 17,
        "desired_version": 1,
        "last_synced_version": 0,
        "odoo_improvement_id": 77,
        "claim_owner": "dead-worker",
        "claim_token": CLAIM_TOKEN,
        "claim_expires_at": aware_now() - timedelta(seconds=1),
        "active_attempt_id": ATTEMPT_ID if attempt_state else None,
        "attempt_count": 0,
        "attempt_id": ATTEMPT_ID if attempt_state else None,
        "projection_version": 1 if attempt_state else None,
        "attempt_state": attempt_state,
        "attempt_remote_id": 77 if attempt_state == "rpc_succeeded" else None,
    }
    row.update(changes)
    return row


@pytest.mark.parametrize("attempt_state", [None, "prepared", "rpc_succeeded"])
def test_recover_expired_releases_only_safe_or_readback_only_states(monkeypatch, attempt_state):
    cursor = use_cursor(
        monkeypatch,
        [expired_row(attempt_state=attempt_state)],
        [{"feedback_id": 17}],
    )
    assert store.recover_expired_claims(aware_now(), limit=10) == 1
    select_sql = normalized_sql(cursor, 0)
    assert "FOR UPDATE OF s SKIP LOCKED" in select_sql
    assert "LIMIT %s" in select_sql
    update_sql = normalized_sql(cursor, 1)
    assert "state = 'idle'" in update_sql
    assert "claim_token = %s" in update_sql
    if attempt_state is None:
        assert "active_attempt_id IS NULL" in update_sql
    else:
        assert "active_attempt_id = %s" in update_sql


def test_recover_expired_dispatch_marked_quarantines_attempt_and_sync(monkeypatch):
    cursor = use_cursor(
        monkeypatch,
        [expired_row(attempt_state="dispatch_marked")],
        [{"attempt_id": ATTEMPT_ID}],
        [{"feedback_id": 17}],
    )
    assert store.recover_expired_claims(aware_now(), limit=1) == 1
    assert "state = 'ambiguous'" in normalized_sql(cursor, 1)
    assert "state = 'quarantined'" in normalized_sql(cursor, 2)
    assert "active_attempt_id = %s" in normalized_sql(cursor, 2)


@pytest.mark.parametrize("unexpected", ["verified", "definitive_failed", "ambiguous"])
def test_recover_expired_unexpected_attempt_state_fails_closed_and_rolls_back(
    monkeypatch, unexpected
):
    cursor = use_cursor(monkeypatch, [expired_row(attempt_state=unexpected)])
    with pytest.raises(store.StateTransitionError):
        store.recover_expired_claims(aware_now(), limit=1)
    assert len(cursor.executions) == 1
    assert_rollback(cursor)


def test_recover_expired_guard_failure_rolls_back_batch(monkeypatch):
    cursor = use_cursor(
        monkeypatch,
        [expired_row(attempt_state=None)],
        [],
    )
    with pytest.raises(store.StateTransitionError):
        store.recover_expired_claims(aware_now(), limit=1)
    assert_rollback(cursor)


@pytest.mark.parametrize("attempt_remote_id", [None, True, 0, -1, 88])
def test_recover_expired_rpc_succeeded_requires_exact_attempt_remote_identity(
    monkeypatch, attempt_remote_id
):
    cursor = use_cursor(
        monkeypatch,
        [
            expired_row(
                attempt_state="rpc_succeeded",
                attempt_remote_id=attempt_remote_id,
            )
        ],
    )
    with pytest.raises(store.StateTransitionError):
        store.recover_expired_claims(aware_now(), limit=1)
    assert len(cursor.executions) == 1
    assert_rollback(cursor)


def quarantined_operator_row(**changes) -> dict[str, object]:
    row = {
        "feedback_id": 17,
        "desired_version": 3,
        "last_synced_version": 2,
        "odoo_improvement_id": 77,
        "due_at": aware_now() + timedelta(minutes=10),
        "attempt_count": 2,
        "sync_state": "quarantined",
        "claim_owner": None,
        "claim_token": None,
        "claim_expires_at": None,
        "active_attempt_id": ATTEMPT_ID,
        "quarantine_reason": "ambiguous_mutation",
        "quarantined_at": aware_now(),
        "attempt_id": ATTEMPT_ID,
        "attempt_feedback_id": 17,
        "attempt_projection_version": 3,
        "attempt_state": "ambiguous",
        "attempt_remote_id": 77,
        "feedback_projection_version": 3,
        "feedback_status": "requested",
        "feedback_lifecycle_origin": "local",
    }
    row.update(changes)
    return row


def pre_attempt_quarantine_row(**changes) -> dict[str, object]:
    row = {
        "feedback_id": 17,
        "desired_version": 3,
        "last_synced_version": 0,
        "odoo_improvement_id": None,
        "due_at": aware_now() + timedelta(minutes=10),
        "attempt_count": 0,
        "sync_state": "quarantined",
        "claim_owner": None,
        "claim_token": None,
        "claim_expires_at": None,
        "active_attempt_id": None,
        "last_error_class": "target_identity_or_contract_mismatch",
        "last_error_summary": "The Odoo target or fields changed.",
        "quarantine_reason": "target_identity_or_contract_mismatch",
        "quarantined_at": aware_now(),
        "feedback_projection_version": 3,
        "feedback_status": "requested",
        "feedback_lifecycle_origin": "local",
    }
    row.update(changes)
    return row


def quarantined_readback_row(**changes) -> dict[str, object]:
    row = attempt_row(
        projection_version=3,
        state="ambiguous",
        outcome_detail="readback_mismatch",
        created_at=aware_now() - timedelta(seconds=3),
        dispatch_marked_at=aware_now() - timedelta(seconds=2),
        rpc_succeeded_at=aware_now() - timedelta(seconds=1),
        updated_at=aware_now(),
    )
    row.update(
        {
            "sync_desired_version": 3,
            "sync_last_synced_version": 2,
            "sync_remote_id": 77,
            "sync_state": "quarantined",
            "sync_claim_owner": None,
            "sync_claim_token": None,
            "sync_claim_expires_at": None,
            "sync_active_attempt_id": ATTEMPT_ID,
            "sync_attempt_count": 0,
            "sync_quarantine_reason": "readback_mismatch",
            "sync_quarantined_at": aware_now(),
            "sync_updated_at": aware_now(),
            "sync_last_error_class": "readback_mismatch",
            "sync_last_error_summary": "The Odoo copy did not match the saved feedback.",
            "feedback_projection_version": 3,
        }
    )
    row.update(changes)
    return row


def test_load_quarantined_readback_evidence_uses_one_exact_read_only_join(monkeypatch):
    cursor = use_cursor(monkeypatch, [quarantined_readback_row()])

    evidence = store.load_quarantined_readback_evidence(ATTEMPT_ID)

    assert evidence.feedback_id == 17
    assert evidence.attempt_id == ATTEMPT_ID
    assert evidence.projection_version == 3
    assert evidence.remote_id == 77
    assert evidence.state == "quarantined"
    assert evidence.reason == "readback_mismatch"
    assert evidence.attempt.state == "ambiguous"
    assert len(cursor.executions) == 1
    sql_text = normalized_sql(cursor, 0)
    assert "WHERE a.attempt_id = %s" in sql_text
    assert "s.active_attempt_id = a.attempt_id" in sql_text
    assert "s.quarantine_reason = 'readback_mismatch'" in sql_text
    assert "a.outcome_detail = 'readback_mismatch'" in sql_text
    for chronology_fence in (
        "a.created_at <= a.dispatch_marked_at",
        "a.dispatch_marked_at <= a.rpc_succeeded_at",
        "a.rpc_succeeded_at <= a.updated_at",
        "s.quarantined_at = a.updated_at",
        "s.updated_at = a.updated_at",
    ):
        assert chronology_fence in sql_text
    assert "s.attempt_count = 0" not in sql_text
    assert "LIMIT 2" in sql_text
    assert "FOR UPDATE" not in sql_text
    assert not any(word in sql_text for word in ("INSERT ", "UPDATE ", "DELETE "))
    assert cursor.executions[0][1] == (ATTEMPT_ID,)
    assert "Safe" not in repr(evidence)


@pytest.mark.parametrize(
    "changes",
    [
        {"sync_state": "idle"},
        {"sync_quarantine_reason": "ambiguous_mutation"},
        {"sync_active_attempt_id": UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")},
        {"attempt_state": "rpc_succeeded", "state": "rpc_succeeded"},
        {"outcome_detail": None},
        {"sync_remote_id": 88},
        {"sync_desired_version": 4},
        {"feedback_projection_version": 4},
        {"created_at": aware_now() - timedelta(seconds=1)},
        {"updated_at": aware_now() - timedelta(seconds=2)},
        {"sync_quarantined_at": aware_now() + timedelta(seconds=1)},
        {"sync_updated_at": aware_now() + timedelta(seconds=1)},
    ],
)
def test_load_quarantined_readback_evidence_rejects_changed_authority(
    monkeypatch, changes
):
    cursor = use_cursor(monkeypatch, [quarantined_readback_row(**changes)])

    with pytest.raises(store.StateTransitionError):
        store.load_quarantined_readback_evidence(ATTEMPT_ID)

    assert len(cursor.executions) == 1


@pytest.mark.parametrize("attempt_count", [1, 2])
def test_load_quarantined_readback_evidence_accepts_prior_retry_count(
    monkeypatch, attempt_count
):
    cursor = use_cursor(
        monkeypatch,
        [quarantined_readback_row(sync_attempt_count=attempt_count)],
    )

    evidence = store.load_quarantined_readback_evidence(ATTEMPT_ID)

    assert evidence.attempt_count == attempt_count
    assert len(cursor.executions) == 1


def test_pre_attempt_release_appends_audit_then_releases_only_exact_locked_authority(
    monkeypatch,
):
    locked = pre_attempt_quarantine_row()
    cursor = use_cursor(
        monkeypatch,
        [locked],
        [
            {
                "id": 1,
                "feedback_id": 17,
                "projection_version": 3,
                "quarantine_reason": "target_identity_or_contract_mismatch",
                "quarantined_at": aware_now(),
            }
        ],
        [
            {
                "feedback_id": 17,
                "desired_version": 3,
                "last_synced_version": 0,
                "odoo_improvement_id": None,
                "due_at": aware_now(),
                "attempt_count": 0,
                "state": "idle",
                "claim_owner": None,
                "claim_token": None,
                "claim_expires_at": None,
                "active_attempt_id": None,
                "last_error_class": None,
                "last_error_summary": None,
                "quarantine_reason": None,
                "quarantined_at": None,
            }
        ],
    )

    result = store.release_pre_attempt_quarantine(
        feedback_id=17,
        reviewer="  Dale Gruber  ",
        now=aware_now(),
    )

    assert result == store.PreAttemptReleaseResult(
        feedback_id=17,
        desired_version=3,
        state="idle",
    )
    with pytest.raises(FrozenInstanceError):
        result.state = "quarantined"
    assert len(cursor.executions) == 3
    lock_sql = normalized_sql(cursor, 0)
    assert "JOIN feedback f ON f.id = s.feedback_id" in lock_sql
    assert "FOR UPDATE OF f, s" in lock_sql
    audit_sql = normalized_sql(cursor, 1)
    update_sql = normalized_sql(cursor, 2)
    assert "INSERT INTO feedback_odoo_pre_attempt_releases" in audit_sql
    assert cursor.executions[1][1] == (
        17,
        3,
        "target_identity_or_contract_mismatch",
        aware_now(),
        "Dale Gruber",
    )
    set_clause = update_sql.split("SET", 1)[1].split("WHERE", 1)[0]
    assert "state = 'idle'" in set_clause
    assert "due_at = %s" in set_clause
    for cleared in (
        "claim_owner = NULL",
        "claim_token = NULL",
        "claim_expires_at = NULL",
        "last_error_class = NULL",
        "last_error_summary = NULL",
        "quarantine_reason = NULL",
        "quarantined_at = NULL",
    ):
        assert cleared in set_clause
    for preserved in (
        "desired_version",
        "last_synced_version",
        "odoo_improvement_id",
        "active_attempt_id",
        "attempt_count",
    ):
        assert preserved not in set_clause
    update_fence = update_sql.split("WHERE", 1)[1].split("RETURNING", 1)[0]
    for authority in (
        "desired_version",
        "last_synced_version",
        "odoo_improvement_id",
        "attempt_count",
        "active_attempt_id",
        "quarantine_reason",
        "quarantined_at",
    ):
        assert authority in update_fence


@pytest.mark.parametrize(
    "changes",
    [
        {"quarantine_reason": "retry_exhausted"},
        {"active_attempt_id": ATTEMPT_ID},
        {"odoo_improvement_id": 77},
        {"attempt_count": 1},
        {"last_synced_version": 3},
        {"feedback_projection_version": 4},
        {"feedback_status": None},
        {"sync_state": "idle"},
    ],
)
def test_pre_attempt_release_refuses_nonexact_locked_authority(monkeypatch, changes):
    cursor = use_cursor(monkeypatch, [pre_attempt_quarantine_row(**changes)])

    with pytest.raises(store.StateTransitionError):
        store.release_pre_attempt_quarantine(
            feedback_id=17,
            reviewer="Dale Gruber",
            now=aware_now(),
        )

    assert len(cursor.executions) == 1
    assert_rollback(cursor)


@pytest.mark.parametrize("reviewer", ["", "   ", "Dale\nsecret", None])
def test_pre_attempt_release_rejects_blank_or_malformed_reviewer_before_database(
    monkeypatch, reviewer
):
    database = MagicMock(side_effect=AssertionError("database opened"))
    monkeypatch.setattr(store.db, "cursor", database)

    with pytest.raises(ValueError, match="reviewer"):
        store.release_pre_attempt_quarantine(
            feedback_id=17,
            reviewer=reviewer,
            now=aware_now(),
        )

    database.assert_not_called()


def test_repeated_pre_attempt_release_fails_safely_without_more_audit(monkeypatch):
    cursor = use_cursor(monkeypatch, [])

    with pytest.raises(store.StateTransitionError):
        store.release_pre_attempt_quarantine(
            feedback_id=17,
            reviewer="Dale Gruber",
            now=aware_now(),
        )

    assert len(cursor.executions) == 1
    assert_rollback(cursor)


@pytest.mark.parametrize(
    "values",
    [
        {"feedback_id": 0, "desired_version": 3, "state": "idle"},
        {"feedback_id": 17, "desired_version": 0, "state": "idle"},
        {"feedback_id": 17, "desired_version": 3, "state": "quarantined"},
    ],
)
def test_pre_attempt_release_result_rejects_malformed_authority(values):
    with pytest.raises(ValueError):
        store.PreAttemptReleaseResult(**values)


def test_quarantine_listing_is_frozen_bounded_ordered_and_privacy_safe(monkeypatch):
    rows = [
        {
            "feedback_id": 17,
            "attempt_id": ATTEMPT_ID,
            "quarantine_reason": "ambiguous_mutation",
            "sync_state": "quarantined",
            "quarantined_at": aware_now(),
            "attempt_count": 2,
        },
        {
            "feedback_id": 18,
            "attempt_id": None,
            "quarantine_reason": "duplicate_compound_identity",
            "sync_state": "quarantined",
            "quarantined_at": aware_now() + timedelta(seconds=1),
            "attempt_count": 0,
        },
    ]
    cursor = use_cursor(monkeypatch, rows)

    result = store.list_quarantined(limit=100)

    assert result == (
        store.QuarantineItem(
            feedback_id=17,
            attempt_id=ATTEMPT_ID,
            reason="ambiguous_mutation",
            state="quarantined",
            quarantined_at=aware_now(),
            attempt_count=2,
        ),
        store.QuarantineItem(
            feedback_id=18,
            attempt_id=None,
            reason="duplicate_compound_identity",
            state="quarantined",
            quarantined_at=aware_now() + timedelta(seconds=1),
            attempt_count=0,
        ),
    )
    sql_text = normalized_sql(cursor, 0)
    assert "ORDER BY quarantined_at, feedback_id" in sql_text
    assert "LIMIT %s" in sql_text
    assert cursor.executions[0][1] == (100,)
    selected = sql_text.split("FROM", 1)[0].casefold()
    for forbidden in (
        "reviewer",
        "manifest",
        "message",
        "submitter",
        "odoo_improvement_id",
        "claim_owner",
        "claim_token",
        "last_error",
    ):
        assert forbidden not in selected
    with pytest.raises(FrozenInstanceError):
        result[0].state = "idle"


@pytest.mark.parametrize(
    "rows",
    [
        [
            {
                "feedback_id": 18,
                "attempt_id": None,
                "quarantine_reason": "duplicate_compound_identity",
                "sync_state": "quarantined",
                "quarantined_at": aware_now(),
                "attempt_count": 0,
            },
            {
                "feedback_id": 17,
                "attempt_id": None,
                "quarantine_reason": "duplicate_compound_identity",
                "sync_state": "quarantined",
                "quarantined_at": aware_now(),
                "attempt_count": 0,
            },
        ],
        [
            {
                "feedback_id": 17,
                "attempt_id": None,
                "quarantine_reason": "caller supplied reason",
                "sync_state": "quarantined",
                "quarantined_at": aware_now(),
                "attempt_count": 0,
            }
        ],
        [quarantined_operator_row(sync_state="idle")],
    ],
)
def test_quarantine_listing_rejects_unordered_or_malformed_database_rows(monkeypatch, rows):
    use_cursor(monkeypatch, rows)
    with pytest.raises(store.StateTransitionError):
        store.list_quarantined(limit=100)


@pytest.mark.parametrize("limit", [None, True, 0, 101, 1.0, "10"])
def test_quarantine_listing_rejects_nonexact_limits_before_database(monkeypatch, limit):
    database = MagicMock(side_effect=AssertionError("database opened"))
    monkeypatch.setattr(store.db, "cursor", database)
    with pytest.raises(ValueError):
        store.list_quarantined(limit=limit)
    database.assert_not_called()


def test_keep_quarantine_appends_one_audit_without_changing_sync_or_attempt(monkeypatch):
    cursor = use_cursor(
        monkeypatch,
        [quarantined_operator_row()],
        [{"id": 1, "attempt_id": ATTEMPT_ID, "action": "keep"}],
    )

    result = store.apply_quarantine_disposition(
        attempt_id=ATTEMPT_ID,
        disposition="keep",
        reviewer="  Human Operator  ",
        human_review_confirmed=False,
        now=aware_now(),
    )

    assert result == store.QuarantineDispositionResult(
        feedback_id=17,
        attempt_id=ATTEMPT_ID,
        disposition="keep",
        state="quarantined",
        desired_version=3,
        warning=None,
    )
    assert len(cursor.executions) == 2
    assert "FOR UPDATE OF f, s, a" in normalized_sql(cursor, 0)
    assert "INSERT INTO feedback_odoo_operator_actions" in normalized_sql(cursor, 1)
    assert cursor.executions[1][1][2] == "Human Operator"
    assert "Human Operator" not in repr(result)


@pytest.mark.parametrize("attempt_state", ["prepared", "definitive_failed"])
def test_release_definitive_retains_attempt_and_leaves_same_version_idle_due(
    monkeypatch, attempt_state
):
    locked = quarantined_operator_row(
        attempt_state=attempt_state,
        quarantine_reason="retry_exhausted",
        attempt_count=store.MAX_MUTATION_ATTEMPTS,
    )
    cursor = use_cursor(
        monkeypatch,
        [locked],
        [
            {
                "id": 1,
                "attempt_id": ATTEMPT_ID,
                "action": "release_definitive",
            }
        ],
        [
            {
                "feedback_id": 17,
                "desired_version": 3,
                "last_synced_version": 2,
                "state": "idle",
                "active_attempt_id": None,
                "due_at": aware_now(),
                "attempt_count": 0,
            }
        ],
    )

    result = store.apply_quarantine_disposition(
        attempt_id=ATTEMPT_ID,
        disposition="release-definitive",
        reviewer="Human Operator",
        human_review_confirmed=False,
        now=aware_now(),
    )

    assert result.state == "idle"
    assert result.desired_version == 3
    statements = [normalized_sql(cursor, index) for index in range(len(cursor.executions))]
    assert all("UPDATE feedback_odoo_attempts" not in sql for sql in statements)
    assert all("DELETE" not in sql for sql in statements)
    update = statements[2]
    set_clause = update.split("SET", 1)[1].split("WHERE", 1)[0]
    assert "state = 'idle'" in set_clause
    assert "active_attempt_id = NULL" in set_clause
    assert "due_at = %s" in set_clause
    assert "attempt_count = 0" in set_clause
    for preserved in ("desired_version", "last_synced_version", "odoo_improvement_id"):
        assert preserved not in set_clause
    assert "attempt_count" in update.split("RETURNING", 1)[1]


@pytest.mark.parametrize(
    "attempt_state",
    ["dispatch_marked", "rpc_succeeded", "ambiguous", "verified"],
)
def test_release_definitive_rejects_unsafe_attempt_states_and_rolls_back(
    monkeypatch, attempt_state
):
    cursor = use_cursor(monkeypatch, [quarantined_operator_row(attempt_state=attempt_state)])
    with pytest.raises(store.StateTransitionError):
        store.apply_quarantine_disposition(
            attempt_id=ATTEMPT_ID,
            disposition="release-definitive",
            reviewer="Human Operator",
            human_review_confirmed=False,
            now=aware_now(),
        )
    assert len(cursor.executions) == 1
    assert_rollback(cursor)


def test_supersede_ambiguous_advances_feedback_and_desire_exactly_once(monkeypatch):
    cursor = use_cursor(
        monkeypatch,
        [quarantined_operator_row(attempt_state="ambiguous")],
        [
            {
                "id": 1,
                "attempt_id": ATTEMPT_ID,
                "action": "supersede_and_retry",
            }
        ],
        [{"id": 17, "projection_version": 4}],
        [
            {
                "feedback_id": 17,
                "desired_version": 4,
                "last_synced_version": 2,
                "state": "idle",
                "active_attempt_id": None,
                "due_at": aware_now(),
                "attempt_count": 0,
            }
        ],
    )

    result = store.apply_quarantine_disposition(
        attempt_id=ATTEMPT_ID,
        disposition="supersede-and-retry",
        reviewer="Human Operator",
        human_review_confirmed=True,
        now=aware_now(),
    )

    assert result == store.QuarantineDispositionResult(
        feedback_id=17,
        attempt_id=ATTEMPT_ID,
        disposition="supersede-and-retry",
        state="idle",
        desired_version=4,
        warning=store.DUPLICATE_RISK_WARNING,
    )
    statements = [normalized_sql(cursor, index) for index in range(len(cursor.executions))]
    assert "projection_version = projection_version + 1" in statements[2]
    assert "desired_version = desired_version + 1" in statements[3]
    assert all("UPDATE feedback_odoo_attempts" not in sql for sql in statements)
    assert all("DELETE" not in sql for sql in statements)
    sync_set = statements[3].split("SET", 1)[1].split("WHERE", 1)[0]
    assert "odoo_improvement_id" not in sync_set
    assert "last_synced_version" not in sync_set
    assert "active_attempt_id = NULL" in sync_set
    assert "attempt_count = 0" in sync_set
    assert "attempt_count" in statements[3].split("RETURNING", 1)[1]


@pytest.mark.parametrize(
    ("disposition", "attempt_state", "confirmed"),
    [
        ("release-definitive", "definitive_failed", False),
        ("supersede-and-retry", "ambiguous", True),
    ],
)
def test_retry_disposition_requires_exact_returned_sync_fence_and_fresh_budget(
    monkeypatch, disposition, attempt_state, confirmed
):
    locked = quarantined_operator_row(
        attempt_state=attempt_state,
        quarantine_reason=(
            "retry_exhausted" if disposition == "release-definitive" else "ambiguous_mutation"
        ),
        attempt_count=store.MAX_MUTATION_ATTEMPTS,
    )
    desired = 4 if disposition == "supersede-and-retry" else 3
    results = [
        [locked],
        [{"id": 1, "attempt_id": ATTEMPT_ID, "action": disposition.replace("-", "_")}],
    ]
    if disposition == "supersede-and-retry":
        results.append([{"id": 17, "projection_version": desired}])
    results.append(
        [
            {
                "feedback_id": 17,
                "desired_version": desired,
                "last_synced_version": 1,
                "state": "idle",
                "active_attempt_id": None,
                "due_at": aware_now(),
                "attempt_count": 0,
            }
        ]
    )
    cursor = use_cursor(monkeypatch, *results)

    with pytest.raises(store.StateTransitionError, match="different state"):
        store.apply_quarantine_disposition(
            attempt_id=ATTEMPT_ID,
            disposition=disposition,
            reviewer="Human Operator",
            human_review_confirmed=confirmed,
            now=aware_now(),
        )

    assert_rollback(cursor)


@pytest.mark.parametrize(
    ("disposition", "attempt_state", "confirmed"),
    [
        ("release-definitive", "definitive_failed", False),
        ("supersede-and-retry", "ambiguous", True),
    ],
)
def test_retry_disposition_rejects_nonzero_returned_retry_budget(
    monkeypatch, disposition, attempt_state, confirmed
):
    locked = quarantined_operator_row(
        attempt_state=attempt_state,
        quarantine_reason=(
            "retry_exhausted" if disposition == "release-definitive" else "ambiguous_mutation"
        ),
        attempt_count=store.MAX_MUTATION_ATTEMPTS,
    )
    desired = 4 if disposition == "supersede-and-retry" else 3
    results = [
        [locked],
        [{"id": 1, "attempt_id": ATTEMPT_ID, "action": disposition.replace("-", "_")}],
    ]
    if disposition == "supersede-and-retry":
        results.append([{"id": 17, "projection_version": desired}])
    results.append(
        [
            {
                "feedback_id": 17,
                "desired_version": desired,
                "last_synced_version": 2,
                "state": "idle",
                "active_attempt_id": None,
                "due_at": aware_now(),
                "attempt_count": 1,
            }
        ]
    )
    cursor = use_cursor(monkeypatch, *results)

    with pytest.raises(store.StateTransitionError, match="different state"):
        store.apply_quarantine_disposition(
            attempt_id=ATTEMPT_ID,
            disposition=disposition,
            reviewer="Human Operator",
            human_review_confirmed=confirmed,
            now=aware_now(),
        )

    assert_rollback(cursor)


def test_supersede_confirmation_is_authoritative_in_store_before_database(monkeypatch):
    database = MagicMock(side_effect=AssertionError("database opened"))
    monkeypatch.setattr(store.db, "cursor", database)

    with pytest.raises(ValueError, match="human review"):
        store.apply_quarantine_disposition(
            attempt_id=ATTEMPT_ID,
            disposition="supersede-and-retry",
            reviewer="Human Operator",
            human_review_confirmed=False,
            now=aware_now(),
        )

    database.assert_not_called()


def test_supersede_signed_64_max_fails_after_lock_but_before_audit(monkeypatch):
    cursor = use_cursor(
        monkeypatch,
        [
            quarantined_operator_row(
                desired_version=MAX_SIGNED_64,
                last_synced_version=MAX_SIGNED_64 - 1,
                attempt_projection_version=MAX_SIGNED_64,
                feedback_projection_version=MAX_SIGNED_64,
            )
        ],
    )

    with pytest.raises(store.StateTransitionError, match="cannot advance"):
        store.apply_quarantine_disposition(
            attempt_id=ATTEMPT_ID,
            disposition="supersede-and-retry",
            reviewer="Human Operator",
            human_review_confirmed=True,
            now=aware_now(),
        )

    assert len(cursor.executions) == 1
    assert "INSERT INTO feedback_odoo_operator_actions" not in normalized_sql(cursor, 0)
    assert_rollback(cursor)


@pytest.mark.parametrize(
    "reviewer",
    ["", "   ", "name\nnext", "name\tvalue", "x" * 129, 17, None],
)
def test_disposition_rejects_hostile_reviewer_before_database(monkeypatch, reviewer):
    database = MagicMock(side_effect=AssertionError("database opened"))
    monkeypatch.setattr(store.db, "cursor", database)

    with pytest.raises(ValueError, match="reviewer"):
        store.apply_quarantine_disposition(
            attempt_id=ATTEMPT_ID,
            disposition="keep",
            reviewer=reviewer,
            human_review_confirmed=False,
            now=aware_now(),
        )

    database.assert_not_called()


@pytest.mark.parametrize(
    "changes",
    [
        {"active_attempt_id": UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")},
        {"attempt_feedback_id": 18},
        {"sync_state": "idle"},
        {"claim_owner": "worker-a"},
        {"feedback_projection_version": 2},
        {"desired_version": MAX_SIGNED_64},
    ],
)
def test_disposition_rejects_malformed_or_changed_locked_authority(monkeypatch, changes):
    cursor = use_cursor(monkeypatch, [quarantined_operator_row(**changes)])
    with pytest.raises(store.StateTransitionError):
        store.apply_quarantine_disposition(
            attempt_id=ATTEMPT_ID,
            disposition="supersede-and-retry",
            reviewer="Human Operator",
            human_review_confirmed=True,
            now=aware_now(),
        )
    assert len(cursor.executions) == 1
    assert_rollback(cursor)


def test_disposition_rolls_back_audit_when_state_update_cardinality_fails(monkeypatch):
    cursor = use_cursor(
        monkeypatch,
        [quarantined_operator_row(attempt_state="definitive_failed")],
        [
            {
                "id": 1,
                "attempt_id": ATTEMPT_ID,
                "action": "release_definitive",
            }
        ],
        [],
    )

    with pytest.raises(store.StateTransitionError):
        store.apply_quarantine_disposition(
            attempt_id=ATTEMPT_ID,
            disposition="release-definitive",
            reviewer="Human Operator",
            human_review_confirmed=False,
            now=aware_now(),
        )

    assert_rollback(cursor)


def test_verified_canary_evidence_uses_one_bounded_exact_join(monkeypatch):
    row = verified_canary_row()
    cursor = use_cursor(monkeypatch, [row])

    evidence = store.load_verified_canary_evidence(17)

    assert evidence.feedback_id == 17
    assert evidence.projection_version == 3
    assert evidence.remote_id == 901
    assert evidence.attempt.state == "verified"
    sql_text = normalized_sql(cursor, 0)
    assert "s.desired_version = s.last_synced_version" in sql_text
    assert "f.projection_version = s.last_synced_version" in sql_text
    assert "a.projection_version = s.last_synced_version" in sql_text
    assert "a.remote_id = s.odoo_improvement_id" in sql_text
    assert "a.state = 'verified'" in sql_text
    for exact_idle_fence in (
        "s.claim_owner IS NULL",
        "s.claim_token IS NULL",
        "s.claim_expires_at IS NULL",
        "s.active_attempt_id IS NULL",
        "s.attempt_count = 0",
        "s.quarantine_reason IS NULL",
        "s.quarantined_at IS NULL",
        "s.last_error_class IS NULL",
        "s.last_error_summary IS NULL",
        "a.dispatch_marked_at IS NOT NULL",
        "a.rpc_succeeded_at IS NOT NULL",
        "a.readback_at IS NOT NULL",
        "a.settled_at IS NOT NULL",
        "a.dispatch_marked_at <= a.rpc_succeeded_at",
        "a.rpc_succeeded_at <= a.readback_at",
        "a.readback_at = a.settled_at",
    ):
        assert exact_idle_fence in sql_text
    assert "LIMIT 2" in sql_text
    assert "private" not in repr(evidence)
    assert "901" not in repr(evidence)


@pytest.mark.parametrize("rows", [[], [attempt_row(state="verified")] * 2])
def test_verified_canary_evidence_rejects_missing_or_duplicate_rows(monkeypatch, rows):
    cursor = use_cursor(monkeypatch, rows)
    with pytest.raises(store.StateTransitionError):
        store.load_verified_canary_evidence(17)
    assert_rollback(cursor)


@pytest.mark.parametrize(
    "changes",
    [
        {"dispatch_marked_at": None},
        {"rpc_succeeded_at": None},
        {"readback_at": None},
        {"settled_at": None},
        {"dispatch_marked_at": datetime(2026, 8, 20, 17, 59, 57)},
        {
            "dispatch_marked_at": aware_now() - timedelta(seconds=1),
            "rpc_succeeded_at": aware_now() - timedelta(seconds=2),
        },
        {
            "readback_at": aware_now() - timedelta(seconds=1),
            "settled_at": aware_now(),
        },
    ],
)
def test_verified_canary_evidence_rejects_incomplete_or_impossible_transition_times(
    monkeypatch, changes
):
    cursor = use_cursor(monkeypatch, [verified_canary_row(**changes)])

    with pytest.raises(store.StateTransitionError):
        store.load_verified_canary_evidence(17)

    assert_rollback(cursor)


@pytest.mark.parametrize(
    "changes",
    [
        {"sync_claim_owner": "worker-a"},
        {"sync_claim_token": CLAIM_TOKEN},
        {"sync_claim_expires_at": aware_now() + timedelta(minutes=5)},
        {"sync_active_attempt_id": ATTEMPT_ID},
        {"sync_attempt_count": 1},
        {"sync_quarantine_reason": "retry_exhausted"},
        {"sync_quarantined_at": aware_now()},
        {"sync_last_error_class": "odoo_fault"},
        {"sync_last_error_summary": "fixed safe summary"},
    ],
)
def test_verified_canary_evidence_rejects_nonsettled_idle_authority(monkeypatch, changes):
    cursor = use_cursor(monkeypatch, [verified_canary_row(**changes)])

    with pytest.raises(store.StateTransitionError):
        store.load_verified_canary_evidence(17)

    assert_rollback(cursor)


_LOOPBACK_DATABASE_HOSTS = {"localhost", "127.0.0.1", "::1"}
_UNSAFE_DATABASE_DSN_OPTIONS = {"hostaddr", "service", "servicefile"}


def _parse_database_dsn(database_url: str | None) -> dict[str, str] | None:
    if not database_url:
        return None
    try:
        return psycopg2.extensions.parse_dsn(database_url)
    except (TypeError, psycopg2.Error):
        return None


def _database_integration_is_safe(database_url: str | None, explicit_opt_in: str | None) -> bool:
    if explicit_opt_in != "1":
        return False
    params = _parse_database_dsn(database_url)
    if not params or _UNSAFE_DATABASE_DSN_OPTIONS.intersection(params):
        return False
    return params.get("host") in _LOOPBACK_DATABASE_HOSTS and params.get("dbname", "").endswith(
        "_test"
    )


SAFE_TEST_DATABASE = _database_integration_is_safe(
    os.environ.get("DATABASE_URL"), os.environ.get("FEEDBACK_SYNC_TEST_DATABASE")
)


@pytest.mark.parametrize("opt_in", [None, "", "0", "true", "yes", " 1 "])
def test_database_guard_requires_exact_opt_in_without_connecting(monkeypatch, opt_in):
    connect = MagicMock(side_effect=AssertionError("must never connect"))
    monkeypatch.setattr(psycopg2, "connect", connect)
    assert (
        _database_integration_is_safe("postgresql" + "://" + "localhost/feedback_sync_test", opt_in)
        is False
    )
    connect.assert_not_called()


@pytest.mark.parametrize(
    "database_url",
    [
        None,
        "",
        "not a postgres dsn",
        "postgresql" + ":///" + "feedback_sync_test",
        "postgresql" + "://" + "localhost.example/feedback_sync_test",
        "postgresql" + "://" + "railway.example/feedback_sync_test",
        "postgresql" + "://" + "localhost/postgres",
        "postgresql" + "://" + "localhost/feedback_sync_test_copy",
        "postgresql" + "://" + "localhost/feedback_sync_test?hostaddr=127.0.0.1",
        "postgresql" + "://" + "localhost/feedback_sync_test?service=prod",
        "servicefile=/tmp/prod host=localhost dbname=feedback_sync_test",
    ],
)
def test_database_guard_rejects_invalid_or_unsafe_dsn_without_connecting(monkeypatch, database_url):
    connect = MagicMock(side_effect=AssertionError("must never connect"))
    monkeypatch.setattr(psycopg2, "connect", connect)
    assert _database_integration_is_safe(database_url, "1") is False
    connect.assert_not_called()


@pytest.mark.parametrize(
    "database_url",
    [
        "postgresql" + "://" + "localhost/feedback_sync_test",
        "postgresql" + "://" + "127.0.0.1:5432/feedback_sync_test",
        "postgresql" + "://" + "[::1]:5432/feedback_sync_test",
    ],
)
def test_database_guard_accepts_only_opted_in_loopback_test_database(database_url):
    assert _database_integration_is_safe(database_url, "1") is True


@pytest.fixture
def isolated_feedback_database(monkeypatch):
    """Create an isolated schema only inside an explicitly opted-in local test DB."""
    from zira_dashboard import db

    original_dsn = os.environ.get("DATABASE_URL")
    if not _database_integration_is_safe(
        original_dsn, os.environ.get("FEEDBACK_SYNC_TEST_DATABASE")
    ):
        pytest.skip("database safety gate changed before fixture setup")
    assert original_dsn is not None
    schema_name = f"feedback_sync_{uuid4().hex}"
    admin = psycopg2.connect(original_dsn)
    admin.autocommit = True
    with admin.cursor() as cursor:
        cursor.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema_name)))
    isolated_dsn = psycopg2.extensions.make_dsn(
        original_dsn, options=f"-csearch_path={schema_name}"
    )
    db.shutdown_pool()
    monkeypatch.setenv("DATABASE_URL", isolated_dsn)
    db.init_pool(minconn=1, maxconn=4)
    db.bootstrap_schema()
    try:
        yield db
    finally:
        db.shutdown_pool()
        with admin.cursor() as cursor:
            cursor.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema_name)))
        admin.close()


def insert_due_feedback(db, *, desired_version=1) -> int:
    with db.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO feedback (
                message, status, lifecycle_origin, projection_version
            ) VALUES (%s, 'requested', 'local', %s)
            RETURNING id
            """,
            (f"feedback sync test {uuid4()}", desired_version),
        )
        feedback_id = cursor.fetchone()["id"]
        cursor.execute(
            """
            INSERT INTO feedback_odoo_sync (feedback_id, desired_version, due_at)
            VALUES (%s, %s, %s)
            """,
            (feedback_id, desired_version, aware_now()),
        )
    return feedback_id


@pytest.mark.skipif(
    not SAFE_TEST_DATABASE,
    reason=(
        "requires FEEDBACK_SYNC_TEST_DATABASE=1 and a loopback DATABASE_URL "
        "whose database name ends in _test"
    ),
)
def test_local_postgres_concurrent_claim_due_has_exactly_one_winner(
    isolated_feedback_database,
):
    db = isolated_feedback_database
    feedback_id = insert_due_feedback(db)
    barrier = Barrier(2)

    def run(worker):
        barrier.wait()
        return store.claim_due(
            now=aware_now(),
            worker_id=worker,
            limit=1,
            canary_feedback_id=feedback_id,
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(run, ("worker-a", "worker-b")))
    assert sorted(len(value) for value in results) == [0, 1]


@pytest.mark.skipif(
    not SAFE_TEST_DATABASE,
    reason=(
        "requires FEEDBACK_SYNC_TEST_DATABASE=1 and a loopback DATABASE_URL "
        "whose database name ends in _test"
    ),
)
def test_local_postgres_second_statement_failure_rolls_back_prepared_attempt(
    isolated_feedback_database,
):
    db = isolated_feedback_database
    feedback_id = insert_due_feedback(db)
    active = store.claim_due(
        now=aware_now(),
        worker_id="worker-a",
        limit=1,
        canary_feedback_id=feedback_id,
    )[0]
    with db.cursor() as cursor:
        cursor.execute(
            """
            CREATE FUNCTION feedback_sync_skip_activation() RETURNS trigger
            LANGUAGE plpgsql AS $$
            BEGIN
              RETURN NULL;
            END
            $$
            """
        )
        cursor.execute(
            """
            CREATE TRIGGER feedback_sync_skip_activation
            BEFORE UPDATE ON feedback_odoo_sync
            FOR EACH ROW
            WHEN (NEW.active_attempt_id IS DISTINCT FROM OLD.active_attempt_id)
            EXECUTE FUNCTION feedback_sync_skip_activation()
            """
        )
    manifest, digest, binaries = manifest_values(feedback_id=feedback_id)

    with pytest.raises(store.StateTransitionError):
        store.prepare_attempt(
            claim=active,
            attempt_id=ATTEMPT_ID,
            mutation_kind="create",
            remote_id=None,
            manifest=manifest,
            manifest_digest=digest,
            binaries=binaries,
            now=aware_now(),
        )

    assert (
        db.query(
            "SELECT attempt_id FROM feedback_odoo_attempts WHERE feedback_id = %s",
            (feedback_id,),
        )
        == []
    )


@pytest.mark.skipif(
    not SAFE_TEST_DATABASE,
    reason=(
        "requires FEEDBACK_SYNC_TEST_DATABASE=1 and a loopback DATABASE_URL "
        "whose database name ends in _test"
    ),
)
def test_local_postgres_settlement_does_not_mark_newer_desired_version_synced(
    isolated_feedback_database,
):
    db = isolated_feedback_database
    feedback_id = insert_due_feedback(db)
    active = store.claim_due(
        now=aware_now(),
        worker_id="worker-a",
        limit=1,
        canary_feedback_id=feedback_id,
    )[0]
    fields = {
        "x_name": "Safe",
        "x_studio_source_id": f"GPI-PM-FB-{feedback_id}",
        "x_studio_source": "GPI Plant Manager",
        "x_studio_date_start": "2026-08-20",
        "x_studio_type": "Digital",
        "x_studio_status": "Requested",
    }
    manifest = {"fields": fields, "binary_evidence": {}}
    digest = hashlib.sha256(
        json.dumps(manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()
    prepared = store.prepare_attempt(
        claim=active,
        attempt_id=ATTEMPT_ID,
        mutation_kind="create",
        remote_id=None,
        manifest=manifest,
        manifest_digest=digest,
        binaries={},
        now=aware_now(),
    )
    store.mark_dispatch(active, prepared, aware_now())
    store.mark_rpc_succeeded(active, prepared, remote_id=901, now=aware_now())
    db.execute(
        "UPDATE feedback_odoo_sync SET desired_version = 2 WHERE feedback_id = %s",
        (feedback_id,),
    )
    assert store.settle_verified(active, prepared, remote_id=901, now=aware_now()) is True
    row = db.query(
        """
        SELECT desired_version, last_synced_version, state, active_attempt_id
        FROM feedback_odoo_sync WHERE feedback_id = %s
        """,
        (feedback_id,),
    )[0]
    assert row == {
        "desired_version": 2,
        "last_synced_version": 1,
        "state": "idle",
        "active_attempt_id": None,
    }


def test_projection_helper_used_by_tests_still_matches_task_6_public_contract():
    manifest, digest, binaries = manifest_values()
    projected = Projection(
        source_id="GPI-PM-FB-17",
        fields=manifest["fields"],
        binaries=binaries,
        manifest=manifest,
        manifest_digest=digest,
    )
    assert projected.manifest == manifest
    assert projected.manifest_digest == digest
