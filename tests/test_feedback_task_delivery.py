"""Focused tests for the independent owner-task delivery outbox."""

from __future__ import annotations

import hashlib
from contextlib import AbstractContextManager
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest

from zira_dashboard import feedback_task_delivery as delivery


NOW = datetime(2026, 8, 26, 12, 0, tzinfo=UTC)
TOKEN = UUID("11111111-1111-1111-1111-111111111111")
EXPIRES = NOW + timedelta(minutes=2)


class RecordingCursor(AbstractContextManager):
    """Cursor with one scripted fetch result for each statement."""

    def __init__(self, *results: list[dict[str, object]]):
        self._results = list(results)
        self._current: list[dict[str, object]] = []
        self.calls: list[tuple[str, object]] = []

    def __exit__(self, exc_type, exc, traceback):
        return False

    def execute(self, sql, params=None):
        self.calls.append((str(sql), params))
        index = len(self.calls) - 1
        self._current = self._results[index] if index < len(self._results) else []

    def fetchone(self):
        return self._current[0] if self._current else None

    def fetchall(self):
        return list(self._current)


def use_cursor(monkeypatch, *results: list[dict[str, object]]) -> RecordingCursor:
    cursor = RecordingCursor(*results)
    monkeypatch.setattr(delivery.db, "cursor", lambda: cursor)
    return cursor


def normalized_sql(cursor: RecordingCursor, index: int) -> str:
    return " ".join(cursor.calls[index][0].split())


def claim(
    *,
    task_id: int | None = None,
    attachment_id: int | None = None,
    expires_at: datetime = EXPIRES,
):
    return delivery.TaskDeliveryClaim(
        feedback_id=42,
        claim_token=TOKEN,
        task_id=task_id,
        before_attachment_id=attachment_id,
        expires_at=expires_at,
    )


def valid_snapshot_row(**changes):
    raw = b"saved-before"
    row: dict[str, object] = {
        "feedback_id": 42,
        "task_type": "bug",
        "message": "Guard rail is loose.",
        "submitter": "operator@example.com",
        "page_url": "/line/1",
        "lifecycle_origin": "local",
        "status": "requested",
        "projection_version": 1,
        "resolution_note": None,
        "before_feedback_id": 42,
        "jpeg_bytes": raw,
        "sha256": hashlib.sha256(raw).hexdigest(),
        "byte_length": len(raw),
        "width": 8,
        "height": 8,
    }
    row.update(changes)
    return row


def assert_current_claim_predicate(statement: str) -> None:
    assert "feedback_id = %s" in statement
    assert "claim_token = %s" in statement


def test_claim_due_uses_skip_locked_and_returns_two_minute_lease(monkeypatch):
    monkeypatch.setattr(delivery, "uuid4", lambda: TOKEN)
    cursor = use_cursor(
        monkeypatch,
        [{"feedback_id": 42, "odoo_task_id": None, "before_attachment_id": None}],
        [
            {
                "feedback_id": 42,
                "claim_token": TOKEN,
                "odoo_task_id": None,
                "before_attachment_id": None,
                "claim_expires_at": EXPIRES,
            }
        ],
    )

    claims = delivery.claim_due(now=NOW, worker_id="task-worker")

    assert len(claims) == 1
    assert claims[0].feedback_id == 42
    assert "FOR UPDATE SKIP LOCKED" in normalized_sql(cursor, 0)
    update_sql = normalized_sql(cursor, 1)
    assert "state = 'in_flight'" in update_sql
    assert EXPIRES in cursor.calls[1][1]
    assert any(type(value) is UUID for value in cursor.calls[1][1])


def test_claim_due_caps_the_database_claim_batch_at_ten(monkeypatch):
    cursor = use_cursor(monkeypatch, [])

    assert delivery.claim_due(now=NOW, worker_id="task-worker", limit=99) == []

    assert cursor.calls[0][1][-1] == 10


