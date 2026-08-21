"""Super-admin feedback lifecycle routes and local transaction behavior."""

from contextlib import contextmanager
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from PIL import Image

from zira_dashboard import feedback_store
from zira_dashboard.feedback_image import MAX_INPUT_BYTES, NormalizedImage
from zira_dashboard.routes import feedback_admin


def valid_png_bytes() -> bytes:
    output = BytesIO()
    Image.new("RGB", (8, 8), "green").save(output, format="PNG")
    return output.getvalue()


def admin_client(upn: str) -> TestClient:
    test_app = FastAPI()

    @test_app.middleware("http")
    async def set_identity(request: Request, call_next):
        request.state.user_upn = upn
        request.state.user_name = upn
        return await call_next(request)

    test_app.include_router(feedback_admin.router)
    return TestClient(test_app, follow_redirects=False)


class ReturningCursor:
    def __init__(self, row):
        self.row = row
        self.executions = []

    def execute(self, sql, params=None):
        self.executions.append((" ".join(sql.split()), params))

    def fetchone(self):
        return self.row


@contextmanager
def cursor_returning(row):
    yield ReturningCursor(row)


def normalized_image() -> NormalizedImage:
    return NormalizedImage(
        jpeg_bytes=b"jpeg",
        sha256="a" * 64,
        byte_length=4,
        width=8,
        height=6,
    )


def test_non_super_admin_cannot_view_or_change_feedback_before_work(monkeypatch):
    monkeypatch.setenv("SUPER_ADMIN_UPNS", "dale@gruberpallets.com")

    def unexpected(*args, **kwargs):
        raise AssertionError("feedback work must not run before authorization")

    monkeypatch.setattr(feedback_store, "for_admin", unexpected)
    monkeypatch.setattr(feedback_store, "transition", unexpected)
    monkeypatch.setattr(feedback_admin, "normalize_image", unexpected)

    with admin_client("person@gruberpallets.com") as client:
        assert client.get("/admin/feedback").status_code == 403
        response = client.post(
            "/admin/feedback/7/status",
            data={"status": "completed", "resolution_note": "No"},
            files={"after_image": ("after.png", b"not an image", "image/png")},
        )

    assert response.status_code == 403


def test_terminal_action_uses_authenticated_admin_and_optional_after_image(
    monkeypatch,
):
    monkeypatch.setenv("SUPER_ADMIN_UPNS", "dale@gruberpallets.com")
    captured = {}
    monkeypatch.setattr(
        feedback_store,
        "transition",
        lambda **values: captured.update(values) or values,
    )

    with admin_client("dale@gruberpallets.com") as client:
        response = client.post(
            "/admin/feedback/7/status",
            data={"status": "completed", "resolution_note": "Fixed safely"},
            files={"after_image": ("after.png", valid_png_bytes(), "image/png")},
        )

    assert response.status_code == 303
    assert response.headers["location"] == "/admin/feedback"
    assert captured["feedback_id"] == 7
    assert captured["actor"] == "dale@gruberpallets.com"
    assert captured["status"] == "completed"
    assert captured["resolution_note"] == "Fixed safely"
    assert captured["after_image"].jpeg_bytes.startswith(b"\xff\xd8")
    assert captured["now"].tzinfo is UTC


def test_after_image_read_is_bounded_to_limit_plus_one(monkeypatch):
    monkeypatch.setenv("SUPER_ADMIN_UPNS", "dale@gruberpallets.com")
    captured = {}

    def capture_image(raw):
        captured["raw_length"] = len(raw)
        return normalized_image()

    monkeypatch.setattr(feedback_admin, "normalize_image", capture_image)
    monkeypatch.setattr(feedback_store, "transition", lambda **values: 2)

    with admin_client("dale@gruberpallets.com") as client:
        response = client.post(
            "/admin/feedback/7/status",
            data={"status": "declined", "resolution_note": "Not planned"},
            files={
                "after_image": (
                    "after.png",
                    b"x" * (MAX_INPUT_BYTES + 100),
                    "image/png",
                )
            },
        )

    assert response.status_code == 303
    assert captured["raw_length"] == MAX_INPUT_BYTES + 1


