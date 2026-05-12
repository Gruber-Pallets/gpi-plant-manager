# Schedule Print + Slack Share — Design

**Date:** 2026-04-29
**Status:** Approved (brainstorming → implementation planning)

## Context

When publishing tomorrow's schedule, Dale wants two one-click actions
on the scheduler page:

- **Print** — open the browser print dialog with a clean printable
  view of the day's schedule, so he can hit a printer or "Save as PDF".
- **Post to Slack** — push the same printable view, rendered
  server-side as a PDF, into the `#mgmt-sups` Slack channel via the
  existing GPI Plant Manager Slack app.

Both actions must be **manual** — no auto-fire on publish, especially
for past-day corrections. Both must be visible on the scheduler page
itself, not buried elsewhere. Minimal clicks: one click → confirm or
done.

The printed/PDF view should match what's on the scheduler today, with
one subtraction: the unscheduled-reserves panel (the left-rail list
showing reserves who weren't pulled into a WC) is hidden, since it's
not relevant to the supervisors reading the post.

## Goals

1. Add a print stylesheet to the scheduler page so `Ctrl+P` produces a
   single-page printable layout (letter landscape).
2. Add a "Print" button next to the existing Publish button that
   triggers `window.print()`.
3. Add a "Post to Slack" button that posts the same printable view as
   a PDF into a configured Slack channel.
4. Use WeasyPrint for server-side PDF generation (no headless browser
   in the deploy).
5. Surface a toast on success (with a "View in Slack" link) and on
   error (with a clear message).

## Non-goals

- A separate print-only template or route. The print stylesheet
  attaches to the existing scheduler template; the "print mode"
  variant is signaled with a `?print=1` query param.
- Auto-posting on Publish. The buttons are always manual.
- Per-channel selection in the UI. The target channel is configured
  via env var; if Dale ever wants multiple channels, that's a future
  feature.
- Rich Block Kit messages. The Slack post is a PDF file with a short
  initial comment — supervisors get the same artifact whether they
  print, save the PDF, or read it in Slack.
- Duplicate-post prevention. Clicking twice posts twice. Acceptable
  trade-off for simpler code.
- Editing the initial comment from the UI. Auto-generated text only;
  edit in Slack after posting if needed.

## Design

### Print stylesheet

Add an inline `<style>` block to `templates/staffing.html` (alongside
the existing styles) with `@media print { ... }` rules. No new file,
no StaticFiles mount, no `?print=1` flag needed:

- Browser print: `@media print` rules apply automatically when the
  user hits Ctrl+P.
- WeasyPrint: applies `@media print` rules automatically when
  rendering HTML for PDF output (it always renders for print media).

The rules:

```css
@media print {
  @page {
    size: letter landscape;
    margin: 0.4in;
  }

  /* Hide the Reserves panel — supervisors don't need to see who's
     not scheduled. */
  .reserves-panel,
  /* Hide all toolbar buttons including the Publish/Print/Slack ones
     themselves so the printout doesn't show its own UI controls. */
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

  /* Tighten WC card padding for single-page fit. */
  .scheduler-grid {
    gap: 4px;
    font-size: 10pt;
  }
  .wc-card {
    padding: 4px 6px;
    page-break-inside: avoid;
  }
  /* Notes panel — move from sidebar to bottom strip if needed.
     Implementer chooses based on where the panel currently lives. */
  .notes-panel {
    margin-top: 8pt;
  }
}
```

The exact selector list (e.g., the actual class name of the Reserves
panel, the toolbar wrapper, etc.) is determined by reading
`templates/staffing.html` during implementation — the existing
template uses specific class names like `.left-rail`, `.dd-list`,
etc. The design intent is "everything interactive disappears, plus
the reserves panel; assignments + cert badges + Unscheduled list +
time-off + WC notes remain."

The Reserves panel is hidden; the Unscheduled list stays visible (a
non-empty Unscheduled list on publish day is an exception flag worth
surfacing on the printout).

### Print button

A new "Print" button in the scheduler toolbar:

```html
<button class="btn btn-print" onclick="printSchedule()">Print</button>
```

```javascript
function printSchedule() {
  // Open the print-mode URL in a new tab and trigger print on load.
  const url = new URL(window.location.href);
  url.searchParams.set('print', '1');
  const win = window.open(url.toString(), '_blank');
  if (win) {
    win.addEventListener('load', () => win.print(), { once: true });
  }
}
```

Opens a new tab in print mode → browser print dialog appears. User
picks a printer or "Save as PDF". Closes the tab when done.

### Slack post button

A new "Post to Slack" button in the scheduler toolbar:

```html
<button class="btn btn-share" onclick="postToSlack()">Post to Slack</button>
```

```javascript
async function postToSlack() {
  const btn = event.currentTarget;
  btn.disabled = true;
  btn.textContent = 'Posting…';
  try {
    const day = currentDay();  // existing helper
    const r = await fetch(`/staffing/share-to-slack?day=${day}`, {
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
    btn.textContent = 'Post to Slack';
  }
}
```

`showToast(msg, permalink, severity?)` is a small helper added to the
scheduler's existing JS — same toast pattern used elsewhere on the
page. When `permalink` is non-null, the toast renders a "View in
Slack" link. (If the page has no existing toast helper, this design
adds one as a tiny utility — a fixed-position div that fades in/out.)

