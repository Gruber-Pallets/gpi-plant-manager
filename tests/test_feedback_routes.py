"""Feedback POST route tests; persistence is monkeypatched (no PG/Odoo)."""

from io import BytesIO

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from PIL import Image

from zira_dashboard import feedback_store, feedback_submitters, odoo_client
from zira_dashboard.app import app
from zira_dashboard.feedback_image import ImageRejected, MAX_INPUT_BYTES
from zira_dashboard.routes import feedback as feedback_route

client = TestClient(app)


@pytest.fixture(autouse=True)
def local_feedback_roster(monkeypatch):
    monkeypatch.setattr(
        feedback_submitters.db,
        "query",
        lambda *_args: [
            {
                "employee_id": 41,
                "name": "Ana",
                "active": True,
                "work_email": "ana@gruberpallets.com",
            }
        ],
    )


def private_client(upn: str) -> TestClient:
    test_app = FastAPI()

    @test_app.middleware("http")
    async def set_identity(request: Request, call_next):
        request.state.user_upn = upn
        return await call_next(request)

    test_app.include_router(feedback_route.router)
    return TestClient(test_app)


def valid_png_bytes() -> bytes:
    output = BytesIO()
    Image.new("RGB", (8, 8), "green").save(output, format="PNG")
    return output.getvalue()


def _fail_if_odoo_is_called(monkeypatch) -> None:
    def fail(*args, **kwargs):
        raise AssertionError((args, kwargs))

    for name in (
        "fetch_employees",
        "fetch_employee_statuses",
        "authenticate",
        "ensure_feedback_project",
        "ensure_feedback_tag",
        "create_feedback_task",
        "add_task_attachment",
    ):
        monkeypatch.setattr(odoo_client, name, fail)


def test_get_feedback_submitters_returns_active_timeclock_choices():
    response = client.get("/api/feedback/submitters")

    assert response.status_code == 200
    assert response.json() == {
        "ok": True,
        "people": [{"employee_id": 41, "name": "Ana"}],
    }


def test_post_feedback_saves_locally_without_calling_odoo(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        feedback_store,
        "create_submission",
        lambda **values: captured.update(values) or 12,
        raising=False,
    )
    _fail_if_odoo_is_called(monkeypatch)

    response = client.post(
        "/feedback",
        data={
            "type": "bug",
            "description": "  It broke  ",
            "page_url": "/recycling",
            "submitter_employee_id": "41",
        },
    )

    assert response.status_code == 200
    assert response.json() == {"ok": True, "id": 12, "task_delivery": "queued"}
    assert captured == {
        "message": "It broke",
        "submitter": "ana@gruberpallets.com",
        "submitter_employee_odoo_id": 41,
        "page_url": "/recycling",
        "task_type": "bug",
        "status": "requested",
        "before_image": None,
    }


@pytest.mark.parametrize("task_type", ["bug", "feature"])
def test_private_feedback_still_persists_when_live_odoo_is_unavailable(
    monkeypatch, task_type
):
    captured = {}
    monkeypatch.setattr(
        feedback_store,
        "create_submission",
        lambda **values: captured.update(values) or 44,
        raising=False,
    )
    _fail_if_odoo_is_called(monkeypatch)

    response = private_client("ANA@gruberpallets.com").post(
        "/feedback",
        data={
            "type": task_type,
            "description": "New view",
            "submitter_employee_id": "999",
        },
    )

    assert response.status_code == 200
    assert response.json() == {"ok": True, "id": 44, "task_delivery": "queued"}
    assert captured["submitter"] == "ana@gruberpallets.com"
    assert captured["submitter_employee_odoo_id"] == 41


@pytest.mark.parametrize("task_type", ["floor_issue", "floor_suggestion"])
def test_post_feedback_saves_each_physical_type_without_coercion(monkeypatch, task_type):
    captured = {}
    monkeypatch.setattr(
        feedback_store,
        "create_submission",
        lambda **values: captured.update(values) or 45,
        raising=False,
    )

    response = client.post(
        "/feedback",
        data={
            "type": task_type,
            "description": "Floor feedback",
            "submitter_employee_id": "41",
        },
    )

    assert response.status_code == 200
    assert captured["task_type"] == task_type


