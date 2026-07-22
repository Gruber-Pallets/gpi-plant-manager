# UI Consolidation Wave 1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Introduce the single desktop document shell `_base_app.html`, convert the three lowest-risk standalone pages (Work Centers, Exception Inbox, Settings) to extend it, re-parent `_staffing_base.html` onto it, and fix two found chrome bugs — enforced by a ratchet test so the standalone-template list can only shrink.

**Architecture:** Pure template refactor — no route, CSS, or JS behavior changes. Each page's `<head>` links, header contents, subnav, body, and scripts move verbatim into named blocks of `_base_app.html`. TV-shared dashboard templates and `staffing.html` are explicitly NOT touched (Waves 2–3 per the spec). Kiosk (`timeclock_base.html`) untouched.

**Tech Stack:** FastAPI + Jinja2 templates, pytest + Starlette TestClient (`AUTH_DISABLED=1` set by `tests/conftest.py`).

**Spec:** `docs/superpowers/specs/2026-07-21-ui-consolidation.md`

**Test command prefix (always):** `ZIRA_API_KEY=test .venv/bin/python -m pytest`

---

### Task 1: Ratchet test + `_base_app.html`

**Files:**
- Create: `tests/test_base_app_template.py`
- Create: `src/zira_dashboard/templates/_base_app.html`

- [ ] **Step 1: Write the ratchet + static-reference guard tests**

Create `tests/test_base_app_template.py`:

```python
"""Chrome consolidation guards.

Ratchet: every full-page template must extend a base layout
(_base_app.html for desktop, timeclock_base.html for kiosk). Standalone
full-document templates are frozen in ALLOWED_STANDALONE and the list
only shrinks — never add to it. See
docs/superpowers/specs/2026-07-21-ui-consolidation.md.
"""
import re
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src" / "zira_dashboard"
TEMPLATES = SRC / "templates"
STATIC = SRC / "static"

BASES = {"_base_app.html", "timeclock_base.html"}

# auth_denied.html stays standalone permanently: it renders for
# UNAUTHENTICATED users and must not include _topnav.html (which calls
# nav_inbox_summary()). Everything else is queued for conversion.
ALLOWED_STANDALONE = {
    "auth_denied.html",             # permanent
    "exceptions.html",              # Wave 1
    "index.html",                   # Wave 1
    "settings.html",                # Wave 1
    "new_dept.html",                # Wave 2 (TV-shared)
    "new_leaderboard_tv.html",      # Wave 2 (TV-shared)
    "recycling.html",               # Wave 2 (TV-shared)
    "recycling_leaderboard_tv.html",  # Wave 2 (TV-shared)
    "wc_dashboard.html",            # Wave 2 (TV-shared)
    "staffing.html",                # Wave 3
}


def test_full_page_templates_extend_a_base():
    for path in sorted(TEMPLATES.glob("*.html")):
        if path.name.startswith("_") or path.name in BASES:
            continue
        src = path.read_text(encoding="utf-8")
        if "{% extends" in src:
            assert path.name not in ALLOWED_STANDALONE, (
                f"{path.name} now extends a base — remove it from ALLOWED_STANDALONE"
            )
        else:
            assert path.name in ALLOWED_STANDALONE, (
                f"{path.name} is a standalone document — extend _base_app.html "
                "or timeclock_base.html instead of hand-rolling chrome"
            )


def test_template_static_references_exist():
    """Every /static/<file> referenced by a template must exist on disk."""
    pattern = re.compile(r"/static/([A-Za-z0-9._-]+\.(?:css|js|png|ico|svg))")
    missing = []
    for path in sorted(TEMPLATES.glob("*.html")):
        for name in pattern.findall(path.read_text(encoding="utf-8")):
            if not (STATIC / name).exists():
                missing.append(f"{path.name} -> /static/{name}")
    assert missing == [], f"templates reference missing static assets: {missing}"
```

- [ ] **Step 2: Run the new tests — expect one pass, one FAIL**

Run: `ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_base_app_template.py -v`