### Backend endpoint

`POST /staffing/share-to-slack?day=YYYY-MM-DD` handler in a new
module `src/zira_dashboard/routes/share.py`:

```python
@router.post("/staffing/share-to-slack")
async def share_to_slack(request: Request, day: str):
    """Render the day's scheduler in print mode → PDF → upload to Slack.

    Returns JSON {ok, channel_name, permalink, error?}.
    """
    # 1. Render print-mode HTML for the day.
    html = _render_print_html(request, day)

    # 2. Render PDF.
    try:
        pdf_bytes = _render_pdf(html)
    except Exception as e:
        return JSONResponse({"ok": False, "error": f"PDF render failed: {e}"}, status_code=500)

    # 3. Upload to Slack.
    try:
        result = slack_client.upload_pdf(
            pdf_bytes,
            filename=f"schedule-{day}.pdf",
            channel_id=settings.SLACK_CHANNEL_ID,
            initial_comment=_format_comment(day),
        )
    except slack_client.SlackError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=502)

    return JSONResponse({
        "ok": True,
        "channel_name": result["channel_name"],
        "permalink": result["permalink"],
    })
```

Helpers:

- `_render_print_html(request, day)` — call the existing scheduler
  GET handler `staffing_page` as a regular Python function (handlers
  are just functions in FastAPI), passing the same request + day:
  ```python
  from .staffing import staffing_page
  response = staffing_page(request, day=day)
  html = response.body.decode("utf-8")
  ```
  No refactor needed; no internal HTTP call. The handler runs with
  the same data-loading path the user's browser sees on `?print=1`.
  Print CSS rules use `@media print` so WeasyPrint applies them
  automatically when rendering the HTML to PDF (WeasyPrint always
  renders for print media).
- `_render_pdf(html)` — wraps WeasyPrint:
  ```python
  from weasyprint import HTML
  return HTML(string=html, base_url=str(request.base_url)).write_pdf()
  ```
  `base_url` is set so relative `/static/...` URLs resolve correctly
  for stylesheets, fonts, and any inline-loaded assets.
- `_format_comment(day)` — returns e.g. `"Schedule for Tue 4/30"`.
  Weekday + month/day, no year. Same date format used by leaderboards.

### Slack client

A new module `src/zira_dashboard/slack_client.py`:

```python
"""Thin wrapper over Slack's Web API for the GPI Plant Manager app.

Uses the bot token in env var SLACK_BOT_TOKEN. Required scopes:
- files:write (upload PDFs)
- chat:write  (post the file with initial_comment)

The bot must be invited into the target channel(s) once.
"""

import os
import requests

class SlackError(Exception):
    pass

def upload_pdf(pdf_bytes: bytes, *, filename: str, channel_id: str,
               initial_comment: str) -> dict:
    """Upload a PDF to a Slack channel using files.upload_v2 (the
    current public API as of late 2025; v1 is being deprecated).

    Returns dict with keys: file_id, permalink, channel_name.
    Raises SlackError on any non-ok response.
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

    # 2) Upload the bytes.
    r = requests.post(upload_url, files={"file": (filename, pdf_bytes)}, timeout=30)
    r.raise_for_status()

    # 3) Complete the upload (which also posts to the channel).
    r = requests.post(
        "https://slack.com/api/files.completeUploadExternal",
        headers={"Authorization": f"Bearer {token}",
                 "Content-Type": "application/json; charset=utf-8"},
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

_CHANNEL_NAME_CACHE: dict[str, str] = {}

def _channel_name_for(channel_id: str, token: str) -> str:
    """Resolve a channel ID to its display name (e.g., 'mgmt-sups').
    Cached in-process for the process lifetime — channel IDs don't
    change once created. Falls back to the raw ID on any error so the
    success toast still has something to show."""
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

`requests` is already a transitive dep via FastAPI's ecosystem; if
not, add it. (Could use `httpx` for async — not necessary for this
endpoint since one request per minute is the steady state.)

### Configuration

Two new env vars:

- `SLACK_BOT_TOKEN` — bot user OAuth token (`xoxb-...`) from the GPI
  Plant Manager app's Install page.
- `SLACK_CHANNEL_ID` — the target channel's ID (`C01234ABCD...`),
  not its name. Use ID so renames don't break the integration.

Both set in Railway's env. If `SLACK_BOT_TOKEN` is missing at runtime,
the share endpoint returns a clear error rather than crashing.

### Slack app setup (one-time, manual)

In the GPI Plant Manager Slack app config (api.slack.com):

1. **OAuth & Permissions** → Bot Token Scopes → add `files:write` and
   `chat:write`. Reinstall the app to apply.
2. Copy the bot user OAuth token (starts with `xoxb-`) and set it as
   `SLACK_BOT_TOKEN` in Railway.
3. In Slack, run `/invite @gpi-plant-manager` in `#mgmt-sups`.
4. Copy the channel ID from the channel's "About" panel and set it
   as `SLACK_CHANNEL_ID` in Railway.