def test_post_feedback_rejects_unknown_type_before_opening_storage(monkeypatch):
    monkeypatch.setattr(
        feedback_store,
        "create_submission",
        lambda **values: (_ for _ in ()).throw(AssertionError(values)),
        raising=False,
    )

    response = client.post(
        "/feedback", data={"type": "other", "description": "Unknown feedback"}
    )

    assert response.status_code == 400
    assert response.json() == {"ok": False, "error": "Unsupported feedback type."}


def test_post_feedback_normalizes_one_optional_image_from_decoded_content(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        feedback_store,
        "create_submission",
        lambda **values: captured.update(values) or 5,
        raising=False,
    )
    _fail_if_odoo_is_called(monkeypatch)

    response = client.post(
        "/feedback",
        data={"type": "bug", "description": "See shot", "submitter_employee_id": "41"},
        files={"screenshot": ("not-an-image.pdf", valid_png_bytes(), "application/pdf")},
    )

    assert response.status_code == 200
    assert response.json() == {"ok": True, "id": 5, "task_delivery": "queued"}
    assert captured["before_image"].jpeg_bytes.startswith(b"\xff\xd8")


def test_post_feedback_reads_at_most_the_image_limit_plus_one_byte(monkeypatch):
    captured = {}

    def reject(raw: bytes):
        captured["length"] = len(raw)
        raise ImageRejected("image must be between 1 byte and 10 MiB")

    monkeypatch.setattr(feedback_route, "normalize_image", reject, raising=False)
    monkeypatch.setattr(
        feedback_store,
        "create_submission",
        lambda **values: (_ for _ in ()).throw(AssertionError(values)),
        raising=False,
    )

    response = client.post(
        "/feedback",
        data={
            "type": "bug",
            "description": "Large shot",
            "submitter_employee_id": "41",
        },
        files={
            "screenshot": (
                "large.png",
                b"x" * (MAX_INPUT_BYTES + 2),
                "image/png",
            )
        },
    )

    assert response.status_code == 400
    assert response.json() == {
        "ok": False,
        "error": "image must be between 1 byte and 10 MiB",
    }
    assert captured["length"] == MAX_INPUT_BYTES + 1


def test_post_feedback_maps_rejected_image_to_safe_client_error(monkeypatch):
    called = {"n": 0}
    monkeypatch.setattr(
        feedback_store,
        "create_submission",
        lambda **values: called.__setitem__("n", called["n"] + 1),
        raising=False,
    )

    response = client.post(
        "/feedback",
        data={"type": "bug", "description": "See shot", "submitter_employee_id": "41"},
        files={"screenshot": ("shot.png", b"not an image", "image/png")},
    )

    assert response.status_code == 400
    body = response.json()
    assert body["ok"] is False
    assert body["error"] == "image could not be decoded safely"
    assert called["n"] == 0


def test_post_feedback_rejects_empty_description(monkeypatch):
    called = {"n": 0}
    monkeypatch.setattr(
        feedback_store,
        "create_submission",
        lambda **values: called.__setitem__("n", called["n"] + 1),
        raising=False,
    )

    response = client.post("/feedback", data={"type": "bug", "description": "   "})

    assert response.status_code == 400
    assert response.json() == {"ok": False, "error": "Description is required."}
    assert called["n"] == 0


def test_post_feedback_drops_unsafe_page_url(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        feedback_store,
        "create_submission",
        lambda **values: captured.update(values) or 1,
        raising=False,
    )

    response = client.post(
        "/feedback",
        data={
            "type": "bug",
            "description": "x",
            "page_url": "javascript:alert(1)",
            "submitter_employee_id": "41",
        },
    )

    assert response.status_code == 200
    assert captured["page_url"] is None


def test_post_feedback_requires_timeclock_employee_without_private_identity(monkeypatch):
    monkeypatch.setattr(
        feedback_store,
        "create_submission",
        lambda **values: (_ for _ in ()).throw(AssertionError(values)),
        raising=False,
    )

    response = client.post(
        "/feedback",
        data={
            "type": "bug",
            "description": "Missing employee",
            "page_url": "/private-looking-page",
            "is_private": "true",
        },
    )

    assert response.status_code == 400
    assert response.json() == {"ok": False, "error": "Choose your name and try again."}