Expected: `test_full_page_templates_extend_a_base` PASSES (the allowlist matches
today's reality). `test_template_static_references_exist` FAILS with
`auth_denied.html -> /static/dashboard.css` (a real pre-existing bug — the file
doesn't exist). Task 2 fixes it; for THIS commit, mark it as expected-fail by
adding this decorator above `test_template_static_references_exist`:

```python
import pytest

@pytest.mark.xfail(
    reason="auth_denied.html links nonexistent dashboard.css — fixed in Wave 1 Task 2",
    strict=True,
)
```

(`strict=True` means the test ERRORS once the bug is fixed, forcing removal of the
marker in the same commit as the fix.)

- [ ] **Step 3: Create `_base_app.html`**

Create `src/zira_dashboard/templates/_base_app.html` with exactly:

```jinja
{#
  _base_app.html — the single document shell for desktop pages.
  Kiosk pages extend timeclock_base.html instead. TV variants of the
  dashboard pages suppress chrome by overriding `header`/`footer`.

  Blocks (all optional except content):
    title        — page name; rendered as "{title} — GPI Plant Manager"
    head         — page <link>/<style> tags
    topnav       — override to set the active menu item, e.g.
                   {% block topnav %}{% set active_nav = 'staffing' %}{% include "_topnav.html" %}{% endblock %}
    header_extra — extra controls inside <header class="app">
    header       — whole-header override (TV mode only)
    subnav       — sub-navigation strip under the header
    main_attrs   — attributes appended to <main>
    content      — page body
    footer       — footer include; TV pages override to empty
    body_end     — trailing script region (intermediate bases add shared
                   scripts here and re-declare `scripts` inside it)
    scripts      — page <script> tags

  No inline styles here on purpose: page styling stays in each page's
  CSS file until Phase 2 (tokens.css) lands.
#}
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="icon" type="image/png" href="/static/gpi-logo.png">
<title>{% block title %}Home{% endblock %} — GPI Plant Manager</title>
<link rel="stylesheet" href="/static/topnav.css?v={{ static_v('topnav.css') }}">
{% block head %}{% endblock %}
</head>
<body>
{% block header %}
<header class="app">
  {% block topnav %}{% include "_topnav.html" %}{% endblock %}
  {% block header_extra %}{% endblock %}
</header>
{% endblock %}
{% block subnav %}{% endblock %}
<main{% block main_attrs %}{% endblock %}>
{% block content %}{% endblock %}
</main>
{% block footer %}{% include '_footer.html' %}{% endblock %}
{% block body_end %}{% block scripts %}{% endblock %}{% endblock %}
</body>
</html>
```

- [ ] **Step 4: Run the guard tests**

Run: `ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_base_app_template.py -v`
Expected: 1 passed, 1 xfailed.

- [ ] **Step 5: Commit**

```bash
git add tests/test_base_app_template.py src/zira_dashboard/templates/_base_app.html
git commit -m "feat: add _base_app document shell with template ratchet"
```

---

### Task 2: Fix the two found chrome bugs

**Files:**
- Modify: `src/zira_dashboard/templates/index.html:15` (form action)
- Modify: `src/zira_dashboard/templates/auth_denied.html:7` (dead stylesheet link)
- Modify: `tests/test_base_app_template.py` (add form test; drop the xfail marker)

- [ ] **Step 1: Write the failing form-action test**

Append to `tests/test_base_app_template.py`:

```python
from starlette.testclient import TestClient

from zira_dashboard.app import app
from zira_dashboard.routes import dashboard as dashboard_route


def test_work_centers_filter_posts_back_to_work_centers(monkeypatch):
    # The Day/Category form must post to /work-centers itself. It used to
    # post to "/", which 307-redirects to /recycling and drops the query
    # string — silently losing the user's filter.
    monkeypatch.setattr(dashboard_route, "leaderboard", lambda *a, **k: [])
    client = TestClient(app)
    resp = client.get("/work-centers")
    assert resp.status_code == 200
    assert 'action="/work-centers"' in resp.text
    assert 'action="/"' not in resp.text
```

- [ ] **Step 2: Run it to verify it fails**

Run: `ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_base_app_template.py::test_work_centers_filter_posts_back_to_work_centers -v`
Expected: FAIL on `'action="/work-centers"' in resp.text`.

(If the assertion unexpectedly passes on a second run, check
`_http_cache.get_cached_response` isn't serving a stale copy from an earlier test
in the same process — run the test file alone.)

- [ ] **Step 3: Fix both templates**

In `src/zira_dashboard/templates/index.html` line 15, change:

```html
  <form method="get" action="/">
```

to:

```html
  <form method="get" action="/work-centers">
```

In `src/zira_dashboard/templates/auth_denied.html`, delete line 7 entirely:

```html
<link rel="stylesheet" href="/static/dashboard.css?v={{ static_v('dashboard.css') }}">
```

(The card already carries inline styles with `var(..., fallback)` defaults; the
linked file has never existed, so this is a no-op visually and removes a 404.)

- [ ] **Step 4: Remove the xfail marker**

In `tests/test_base_app_template.py`, delete the `@pytest.mark.xfail(...)` decorator
(and the now-unused `import pytest`) from `test_template_static_references_exist` —
with the dead link gone it must pass outright.

- [ ] **Step 5: Run the test file**

Run: `ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_base_app_template.py -v`
Expected: 3 passed.

- [ ] **Step 6: Commit**

```bash
git add src/zira_dashboard/templates/index.html src/zira_dashboard/templates/auth_denied.html tests/test_base_app_template.py
git commit -m "fix: work-centers filter posts to itself; drop dead auth_denied stylesheet"
```

---

### Task 3: Convert `index.html` (Work Centers) to `_base_app.html`

**Files:**
- Modify: `src/zira_dashboard/templates/index.html` (full rewrite of chrome; body verbatim)
- Modify: `tests/test_base_app_template.py` (shrink allowlist; add chrome test)

- [ ] **Step 1: Shrink the allowlist (the failing test)**

In `tests/test_base_app_template.py`, delete the line `"index.html",` from
`ALLOWED_STANDALONE`, and append this test:

```python
def _assert_single_chrome(html: str):
    lowered = html.lower()
    assert lowered.count("<!doctype") == 1
    assert html.count('class="brand-row"') == 1
    assert "changelog-modal" in html  # _footer.html present


def test_work_centers_extends_base_app(monkeypatch):
    monkeypatch.setattr(dashboard_route, "leaderboard", lambda *a, **k: [])
    client = TestClient(app)
    resp = client.get("/work-centers")
    assert resp.status_code == 200
    _assert_single_chrome(resp.text)
    assert "<title>Work Centers — GPI Plant Manager</title>" in resp.text
```

- [ ] **Step 2: Run to verify the ratchet fails**

Run: `ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_base_app_template.py -v`
Expected: `test_full_page_templates_extend_a_base` FAILS
("index.html is a standalone document…"). The chrome test itself passes already
(the old page also renders valid chrome) — the ratchet is the red gate.

- [ ] **Step 3: Convert the template**

Replace the chrome of `src/zira_dashboard/templates/index.html`. The body markup
moves **verbatim** (current lines 16–24 into `header_extra`, 29–92 into `content`);
only the wrapper changes. New file shape:

```jinja
{% extends "_base_app.html" %}
{% block title %}Work Centers{% endblock %}
{% block head %}
<link rel="stylesheet" href="/static/index.css?v={{ static_v('index.css') }}">
<link rel="stylesheet" href="/static/dashboards-subnav.css?v={{ static_v('dashboards-subnav.css') }}">
{% endblock %}
{% block topnav %}{% set active_nav = 'dashboards' %}{% include "_topnav.html" %}{% endblock %}
{% block header_extra %}
  <form method="get" action="/work-centers">
    <label for="day">Day</label>
    <input id="day" type="date" name="day" value="{{ day }}" max="{{ today }}">
    <label for="category">Category</label>
    <select id="category" name="category">
      {% for c in categories %}
        <option value="{{ c }}" {% if c == category %}selected{% endif %}>{{ c }}</option>
      {% endfor %}
    </select>
    <button type="submit">Update</button>
  </form>
{% endblock %}
{% block subnav %}{% include "_dashboards_subnav.html" %}{% endblock %}
{% block content %}
  ...current lines 29–92 verbatim (summary pills, category grids,
  leaderboard table, "Refreshed …" footer div) — do not re-type them,
  cut-and-paste from the existing file...
{% endblock %}
{% block scripts %}
{% if is_today %}
<script defer src="/static/tv-refresh.js?v={{ static_v('tv-refresh.js') }}"></script>
{% endif %}
{% endblock %}
```

Notes:
- The `topnav.css` link and favicon come from the base — do not repeat them.
- The old `<header>` had no class; the base renders `<header class="app">`.
  `index.css` selects bare `header`, which still matches.
- The footer include moves from after the script to the base's default position
  (before `body_end`) — harmless reorder of a `defer` script.

- [ ] **Step 4: Run the guards + the page's existing tests**

Run: `ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_base_app_template.py tests/test_dashboards_polish.py -v`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/zira_dashboard/templates/index.html tests/test_base_app_template.py
git commit -m "refactor: work-centers page extends _base_app"
```

---

### Task 4: Convert `exceptions.html` to `_base_app.html`

**Files:**
- Modify: `src/zira_dashboard/templates/exceptions.html`
- Modify: `tests/test_base_app_template.py`

- [ ] **Step 1: Shrink the allowlist + add the chrome test**

Delete `"exceptions.html",` from `ALLOWED_STANDALONE`. Append:

```python
def test_exceptions_extends_base_app(monkeypatch):
    from zira_dashboard.routes import exceptions as exceptions_route

    monkeypatch.setattr(
        exceptions_route.exception_inbox,
        "build_snapshot",
        lambda **k: {
            "today": "2026-07-21", "generated_at": "1:22 PM", "total": 0,
            "urgent_total": 0, "follow_up_total": 0, "source_errors": [],
            "work_centers": [], "people": [], "sections": [], "queue": [],
        },
    )
    client = TestClient(app)
    resp = client.get("/exceptions")
    assert resp.status_code == 200
    _assert_single_chrome(resp.text)
    assert '<main class="inbox-shell">' in resp.text
```

(Copy the `build_snapshot` monkeypatch shape from
`tests/test_exceptions_report_breakdown_button.py` if the signature has drifted.)

- [ ] **Step 2: Run to verify the ratchet fails**

Run: `ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_base_app_template.py -v`
Expected: ratchet FAILS on exceptions.html.

- [ ] **Step 3: Convert the template**

Current chrome: lines 1–15 (doctype/head/header) and the tail (footer include +
`exceptions.js` script + `</body></html>`). Everything between
`<main class="inbox-shell">` and `</main>` moves verbatim into `content`.

```jinja
{% extends "_base_app.html" %}
{% block title %}Exception Inbox{% endblock %}
{% block head %}
<link rel="stylesheet" href="/static/exceptions.css?v={{ static_v('exceptions.css') }}">
{% endblock %}
{% block topnav %}{% set active_nav = 'inbox' %}{% include "_topnav.html" %}{% endblock %}
{% block main_attrs %} class="inbox-shell"{% endblock %}
{% block content %}
  ...everything currently between <main class="inbox-shell"> and </main>, verbatim...
{% endblock %}
{% block scripts %}
<script src="/static/exceptions.js?v={{ static_v('exceptions.js') }}"></script>
{% endblock %}
```

The `{# Inbox-count bootstrap blob is emitted by _topnav.html #}` comment near the
old footer include can be deleted (the include now lives in the base).

- [ ] **Step 4: Run the guards + the inbox test files**

Run: `ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_base_app_template.py tests/test_exception_inbox.py tests/test_exceptions_report_breakdown_button.py tests/test_exception_inbox_breakdown_template.py -v`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/zira_dashboard/templates/exceptions.html tests/test_base_app_template.py
git commit -m "refactor: exception inbox extends _base_app"
```

---

### Task 5: Convert `settings.html` to `_base_app.html`

**Files:**
- Modify: `src/zira_dashboard/templates/settings.html`
- Modify: `tests/test_base_app_template.py`

- [ ] **Step 1: Shrink the allowlist + add the chrome test**

Delete `"settings.html",` from `ALLOWED_STANDALONE`. Append:

```python
def test_settings_extends_base_app():
    client = TestClient(app)
    resp = client.get("/settings?section=api")
    assert resp.status_code == 200
    _assert_single_chrome(resp.text)
    assert 'id="page-undo-btn"' in resp.text  # header_extra survived
```

(`/settings?section=api` is the section other tests fetch without extra mocks —
see `tests/test_settings_api_keys.py`. If it needs state, reuse that file's
fixtures.)

- [ ] **Step 2: Run to verify the ratchet fails**

Run: `ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_base_app_template.py -v`
Expected: ratchet FAILS on settings.html.

- [ ] **Step 3: Convert the template**

Chrome to replace: lines 1–19 (doctype/head/header + `<main>` open) and the tail
(`</main>`, `PROD_MIN` script, `settings.js` script, footer include,
`</body></html>`). The entire `<div class="settings-shell">…</div>` (lines 20 to
the tail) moves verbatim into `content`.

```jinja
{% extends "_base_app.html" %}
{% block title %}Settings{% endblock %}
{% block head %}
<link rel="stylesheet" href="/static/settings.css?v={{ static_v('settings.css') }}">
{% endblock %}
{% block topnav %}{% set active_nav = 'settings' %}{% include "_topnav.html" %}{% endblock %}
{% block header_extra %}
  <div class="page-actions">
    <button type="button" class="undo-btn" id="page-undo-btn" title="Undo last save" aria-label="Undo" disabled>↶</button>
    <button type="button" class="redo-btn" id="page-redo-btn" title="Redo" aria-label="Redo" disabled>↷</button>
  </div>
{% endblock %}
{% block content %}
<div class="settings-shell">
  ...current lines 21 onward, verbatim, through the closing </div> of settings-shell...
</div>
{% endblock %}
{% block scripts %}
<script>window.PROD_MIN = {{ productive_minutes }};</script>
<script src="/static/settings.js?v={{ static_v('settings.js') }}"></script>
{% endblock %}
```

Note: the old page had `<header>` without the `app` class; `settings.css` selects
bare `header`, which still matches `<header class="app">`.

- [ ] **Step 4: Run the guards + the settings test files**

Run: `ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_base_app_template.py tests/test_settings_api_keys.py tests/test_settings_auto_lunch.py tests/test_settings_saturday_schedule.py tests/test_settings_rounding_systems.py tests/test_page_usage_route.py -v`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/zira_dashboard/templates/settings.html tests/test_base_app_template.py
git commit -m "refactor: settings page extends _base_app"
```

---

### Task 6: Re-parent `_staffing_base.html` onto `_base_app.html`

**Files:**
- Modify: `src/zira_dashboard/templates/_staffing_base.html` (full rewrite)
- Its 12 child templates are NOT touched — their block names keep working.

- [ ] **Step 1: Rewrite `_staffing_base.html`**

Replace the whole file with (the `:root`/component styles and the details-toggle
script move **verbatim** from the current file — current lines 10–56 and 72–84):

```jinja
{% extends "_base_app.html" %}
{% from "_goat_badges.html" import hover_tip_clamp_script %}
{% block title %}Staffing{% endblock %}
{% block head %}
<style>
  ...current lines 11–55 verbatim: the :root palette, box-sizing, body,
  header.app, .sub-nav, main, h2, .hint, .panel rules...
  {% block styles %}{% endblock %}
</style>
{% block extra_head %}{% endblock %}
{% endblock %}
{% block topnav %}{% set active_nav = 'trophies' if active in ['trophies', 'leaderboards'] else 'staffing' %}{% include "_topnav.html" %}{% endblock %}
{% block subnav %}
{% if active in ['trophies', 'leaderboards'] %}
{% with active=active %}{% include '_trophies_subnav.html' %}{% endwith %}
{% else %}
{% with active=active %}{% include '_staffing_subnav.html' %}{% endwith %}
{% endif %}
{% endblock %}
{% block body_end %}
<script>
// Only one <details> open at a time across the page. Skips ancestors of the
// just-opened element so opening a nested group doesn't collapse its parent.
document.addEventListener('toggle', (e) => {
  const t = e.target;
  if (!(t instanceof HTMLDetailsElement) || !t.open) return;
  document.querySelectorAll('details[open]').forEach(d => {
    if (d === t || d.contains(t)) return;
    d.open = false;
  });
}, true);
</script>
{{ hover_tip_clamp_script() }}
{% block scripts %}{% endblock %}
{% endblock %}
```

Why this preserves the 12 children unchanged:
- Children override `title` → the base renders `{child title} — GPI Plant Manager`,
  exactly what the old hardcoded suffix produced.
- Children's `styles` and `extra_head` blocks are re-declared here inside `head`.
- Children's `scripts` block is re-declared inside `body_end`, after the shared
  scripts — same order as before.
- `main`/`content` and the footer come from `_base_app.html` (the old file included
  `_footer.html` itself; the base does now).
- The `header.app` styling this file carries inline continues to apply — the base
  emits `<header class="app">`.

- [ ] **Step 2: Run every child page's test files + the guards**

Run: `ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_base_app_template.py tests/test_page_usage_route.py tests/test_admin_devices_template.py tests/test_leaderboards_static.py -v`
Expected: all pass. (The ratchet skips `_staffing_base.html` — underscore prefix.)

- [ ] **Step 3: Run the full suite**

Run: `ZIRA_API_KEY=test .venv/bin/python -m pytest -q`
Expected: no new failures vs the pre-task baseline (record the baseline count in
the first run of this plan; 2026-07-11 reference was 1,616 passed / 301 skipped).

- [ ] **Step 4: Commit**

```bash
git add src/zira_dashboard/templates/_staffing_base.html
git commit -m "refactor: _staffing_base extends _base_app"
```

---

### Task 7: Wave 1 wrap-up verification

**Files:** none (verification only)

- [ ] **Step 1: Full suite**

Run: `ZIRA_API_KEY=test .venv/bin/python -m pytest -q`
Expected: same pass/skip counts as Task 6's run.

- [ ] **Step 2: Best-effort visual check**

If a local server can run (embedded pgserver + `ZIRA_API_KEY=test`), preview
`/work-centers`, `/exceptions`, and `/settings?section=work_centers` and compare
chrome (topnav, subnav, footer, header controls) against production. If the local
environment can't reach Odoo/Zira, note it and rely on the render tests — these
three pages are auth-gated desktop pages, per project precedent (rotations UI).

- [ ] **Step 3: Report**

Summarize to Dale: conversions done, test counts, any visual diffs, and the
reminder that unpushed forklift commits ride `main` (Dale pushes).
