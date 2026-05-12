# Schedule Print + Slack Share Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a Print button and a Post-to-Slack button to the scheduler page so the day's schedule can be printed (browser print dialog) or pushed to `#mgmt-sups` as a server-rendered PDF, manually triggered.

**Architecture:** Add `@media print` CSS to the existing scheduler template — both browser print and WeasyPrint apply those rules automatically. The Slack share endpoint calls the existing `staffing_page` GET handler as a Python function (no refactor, no internal HTTP), pipes the resulting HTML through WeasyPrint, and uploads the PDF via Slack's `files.upload_v2` Web API.

**Tech Stack:** FastAPI · Jinja2 · WeasyPrint (server-side PDF) · Slack Web API · `requests` for HTTP

---

## Setup (one-time, manual — done in parallel with the build)

These are operator steps, not code tasks. Confirm done before Task 7's smoke test.

1. In api.slack.com → GPI Plant Manager app → OAuth & Permissions → add bot token scopes `files:write` and `chat:write`. Reinstall the app to apply.
2. Copy the bot token (`xoxb-...`) → set as `SLACK_BOT_TOKEN` in Railway env.
3. In Slack, run `/invite @gpi-plant-manager` in `#mgmt-sups`.
4. Copy the channel ID (the `C01...` string from `#mgmt-sups`'s About panel) → set as `SLACK_CHANNEL_ID` in Railway env.

---

## File Structure

**New files:**
- `src/zira_dashboard/slack_client.py` — Slack Web API wrapper
- `src/zira_dashboard/routes/share.py` — POST /staffing/share-to-slack endpoint
- `tests/test_slack_client.py` — mocked Web API tests
- `tests/test_share_route.py` — mocked route + WeasyPrint tests

**Modified files:**
- `Dockerfile` — install WeasyPrint's system libs (libpango, libpangoft2, libharfbuzz)
- `requirements.txt` — add `weasyprint` and `requests`
- `src/zira_dashboard/app.py` — include the new share router
- `src/zira_dashboard/templates/staffing.html` — print CSS (`@media print`); Print + Post-to-Slack buttons; `printSchedule` / `postToSlack` / `showToast` JS helpers

---

## Task 1: System + Python dependencies

**Files:**
- Modify: `Dockerfile`
- Modify: `requirements.txt`

WeasyPrint is a Python lib with C-library dependencies. The base image `python:3.13-slim` does not include them; we add them via apt.

- [ ] **Step 1: Update Dockerfile**

Read the current `Dockerfile` (just 11 lines). Insert an apt-install step after `WORKDIR /app` and before `COPY . .`:

```dockerfile
FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1

WORKDIR /app

# WeasyPrint system deps (Pango/HarfBuzz; Cairo + GLib pulled transitively)
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpango-1.0-0 libpangoft2-1.0-0 libharfbuzz0b \
    && rm -rf /var/lib/apt/lists/*

COPY . .

RUN pip install --upgrade pip && pip install .

CMD ["sh", "-c", "uvicorn zira_dashboard.app:app --host 0.0.0.0 --port ${PORT}"]
```

- [ ] **Step 2: Update requirements.txt**

Read the current `requirements.txt` (only `psycopg2-binary>=2.9.9`). Append:

```
weasyprint>=60.0
requests>=2.31.0
```

(`requests` is a transitive dep of many things but listing it explicitly avoids surprise version drift.)

- [ ] **Step 3: Install locally and verify import**

Run:

```bash
C:\Users\dale.gruber\Projects\zira\.venv\Scripts\python.exe -m pip install weasyprint>=60.0 requests>=2.31.0
```

WeasyPrint may fail to install on Windows due to GTK runtime not being on PATH — that's expected. The package wheel will install but `import weasyprint` may raise at runtime locally. The Linux container build is what matters; we'll verify that in Task 7's smoke test.

Verify `requests` imports cleanly:

```bash
C:\Users\dale.gruber\Projects\zira\.venv\Scripts\python.exe -c "import requests; print(requests.__version__)"
```

Expected: a version string, no traceback.

- [ ] **Step 4: Commit**

```bash
git add Dockerfile requirements.txt
git commit -m "build: add WeasyPrint + requests for schedule PDF/Slack share"
```

---

## Task 2: Print CSS in staffing.html

**Files:**
- Modify: `src/zira_dashboard/templates/staffing.html` — append `@media print { ... }` block to the existing `<style>` section

- [ ] **Step 1: Locate the existing `<style>` block**

Read `src/zira_dashboard/templates/staffing.html` and find the existing `<style>` section (or `{% block styles %}` — whichever the file uses). The print rules go inside it.

- [ ] **Step 2: Locate the actual class names**

Find the class names used by:
1. The Reserves panel (the left-rail list of reserves who aren't scheduled)
2. The toolbar wrapper containing the Publish button
3. Any per-row "remove" / "X" buttons on assignments and time-off chips
4. WC card drag handle, picker trigger
5. Top nav bar

Note these for Step 3.

- [ ] **Step 3: Add the @media print block**

Append to the `<style>` section:

```css
@media print {
  @page {
    size: letter landscape;
    margin: 0.4in;
  }

  /* Hide the Reserves panel — supervisors don't need who's not scheduled. */
  .reserves-panel,
  /* Hide the entire toolbar (publish/print/slack/edit/refresh buttons). */
  .toolbar,
  /* Hide top nav bar. */
  nav.top-bar,
  /* Hide per-row interactive controls. */
  .dd-item .remove-btn,
  .timeoff-chip .remove-btn,
  .wc-card .drag-handle,
  .wc-card .picker-trigger {
    display: none !important;
  }

  body { background: white; color: black; }
  a { color: black; text-decoration: none; }

  /* Tighten layout for single-page fit. */
  .scheduler-grid {
    gap: 4px;
    font-size: 10pt;
  }
  .wc-card {
    padding: 4px 6px;
    page-break-inside: avoid;
  }
  .notes-panel {
    margin-top: 8pt;
  }
}
```

**IMPORTANT:** Replace the placeholder selectors above with the actual class names you found in Step 2. For example, if the Reserves panel uses `.left-rail-reserves`, use that name. If you can't find a particular element (e.g., no top nav bar exists), drop that selector. The intent is "hide everything interactive + reserves panel; keep WC assignments, cert badges, Unscheduled list, time-off chips, WC notes."

- [ ] **Step 4: Manually verify in browser**

Run the dev server (or against a remote deploy) and open `/staffing`. Hit Ctrl+P / Cmd+P to open the print preview. Verify:
- Reserves panel disappears.
- Toolbar (with Publish button) disappears.
- All `<remove>` / X buttons disappear.
- WC assignments + cert badges + Unscheduled + time-off chips + notes panel remain visible.
- Layout fits on one landscape letter page (or close to it).

If the layout overflows or selectors don't match, tweak the CSS and repeat.

- [ ] **Step 5: Commit**

```bash
git add src/zira_dashboard/templates/staffing.html
git commit -m "feat(scheduler): print stylesheet hides reserves + interactive controls"
```

---

## Task 3: Print button + printSchedule JS

**Files:**
- Modify: `src/zira_dashboard/templates/staffing.html` — add Print button to toolbar; add `printSchedule()` JS

- [ ] **Step 1: Find the toolbar markup**

Read `staffing.html` and locate the toolbar containing the existing Publish button (probably labeled "Publish" or "Post"). Note its class/structure.

- [ ] **Step 2: Add the Print button**

Insert a Print button immediately after (or before) the existing Publish button in the toolbar:

```html
<button type="button" class="btn btn-print" onclick="printSchedule()">Print</button>
```

Use whatever class style the existing toolbar buttons use — match the visual treatment of Publish.

- [ ] **Step 3: Add the printSchedule JS helper**

Find the existing `<script>` block (or end of body) and append:

```javascript
function printSchedule() {
  // Open the page in a new tab and trigger print on load.
  // Using the current URL (which already has ?day=) means the
  // print preview shows the same day the user is viewing.
  const url = window.location.href;
  const win = window.open(url, '_blank');
  if (!win) {
    // Popup blocker; fall back to printing the current tab.
    window.print();
    return;
  }
  win.addEventListener('load', () => {
    win.focus();
    win.print();
  }, { once: true });
}
```

- [ ] **Step 4: Smoke test**

Reload `/staffing`, click the new Print button. Verify a new tab opens and the browser's print dialog appears with the print-styled view (assignments, hidden controls, etc.). Cancel the print to confirm the tab.

If popup blocker prevents the new tab, the fallback `window.print()` runs in the same tab — verify that path too by simulating a popup block.

- [ ] **Step 5: Commit**

```bash
git add src/zira_dashboard/templates/staffing.html
git commit -m "feat(scheduler): add Print button"
```

---

## Task 4: Slack client module

**Files:**
- Create: `src/zira_dashboard/slack_client.py`
- Test: `tests/test_slack_client.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_slack_client.py`:

```python
from unittest.mock import patch, MagicMock

import pytest

from zira_dashboard import slack_client


def _ok_response(json_body):
    r = MagicMock()
    r.json.return_value = json_body
    r.raise_for_status.return_value = None
    return r


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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `C:\Users\dale.gruber\Projects\zira\.venv\Scripts\python.exe -m pytest tests/test_slack_client.py -v`
Expected: FAIL (`ModuleNotFoundError: No module named 'zira_dashboard.slack_client'`).

- [ ] **Step 3: Create the module**

Create `src/zira_dashboard/slack_client.py`:

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `C:\Users\dale.gruber\Projects\zira\.venv\Scripts\python.exe -m pytest tests/test_slack_client.py -v`
Expected: PASS (all five tests).

- [ ] **Step 5: Commit**

```bash
git add src/zira_dashboard/slack_client.py tests/test_slack_client.py
git commit -m "feat(slack): client wrapper for files.upload_v2"
```

---

## Task 5: Share-to-Slack route

**Files:**
- Create: `src/zira_dashboard/routes/share.py`
- Modify: `src/zira_dashboard/app.py` — include the new router
- Test: `tests/test_share_route.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_share_route.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `C:\Users\dale.gruber\Projects\zira\.venv\Scripts\python.exe -m pytest tests/test_share_route.py -v`
Expected: FAIL (route doesn't exist; `404 Not Found` on POST).

- [ ] **Step 3: Create the route module**

Create `src/zira_dashboard/routes/share.py`:

```python
"""POST /staffing/share-to-slack — render the day's scheduler in
print mode, convert to PDF, upload to the configured Slack channel.
"""

from __future__ import annotations

import os
from datetime import date

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse

from .. import slack_client
from .staffing import staffing_page

router = APIRouter()


def _format_comment(day: str) -> str:
    """Return e.g. 'Schedule for Tue 4/30' for the given YYYY-MM-DD."""
    try:
        d = date.fromisoformat(day)
    except ValueError:
        return f"Schedule for {day}"
    weekday = d.strftime("%a")  # 'Mon', 'Tue', ...
    return f"Schedule for {weekday} {d.month}/{d.day}"


def _render_pdf(html: str, base_url: str) -> bytes:
    """Render HTML to PDF bytes via WeasyPrint.

    `base_url` lets WeasyPrint resolve any relative asset URLs in the
    HTML (stylesheets, fonts) against the running server.
    """
    from weasyprint import HTML  # imported lazily — heavy dep
    return HTML(string=html, base_url=base_url).write_pdf()


@router.post("/staffing/share-to-slack")
def share_to_slack(
    request: Request,
    day: str = Query(...),
):
    """Render the day's scheduler -> PDF -> upload to Slack."""
    channel_id = os.environ.get("SLACK_CHANNEL_ID")
    if not channel_id:
        return JSONResponse(
            {"ok": False, "error": "Slack not configured (SLACK_CHANNEL_ID missing)"},
            status_code=500,
        )

    # 1. Render the scheduler page for this day by calling the existing
    #    handler as a regular function. The handler returns an
    #    HTMLResponse; we read its body for the HTML string.
    response = staffing_page(request, day=day)
    html = response.body.decode("utf-8")

    # 2. Render the HTML to PDF.
    try:
        pdf_bytes = _render_pdf(html, base_url=str(request.base_url))
    except Exception as e:
        return JSONResponse(
            {"ok": False, "error": f"PDF render failed: {e}"},
            status_code=500,
        )

    # 3. Upload to Slack.
    try:
        result = slack_client.upload_pdf(
            pdf_bytes,
            filename=f"schedule-{day}.pdf",
            channel_id=channel_id,
            initial_comment=_format_comment(day),
        )
    except slack_client.SlackError as e:
        return JSONResponse(
            {"ok": False, "error": str(e)},
            status_code=502,
        )

    return JSONResponse({
        "ok": True,
        "channel_name": result["channel_name"],
        "permalink": result["permalink"],
    })
```

- [ ] **Step 4: Register the router in app.py**

Edit `src/zira_dashboard/app.py`. Find the existing `from .routes import (...)` block and add `share` to the list. Find the existing `app.include_router(...)` calls and add:

```python
from .routes import share
...
app.include_router(share.router)
```

(Insert into the existing imports / include_router block; don't create a new section.)

- [ ] **Step 5: Run tests to verify they pass**

Run: `C:\Users\dale.gruber\Projects\zira\.venv\Scripts\python.exe -m pytest tests/test_share_route.py -v`
Expected: PASS (all four tests).

Also run a smoke check that the app still imports:

```bash
C:\Users\dale.gruber\Projects\zira\.venv\Scripts\python.exe -c "from zira_dashboard.app import app; print(app.title)"
```
Expected: `Zira Station Dashboard`, no traceback.

- [ ] **Step 6: Commit**

```bash
git add src/zira_dashboard/routes/share.py src/zira_dashboard/app.py tests/test_share_route.py
git commit -m "feat(slack): /staffing/share-to-slack endpoint (PDF via WeasyPrint)"
```

---

## Task 6: Post to Slack button + JS + showToast helper

**Files:**
- Modify: `src/zira_dashboard/templates/staffing.html` — add Post-to-Slack button to toolbar; add `postToSlack()` JS; add `showToast` helper if absent

- [ ] **Step 1: Add the Post to Slack button**

Insert next to the Print button in the toolbar:

```html
<button type="button" class="btn btn-share" onclick="postToSlack(this)">Post to Slack</button>
```

(`onclick` passes `this` so the JS can manipulate the button without `event.currentTarget`.)

- [ ] **Step 2: Add the postToSlack JS helper**

Append to the existing `<script>` block:

```javascript
async function postToSlack(btn) {
  const originalLabel = btn.textContent;
  btn.disabled = true;
  btn.textContent = 'Posting…';
  try {
    // Pull the day from the URL — same key the GET handler uses.
    const params = new URLSearchParams(window.location.search);
    const day = params.get('day');
    if (!day) {
      showToast('No day in URL — refresh the page', null, 'error');
      return;
    }
    const r = await fetch(`/staffing/share-to-slack?day=${encodeURIComponent(day)}`, {
      method: 'POST',
      headers: { 'Accept': 'application/json' },
    });
    const data = await r.json();
    if (data.ok) {
      showToast(`Posted to #${data.channel_name}`, data.permalink);
    } else {
      showToast(`Slack post failed: ${data.error}`, null, 'error');
    }
  } catch (e) {
    showToast(`Slack post failed: ${e.message}`, null, 'error');
  } finally {
    btn.disabled = false;
    btn.textContent = originalLabel;
  }
}
```

- [ ] **Step 3: Add showToast helper (if not present)**

Search the existing JS for an existing `showToast` or `toast` helper. If none exists, add one. Place it before `postToSlack`:

```javascript
function showToast(message, link, severity) {
  let host = document.getElementById('toast-host');
  if (!host) {
    host = document.createElement('div');
    host.id = 'toast-host';
    host.style.cssText =
      'position:fixed;bottom:20px;right:20px;z-index:9999;' +
      'display:flex;flex-direction:column;gap:8px;';
    document.body.appendChild(host);
  }
  const toast = document.createElement('div');
  const isErr = severity === 'error';
  toast.style.cssText =
    'background:' + (isErr ? '#fee' : '#efe') + ';' +
    'color:' + (isErr ? '#900' : '#060') + ';' +
    'border:1px solid ' + (isErr ? '#fcc' : '#cfc') + ';' +
    'border-radius:6px;padding:10px 14px;font-size:0.9rem;' +
    'box-shadow:0 2px 8px rgba(0,0,0,0.15);max-width:340px;';
  toast.textContent = message;
  if (link) {
    toast.appendChild(document.createTextNode(' '));
    const a = document.createElement('a');
    a.href = link;
    a.target = '_blank';
    a.textContent = 'View in Slack';
    a.style.cssText = 'color:#06c;text-decoration:underline;';
    toast.appendChild(a);
  }
  host.appendChild(toast);
  setTimeout(() => toast.remove(), 6000);
}
```

If a `showToast` already exists with a similar signature, reuse it instead of duplicating.

- [ ] **Step 4: Hide print/share buttons in print mode**

Confirm Task 2's CSS already hides `.toolbar` (or whatever the toolbar class is) under `@media print`. If the new `.btn-print` and `.btn-share` aren't covered by the existing toolbar selector, add them explicitly to the `@media print` block:

```css
.btn-print, .btn-share {
  display: none !important;
}
```

- [ ] **Step 5: Smoke test (after Railway deploy + Slack setup steps done)**

Open `/staffing` in production. Click Post to Slack. Verify:
- Button shows "Posting…" and is disabled.
- After ~2-5s, toast appears: "Posted to #mgmt-sups" with a "View in Slack" link.
- Clicking the link opens the post in Slack.
- The PDF in Slack shows the schedule (assignments, badges, time-off, notes), with the Reserves panel hidden.

If the toast shows an error like "Bot needs to be invited to the channel", complete the Slack setup step `/invite @gpi-plant-manager` in `#mgmt-sups`.

- [ ] **Step 6: Commit**

```bash
git add src/zira_dashboard/templates/staffing.html
git commit -m "feat(scheduler): Post to Slack button + toast helper"
```

---

## Task 7: Full smoke test + verification

This task is operator-only — code is already shipped after Task 6.

- [ ] **Step 1: Push to origin**

```bash
git push
```

Wait for Railway to build the new Docker image (~3-5 min — the new apt-install step adds ~30s on first build). Watch the build logs for any WeasyPrint install issues.

- [ ] **Step 2: Verify env vars set**

In Railway → web service → Variables, confirm both:
- `SLACK_BOT_TOKEN` (starts with `xoxb-`)
- `SLACK_CHANNEL_ID` (starts with `C`)

- [ ] **Step 3: Verify deploy is healthy**

Open `/staffing` — page should render normally with no 500.

- [ ] **Step 4: Test Print**

Click the Print button. New tab opens; browser print dialog appears with the print-styled layout. Visual checks:
- Reserves panel is hidden.
- All toolbar buttons are hidden.
- WC assignments + cert badges + Unscheduled list + time-off chips + WC notes are all visible.
- Layout fits on one landscape letter page (or close — paginates cleanly if it overflows).

Cancel the print dialog. Close the tab.

- [ ] **Step 5: Test Post to Slack**

On the scheduler page, click Post to Slack. Verify:
- Button shows "Posting…" and is disabled.
- After ~2-5s, success toast appears with "Posted to #mgmt-sups" and a "View in Slack" link.
- Click the link → opens the channel in Slack with the PDF visible.
- The comment text is `Schedule for {Weekday} {M/D}`.
- The PDF content matches what the print preview showed.

- [ ] **Step 6: Test error paths (optional, if you want to verify them)**

- Temporarily unset `SLACK_BOT_TOKEN` in Railway → click Post to Slack → toast shows "Slack not configured". Restore the token.
- Kick the bot out of `#mgmt-sups` (`/kick @gpi-plant-manager`) → click Post to Slack → toast shows "not_in_channel" or similar. Re-invite the bot.

- [ ] **Step 7: Done**

Feature is shipped.

---

## Acceptance Recap

After all tasks merge and deploy:

- ✅ Print button on `/staffing` opens a new tab in the same day's view, browser print dialog appears with the print-styled layout (reserves + interactive controls hidden).
- ✅ Post to Slack button on `/staffing` posts a PDF of the same view to `#mgmt-sups` with comment `Schedule for {Day} {M/D}`.
- ✅ Toast confirms success with a clickable "View in Slack" link, or surfaces the error message verbatim.
- ✅ Manual triggers only — neither button auto-fires on Publish.
- ✅ Two clicks max from "I want to share tomorrow's schedule" to "it's in Slack".