def test_post_feedback_uses_authenticated_upn_and_ignores_posted_employee_id(
    monkeypatch,
):
    captured = {}
    monkeypatch.setattr(
        feedback_store,
        "create_submission",
        lambda **values: captured.update(values) or 47,
        raising=False,
    )
    monkeypatch.setattr(
        feedback_submitters.db,
        "query",
        lambda *_args: [
            {
                "employee_id": 41,
                "name": "Posted Person",
                "active": True,
                "work_email": "posted@example.com",
            },
            {
                "employee_id": 52,
                "name": "Private User",
                "active": True,
                "work_email": "private@example.com",
            },
        ],
    )
    _fail_if_odoo_is_called(monkeypatch)

    response = private_client(" Private@Example.com ").post(
        "/feedback",
        data={
            "type": "bug",
            "description": "Private report",
            "submitter_employee_id": "41",
        },
    )

    assert response.status_code == 200
    assert captured["submitter"] == "private@example.com"
    assert captured["submitter_employee_odoo_id"] == 52


def test_post_feedback_rejects_unresolved_submitter(monkeypatch):
    monkeypatch.setattr(
        feedback_submitters.db,
        "query",
        lambda *_args: [],
    )

    response = client.post(
        "/feedback",
        data={
            "type": "bug",
            "description": "Unknown employee",
            "submitter_employee_id": "99",
        },
    )

    assert response.status_code == 400
    assert response.json() == {"ok": False, "error": "Choose your name and try again."}


def test_post_feedback_rejects_unbounded_employee_id_text(monkeypatch):
    monkeypatch.setattr(
        feedback_store,
        "create_submission",
        lambda **values: (_ for _ in ()).throw(AssertionError(values)),
        raising=False,
    )

    response = client.post(
        "/feedback",
        data={
            "type": "bug",
            "description": "Bad employee id",
            "submitter_employee_id": "9" * 5000,
        },
    )

    assert response.status_code == 400
    assert response.json() == {"ok": False, "error": "Choose your name and try again."}


def test_post_feedback_rejects_repair_without_inserting(monkeypatch):
    monkeypatch.setattr(
        feedback_store,
        "create_submission",
        lambda **values: (_ for _ in ()).throw(AssertionError(values)),
        raising=False,
    )

    response = client.post(
        "/feedback",
        data={
            "type": "repair",
            "description": "Fix the lift",
            "submitter_employee_id": "41",
        },
    )

    assert response.status_code == 400
    assert response.json() == {"ok": False, "error": "Unsupported feedback type."}


def test_post_feedback_accepts_two_s_improvement(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        feedback_store,
        "create_submission",
        lambda **values: captured.update(values) or 48,
        raising=False,
    )

    response = client.post(
        "/feedback",
        data={
            "type": "two_s_improvement",
            "description": "Move the rack",
            "submitter_employee_id": "41",
        },
    )

    assert response.status_code == 200
    assert captured["task_type"] == "two_s_improvement"


def test_my_feedback_returns_canonical_type_labels_including_legacy_bug(monkeypatch):
    rows = [
        {
            "task_type": task_type,
            "message": f"Message {index}",
            "created_at": "2026-09-02 12:00:00",
            "page_url": None,
            "status": "requested",
        }
        for index, task_type in enumerate(
            ("bug", "feature", "floor_issue", "floor_suggestion", None),
            start=1,
        )
    ]
    monkeypatch.setattr(feedback_store, "for_submitter", lambda _submitter: rows)

    response = client.get("/api/feedback/mine")

    assert response.status_code == 200
    assert [item["type"] for item in response.json()["items"]] == [
        "bug",
        "feature",
        "floor_issue",
        "floor_suggestion",
        "bug",
    ]
    assert [item["type_label"] for item in response.json()["items"]] == [
        "Bug",
        "New Feature",
        "Floor Issue",
        "Floor Suggestion",
        "Bug",
    ]


def test_admin_feedback_route_requires_super_admin():
    response = client.get("/admin/feedback")
    assert response.status_code == 403
