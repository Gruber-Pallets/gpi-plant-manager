"""Thin wrapper over Slack's Web API for the GPI Plant Manager app.

Uses the bot token in env var SLACK_BOT_TOKEN. Required scopes:
- files:write (upload PDFs)
- chat:write  (post the file with initial_comment)

The bot must be invited into the target channel(s) once.

Uses files.upload_v2 (the current public API as of late 2025;
files.upload v1 is being deprecated). Three-step flow:
  1. POST files.getUploadURLExternal -> returns upload_url + file_id
  2. POST upload_url with the file bytes
  3. POST files.completeUploadExternal with file_id + channel_id +
     initial_comment -> Slack posts the file to the channel.
"""

from __future__ import annotations

import os

import requests


class SlackError(Exception):
    """Raised on any Slack API failure or missing config."""


_CHANNEL_NAME_CACHE: dict[str, str] = {}


def upload_pdf(
    pdf_bytes: bytes,
    *,
    filename: str,
    channel_id: str,
    initial_comment: str,
) -> dict:
    """Upload a PDF to a Slack channel.

    Returns dict: {file_id, permalink, channel_name}.
    Raises SlackError on any non-ok Slack response or missing token.
    """
    token = os.environ.get("SLACK_BOT_TOKEN")
    if not token:
        raise SlackError("Slack not configured (SLACK_BOT_TOKEN missing)")

    # 1) Get an upload URL.
    r = requests.post(
        "https://slack.com/api/files.getUploadURLExternal",
        headers={"Authorization": f"Bearer {token}"},
        data={"filename": filename, "length": len(pdf_bytes)},
        timeout=15,
    )
    r.raise_for_status()
    j = r.json()
    if not j.get("ok"):
        raise SlackError(f"getUploadURLExternal failed: {j.get('error')}")
    upload_url = j["upload_url"]
    file_id = j["file_id"]

    # 2) Upload the bytes to the returned URL.
    r = requests.post(
        upload_url,
        files={"file": (filename, pdf_bytes)},
        timeout=30,
    )
    r.raise_for_status()

    # 3) Complete the upload (this is the step that posts to channel).
    r = requests.post(
        "https://slack.com/api/files.completeUploadExternal",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json; charset=utf-8",
        },
        json={
            "files": [{"id": file_id, "title": filename}],
            "channel_id": channel_id,
            "initial_comment": initial_comment,
        },
        timeout=15,
    )
    r.raise_for_status()
    j = r.json()
    if not j.get("ok"):
        raise SlackError(f"completeUploadExternal failed: {j.get('error')}")

    file_info = j["files"][0]
    return {
        "file_id": file_info["id"],
        "permalink": file_info.get("permalink", ""),
        "channel_name": _channel_name_for(channel_id, token),
    }


def _channel_name_for(channel_id: str, token: str) -> str:
    """Resolve a channel ID to its display name (e.g., 'mgmt-sups').
    Cached in-process. Falls back to the raw ID on any error."""
    if channel_id in _CHANNEL_NAME_CACHE:
        return _CHANNEL_NAME_CACHE[channel_id]
    try:
        r = requests.get(
            "https://slack.com/api/conversations.info",
            headers={"Authorization": f"Bearer {token}"},
            params={"channel": channel_id},
            timeout=10,
        )
        r.raise_for_status()
        j = r.json()
        if j.get("ok"):
            name = j["channel"]["name"]
            _CHANNEL_NAME_CACHE[channel_id] = name
            return name
    except Exception:
        pass
    return channel_id