@pytest.mark.parametrize(
    ("error", "expected_status"),
    [
        (KeyError(7), 404),
        (feedback_store.InvalidTransition("invalid"), 422),
    ],
)
def test_post_maps_store_errors(monkeypatch, error, expected_status):
    monkeypatch.setenv("SUPER_ADMIN_UPNS", "dale@gruberpallets.com")

    def fail(**values):
        raise error

    monkeypatch.setattr(feedback_store, "transition", fail)

    with admin_client("dale@gruberpallets.com") as client:
        response = client.post(
            "/admin/feedback/7/status",
            data={"status": "in_progress"},
        )

    assert response.status_code == expected_status


def test_post_maps_rejected_image_to_unprocessable_entity(monkeypatch):
    monkeypatch.setenv("SUPER_ADMIN_UPNS", "dale@gruberpallets.com")

    with admin_client("dale@gruberpallets.com") as client:
        response = client.post(
            "/admin/feedback/7/status",
            data={"status": "completed", "resolution_note": "Fixed"},
            files={"after_image": ("after.txt", b"not an image", "text/plain")},
        )

    assert response.status_code == 422


def test_admin_list_queries_local_and_sync_state(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        feedback_store.db,
        "query",
        lambda sql, params: captured.update(sql=" ".join(sql.split()), params=params) or [],
    )

    assert feedback_store.for_admin(limit=25) == []

    assert "FROM feedback f" in captured["sql"]
    assert "LEFT JOIN feedback_odoo_sync s" in captured["sql"]
    assert "f.status" in captured["sql"]
    assert "s.state AS sync_state" in captured["sql"]
    assert "s.desired_version" in captured["sql"]
    assert "s.last_synced_version" in captured["sql"]
    assert captured["params"] == (25,)


def test_admin_template_renders_states_actions_and_escaped_text(monkeypatch):
    monkeypatch.setenv("SUPER_ADMIN_UPNS", "dale@gruberpallets.com")
    rows = [
        {
            "id": 1,
            "created_at": "2026-08-20 10:00",
            "submitter": "person@example.com",
            "page_url": "/recycling?x=<bad>",
            "task_type": "bug",
            "message": "Broken <script>alert(1)</script>",
            "status": "requested",
            "finished_at": None,
            "finished_by": None,
            "resolution_note": None,
            "projection_version": 1,
            "sync_state": "idle",
            "desired_version": 1,
            "last_synced_version": 0,
            "has_before_image": True,
            "has_after_image": False,
        },
        {
            "id": 2,
            "created_at": "2026-08-20 11:00",
            "submitter": None,
            "page_url": None,
            "task_type": "feature",
            "message": "Add a tool",
            "status": "in_progress",
            "finished_at": None,
            "finished_by": None,
            "resolution_note": None,
            "projection_version": 2,
            "sync_state": "quarantined",
            "desired_version": 2,
            "last_synced_version": 1,
            "has_before_image": False,
            "has_after_image": False,
        },
        {
            "id": 3,
            "created_at": "2026-08-20 12:00",
            "submitter": "done@example.com",
            "page_url": None,
            "task_type": "bug",
            "message": "Finished",
            "status": "completed",
            "finished_at": "2026-08-20 13:00",
            "finished_by": "dale@gruberpallets.com",
            "resolution_note": "Fixed <carefully>",
            "projection_version": 3,
            "sync_state": "idle",
            "desired_version": 3,
            "last_synced_version": 3,
            "has_before_image": False,
            "has_after_image": True,
        },
        {
            "id": 4,
            "created_at": "2026-08-20 14:00",
            "submitter": "no@example.com",
            "page_url": None,
            "task_type": "feature",
            "message": "No",
            "status": "declined",
            "finished_at": "2026-08-20 15:00",
            "finished_by": "dale@gruberpallets.com",
            "resolution_note": "Not planned",
            "projection_version": 2,
            "sync_state": "idle",
            "desired_version": 2,
            "last_synced_version": 0,
            "has_before_image": False,
            "has_after_image": False,
        },
    ]
    monkeypatch.setattr(feedback_store, "for_admin", lambda limit=200: rows)

    with admin_client("dale@gruberpallets.com") as client:
        response = client.get("/admin/feedback")

    assert response.status_code == 200
    assert "Requested" in response.text
    assert "In Progress" in response.text
    assert "Completed" in response.text
    assert "Declined" in response.text
    assert "quarantined" in response.text
    assert "Broken &lt;script&gt;alert(1)&lt;/script&gt;" in response.text
    assert "<script>alert(1)</script>" not in response.text
    assert "Fixed &lt;carefully&gt;" in response.text
    assert response.text.count('action="/admin/feedback/1/status"') == 3
    assert response.text.count('action="/admin/feedback/2/status"') == 2
    assert 'action="/admin/feedback/3/status"' not in response.text
    assert 'action="/admin/feedback/4/status"' not in response.text
    assert 'name="resolution_note"' in response.text
    assert 'name="after_image"' in response.text

    source = Path("src/zira_dashboard/templates/admin_feedback.html").read_text()
    assert "|safe" not in source


