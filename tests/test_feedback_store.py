"""Focused unit and optional Postgres tests for feedback_store."""

from contextlib import contextmanager
import os

import pytest

from zira_dashboard import db, feedback_store
from zira_dashboard.feedback_image import NormalizedImage


class RecordingCursor:
    def __init__(self):
        self.calls = []

    def execute(self, sql, params):
        self.calls.append((" ".join(sql.split()), params))

    def fetchone(self):
        return {"id": 42}


def _record_transaction(monkeypatch):
    cursor = RecordingCursor()
    transactions = []

    @contextmanager
    def fake_cursor():
        transactions.append(cursor)
        yield cursor

    monkeypatch.setattr(feedback_store.db, "cursor", fake_cursor)
    return cursor, transactions


def test_create_submission_inserts_feedback_image_and_sync_intent_atomically(monkeypatch):
    cursor, transactions = _record_transaction(monkeypatch)
    image = NormalizedImage(
        jpeg_bytes=b"jpeg",
        sha256="a" * 64,
        byte_length=4,
        width=8,
        height=6,
    )

    feedback_id = feedback_store.create_submission(
        message="A safe report",
        submitter="tester@gruberpallets.com",
        page_url="/recycling",
        task_type="bug",
        status="requested",
        before_image=image,
    )

    assert feedback_id == 42
    assert transactions == [cursor]
    assert len(cursor.calls) == 4
    feedback_sql, feedback_params = cursor.calls[0]
    image_sql, image_params = cursor.calls[1]
    sync_sql, sync_params = cursor.calls[2]
    delivery_sql, delivery_params = cursor.calls[3]
    assert "INSERT INTO feedback" in feedback_sql
    assert "status, lifecycle_origin, projection_version, updated_at" in feedback_sql
    assert "'requested', 'local', 1, now()" in feedback_sql
    assert feedback_params == (
        "tester@gruberpallets.com",
        "/recycling",
        "bug",
        "A safe report",
    )
    assert "INSERT INTO feedback_images" in image_sql
    assert "'before'" in image_sql
    assert image_params == (42, b"jpeg", "a" * 64, 4, 8, 6)
    assert "INSERT INTO feedback_odoo_sync" in sync_sql
    assert "VALUES (%s, 1, 0, now(), 'idle')" in sync_sql
    assert sync_params == (42,)
    assert "INSERT INTO feedback_task_delivery" in delivery_sql
    assert delivery_params == (42,)


def test_create_submission_without_image_still_creates_sync_intent(monkeypatch):
    cursor, transactions = _record_transaction(monkeypatch)

    feedback_id = feedback_store.create_submission(
        message="No picture",
        submitter=None,
        page_url=None,
        task_type="feature",
    )

    assert feedback_id == 42
    assert transactions == [cursor]
    assert len(cursor.calls) == 3
    assert "INSERT INTO feedback" in cursor.calls[0][0]
    assert "INSERT INTO feedback_odoo_sync" in cursor.calls[1][0]
    assert "INSERT INTO feedback_task_delivery" in cursor.calls[2][0]
    assert cursor.calls[2][1] == (42,)
    assert all("feedback_images" not in sql for sql, _params in cursor.calls)


@pytest.mark.parametrize("task_type", ["floor_issue", "floor_suggestion"])
def test_create_submission_accepts_each_physical_type(monkeypatch, task_type):
    cursor, transactions = _record_transaction(monkeypatch)

    feedback_id = feedback_store.create_submission(
        message="Floor feedback",
        submitter=None,
        page_url=None,
        task_type=task_type,
    )

    assert feedback_id == 42
    assert transactions == [cursor]
    assert cursor.calls[0][1][2] == task_type


@pytest.mark.parametrize(
    ("task_type", "status", "message"),
    [
        ("other", "requested", "unsupported feedback type"),
        ("bug", "completed", "new feedback must start requested"),
    ],
)
def test_create_submission_rejects_invalid_initial_state_before_opening_transaction(
    monkeypatch, task_type, status, message
):
    monkeypatch.setattr(
        feedback_store.db,
        "cursor",
        lambda: (_ for _ in ()).throw(AssertionError("transaction opened")),
    )

    with pytest.raises(ValueError, match=message):
        feedback_store.create_submission(
            message="No",
            submitter=None,
            page_url=None,
            task_type=task_type,
            status=status,
        )


def test_for_submitter_selects_local_status(monkeypatch):
    seen = {}
    monkeypatch.setattr(
        feedback_store.db,
        "query",
        lambda sql, params: seen.update(sql=sql, params=params) or [],
    )

    feedback_store.for_submitter("tester@gruberpallets.com")

    selected = seen["sql"].split("FROM feedback", 1)[0]
    assert "status" in selected
    assert seen["params"] == ("tester@gruberpallets.com", 100)


needs_postgres = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"), reason="needs Postgres"
)


@pytest.fixture
def feedback_schema():
    db.init_pool()
    db.bootstrap_schema()


@needs_postgres
def test_insert_then_for_submitter_round_trip(feedback_schema):
    new_id = feedback_store.insert(
        message="Round-trip test message",
        submitter="tester@gruberpallets.com",
        page_url="/recycling",
        task_type="bug",
        odoo_task_id=999001,
    )
    try:
        assert isinstance(new_id, int)
        rows = feedback_store.for_submitter("tester@gruberpallets.com", limit=50)
        match = next((row for row in rows if row["id"] == new_id), None)
        assert match is not None
        assert match["message"] == "Round-trip test message"
        assert match["task_type"] == "bug"
        assert match["odoo_task_id"] == 999001
        assert match["status"] is None
    finally:
        db.execute("DELETE FROM feedback WHERE id = %s", (new_id,))
