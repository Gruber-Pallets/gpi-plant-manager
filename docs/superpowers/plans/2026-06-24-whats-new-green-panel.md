# What's New Green Panel Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the footer "What's new" text link with a green, top-right icon button that opens a card-based changelog panel with per-entry read state and a Send-feedback action, and move recycling's in-header date-range row below the subnav so it no longer collides with the new button.

**Architecture:** The server-rendered `/changelog` route is upgraded to emit one semantic `<article class="cl-entry">` card per deploy, parsing optional `#### Features` / `#### Fixes` sub-headings (everything else → "Highlights") and a `data-feature` flag for the green "New feature" badge. Read state lives in `localStorage` as a `changelog_cutoff` timestamp plus an explicit `changelog_read` key-set; the shared `footer.js` injects the trigger into each page's `<header>` and manages open/close, read state, and feedback submission. Feedback persists to a new `feedback` Postgres table via a small store module, surfaced read-only at `/admin/feedback`.

**Tech Stack:** FastAPI + Jinja2 (server-rendered), psycopg2 (RealDictCursor pool in `db.py`), vanilla ES5 `footer.js`, pytest + `fastapi.testclient.TestClient` (conftest sets `AUTH_DISABLED=1` and a dummy `ZIRA_API_KEY`, so route tests need no login).

**Testing note:** Python tasks (changelog renderer, feedback store/routes) are TDD with pytest. The repo has **no JS test harness**, and rendering a full page through `TestClient` needs Postgres/Odoo, so the frontend tasks (`footer.js`, `footer.css`, `_footer.html`, recycling markup) ship complete code plus explicit **manual verification** steps run against the local app. Run the app with `.venv/bin/python -m uvicorn zira_dashboard.app:app --reload` (requires a working `.env` with `DATABASE_URL`); test commands assume the project venv at `.venv`.

---

## File Structure

**Modify:**
- `src/zira_dashboard/routes/changelog.py` — rewrite `_md_to_html` to emit per-deploy cards with Features/Fixes/Highlights groups, stable `data-key`, and `data-feature`. (`_parse_time_to_24h`, `_latest_deploy_when`, and the route functions are unchanged.)
- `src/zira_dashboard/_schema.py` — add the `feedback` table to `SCHEMA_DDL`.
- `src/zira_dashboard/app.py` — import and include the feedback router.
- `src/zira_dashboard/templates/_footer.html` — drop the footer text link; rebuild the modal into the card panel + feedback form.
- `src/zira_dashboard/static/footer.css` — retire `.app-footer`/`.changelog-link`; add trigger button, panel card, badge, and feedback-form styles.
- `src/zira_dashboard/static/footer.js` — replace the changelog IIFE with trigger injection + read-state + mark-read/all + feedback submit (the gpiFetch shim and the alert-badges IIFE are untouched).
- `src/zira_dashboard/templates/recycling.html` — move the `<form class="rc-toolbar">` out of `<header>` to below the subnav.
- `src/zira_dashboard/static/recycling.css` — make `.rc-toolbar` a standalone row.
- `src/zira_dashboard/templates/settings.html` — add a Feedback link to the header nav.
- `CHANGELOG.md` — add a one-line format note and author this feature's entry in the new structured form.

**Create:**
- `src/zira_dashboard/feedback_store.py` — `insert(...)` / `recent(...)`.
- `src/zira_dashboard/routes/feedback.py` — `POST /feedback`, `GET /admin/feedback`.
- `src/zira_dashboard/templates/admin_feedback.html` — feedback list page.
- `tests/test_changelog_render.py` — renderer unit tests.
- `tests/test_feedback_routes.py` — route tests (no DB; store monkeypatched).
- `tests/test_feedback_store.py` — store round-trip (DB-gated).

---

## Task 1: Changelog renderer — cards, Features/Fixes, stable keys, feature badge

**Files:**
- Modify: `src/zira_dashboard/routes/changelog.py` (replace the `_md_to_html` function, lines 43-111)
- Test: `tests/test_changelog_render.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_changelog_render.py`:

```python
"""Unit tests for the /changelog markdown-to-cards renderer (pure, no app/DB)."""

from zira_dashboard.routes.changelog import _md_to_html


def test_structured_entry_has_groups_badge_title_and_key():
    md = (
        "# What's New\n\n"
        "Intro paragraph that should not render as a card.\n\n"
        "## 2026-06-24\n\n"
        "### 9:00 AM — Tasks redesign\n\n"
        "#### Features\n"
        "- **Date-only list views.** Tasks show a date in lists.\n"
        "#### Fixes\n"
        "- **Fixed empty deadline.** Odoo sent a blank date.\n"
    )
    out = _md_to_html(md)
    assert 'class="cl-entry"' in out
    assert 'data-key="2026-06-24T09:00"' in out
    assert 'data-feature="1"' in out
    assert '<span class="cl-entry-title">Tasks redesign</span>' in out
    assert '<span class="cl-entry-date">2026-06-24</span>' in out
    assert '<span class="cl-badge">New feature</span>' in out
    assert '<h4 class="cl-group-title">Features</h4>' in out
    assert '<h4 class="cl-group-title">Fixes</h4>' in out
    assert "<strong>Date-only list views.</strong>" in out
    assert 'class="cl-markread" data-key="2026-06-24T09:00"' in out


def test_legacy_prose_entry_is_highlights_with_no_badge():
    md = (
        "## 2026-06-09\n\n"
        "### 8:38 AM\n\n"
        "- **New Missed Punch Out alert.** Auto clock-out at midnight.\n"
        "- **Second note.** Another thing shipped.\n"
    )
    out = _md_to_html(md)
    assert 'data-key="2026-06-09T08:38"' in out
    assert 'data-feature="0"' in out
    assert 'class="cl-badge"' not in out
    assert '<h4 class="cl-group-title">Highlights</h4>' in out
    assert out.count("<li>") == 2


def test_untimed_deploy_falls_back_to_indexed_key():
    md = (
        "## 2026-06-01\n\n"
        "### Notes\n\n"
        "- **Something.** A change with no deploy time.\n"
    )
    out = _md_to_html(md)
    assert 'data-key="2026-06-01#0"' in out
    assert 'data-feature="0"' in out
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_changelog_render.py -v`
Expected: FAIL — the current `_md_to_html` emits `<section class="changelog-deploy">`, so `class="cl-entry"` is absent.