These steps are documented in the implementation plan's setup section.

### Error handling

| Failure | Handling |
|---|---|
| `SLACK_BOT_TOKEN` missing | endpoint returns `{ok: false, error: "Slack not configured"}` (500) |
| Bot not in channel (`not_in_channel`) | endpoint returns `{ok: false, error: "Bot needs to be invited to the channel"}` (502) |
| Channel ID invalid | `{ok: false, error: "Channel not found"}` (502) |
| WeasyPrint render error | `{ok: false, error: "PDF render failed: ..."}` (500) |
| Network timeout | `{ok: false, error: "Slack request timed out"}` (504) |

Frontend reads `data.error` and shows it in a toast. No silent
failures.

## Acceptance criteria

- Scheduler page shows three buttons: Publish, Print, Post to Slack.
- Clicking Print opens a new tab at `/staffing?day=...&print=1` with
  the print stylesheet applied; the browser print dialog appears.
  Reserves panel is hidden; interactive controls are hidden;
  assignments + cert badges + time-off + WC notes are visible.
- Clicking Post to Slack posts a PDF of the same view to
  `#mgmt-sups` with comment `Schedule for {Day} {M/D}`. Toast shows
  "Posted to #mgmt-sups" with a "View in Slack" link.
- Posting twice creates two posts (no dedupe).
- Posting a still-DRAFT day works (no publish requirement).
- Missing/invalid token, bot-not-in-channel, channel-not-found, and
  PDF render errors all surface as toast errors with the relevant
  detail.

## Risks

- **WeasyPrint CSS support.** WeasyPrint handles flexbox, grid, and
  most modern CSS, but has known quirks around some edge cases
  (e.g., `position: sticky`, complex 3D transforms). The scheduler's
  CSS uses standard flexbox layout, which should render fine. If the
  PDF looks off after first ship, the fallback is to swap the
  rendering call to Playwright (~270 MB extra deploy weight, but
  pixel-identical to Chrome). Spec leaves this as a follow-up
  contingency, not a planned switch.
- **Single-page fit.** "Letter landscape" gives ~10×7.5 inches of
  usable space. With 10–15 WC cards + time-off + notes, fit is
  tight. Mitigation: print stylesheet shrinks WC card padding and
  font-size; if a particular busy day overflows, the page simply
  paginates to two pages — supervisors can still read it. Not
  blocking ship.
- **Slack API churn.** `files.upload_v2` is the current public path;
  v1 is deprecated. Slack has changed file-upload mechanics
  before. Mitigation: keep the upload helper isolated in
  `slack_client.py` so a future API rev is a single-file change.
- **Bot token security.** A leaked bot token grants channel-write
  access to the workspace. Mitigation: stored only in Railway env
  (not in repo), not logged, not echoed in error responses.

## File touch list

- New: `src/zira_dashboard/slack_client.py` — Slack Web API wrapper
- New: `src/zira_dashboard/routes/share.py` — POST endpoint
- New: `src/zira_dashboard/static/staffing-print.css` — print stylesheet
- Modified: `src/zira_dashboard/app.py` — register the new router
- Modified: `src/zira_dashboard/routes/staffing.py` — accept `print=1`
  query param, add `print_mode` to template context
- Modified: `src/zira_dashboard/templates/staffing.html` — load print
  stylesheet, add Print + Post to Slack buttons, add `printSchedule`
  / `postToSlack` JS helpers, add `showToast` helper if absent
- Modified: `requirements.txt` — add `weasyprint` and `requests` (if
  not already present)
- New: `tests/test_slack_client.py` — mocked Web API round-trip
  (success, missing token, not-in-channel, channel-not-found,
  network error)
- New: `tests/test_share_route.py` — mocked Slack client + WeasyPrint
  patched, verify endpoint returns the right shape on each error path