def test_transition_rejects_reopening_terminal_feedback(monkeypatch):
    cursor = ReturningCursor({"status": "completed", "projection_version": 2})
    monkeypatch.setattr(feedback_store.db, "cursor", lambda: cursor_returning(cursor.row))

    with pytest.raises(feedback_store.InvalidTransition, match="terminal"):
        feedback_store.transition(
            feedback_id=7,
            status="in_progress",
            actor="dale@gruberpallets.com",
            resolution_note=None,
            after_image=None,
            now=datetime(2026, 8, 20, 18, 0, tzinfo=UTC),
        )


@pytest.mark.parametrize("current", ["completed", "declined", None, "unknown"])
def test_transition_rejects_invalid_current_states_without_updates(monkeypatch, current):
    cursor = ReturningCursor({"status": current, "projection_version": 2})

    @contextmanager
    def fake_cursor():
        yield cursor

    monkeypatch.setattr(feedback_store.db, "cursor", fake_cursor)

    with pytest.raises(feedback_store.InvalidTransition):
        feedback_store.transition(
            feedback_id=7,
            status="completed",
            actor="dale@gruberpallets.com",
            resolution_note="Done",
            after_image=None,
            now=datetime(2026, 8, 20, 18, 0, tzinfo=UTC),
        )

    assert len(cursor.executions) == 1
    assert cursor.executions[0][0].startswith("SELECT")


def test_transition_requires_terminal_actor_and_note_before_updates(monkeypatch):
    cursor = ReturningCursor({"status": "requested", "projection_version": 2})

    @contextmanager
    def fake_cursor():
        yield cursor

    monkeypatch.setattr(feedback_store.db, "cursor", fake_cursor)

    with pytest.raises(feedback_store.InvalidTransition, match="actor and resolution note"):
        feedback_store.transition(
            feedback_id=7,
            status="completed",
            actor="  ",
            resolution_note="  ",
            after_image=None,
            now=datetime(2026, 8, 20, 18, 0, tzinfo=UTC),
        )

    assert len(cursor.executions) == 1


def test_transition_rejects_nonterminal_after_image_before_persistent_sql(monkeypatch):
    cursor = ReturningCursor({"status": "requested", "projection_version": 2})

    @contextmanager
    def fake_cursor():
        yield cursor

    monkeypatch.setattr(feedback_store.db, "cursor", fake_cursor)

    with pytest.raises(feedback_store.InvalidTransition, match="after image"):
        feedback_store.transition(
            feedback_id=7,
            status="in_progress",
            actor="dale@gruberpallets.com",
            resolution_note=None,
            after_image=normalized_image(),
            now=datetime(2026, 8, 20, 18, 0, tzinfo=UTC),
        )

    assert len(cursor.executions) == 1
    assert all(not sql.startswith(("UPDATE", "INSERT")) for sql, _ in cursor.executions)