- [ ] **Step 3: Replace `_md_to_html` with the card renderer**

In `src/zira_dashboard/routes/changelog.py`, replace the entire `_md_to_html` function (the `def _md_to_html(text: str) -> str:` block, lines 43-111) with:

```python
def _fmt_inline(text: str) -> str:
    """Escape, then apply the small inline markdown subset (**bold**, `code`, *em*)."""
    line = html.escape(text.rstrip())
    line = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", line)
    line = re.sub(r"`([^`]+)`", r"<code>\1</code>", line)
    line = re.sub(r"(?<!\*)\*([^*\n]+)\*(?!\*)", r"<em>\1</em>", line)
    return line


def _parse_entries(text: str) -> list[dict]:
    """Parse CHANGELOG.md into one entry (card) per `### TIME` deploy.

    `## YYYY-MM-DD` sets the date. `### TIME [— Title]` opens a deploy; an
    optional title follows an em dash or spaced hyphen. `#### Features` /
    `#### Fixes` group the bullets that follow; bullets with no group go to
    Highlights. Each entry gets a stable key: `YYYY-MM-DDTHH:MM` when the time
    parses, else `YYYY-MM-DD#<index-within-date>`.
    """
    entries: list[dict] = []
    cur_date: str | None = None
    date_counts: dict[str | None, int] = {}
    entry: dict | None = None
    group = "highlights"

    def push():
        nonlocal entry
        if entry is not None:
            entries.append(entry)
            entry = None

    for raw in text.splitlines():
        s = raw.rstrip()
        if s.startswith("#### "):
            label = s[5:].strip().lower()
            if entry is None:
                continue
            group = "features" if label.startswith("feature") else "fixes" if label.startswith("fix") else "highlights"
        elif s.startswith("### "):
            push()
            head = s[4:].strip()
            parts = re.split(r"\s+[—-]\s+", head, maxsplit=1)
            time_text = parts[0].strip()
            title = parts[1].strip() if len(parts) > 1 else None
            t24 = _parse_time_to_24h(time_text)
            idx = date_counts.get(cur_date, 0)
            date_counts[cur_date] = idx + 1
            if cur_date and t24:
                key = f"{cur_date}T{t24}"
            elif cur_date:
                key = f"{cur_date}#{idx}"
            else:
                key = f"entry#{len(entries)}"
            entry = {"date": cur_date, "title": title, "key": key,
                     "features": [], "fixes": [], "highlights": []}
            group = "highlights"
        elif s.startswith("## "):
            push()
            m = re.match(r"^##\s+(\d{4}-\d{2}-\d{2})", s)
            cur_date = m.group(1) if m else (s[3:].strip() or None)
        elif s.startswith("# "):
            push()
        elif s.startswith("- ") and entry is not None:
            entry[group].append(_fmt_inline(s[2:]))
    push()
    return entries


def _render_entry(e: dict) -> str:
    has_feature = bool(e["features"])
    out = [
        f'<article class="cl-entry" data-key="{html.escape(e["key"], quote=True)}" '
        f'data-feature="{"1" if has_feature else "0"}">',
        '<header class="cl-entry-head">',
    ]
    if e["title"]:
        out.append(f'<span class="cl-entry-title">{_fmt_inline(e["title"])}</span>')
    if e["date"]:
        out.append(f'<span class="cl-entry-date">{html.escape(e["date"])}</span>')
    if has_feature:
        out.append('<span class="cl-badge">New feature</span>')
    out.append("</header>")

    def group_html(label: str, items: list[str]) -> str:
        if not items:
            return ""
        lis = "".join(f"<li>{x}</li>" for x in items)
        return f'<div class="cl-group"><h4 class="cl-group-title">{label}</h4><ul>{lis}</ul></div>'

    out.append(group_html("Features", e["features"]))
    out.append(group_html("Fixes", e["fixes"]))
    out.append(group_html("Highlights", e["highlights"]))
    out.append(
        f'<button type="button" class="cl-markread" '
        f'data-key="{html.escape(e["key"], quote=True)}">Mark read</button>'
    )
    out.append("</article>")
    return "".join(out)


def _md_to_html(text: str) -> str:
    """Render CHANGELOG.md as a stack of per-deploy cards."""
    return "\n".join(_render_entry(e) for e in _parse_entries(text))
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_changelog_render.py -v`
Expected: PASS (3 passed).

- [ ] **Step 5: Confirm the real changelog still renders without error**

Run: `.venv/bin/python -c "from pathlib import Path; from zira_dashboard.routes.changelog import _md_to_html; print(_md_to_html(Path('CHANGELOG.md').read_text())[:200])"`
Expected: prints HTML starting with `<article class="cl-entry"` — no exception.

- [ ] **Step 6: Commit**

```bash
git add src/zira_dashboard/routes/changelog.py tests/test_changelog_render.py
git commit -m "feat(changelog): render deploys as cards with Features/Fixes + stable keys"
```

---

## Task 2: Feedback table + store module

**Files:**
- Modify: `src/zira_dashboard/_schema.py` (append a CREATE TABLE to `SCHEMA_DDL`)
- Create: `src/zira_dashboard/feedback_store.py`
- Test: `tests/test_feedback_store.py` (DB-gated)

- [ ] **Step 1: Add the `feedback` table to the schema**

In `src/zira_dashboard/_schema.py`, inside the `SCHEMA_DDL = """ ... """` string, add this block near the other `CREATE TABLE IF NOT EXISTS` statements (anywhere before the closing `"""`):

```sql
CREATE TABLE IF NOT EXISTS feedback (
  id          SERIAL PRIMARY KEY,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  submitter   TEXT,
  page_url    TEXT,
  category    TEXT,
  message     TEXT NOT NULL
);
```

- [ ] **Step 2: Write the failing store test**

Create `tests/test_feedback_store.py`:

```python
"""Round-trip test for feedback_store (needs Postgres)."""

import os

import pytest

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"), reason="needs Postgres"
)

from zira_dashboard import db, feedback_store


@pytest.fixture(autouse=True)
def _schema():
    db.init_pool()
    db.bootstrap_schema()
    yield


def test_insert_then_recent_round_trip():
    new_id = feedback_store.insert(
        message="Round-trip test message",
        submitter="tester@gruberpallets.com",
        page_url="/recycling",
        category="Idea",
    )
    assert isinstance(new_id, int)
    rows = feedback_store.recent(limit=50)
    match = next((r for r in rows if r["id"] == new_id), None)
    assert match is not None
    assert match["message"] == "Round-trip test message"
    assert match["submitter"] == "tester@gruberpallets.com"
    assert match["category"] == "Idea"
    db.execute("DELETE FROM feedback WHERE id = %s", (new_id,))
```

- [ ] **Step 3: Run the test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_feedback_store.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'zira_dashboard.feedback_store'` (or SKIP if no `DATABASE_URL` locally — in that case verify the import error by running `.venv/bin/python -c "from zira_dashboard import feedback_store"`, which must fail).

- [ ] **Step 4: Create the store module**

Create `src/zira_dashboard/feedback_store.py`:

```python
"""Persistence for user-submitted feedback (the What's-new panel)."""

from __future__ import annotations

from . import db


def insert(
    message: str,
    submitter: str | None = None,
    page_url: str | None = None,
    category: str | None = None,
) -> int:
    """Insert one feedback row; return its new id."""
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO feedback (submitter, page_url, category, message) "
            "VALUES (%s, %s, %s, %s) RETURNING id",
            (submitter, page_url, category, message),
        )
        return cur.fetchone()["id"]


def recent(limit: int = 200) -> list[dict]:
    """Return the most recent feedback rows, newest first."""
    return db.query(
        "SELECT id, created_at, submitter, page_url, category, message "
        "FROM feedback ORDER BY id DESC LIMIT %s",
        (limit,),
    )
```

- [ ] **Step 5: Run the test to verify it passes (or imports cleanly)**

Run: `.venv/bin/python -m pytest tests/test_feedback_store.py -v`
Expected: PASS with `DATABASE_URL` set; SKIP otherwise. With no DB, confirm the module imports: `.venv/bin/python -c "from zira_dashboard import feedback_store; print('ok')"` → prints `ok`.

- [ ] **Step 6: Commit**

```bash
git add src/zira_dashboard/_schema.py src/zira_dashboard/feedback_store.py tests/test_feedback_store.py
git commit -m "feat(feedback): add feedback table + store module"
```

---

## Task 3: Feedback routes + admin page + app wiring + settings link

**Files:**
- Create: `src/zira_dashboard/routes/feedback.py`
- Create: `src/zira_dashboard/templates/admin_feedback.html`
- Modify: `src/zira_dashboard/app.py` (import + include the router)
- Modify: `src/zira_dashboard/templates/settings.html` (nav link)
- Test: `tests/test_feedback_routes.py` (no DB; store monkeypatched)

- [ ] **Step 1: Write the failing route tests**

Create `tests/test_feedback_routes.py`:

```python
"""Feedback route tests. conftest sets AUTH_DISABLED=1, so no login needed.
The store is monkeypatched, so these run without Postgres."""

import pytest
from fastapi.testclient import TestClient

from zira_dashboard.app import app
from zira_dashboard import feedback_store

client = TestClient(app)


def test_post_feedback_inserts_and_returns_id(monkeypatch):
    captured = {}

    def fake_insert(**kwargs):
        captured.update(kwargs)
        return 123

    monkeypatch.setattr(feedback_store, "insert", fake_insert)
    r = client.post("/feedback", json={"message": "  Great app  ", "category": "Idea",
                                       "page_url": "/recycling"})
    assert r.status_code == 200
    body = r.json()
    assert body == {"ok": True, "id": 123}
    assert captured["message"] == "Great app"  # trimmed
    assert captured["category"] == "Idea"
    assert captured["page_url"] == "/recycling"


def test_post_feedback_rejects_empty_message(monkeypatch):
    called = {"n": 0}
    monkeypatch.setattr(feedback_store, "insert",
                        lambda **k: called.__setitem__("n", called["n"] + 1) or 1)
    r = client.post("/feedback", json={"message": "   "})
    assert r.status_code == 400
    assert r.json()["ok"] is False
    assert called["n"] == 0


def test_admin_feedback_renders_rows(monkeypatch):
    monkeypatch.setattr(feedback_store, "recent", lambda limit=200: [
        {"id": 5, "created_at": "2026-06-24 09:00", "submitter": "dale@x.com",
         "page_url": "/staffing", "category": "Bug", "message": "Sticky note text here"},
    ])
    r = client.get("/admin/feedback")
    assert r.status_code == 200
    assert "Sticky note text here" in r.text
    assert "dale@x.com" in r.text
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_feedback_routes.py -v`
Expected: FAIL — `/feedback` and `/admin/feedback` return 404 (routes not registered yet).

- [ ] **Step 3: Create the feedback router**

Create `src/zira_dashboard/routes/feedback.py`:

```python
"""User feedback: POST from the What's-new panel + a read-only admin list."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

from .. import feedback_store
from ..deps import templates

router = APIRouter()


class FeedbackIn(BaseModel):
    message: str
    category: str | None = None
    page_url: str | None = None


@router.post("/feedback")
def submit_feedback(payload: FeedbackIn, request: Request) -> JSONResponse:
    message = (payload.message or "").strip()
    if not message:
        return JSONResponse({"ok": False, "error": "Message is required."}, status_code=400)
    submitter = getattr(request.state, "user_upn", None)
    new_id = feedback_store.insert(
        message=message,
        submitter=submitter,
        page_url=(payload.page_url or None),
        category=(payload.category or None),
    )
    return JSONResponse({"ok": True, "id": new_id})


@router.get("/admin/feedback", response_class=HTMLResponse)
def admin_feedback(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "admin_feedback.html",
        {"items": feedback_store.recent(), "active": "admin"},
    )
```

- [ ] **Step 4: Create the admin template**

Create `src/zira_dashboard/templates/admin_feedback.html` (self-contained — no base/static dependency, so it always renders):

```html
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Feedback — GPI Plant Manager</title>
<style>
  body { font-family: system-ui, -apple-system, sans-serif; margin: 2rem; color: #1f2937; }
  h1 { font-size: 1.2rem; }
  table { border-collapse: collapse; width: 100%; font-size: 0.9rem; }
  th, td { border: 1px solid #d8dee5; padding: 0.4rem 0.6rem; text-align: left; vertical-align: top; }
  th { background: #f1f4f7; }
  .muted { color: #6b7280; font-size: 0.8rem; }
  .empty { color: #6b7280; }
  a { color: #16a34a; }
</style>
</head>
<body>
  <h1>Feedback</h1>
  <p><a href="/recycling">← Back to dashboards</a></p>
  {% if items %}
  <table>
    <thead><tr><th>When</th><th>From</th><th>Category</th><th>Message</th><th>Page</th></tr></thead>
    <tbody>
      {% for it in items %}
      <tr>
        <td class="muted">{{ it.created_at }}</td>
        <td>{{ it.submitter or '—' }}</td>
        <td>{{ it.category or '—' }}</td>
        <td>{{ it.message }}</td>
        <td class="muted">{{ it.page_url or '—' }}</td>
      </tr>
      {% endfor %}
    </tbody>
  </table>
  {% else %}
  <p class="empty">No feedback yet.</p>
  {% endif %}
</body>
</html>
```

- [ ] **Step 5: Wire the router into the app**

In `src/zira_dashboard/app.py`:
1. Add `feedback` to the `from .routes import (` import block (alongside `changelog`).
2. Immediately after the line `app.include_router(changelog.router)`, add:

```python
app.include_router(feedback.router)
```

- [ ] **Step 6: Add a Feedback link to the settings nav**

In `src/zira_dashboard/templates/settings.html`, in the header `<nav>`, add this link right after the Settings link (`<a href="/settings" class="active">Settings</a>`):

```html
    <a href="/admin/feedback">Feedback</a>
```

- [ ] **Step 7: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_feedback_routes.py -v`
Expected: PASS (3 passed).

- [ ] **Step 8: Commit**

```bash
git add src/zira_dashboard/routes/feedback.py src/zira_dashboard/templates/admin_feedback.html src/zira_dashboard/app.py src/zira_dashboard/templates/settings.html tests/test_feedback_routes.py
git commit -m "feat(feedback): POST /feedback + /admin/feedback page, wired into app"
```

---

## Task 4: Rebuild `_footer.html` into the card panel + feedback form

**Files:**
- Modify: `src/zira_dashboard/templates/_footer.html` (full replace)

- [ ] **Step 1: Replace the file contents**

Replace the entire contents of `src/zira_dashboard/templates/_footer.html` with:

```html
<div id="changelog-modal" class="changelog-modal" hidden>
  <div class="changelog-backdrop" id="changelog-backdrop"></div>
  <div class="changelog-card" role="dialog" aria-modal="true" aria-label="What's new">
    <div class="changelog-head">
      <div class="changelog-titles">
        <h3>What's new</h3>
        <p class="changelog-sub">Recent Plant Manager changes</p>
      </div>
      <div class="changelog-head-actions">
        <button type="button" id="changelog-feedback-toggle" class="changelog-feedback-btn">Send feedback</button>
        <button type="button" id="changelog-close" class="changelog-close" aria-label="Close">Close</button>
      </div>
    </div>
    <form id="changelog-feedback" class="changelog-feedback" hidden>
      <select id="changelog-feedback-category" class="changelog-feedback-category" aria-label="Feedback category">
        <option value="">General</option>
        <option value="Bug">Bug</option>
        <option value="Idea">Idea</option>
        <option value="Other">Other</option>
      </select>
      <textarea id="changelog-feedback-message" class="changelog-feedback-message" rows="3"
                placeholder="Tell us what's working or what's not…"></textarea>
      <div class="changelog-feedback-actions">
        <button type="submit" class="changelog-feedback-send">Send</button>
        <button type="button" id="changelog-feedback-cancel" class="changelog-feedback-cancel">Cancel</button>
        <span id="changelog-feedback-status" class="changelog-feedback-status" hidden></span>
      </div>
    </form>
    <div class="changelog-toolbar">
      <button type="button" id="changelog-markall" class="changelog-markall">Mark all read</button>
    </div>
    <div id="changelog-body" class="changelog-body">Loading…</div>
  </div>
</div>
<link rel="stylesheet" href="/static/footer.css?v={{ static_v('footer.css') }}">
<script src="/static/footer.js?v={{ static_v('footer.js') }}"></script>
```

(The `<footer class="app-footer">` element and the `#changelog-open` link are removed — the trigger is now injected into the header by `footer.js`.)

- [ ] **Step 2: Manual verification (after Tasks 5 & 6 land)**

This task has no automated test (template render needs DB). It is verified jointly in Task 6's manual checks.

- [ ] **Step 3: Commit**

```bash
git add src/zira_dashboard/templates/_footer.html
git commit -m "feat(whatsnew): rebuild footer modal into the card panel + feedback form"
```

---

## Task 5: `footer.css` — trigger button, panel cards, badge, feedback form

**Files:**
- Modify: `src/zira_dashboard/static/footer.css`

- [ ] **Step 1: Remove the obsolete footer-link styles**

In `src/zira_dashboard/static/footer.css`, delete the `.app-footer` and `.changelog-link` rules (the block spanning the current lines 1-26, from `.app-footer {` through the `.app-footer .changelog-link.has-new::before { ... }` rule). Leave the `.changelog-modal` / `.changelog-backdrop` / `.changelog-card` / `.changelog-head` / `.changelog-close` / `.changelog-body` rules in place.

- [ ] **Step 2: Add the new styles**

At the top of `src/zira_dashboard/static/footer.css` (before the `.changelog-modal[hidden]` rule), add:

```css
  /* What's-new trigger button (injected into the header by footer.js) */
  .whatsnew-slot { margin-left: auto; display: flex; align-items: center; }
  .whatsnew-btn {
    position: relative; display: inline-flex; align-items: center; justify-content: center;
    width: 34px; height: 30px; padding: 0; cursor: pointer;
    background: transparent; color: var(--accent, #16a34a);
    border: 1px solid var(--accent, #16a34a); border-radius: 8px;
  }
  .whatsnew-btn:hover { background: var(--accent-dim, #dcfce7); }
  .whatsnew-dot {
    position: absolute; top: -3px; right: -3px;
    width: 9px; height: 9px; border-radius: 50%;
    background: var(--accent, #16a34a); border: 2px solid var(--panel, #fff);
  }

  /* Panel head + toolbar + feedback form */
  .changelog-titles h3 { margin: 0; font-size: 1rem; font-weight: 700; }
  .changelog-sub { margin: 0.1rem 0 0; font-size: 0.8rem; color: var(--muted, #6b7280); }
  .changelog-head-actions { display: flex; align-items: center; gap: 0.5rem; }
  .changelog-feedback-btn {
    background: transparent; color: var(--accent, #16a34a);
    border: 1px solid var(--accent, #16a34a); border-radius: 6px;
    padding: 0.3rem 0.7rem; font: inherit; font-size: 0.82rem; cursor: pointer;
  }
  .changelog-feedback-btn:hover { background: var(--accent-dim, #dcfce7); }
  .changelog-toolbar { display: flex; justify-content: flex-end; padding: 0.5rem 1.1rem 0; }
  .changelog-markall {
    background: transparent; color: var(--fg, #1f2937);
    border: 1px solid var(--border, #d8dee5); border-radius: 6px;
    padding: 0.25rem 0.6rem; font: inherit; font-size: 0.8rem; cursor: pointer;
  }
  .changelog-markall:hover { border-color: var(--accent, #16a34a); color: var(--accent, #16a34a); }
  .changelog-feedback {
    display: flex; flex-direction: column; gap: 0.4rem;
    padding: 0.6rem 1.1rem; border-bottom: 1px solid var(--border, #d8dee5);
  }
  .changelog-feedback-category {
    align-self: flex-start; border: 1px solid var(--border, #d8dee5);
    border-radius: 6px; padding: 0.3rem; font: inherit;
  }
  .changelog-feedback-message {
    width: 100%; resize: vertical; font: inherit;
    border: 1px solid var(--border, #d8dee5); border-radius: 6px; padding: 0.4rem;
  }
  .changelog-feedback-actions { display: flex; align-items: center; gap: 0.5rem; }
  .changelog-feedback-send {
    background: var(--accent, #16a34a); color: #fff;
    border: 1px solid var(--accent, #16a34a); border-radius: 6px;
    padding: 0.3rem 0.8rem; font: inherit; font-weight: 600; cursor: pointer;
  }
  .changelog-feedback-cancel {
    background: transparent; color: var(--fg, #1f2937);
    border: 1px solid var(--border, #d8dee5); border-radius: 6px;
    padding: 0.3rem 0.7rem; font: inherit; cursor: pointer;
  }
  .changelog-feedback-status { font-size: 0.8rem; color: var(--muted, #6b7280); }

  /* Changelog entry cards */
  .cl-entry {
    border: 1px solid var(--border, #d8dee5); border-radius: 12px;
    padding: 0.8rem 1rem; margin-bottom: 0.75rem;
  }
  .cl-entry.cl-read { opacity: 0.7; }
  .cl-entry-head { display: flex; align-items: baseline; gap: 0.5rem; flex-wrap: wrap; margin-bottom: 0.4rem; }
  .cl-entry-title { font-size: 0.98rem; font-weight: 700; }
  .cl-entry-date { font-size: 0.78rem; color: var(--muted, #6b7280); }
  .cl-badge {
    background: var(--accent-dim, #dcfce7); color: #166534;
    font-size: 0.72rem; font-weight: 700; padding: 0.1rem 0.5rem; border-radius: 999px;
  }
  .cl-group { margin: 0.3rem 0; }
  .cl-group-title {
    font-size: 0.72rem; font-weight: 700; text-transform: uppercase; letter-spacing: 0.5px;
    color: var(--muted, #6b7280); margin: 0.4rem 0 0.2rem;
  }
  .cl-group ul { margin: 0 0 0.4rem 1.1rem; padding: 0; }
  .cl-group li { margin: 0.18rem 0; }
  .cl-markread {
    background: transparent; color: var(--fg, #1f2937);
    border: 1px solid var(--border, #d8dee5); border-radius: 6px;
    padding: 0.25rem 0.6rem; font: inherit; font-size: 0.8rem; cursor: pointer; margin-top: 0.3rem;
  }
  .cl-markread:hover { border-color: var(--accent, #16a34a); color: var(--accent, #16a34a); }
```

- [ ] **Step 3: Commit**

```bash
git add src/zira_dashboard/static/footer.css
git commit -m "feat(whatsnew): green trigger button, card panel, badge + feedback styles"
```

---

## Task 6: `footer.js` — trigger injection, read state, mark read/all, feedback submit

**Files:**
- Modify: `src/zira_dashboard/static/footer.js` (replace the changelog IIFE, lines 31-99)

- [ ] **Step 1: Replace the changelog IIFE**

In `src/zira_dashboard/static/footer.js`, replace the second IIFE — the one beginning `(function () {\n  var btn = document.getElementById('changelog-open');` and ending at its closing `})();` (current lines 31-99) — with the following. Do **not** touch the first IIFE (the `gpiFetch` shim, lines 1-29) or the third IIFE (the alert-badges factory, lines 101+).

```javascript
(function () {
  var btn = null, dot = null, modal = null, body = null, panelLoaded = false;

  function getCutoff() { try { return localStorage.getItem('changelog_cutoff') || ''; } catch (e) { return ''; } }
  function setCutoff(v) { try { localStorage.setItem('changelog_cutoff', v || ''); } catch (e) {} }
  function getRead() {
    try { return new Set(JSON.parse(localStorage.getItem('changelog_read') || '[]')); }
    catch (e) { return new Set(); }
  }
  function setRead(s) {
    try { localStorage.setItem('changelog_read', JSON.stringify(Array.from(s))); } catch (e) {}
  }
  function whenOf(key) { return String(key || '').split('#')[0]; }
  function isUnread(key) { return whenOf(key) > getCutoff() && !getRead().has(key); }

  // Inject the green trigger into the page header, far right. Headers vary:
  // most pages have [left group][right group] (append into the right group);
  // a header with a single child gets a right-aligned slot created for it.
  function injectButton() {
    var header = document.querySelector('header');
    if (!header || header.querySelector('.whatsnew-btn')) return;
    btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'whatsnew-btn';
    btn.setAttribute('aria-label', "What's new");
    btn.setAttribute('aria-haspopup', 'dialog');
    btn.innerHTML = '<svg viewBox="0 0 24 24" width="18" height="18" aria-hidden="true" '
      + 'fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" '
      + 'stroke-linejoin="round"><path d="M3 11l15-5v13L3 14z"></path>'
      + '<path d="M11.5 19a3 3 0 0 1-5.5-1.7"></path></svg>'
      + '<span class="whatsnew-dot" hidden></span>';
    dot = btn.querySelector('.whatsnew-dot');
    var kids = header.children;
    if (kids.length >= 2) {
      kids[kids.length - 1].appendChild(btn);
    } else {
      var slot = document.createElement('div');
      slot.className = 'whatsnew-slot';
      slot.appendChild(btn);
      header.appendChild(slot);
    }
    btn.addEventListener('click', openPanel);
  }

  // Cheap on-load dot: ask /changelog/latest and seed the cutoff on first visit.
  function refreshDot() {
    if (!dot) return;
    window.gpiFetch('/changelog/latest').then(function (r) { return r.json(); }).then(function (d) {
      var latest = d && d.latest_date;
      if (localStorage.getItem('changelog_cutoff') == null) {
        var seen = '';
        try { seen = localStorage.getItem('changelog_seen') || ''; } catch (e) {}
        setCutoff(seen || latest || '');
      }
      dot.hidden = !(latest && isUnread(latest));
    }).catch(function () {});
  }

  function ensureModal() {
    modal = document.getElementById('changelog-modal');
    body = document.getElementById('changelog-body');
    if (!modal || modal.dataset.wired) return !!modal;
    modal.dataset.wired = '1';
    var backdrop = document.getElementById('changelog-backdrop');
    var closeBtn = document.getElementById('changelog-close');
    var markAll = document.getElementById('changelog-markall');
    var fbToggle = document.getElementById('changelog-feedback-toggle');
    var fbForm = document.getElementById('changelog-feedback');
    var fbCancel = document.getElementById('changelog-feedback-cancel');
    if (backdrop) backdrop.addEventListener('click', closePanel);
    if (closeBtn) closeBtn.addEventListener('click', closePanel);
    if (markAll) markAll.addEventListener('click', markAllRead);
    if (fbToggle) fbToggle.addEventListener('click', toggleFeedback);
    if (fbForm) fbForm.addEventListener('submit', submitFeedback);
    if (fbCancel) fbCancel.addEventListener('click', toggleFeedback);
    document.addEventListener('keydown', function (e) {
      if (e.key === 'Escape' && modal && !modal.hidden) closePanel();
    });
    return true;
  }

  function openPanel(e) {
    if (e) e.preventDefault();
    if (!ensureModal()) return;
    modal.hidden = false;
    document.documentElement.style.overflow = 'hidden';
    if (!panelLoaded) {
      window.gpiFetch('/changelog').then(function (r) { return r.text(); }).then(function (htmlStr) {
        body.innerHTML = htmlStr;
        panelLoaded = true;
        wireCards();
        applyReadState();
      }).catch(function () { body.innerHTML = '<p>Could not load changelog.</p>'; });
    } else {
      applyReadState();
    }
  }

  function closePanel() {
    if (modal) modal.hidden = true;
    document.documentElement.style.overflow = '';
  }

  function applyReadState() {
    if (!body) return;
    Array.prototype.forEach.call(body.querySelectorAll('.cl-entry'), function (card) {
      var key = card.getAttribute('data-key');
      var unread = isUnread(key);
      card.classList.toggle('cl-read', !unread);
      var mr = card.querySelector('.cl-markread');
      if (mr) mr.hidden = !unread;
    });
    refreshDotFromCards();
  }

  function refreshDotFromCards() {
    if (!dot || !body) return;
    var anyUnread = Array.prototype.some.call(
      body.querySelectorAll('.cl-entry'),
      function (c) { return isUnread(c.getAttribute('data-key')); }
    );
    dot.hidden = !anyUnread;
  }

  function wireCards() {
    Array.prototype.forEach.call(body.querySelectorAll('.cl-markread'), function (mr) {
      mr.addEventListener('click', function () {
        var s = getRead(); s.add(mr.getAttribute('data-key')); setRead(s);
        applyReadState();
      });
    });
  }

  function markAllRead() {
    if (!body) return;
    var newest = getCutoff();
    Array.prototype.forEach.call(body.querySelectorAll('.cl-entry'), function (c) {
      var w = whenOf(c.getAttribute('data-key'));
      if (w > newest) newest = w;
    });
    setCutoff(newest);
    setRead(new Set());
    applyReadState();
  }

  function toggleFeedback(e) {
    if (e) e.preventDefault();
    var form = document.getElementById('changelog-feedback');
    if (form) form.hidden = !form.hidden;
  }

  function submitFeedback(e) {
    e.preventDefault();
    var msgEl = document.getElementById('changelog-feedback-message');
    var catEl = document.getElementById('changelog-feedback-category');
    var statusEl = document.getElementById('changelog-feedback-status');
    var sendBtn = e.target.querySelector('button[type="submit"]');
    var msg = ((msgEl && msgEl.value) || '').trim();
    if (statusEl) statusEl.hidden = false;
    if (!msg) { if (statusEl) statusEl.textContent = 'Please enter a message.'; return; }
    if (sendBtn) sendBtn.disabled = true;
    if (statusEl) statusEl.textContent = 'Sending…';
    window.gpiFetch('/feedback', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        message: msg,
        category: (catEl && catEl.value) || null,
        page_url: window.location.href
      })
    }).then(function (r) { return r.json(); }).then(function (resp) {
      if (resp && resp.ok) {
        if (msgEl) msgEl.value = '';
        if (statusEl) statusEl.textContent = 'Thanks — sent!';
        setTimeout(function () {
          var f = document.getElementById('changelog-feedback');
          if (f) f.hidden = true;
          if (statusEl) statusEl.hidden = true;
        }, 1500);
      } else if (statusEl) {
        statusEl.textContent = 'Failed: ' + ((resp && resp.error) || 'unknown');
      }
      if (sendBtn) sendBtn.disabled = false;
    }).catch(function () {
      if (statusEl) statusEl.textContent = 'Network error.';
      if (sendBtn) sendBtn.disabled = false;
    });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', function () { injectButton(); refreshDot(); });
  } else {
    injectButton();
    refreshDot();
  }
})();
```

- [ ] **Step 2: Manual verification** (covers Tasks 4, 5, 6 together)

Start the app: `.venv/bin/python -m uvicorn zira_dashboard.app:app --reload` and sign in. Then:

1. **Trigger placement** — On `/`, `/staffing`, `/recycling`, `/handoff`, `/exceptions`, `/settings`: a green outlined icon button appears at the **top-right of the header**. Confirm it does not overlap existing header controls (undo/redo on staffing/settings, the day chip on handoff/exceptions).
2. **Panel opens** — Click it: the panel opens centered, titled "What's new" with the "Recent Plant Manager changes" subtitle, a green "Send feedback" button, and "Close". The body shows changelog **cards**; recent ones show a green "New feature" badge where the entry has a Features section.
3. **Read state** — Click "Mark read" on the top card → it dims and its button disappears. Reload the page → that card stays read (the trigger dot reflects remaining unread). Click "Mark all read" → all cards dim, dot clears.
4. **Esc / backdrop / Close** all dismiss the panel.
5. **Feedback** — Click "Send feedback" → form expands. Submit empty → "Please enter a message." Type a message, pick a category, Send → "Thanks — sent!" then visit `/admin/feedback` and confirm the row (message, category, page URL) is listed.
6. **No console errors** in the browser devtools on any of the above.

- [ ] **Step 3: Commit**

```bash
git add src/zira_dashboard/static/footer.js
git commit -m "feat(whatsnew): inject top-right trigger, per-entry read state, feedback submit"
```

---

## Task 7: Move recycling's date-range row below the header

**Files:**
- Modify: `src/zira_dashboard/templates/recycling.html`
- Modify: `src/zira_dashboard/static/recycling.css`

- [ ] **Step 1: Move the range form out of the header**

In `src/zira_dashboard/templates/recycling.html`, cut the entire `<form class="rc-toolbar" method="get" action="/recycling"> … </form>` block (currently lines 38-60, ending just before `</header>`) and paste it **after** the `{% include "_dashboards_subnav.html" %}` line. Result:

```html
  </div>
</header>
{% include "_dashboards_subnav.html" %}
<form class="rc-toolbar" method="get" action="/recycling">
  <span class="rc-tool-label">Range:</span>
  ... (the windows loop + Custom popover, unchanged) ...
</form>
```

- [ ] **Step 2: Make `.rc-toolbar` a standalone row**

In `src/zira_dashboard/static/recycling.css`, replace the `.rc-toolbar` rule (currently lines 795-799):

```css
  .rc-toolbar {
    display: inline-flex; flex-wrap: wrap; gap: 0.4rem; align-items: center;
    margin-left: 1rem;
    font-size: 0.85rem;
  }
```

with:

```css
  .rc-toolbar {
    display: flex; flex-wrap: wrap; gap: 0.4rem; align-items: center;
    margin: 0.9rem 1rem 0.4rem;
    font-size: 0.85rem;
  }
```

- [ ] **Step 3: Manual verification**

Reload `/recycling`: the "Range:" chip row now sits on its **own line below the dashboards subnav** (not inside the top header), the green What's-new button sits cleanly at the top-right of the header, and hovering a person's production bar shows the "… expected (…%)" tooltip **without it overlapping the range controls**. Also load `/recycling?tv=1` (or the TV path) and confirm **no** What's-new button appears (footer is `tv_mode`-guarded).

- [ ] **Step 4: Commit**

```bash
git add src/zira_dashboard/templates/recycling.html src/zira_dashboard/static/recycling.css
git commit -m "fix(recycling): move date-range row below the subnav, clear of the header button"
```

---

## Task 8: Document the format and write this feature's changelog entry

**Files:**
- Modify: `CHANGELOG.md`

- [ ] **Step 1: Add a format note near the top**

In `CHANGELOG.md`, directly under the intro paragraph (after line 3), add:

```markdown

<!-- Format: `## YYYY-MM-DD` date, then `### TIME — Optional Title` per deploy.
     Within a deploy, optional `#### Features` / `#### Fixes` group the bullets
     (anything ungrouped renders as "Highlights"). An entry with a Features
     group shows a green "New feature" badge in the What's-new panel. -->
```

- [ ] **Step 2: Add the entry for this change (today's date) as the newest entry**

Immediately below the format note and above the first existing `## ` date, add (use today's real date for the `## ` heading):

```markdown
## 2026-06-24

### 12:00 PM — What's new panel, top-right

#### Features
- **Redesigned the "What's new" experience.** The footer link is gone; a green announcement button now sits at the top-right of every page header and opens a card-based panel — one card per deploy, grouped into Features / Fixes, with per-entry "Mark read" and "Mark all read". A new **Send feedback** action saves straight to the app (viewable at `/admin/feedback`).

#### Fixes
- **Moved the dashboard date-range row down.** On the recycling dashboard the range chips moved out of the top header onto their own row below the subnav, so production hover tooltips no longer overlap the controls and the new top-right button has room.
```

- [ ] **Step 3: Verify it renders as structured cards**

Run: `.venv/bin/python -c "from pathlib import Path; from zira_dashboard.routes.changelog import _md_to_html; h=_md_to_html(Path('CHANGELOG.md').read_text()); assert 'data-key=\"2026-06-24T12:00\"' in h and 'New feature' in h; print('ok')"`
Expected: prints `ok`.

- [ ] **Step 4: Commit**

```bash
git add CHANGELOG.md
git commit -m "docs(changelog): note the structured format + add the What's-new entry"
```

---

## Task 9: Full verification pass

**Files:** none (verification only)

- [ ] **Step 1: Run the whole Python suite**

Run: `.venv/bin/python -m pytest -q`
Expected: all pass (DATABASE_URL-gated tests skip locally; the new `test_changelog_render.py` and `test_feedback_routes.py` pass, `test_feedback_store.py` skips without `DATABASE_URL`).

- [ ] **Step 2: Lint**

Run: `.venv/bin/python -m ruff check src/zira_dashboard/routes/changelog.py src/zira_dashboard/routes/feedback.py src/zira_dashboard/feedback_store.py`
Expected: no errors.

- [ ] **Step 3: Manual QA matrix** (app running, signed in)

- Trigger top-right and panel open on `/`, `/staffing`, `/recycling`, `/handoff`, `/exceptions`, `/settings`.
- Cards render with green badges; Mark read / Mark all read persist across reload; dot behaves.
- Feedback submit → row visible at `/admin/feedback`; Settings nav shows the Feedback link.
- Recycling range row sits below the subnav; tooltip no longer overlaps it.
- `/recycling?tv=1` shows no trigger button.
- No browser console errors anywhere.

- [ ] **Step 4: Final confirmation**

All automated tests pass, lint clean, manual matrix green. Feature complete.