def test_enqueue_submission_records_requested_lifecycle_intent():
    cursor = RecordingCursor()

    delivery.enqueue_submission(
        cursor, 42, desired_version=1, desired_status="requested"
    )

    sql, params = cursor.calls[0]
    assert "desired_version, last_synced_version, desired_status" in sql
    assert params == (42, 1, "requested")


def test_lifecycle_enqueue_advances_existing_intent():
    cursor = RecordingCursor([{"feedback_id": 42}])

    delivery.enqueue_lifecycle(
        cursor, 42, desired_version=3, desired_status="completed", now=NOW
    )

    sql, params = cursor.calls[0]
    assert "desired_version = %s" in sql
    assert "desired_status = %s" in sql
    assert "state = CASE WHEN state = 'in_flight' THEN state ELSE 'pending' END" in " ".join(sql.split())
    assert params == (3, "completed", NOW, NOW, 42, 3)


def test_existing_lifecycle_reconciliation_is_bounded_and_retains_task_identity(
    monkeypatch,
):
    cursor = use_cursor(monkeypatch, [{"queued": 1}])

    assert delivery.queue_existing_lifecycle_mismatches() == 1

    sql = normalized_sql(cursor, 0)
    assert "FOR UPDATE OF td SKIP LOCKED" in sql
    assert "td.odoo_task_id IS NOT NULL" in sql
    assert "td.state <> 'blocked'" in sql
    assert "desired_version = candidates.projection_version" in sql
    assert "desired_status = candidates.status" in sql
    assert "odoo_task_id" not in sql.split(" SET ", 1)[1].split(" FROM ", 1)[0]
    assert cursor.calls[0][1] == (100,)


def test_renew_claim_refuses_an_expired_or_reclaimed_lease(monkeypatch):
    expires = NOW + timedelta(minutes=2)
    active = delivery.TaskDeliveryClaim(
        feedback_id=42,
        claim_token=TOKEN,
        task_id=None,
        before_attachment_id=None,
        expires_at=expires,
    )
    cursor = use_cursor(monkeypatch, [])

    with pytest.raises(delivery.StateTransitionError):
        delivery.renew_claim(active, now=expires)

    statement = normalized_sql(cursor, 0)
    assert "claim_expires_at = %s" in statement
    assert "claim_expires_at > %s" in statement
    assert_current_claim_predicate(statement)


def test_schedule_retry_clears_lease_and_caps_backoff_at_one_hour(monkeypatch):
    due = NOW + timedelta(hours=1)
    cursor = use_cursor(
        monkeypatch,
        [{"feedback_id": 42, "attempt_count": 8}],
        [{"feedback_id": 42, "due_at": due}],
    )

    delivery.schedule_retry(claim(task_id=900), now=NOW)

    update_sql = normalized_sql(cursor, 1)
    assert "state = 'attention'" in update_sql
    assert "claim_owner = NULL, claim_token = NULL, claim_expires_at = NULL" in update_sql
    assert "Odoo task delivery needs attention and will retry." in cursor.calls[1][1]
    assert due in cursor.calls[1][1]
    assert_current_claim_predicate(update_sql)


def test_mark_delivered_requires_current_claim_token(monkeypatch):
    cursor = use_cursor(monkeypatch, [])

    with pytest.raises(delivery.StateTransitionError):
        delivery.mark_delivered(claim(task_id=900))

    statement = normalized_sql(cursor, 0)
    assert_current_claim_predicate(statement)


def test_block_records_an_allowlisted_owner_reason_without_another_attempt(monkeypatch):
    cursor = use_cursor(monkeypatch, [{"feedback_id": 42}])

    delivery.block(
        claim(task_id=900),
        "More than one matching owner task exists.",
        now=NOW,
    )

    statement = normalized_sql(cursor, 0)
    set_clause = statement.split(" SET ", 1)[1].split(" WHERE ", 1)[0]
    assert "state = 'blocked'" in set_clause
    assert "claim_owner = NULL, claim_token = NULL, claim_expires_at = NULL" in set_clause
    assert "due_at" not in set_clause
    assert "More than one matching owner task exists." in cursor.calls[0][1]
    assert_current_claim_predicate(statement)