def test_transition_marks_in_progress_and_new_projection_due_atomically(monkeypatch):
    cursor = ReturningCursor({"status": "requested", "projection_version": 4})
    transactions = []

    @contextmanager
    def fake_cursor():
        transactions.append(cursor)
        yield cursor

    monkeypatch.setattr(feedback_store.db, "cursor", fake_cursor)
    now = datetime(2026, 8, 20, 18, 0, tzinfo=UTC)

    version = feedback_store.transition(
        feedback_id=7,
        status="in_progress",
        actor="dale@gruberpallets.com",
        resolution_note=None,
        after_image=None,
        now=now,
    )

    assert version == 5
    assert transactions == [cursor]
    assert len(cursor.executions) == 3
    update_sql, update_params = cursor.executions[1]
    sync_sql, sync_params = cursor.executions[2]
    assert update_sql.startswith("UPDATE feedback SET")
    assert update_params == ("in_progress", None, None, None, 5, now, 7)
    assert "UPDATE feedback_odoo_sync" in sync_sql
    assert "state = CASE WHEN state = 'quarantined' THEN state ELSE 'idle' END" in sync_sql
    assert sync_params == (5, now, now, 7)
    assert all("feedback_images" not in sql for sql, _ in cursor.executions)


@pytest.mark.parametrize(
    ("current", "target"),
    [
        ("requested", "completed"),
        ("requested", "declined"),
        ("in_progress", "completed"),
        ("in_progress", "declined"),
    ],
)
def test_terminal_transition_saves_authoritative_finish_image_and_due_version(
    monkeypatch, current, target
):
    cursor = ReturningCursor({"status": current, "projection_version": 2})

    @contextmanager
    def fake_cursor():
        yield cursor

    monkeypatch.setattr(feedback_store.db, "cursor", fake_cursor)
    now = datetime(2026, 8, 20, 18, 0, tzinfo=UTC)
    image = normalized_image()

    version = feedback_store.transition(
        feedback_id=7,
        status=target,
        actor=" DALE@GRUBERPALLETS.COM ",
        resolution_note="  Fixed safely  ",
        after_image=image,
        now=now,
    )

    assert version == 3
    assert len(cursor.executions) == 4
    update_sql, update_params = cursor.executions[1]
    image_sql, image_params = cursor.executions[2]
    sync_sql, sync_params = cursor.executions[3]
    assert "lifecycle_origin = 'local'" in update_sql
    assert update_params == (
        target,
        now,
        "dale@gruberpallets.com",
        "Fixed safely",
        3,
        now,
        7,
    )
    assert "INSERT INTO feedback_images" in image_sql
    assert "'after'" in image_sql
    assert "ON CONFLICT (feedback_id, role) DO UPDATE" in image_sql
    assert image_params == (7, b"jpeg", "a" * 64, 4, 8, 6)
    assert "UPDATE feedback_odoo_sync" in sync_sql
    assert "state = CASE WHEN state = 'quarantined' THEN state ELSE 'idle' END" in sync_sql
    assert sync_params == (3, now, now, 7)


def test_transition_missing_feedback_raises_key_error_without_update(monkeypatch):
    cursor = ReturningCursor(None)

    @contextmanager
    def fake_cursor():
        yield cursor

    monkeypatch.setattr(feedback_store.db, "cursor", fake_cursor)

    with pytest.raises(KeyError, match="7"):
        feedback_store.transition(
            feedback_id=7,
            status="in_progress",
            actor="dale@gruberpallets.com",
            resolution_note=None,
            after_image=None,
            now=datetime(2026, 8, 20, 18, 0, tzinfo=UTC),
        )

    assert len(cursor.executions) == 1
