"""Feedback POST route tests; Odoo + store are monkeypatched (no PG/Odoo)."""

from fastapi.testclient import TestClient

from zira_dashboard import feedback_store, odoo_client
from zira_dashboard.app import app

client = TestClient(app)


def _patch_odoo(monkeypatch, created=None):
    calls = {"task": None, "attachments": [], "tags": []}

    monkeypatch.setattr(odoo_client, "ensure_feedback_project", lambda: 7)
    monkeypatch.setattr(odoo_client, "authenticate", lambda: 3)

    def fake_tag(name):
        calls["tags"].append(name)
        return 55

    def fake_task(**kwargs):
        calls["task"] = kwargs
        return created or 900

    def fake_att(**kwargs):
        calls["attachments"].append(kwargs)
        return len(calls["attachments"])

    monkeypatch.setattr(odoo_client, "ensure_feedback_tag", fake_tag)
    monkeypatch.setattr(
        odoo_client, "create_feedback_task",
        lambda **kw: fake_task(**kw),
    )
    monkeypatch.setattr(
        odoo_client, "add_task_attachment",
        lambda **kw: fake_att(**kw),
    )
    return calls


def test_post_feedback_creates_task_and_local_row(monkeypatch):
    calls = _patch_odoo(monkeypatch)
    captured = {}
    monkeypatch.setattr(
        feedback_store, "insert",
        lambda **kw: captured.update(kw) or 12,
    )

    resp = client.post(
        "/feedback",
        data={"type": "bug", "description": "  It broke  ", "page_url": "/recycling"},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True and body["task_id"] == 900
    assert calls["task"]["project_id"] == 7
    assert calls["task"]["assignee_uid"] == 3
    assert calls["task"]["tag_id"] == 55
    assert calls["task"]["name"].startswith("[Bug] It broke")
    assert calls["tags"] == ["Bug"]
    assert captured["task_type"] == "bug"
    assert captured["odoo_task_id"] == 900
    assert captured["message"] == "It broke"
    assert captured["page_url"] == "/recycling"


def test_post_feedback_feature_uses_feature_tag(monkeypatch):
    calls = _patch_odoo(monkeypatch)
    monkeypatch.setattr(feedback_store, "insert", lambda **kw: 1)

    resp = client.post(
        "/feedback",
        data={"type": "feature", "description": "Add dark mode"},
    )

    assert resp.status_code == 200
    assert calls["tags"] == ["Feature request"]
    assert calls["task"]["name"].startswith("[Feature] Add dark mode")


def test_post_feedback_uploads_attachments(monkeypatch):
    calls = _patch_odoo(monkeypatch)
    monkeypatch.setattr(feedback_store, "insert", lambda **kw: 1)

    resp = client.post(
        "/feedback",
        data={"type": "bug", "description": "see image"},
        files=[("files", ("shot.png", b"\x89PNG\r\n", "image/png"))],
    )

    assert resp.status_code == 200
    assert len(calls["attachments"]) == 1
    assert calls["attachments"][0]["task_id"] == 900
    assert calls["attachments"][0]["filename"] == "shot.png"
    assert calls["attachments"][0]["raw_bytes"] == b"\x89PNG\r\n"


def test_post_feedback_rejects_empty_description(monkeypatch):
    _patch_odoo(monkeypatch)
    called = {"n": 0}
    monkeypatch.setattr(
        feedback_store, "insert",
        lambda **kw: called.__setitem__("n", called["n"] + 1) or 1,
    )

    resp = client.post("/feedback", data={"type": "bug", "description": "   "})

    assert resp.status_code == 400
    assert resp.json()["ok"] is False
    assert called["n"] == 0


def test_post_feedback_drops_unsafe_page_url(monkeypatch):
    calls = _patch_odoo(monkeypatch)
    captured = {}
    monkeypatch.setattr(
        feedback_store, "insert",
        lambda **kw: captured.update(kw) or 1,
    )

    resp = client.post(
        "/feedback",
        data={"type": "bug", "description": "x", "page_url": "javascript:alert(1)"},
    )

    assert resp.status_code == 200
    assert captured["page_url"] is None


def test_post_feedback_returns_502_and_skips_local_row_on_odoo_failure(monkeypatch):
    monkeypatch.setattr(odoo_client, "authenticate", lambda: 3)
    monkeypatch.setattr(odoo_client, "ensure_feedback_tag", lambda name: 55)

    def boom():
        raise RuntimeError("odoo down")

    monkeypatch.setattr(odoo_client, "ensure_feedback_project", boom)
    inserted = {"n": 0}
    monkeypatch.setattr(
        feedback_store, "insert",
        lambda **kw: inserted.__setitem__("n", inserted["n"] + 1) or 1,
    )

    resp = client.post("/feedback", data={"type": "bug", "description": "x"})

    assert resp.status_code == 502
    assert resp.json()["ok"] is False
    assert inserted["n"] == 0


def test_post_feedback_escapes_html_in_description(monkeypatch):
    calls = _patch_odoo(monkeypatch)
    monkeypatch.setattr(feedback_store, "insert", lambda **kw: 1)

    resp = client.post(
        "/feedback",
        data={"type": "bug", "description": "a < b & <script>x</script>"},
    )

    assert resp.status_code == 200
    body_html = calls["task"]["description_html"]
    assert "<script>" not in body_html
    assert "&lt;script&gt;" in body_html
    assert "a &lt; b &amp;" in body_html


def test_admin_feedback_route_removed():
    resp = client.get("/admin/feedback")
    assert resp.status_code == 404
