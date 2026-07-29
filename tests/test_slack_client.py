from unittest.mock import MagicMock

import pytest
import requests

from zira_dashboard import slack_client


def _ok_response(json_body):
    r = MagicMock()
    r.json.return_value = json_body
    r.raise_for_status.return_value = None
    return r


def test_post_message_posts_fallback_and_blocks(monkeypatch):
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    response = _ok_response({"ok": True, "channel": "C-MGMT", "ts": "1722280000.000100"})
    seen = {}

    def fake_post(url, **kwargs):
        seen["url"] = url
        seen.update(kwargs)
        return response

    monkeypatch.setattr(slack_client.requests, "post", fake_post)
    result = slack_client.post_message(
        channel_id="C-MGMT",
        text="NEW REPAIRS GOAT: Jose O., 898 pallets.",
        blocks=[{"type": "header", "text": {"type": "plain_text", "text": "🏆 NEW REPAIRS GOAT!"}}],
    )

    assert seen["url"] == "https://slack.com/api/chat.postMessage"
    assert seen["headers"]["Authorization"] == "Bearer xoxb-test"
    assert seen["json"]["channel"] == "C-MGMT"
    assert seen["json"]["text"].startswith("NEW REPAIRS GOAT")
    assert seen["json"]["blocks"][0]["type"] == "header"
    assert seen["json"]["unfurl_links"] is False
    assert result == {"message_ts": "1722280000.000100"}


def test_post_message_wraps_api_error(monkeypatch):
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.setattr(slack_client.requests, "post", lambda *args, **kwargs: _ok_response({"ok": False, "error": "not_in_channel"}))

    with pytest.raises(slack_client.SlackError, match="not_in_channel"):
        slack_client.post_message(channel_id="C-MGMT", text="x", blocks=[])


def test_upload_pdf_missing_token_raises(monkeypatch):
    monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
    with pytest.raises(slack_client.SlackError, match="not configured"):
        slack_client.upload_pdf(
            b"%PDF-1.4 fake",
            filename="test.pdf",
            channel_id="C123",
            initial_comment="hi",
        )


def test_upload_pdf_full_three_step_flow(monkeypatch):
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")

    # Three sequential calls: get URL, upload to URL, complete upload.
    # _channel_name_for adds a fourth call for conversations.info.
    responses = iter([
        _ok_response({"ok": True, "upload_url": "https://files.slack.com/upload-x", "file_id": "F123"}),
        _ok_response({}),  # the upload-bytes POST returns no JSON we use
        _ok_response({
            "ok": True,
            "files": [{"id": "F123", "permalink": "https://slack.com/archives/C123/p999"}],
        }),
        _ok_response({"ok": True, "channel": {"name": "mgmt-sups"}}),
    ])

    def fake_post(url, **kwargs):
        return next(responses)

    def fake_get(url, **kwargs):
        return next(responses)

    monkeypatch.setattr(slack_client.requests, "post", fake_post)
    monkeypatch.setattr(slack_client.requests, "get", fake_get)
    slack_client._CHANNEL_NAME_CACHE.clear()

    result = slack_client.upload_pdf(
        b"%PDF-1.4 fake",
        filename="schedule-2026-04-30.pdf",
        channel_id="C123",
        initial_comment="Schedule for Tue 4/30",
    )

    assert result["file_id"] == "F123"
    assert result["permalink"].startswith("https://slack.com/")
    assert result["channel_name"] == "mgmt-sups"


def test_upload_pdf_get_upload_url_failure_raises(monkeypatch):
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.setattr(
        slack_client.requests, "post",
        lambda *a, **kw: _ok_response({"ok": False, "error": "rate_limited"}),
    )
    with pytest.raises(slack_client.SlackError, match="rate_limited"):
        slack_client.upload_pdf(
            b"%PDF-1.4",
            filename="t.pdf",
            channel_id="C123",
            initial_comment="hi",
        )


def test_upload_pdf_complete_failure_raises(monkeypatch):
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    responses = iter([
        _ok_response({"ok": True, "upload_url": "https://files.slack.com/x", "file_id": "F1"}),
        _ok_response({}),
        _ok_response({"ok": False, "error": "not_in_channel"}),
    ])
    monkeypatch.setattr(slack_client.requests, "post", lambda *a, **kw: next(responses))
    with pytest.raises(slack_client.SlackError, match="not_in_channel"):
        slack_client.upload_pdf(
            b"%PDF-1.4",
            filename="t.pdf",
            channel_id="C123",
            initial_comment="hi",
        )


def test_upload_pdf_network_error_during_upload_raises_slack_error(monkeypatch):
    """A dropped connection while uploading bytes to Slack's upload_url
    (seen in prod as ConnectionResetError) must surface as SlackError,
    not the raw requests exception -- otherwise the route's
    `except slack_client.SlackError` doesn't catch it and the endpoint
    crashes with a non-JSON 500.
    """
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")

    responses = iter([
        _ok_response({"ok": True, "upload_url": "https://files.slack.com/x", "file_id": "F1"}),
    ])

    def fake_post(url, **kwargs):
        try:
            return next(responses)
        except StopIteration:
            raise requests.exceptions.ConnectionError(
                "('Connection aborted.', ConnectionResetError(104, 'Connection reset by peer'))"
            )

    monkeypatch.setattr(slack_client.requests, "post", fake_post)

    with pytest.raises(slack_client.SlackError):
        slack_client.upload_pdf(
            b"%PDF-1.4",
            filename="t.pdf",
            channel_id="C123",
            initial_comment="hi",
        )


def test_channel_name_falls_back_to_id_on_lookup_error(monkeypatch):
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    responses = iter([
        _ok_response({"ok": True, "upload_url": "https://files.slack.com/x", "file_id": "F1"}),
        _ok_response({}),
        _ok_response({"ok": True, "files": [{"id": "F1", "permalink": "https://x"}]}),
        _ok_response({"ok": False, "error": "channel_not_found"}),
    ])
    def fake_post(url, **kw): return next(responses)
    def fake_get(url, **kw): return next(responses)
    monkeypatch.setattr(slack_client.requests, "post", fake_post)
    monkeypatch.setattr(slack_client.requests, "get", fake_get)
    slack_client._CHANNEL_NAME_CACHE.clear()

    result = slack_client.upload_pdf(
        b"%PDF-1.4", filename="t.pdf", channel_id="C123",
        initial_comment="hi",
    )
    assert result["channel_name"] == "C123"  # fallback to raw id
