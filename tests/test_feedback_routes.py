"""Feedback POST route tests; persistence is monkeypatched (no PG/Odoo)."""

from io import BytesIO

from fastapi.testclient import TestClient
from PIL import Image

from zira_dashboard import feedback_store, odoo_client
from zira_dashboard.app import app
from zira_dashboard.feedback_image import ImageRejected, MAX_INPUT_BYTES
from zira_dashboard.routes import feedback as feedback_route

client = TestClient(app)


def valid_png_bytes() -> bytes:
    output = BytesIO()
    Image.new("RGB", (8, 8), "green").save(output, format="PNG")
    return output.getvalue()


def _fail_if_odoo_is_called(monkeypatch) -> None:
    def fail(*args, **kwargs):
        raise AssertionError((args, kwargs))

    for name in (
        "authenticate",
        "ensure_feedback_project",
        "ensure_feedback_tag",
        "create_feedback_task",
        "add_task_attachment",
    ):
        monkeypatch.setattr(odoo_client, name, fail)


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
        data={"type": "bug", "description": "  It broke  ", "page_url": "/recycling"},
    )

    assert response.status_code == 200
    assert response.json() == {"ok": True, "id": 12, "task_delivery": "queued"}
    assert captured == {
        "message": "It broke",
        "submitter": None,
        "page_url": "/recycling",
        "task_type": "bug",
        "status": "requested",
        "before_image": None,
    }


def test_post_feedback_still_succeeds_when_odoo_is_unavailable(monkeypatch):
    monkeypatch.setattr(
        feedback_store, "create_submission", lambda **values: 44, raising=False
    )
    monkeypatch.setattr(
        odoo_client,
        "authenticate",
        lambda: (_ for _ in ()).throw(RuntimeError("down")),
    )

    response = client.post(
        "/feedback", data={"type": "feature", "description": "New view"}
    )

    assert response.status_code == 200
    assert response.json() == {"ok": True, "id": 44, "task_delivery": "queued"}


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
        data={"type": "bug", "description": "See shot"},
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
        data={"type": "bug", "description": "Large shot"},
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
        data={"type": "bug", "description": "See shot"},
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
        data={"type": "bug", "description": "x", "page_url": "javascript:alert(1)"},
    )

    assert response.status_code == 200
    assert captured["page_url"] is None


def test_admin_feedback_route_requires_super_admin():
    response = client.get("/admin/feedback")
    assert response.status_code == 403