def test_block_rejects_untrusted_reason_before_database_work(monkeypatch):
    cursor = use_cursor(monkeypatch)

    with pytest.raises(ValueError, match="block reason is not supported"):
        delivery.block(claim(task_id=900), "remote details are unsafe", now=NOW)

    assert cursor.calls == []


@pytest.mark.parametrize(
    "row",
    [
        valid_snapshot_row(lifecycle_origin="legacy_project_task"),
        valid_snapshot_row(byte_length=1),
    ],
)
def test_load_snapshot_rejects_nonlocal_and_malformed_rows(monkeypatch, row):
    use_cursor(monkeypatch, [row])

    with pytest.raises(delivery.SnapshotValidationError):
        delivery.load_snapshot(42)


def test_load_snapshot_locks_only_feedback_not_the_optional_image_join(monkeypatch):
    cursor = use_cursor(monkeypatch, [valid_snapshot_row()])

    delivery.load_snapshot(42)

    assert "FOR SHARE OF f" in normalized_sql(cursor, 0)


@pytest.mark.parametrize("task_type", ["floor_issue", "floor_suggestion"])
def test_load_snapshot_accepts_each_canonical_physical_type(monkeypatch, task_type):
    cursor = use_cursor(monkeypatch, [valid_snapshot_row(task_type=task_type)])

    item = delivery.load_snapshot(42)

    assert item.task_type == task_type
    assert cursor.calls[0][1] == (
        42,
        ["bug", "feature", "floor_issue", "floor_suggestion"],
    )


def test_recorded_remote_ids_are_guarded_by_the_current_claim_token(monkeypatch):
    task_cursor = use_cursor(
        monkeypatch,
        [
            {
                "feedback_id": 42,
                "claim_token": TOKEN,
                "odoo_task_id": 900,
                "before_attachment_id": None,
                "claim_expires_at": EXPIRES,
            }
        ],
    )
    saved_task = delivery.record_task_id(claim(), task_id=900)

    assert saved_task.task_id == 900
    assert_current_claim_predicate(normalized_sql(task_cursor, 0))

    attachment_cursor = use_cursor(
        monkeypatch,
        [
            {
                "feedback_id": 42,
                "claim_token": TOKEN,
                "odoo_task_id": 900,
                "before_attachment_id": 901,
                "claim_expires_at": EXPIRES,
            }
        ],
    )
    saved_attachment = delivery.record_before_attachment(saved_task, attachment_id=901)

    assert saved_attachment.before_attachment_id == 901
    assert_current_claim_predicate(normalized_sql(attachment_cursor, 0))


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        ("pending", ("Queued for app owner", None)),
        ("in_flight", ("Queued for app owner", None)),
        ("attention", ("Needs attention", "Odoo task delivery needs attention and will retry.")),
        ("delivered", ("Assigned to app owner", None)),
        ("blocked", ("Needs attention", "Task delivery needs owner review.")),
        (None, ("Needs attention", "Task delivery record is missing.")),
        ("untrusted database text", ("Needs attention", "Task delivery record is missing.")),
        ([], ("Needs attention", "Task delivery record is missing.")),
    ],
)
def test_admin_status_for_exposes_only_fixed_summaries(state, expected):
    assert delivery.admin_status_for(state) == expected


def test_admin_status_for_reads_only_the_state_from_an_admin_row():
    row = {
        "task_delivery_state": "attention",
        "task_delivery_error": "untrusted database error",
        "task_delivery_block_reason": "untrusted database reason",
    }

    assert delivery.admin_status_for(row) == (
        "Needs attention",
        "Odoo task delivery needs attention and will retry.",
    )
