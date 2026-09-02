"""Focused unit and optional Postgres tests for feedback_store."""

from contextlib import contextmanager

import pytest

from zira_dashboard import feedback_store
from zira_dashboard.feedback_image import NormalizedImage


MAX_SIGNED_64 = 9_223_372_036_854_775_807


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
        submitter=" Tester@GruberPallets.com ",
        submitter_employee_odoo_id=41,
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
        41,
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
    assert delivery_params == (42, 1, "requested", 2)


def test_create_submission_without_image_still_creates_sync_intent(monkeypatch):
    cursor, transactions = _record_transaction(monkeypatch)

    feedback_id = feedback_store.create_submission(
        message="No picture",
        submitter="ana@example.com",
        submitter_employee_odoo_id=41,
        page_url=None,
        task_type="feature",
    )

    assert feedback_id == 42
    assert transactions == [cursor]
    assert len(cursor.calls) == 3
    assert "INSERT INTO feedback" in cursor.calls[0][0]
    assert "INSERT INTO feedback_odoo_sync" in cursor.calls[1][0]
    assert "INSERT INTO feedback_task_delivery" in cursor.calls[2][0]
    assert cursor.calls[2][1] == (42, 1, "requested", 2)
    assert all("feedback_images" not in sql for sql, _params in cursor.calls)


@pytest.mark.parametrize("task_type", ["floor_issue", "floor_suggestion"])
def test_create_submission_accepts_each_physical_type(monkeypatch, task_type):
    cursor, transactions = _record_transaction(monkeypatch)

    feedback_id = feedback_store.create_submission(
        message="Floor feedback",
        submitter="ana@example.com",
        submitter_employee_odoo_id=41,
        page_url=None,
        task_type=task_type,
    )

    assert feedback_id == 42
    assert transactions == [cursor]
    assert cursor.calls[0][1][3] == task_type


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
            submitter="ana@example.com",
            submitter_employee_odoo_id=41,
            page_url=None,
            task_type=task_type,
            status=status,
        )


@pytest.mark.parametrize("employee_id", [None, True, 0, -1, MAX_SIGNED_64 + 1])
def test_create_submission_rejects_invalid_submitter_employee_id_before_transaction(
    monkeypatch, employee_id
):
    monkeypatch.setattr(
        feedback_store.db,
        "cursor",
        lambda: (_ for _ in ()).throw(AssertionError("transaction opened")),
    )

    with pytest.raises(ValueError, match="submitter employee id"):
        feedback_store.create_submission(
            message="No",
            submitter="ana@example.com",
            submitter_employee_odoo_id=employee_id,
            page_url=None,
            task_type="bug",
        )


def test_create_submission_rejects_external_repair_before_transaction(monkeypatch):
    monkeypatch.setattr(
        feedback_store.db,
        "cursor",
        lambda: (_ for _ in ()).throw(AssertionError("transaction opened")),
    )

    with pytest.raises(ValueError, match="unsupported feedback type"):
        feedback_store.create_submission(
            message="No",
            submitter="ana@example.com",
            submitter_employee_odoo_id=41,
            page_url=None,
            task_type="repair",
        )


@pytest.mark.parametrize("submitter", [None, "", "not-an-email", "two @example.com"])
def test_create_submission_rejects_invalid_submitter_email_before_transaction(
    monkeypatch, submitter
):
    monkeypatch.setattr(
        feedback_store.db,
        "cursor",
        lambda: (_ for _ in ()).throw(AssertionError("transaction opened")),
    )

    with pytest.raises(ValueError, match="submitter email"):
        feedback_store.create_submission(
            message="No",
            submitter=submitter,
            submitter_employee_odoo_id=41,
            page_url=None,
            task_type="bug",
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


class LifecycleCursor:
    def __init__(self, row):
        self.row = row
        self.calls = []

    def execute(self, sql, params):
        self.calls.append((" ".join(sql.split()), params))

    def fetchone(self):
        return self.row


def _lifecycle_cursor(monkeypatch, row):
    cursor = LifecycleCursor(row)

    @contextmanager
    def fake_cursor():
        yield cursor

    monkeypatch.setattr(feedback_store.db, "cursor", fake_cursor)
    return cursor


def test_lifecycle_state_reads_one_exact_local_row_with_task_sync_state(monkeypatch):
    row = {
        "id": 17,
        "status": "in_progress",
        "lifecycle_origin": "local",
        "projection_version": 3,
        "task_sync_state": "delivered",
        "task_desired_version": 3,
        "task_last_synced_version": 3,
        "task_desired_contract_version": 2,
        "task_last_synced_contract_version": 2,
    }
    cursor = _lifecycle_cursor(monkeypatch, row)

    result = feedback_store.lifecycle_state(17)

    assert dict(result) == row
    assert len(cursor.calls) == 1
    sql, params = cursor.calls[0]
    assert sql.startswith("SELECT f.id, f.status, f.lifecycle_origin")
    assert "LEFT JOIN feedback_task_delivery td" in sql
    assert "WHERE f.id = %s" in sql
    assert "UPDATE" not in sql
    assert "feedback_odoo_sync" not in sql
    assert params == (17,)


@pytest.mark.parametrize("feedback_id", [0, -1, MAX_SIGNED_64 + 1, True, "17"])
def test_lifecycle_state_rejects_invalid_ids_before_opening_a_transaction(
    monkeypatch, feedback_id
):
    monkeypatch.setattr(
        feedback_store.db,
        "cursor",
        lambda: (_ for _ in ()).throw(AssertionError("transaction opened")),
    )

    with pytest.raises(ValueError, match="feedback id"):
        feedback_store.lifecycle_state(feedback_id)


@pytest.mark.parametrize(
    "row",
    [
        None,
        {
            "id": 17,
            "status": "requested",
            "lifecycle_origin": "legacy_project_task",
            "projection_version": 3,
        },
        {
            "id": 17,
            "status": "requested",
            "lifecycle_origin": None,
            "projection_version": 3,
        },
    ],
)
def test_lifecycle_state_rejects_missing_or_nonlocal_rows_without_sync_changes(
    monkeypatch, row
):
    cursor = _lifecycle_cursor(monkeypatch, row)

    with pytest.raises(feedback_store.InvalidTransition):
        feedback_store.lifecycle_state(17)

    assert len(cursor.calls) == 1
    assert all("UPDATE" not in sql for sql, _params in cursor.calls)
    assert all("feedback_odoo_sync" not in sql for sql, _params in cursor.calls)
