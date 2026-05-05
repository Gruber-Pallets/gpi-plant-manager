from unittest.mock import patch, MagicMock

import pytest
from fastapi.testclient import TestClient

from zira_dashboard.app import app
from zira_dashboard import slack_client


def test_share_returns_ok_when_slack_succeeds(monkeypatch):
    monkeypatch.setenv("SLACK_CHANNEL_ID", "C123")

    fake_html_response = MagicMock()
    fake_html_response.body = b"<html>fake schedule</html>"

    with patch(
        "zira_dashboard.routes.share.staffing_page",
        return_value=fake_html_response,
    ), patch(
        "zira_dashboard.routes.share._render_pdf",
        return_value=b"%PDF-1.4 fake",
    ), patch(
        "zira_dashboard.routes.share.slack_client.upload_pdf",
        return_value={
            "file_id": "F999",
            "permalink": "https://slack.com/archives/C123/p1",
            "channel_name": "mgmt-sups",
        },
    ) as mock_upload:
        client = TestClient(app)
        resp = client.post("/staffing/share-to-slack?day=2026-04-30")

    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["channel_name"] == "mgmt-sups"
    assert body["permalink"].startswith("https://slack.com/")

    # The endpoint passed the right kwargs to upload_pdf.
    kwargs = mock_upload.call_args.kwargs
    assert kwargs["filename"] == "schedule-2026-04-30.pdf"
    assert kwargs["channel_id"] == "C123"
    assert "Schedule for" in kwargs["initial_comment"]


def test_share_returns_json_500_when_staffing_page_raises(monkeypatch):
    """If the underlying schedule render throws, the route must still
    return JSON — otherwise the client's `await r.json()` chokes on
    FastAPI's plain-text "Internal Server Error" page.
    """
    monkeypatch.setenv("SLACK_CHANNEL_ID", "C123")

    with patch(
        "zira_dashboard.routes.share.staffing_page",
        side_effect=RuntimeError("db connection lost"),
    ):
        client = TestClient(app)
        resp = client.post(
            "/staffing/share-to-slack?day=2026-04-30",
            # TestClient defaults to raising on server errors; we want
            # to inspect the response body instead.
        )

    assert resp.status_code == 500
    body = resp.json()  # would raise if the body weren't JSON
    assert body["ok"] is False
    assert "Schedule render failed" in body["error"]
    assert "db connection lost" in body["error"]


def test_share_passes_concrete_defaults_to_staffing_page(monkeypatch):
    """staffing_page is a FastAPI handler whose params use Query() defaults.
    When called directly (not via the router), Python passes the Query
    objects through as defaults — so callers MUST pass explicit values
    or downstream `int(publish_blocked or 0)` blows up.
    """
    monkeypatch.setenv("SLACK_CHANNEL_ID", "C123")

    fake_html_response = MagicMock()
    fake_html_response.body = b"<html>fake</html>"

    with patch(
        "zira_dashboard.routes.share.staffing_page",
        return_value=fake_html_response,
    ) as mock_page, patch(
        "zira_dashboard.routes.share._render_pdf",
        return_value=b"%PDF-1.4",
    ), patch(
        "zira_dashboard.routes.share.slack_client.upload_pdf",
        return_value={"file_id": "F1", "permalink": "x", "channel_name": "y"},
    ):
        client = TestClient(app)
        client.post("/staffing/share-to-slack?day=2026-04-30")

    kwargs = mock_page.call_args.kwargs
    # Must be the concrete defaults, not Query() instances.
    assert kwargs["publish_blocked"] == 0
    assert kwargs["view"] == "draft"
    assert isinstance(kwargs["publish_blocked"], int)
    assert isinstance(kwargs["view"], str)


def test_share_returns_500_when_pdf_render_fails(monkeypatch):
    monkeypatch.setenv("SLACK_CHANNEL_ID", "C123")

    fake_html_response = MagicMock()
    fake_html_response.body = b"<html>fake</html>"

    with patch(
        "zira_dashboard.routes.share.staffing_page",
        return_value=fake_html_response,
    ), patch(
        "zira_dashboard.routes.share._render_pdf",
        side_effect=RuntimeError("css parse error"),
    ):
        client = TestClient(app)
        resp = client.post("/staffing/share-to-slack?day=2026-04-30")

    assert resp.status_code == 500
    assert resp.json()["ok"] is False
    assert "PDF render failed" in resp.json()["error"]


def test_share_returns_502_on_slack_error(monkeypatch):
    monkeypatch.setenv("SLACK_CHANNEL_ID", "C123")

    fake_html_response = MagicMock()
    fake_html_response.body = b"<html>fake</html>"

    with patch(
        "zira_dashboard.routes.share.staffing_page",
        return_value=fake_html_response,
    ), patch(
        "zira_dashboard.routes.share._render_pdf",
        return_value=b"%PDF-1.4 fake",
    ), patch(
        "zira_dashboard.routes.share.slack_client.upload_pdf",
        side_effect=slack_client.SlackError("not_in_channel"),
    ):
        client = TestClient(app)
        resp = client.post("/staffing/share-to-slack?day=2026-04-30")

    assert resp.status_code == 502
    body = resp.json()
    assert body["ok"] is False
    assert "not_in_channel" in body["error"]


def test_share_initial_comment_uses_short_date_format(monkeypatch):
    monkeypatch.setenv("SLACK_CHANNEL_ID", "C123")

    fake_html_response = MagicMock()
    fake_html_response.body = b"<html>fake</html>"

    with patch(
        "zira_dashboard.routes.share.staffing_page",
        return_value=fake_html_response,
    ), patch(
        "zira_dashboard.routes.share._render_pdf",
        return_value=b"%PDF-1.4",
    ), patch(
        "zira_dashboard.routes.share.slack_client.upload_pdf",
        return_value={"file_id": "F1", "permalink": "x", "channel_name": "y"},
    ) as mock_upload:
        client = TestClient(app)
        resp = client.post("/staffing/share-to-slack?day=2026-04-30")

    assert resp.status_code == 200
    comment = mock_upload.call_args.kwargs["initial_comment"]
    # 2026-04-30 was a Thursday
    assert "Thu 4/30" in comment
