# TV Dashboards — VS Mode (Sub-Project 1) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Strip-chrome TV variants of `/recycling` and `/new-vs` at `/tv/recycling` and `/tv/new-vs` — no top nav, no range chips, no widget edit buttons, larger fonts, dashboard title top-left, per-display light/dark theme via URL param.

**Architecture:** Same handler logic + same template as the screen versions; new thin route wrappers force `tv_mode=True`, `window="today"`, and a `tv_theme` ("dark" default, "light" via `?theme=light`). Templates set `data-tv-theme` on `<html>` only when `tv_mode`. A single new `static/tv-mode.css` (loaded only on TV routes) hides chrome via attribute selectors, scales typography, and swaps the CSS variable palette for `[data-tv-theme="dark"]` / `[data-tv-theme="light"]`. Gridstack drag is disabled via a single `grid.disable()` call inside an `{% if tv_mode %}` script block.

**Tech Stack:** Python 3.11+, FastAPI, Jinja2, gridstack, pytest. No new dependencies, no DB changes, no migrations.

**Spec:** `docs/superpowers/specs/2026-05-13-tv-dashboards-design.md` — sub-project 1 of 4.

---

## File Structure

**New files:**
- `src/zira_dashboard/static/tv-mode.css` — chrome-hide selectors + dark/light palette + larger typography
- `src/zira_dashboard/templates/_tv_header.html` — Jinja macro for the top-left dashboard title (and optional right slot for per-WC operator names in sub-project 2)
- `tests/test_tv_dashboards_vs.py` — integration tests for `/tv/recycling` and `/tv/new-vs`

**Modified files:**
- `src/zira_dashboard/routes/value_streams.py` — extract data-prep into shared helpers, add `/tv/recycling` and `/tv/new-vs` thin route wrappers
- `src/zira_dashboard/templates/recycling.html` — conditional `data-tv-theme` + tv-mode.css link + `_tv_header.html` include + `grid.disable()` when tv_mode
- `src/zira_dashboard/templates/new_vs.html` — same conditional plumbing
- `CHANGELOG.md` — one deploy entry

**Responsibility split:** `tv-mode.css` owns every visual transformation (hide + resize + recolor). The `_tv_header.html` macro owns the new top-left/top-right header layout — reusable in sub-project 2 for per-WC dashboards. Route wrappers in `value_streams.py` are pure thin shells (parse query → call existing handler with flag overrides → return). Templates only carry conditional plumbing for the wrappers; visual rules stay in CSS.

---

## Conventions

- Tests use the existing `test_dashboards_polish.py` pattern: `TestClient(app)` with `monkeypatch.setattr` to stub external calls. Module-level `pytestmark = skipif(not DATABASE_URL)` because the value-stream render path transitively hits the schedule store.
- Python interpreter on Dale's Windows box: `.venv/Scripts/python.exe`. Always use that, not plain `python`.
- Commit messages follow repo convention: `feat(tv-mode):` / `test(tv-mode):` / `docs:`.

---

## Task 1: `tv-mode.css` — chrome-hide + theme palette + typography

**Files:**
- Create: `src/zira_dashboard/static/tv-mode.css`

- [ ] **Step 1: Create the stylesheet**

Create `src/zira_dashboard/static/tv-mode.css`:

```css
/* TV-mode overrides. Loaded only on /tv/* routes via a conditional
 * <link> in the page templates. The presence of `data-tv-theme` on
 * <html> is the "we are in TV mode" indicator.
 *
 * Two jobs:
 *   1. Hide every interactive / navigational chrome thing.
 *   2. Swap the CSS variable palette between dark (default) and light
 *      based on data-tv-theme. The screen-mode pages don't set the
 *      attribute and are unaffected.
 */

/* ---- Palette: dark (default for TVs) ---- */
html[data-tv-theme="dark"] {
  --bg: #0b1220;
  --panel: #111827;
  --panel-2: #1e293b;
  --panel-3: #334155;
  --border: #334155;
  --fg: #e2e8f0;
  --muted: #94a3b8;
  --accent: #22c55e;
  --accent-dim: #14532d;
  --warn: #f59e0b;
  --warn-dim: #78350f;
  --bad: #ef4444;
  --bad-dim: #7f1d1d;
  color-scheme: dark;
}

/* ---- Palette: light (explicit override) ---- */
html[data-tv-theme="light"] {
  /* Inherit the existing screen-mode defaults — no overrides needed. */
  color-scheme: light;
}

/* ---- Chrome-hide: every TV theme ---- */
html[data-tv-theme] header,
html[data-tv-theme] .sub-nav,
html[data-tv-theme] .rc-toolbar,
html[data-tv-theme] .save-block,
html[data-tv-theme] .widget-edit-btn,
html[data-tv-theme] .widget-edit,
html[data-tv-theme] .no-assign-btn,
html[data-tv-theme] form[action="/new-vs"],
html[data-tv-theme] .empty-state a {
  display: none !important;
}

/* ---- Bigger everything for plant-floor viewing ---- */
html[data-tv-theme] body {
  font-size: 18px;
  line-height: 1.4;
}
html[data-tv-theme] .kpi .label { font-size: 0.85rem; }
html[data-tv-theme] .kpi .val { font-size: 3.4rem; font-weight: 800; }
html[data-tv-theme] .bar-row .name-primary,
html[data-tv-theme] .vbar-name .name-primary { font-size: 1.4rem; font-weight: 700; }
html[data-tv-theme] .widget-total { font-size: 1.6rem; font-weight: 800; }
html[data-tv-theme] table.sched,
html[data-tv-theme] .panel h3 { font-size: 1.25rem; }

/* ---- TV header layout ---- */
html[data-tv-theme] .tv-header {
  display: grid;
  grid-template-columns: 1fr auto;
  align-items: end;
  gap: 1rem;
  padding: 18px 28px 12px;
  border-bottom: 2px solid var(--border);
  margin-bottom: 10px;
}
html[data-tv-theme] .tv-header .crumb {
  font-size: 12px;
  letter-spacing: 2px;
  opacity: 0.55;
  font-weight: 600;
  text-transform: uppercase;
}
html[data-tv-theme] .tv-header .name {
  font-size: 38px;
  font-weight: 900;
  line-height: 1;
  letter-spacing: -0.5px;
}
html[data-tv-theme] .tv-header .right { text-align: right; }
```

- [ ] **Step 2: Sanity check the file is valid CSS**

Run: `.venv/Scripts/python.exe -c "import pathlib; print(pathlib.Path('src/zira_dashboard/static/tv-mode.css').read_text().count('{') == pathlib.Path('src/zira_dashboard/static/tv-mode.css').read_text().count('}'))"`
Expected: `True` (braces balance).

- [ ] **Step 3: Commit**

```bash
git add src/zira_dashboard/static/tv-mode.css
git commit -m "feat(tv-mode): static/tv-mode.css — chrome-hide + dark/light palette"
```

---

## Task 2: `_tv_header.html` — top-left + top-right header macro

**Files:**
- Create: `src/zira_dashboard/templates/_tv_header.html`

- [ ] **Step 1: Create the macro file**

Create `src/zira_dashboard/templates/_tv_header.html`:

```jinja
{# TV-mode header. Top-left: dashboard title + optional crumb above.
   Top-right: optional secondary label (used in sub-project 2 to show
   the operator's name on a per-WC dashboard; unused for VS variants).

   Usage:
     {% from "_tv_header.html" import tv_header %}
     ...
     {{ tv_header("Recycling VS", crumb="VALUE STREAMS") }}
     {{ tv_header("REPAIR 1", crumb="RECYCLING · REPAIRS", right="CHRISTIAN · JOSE L") }}
#}

{% macro tv_header(name, crumb=None, right=None) -%}
<div class="tv-header">
  <div>
    {%- if crumb %}<div class="crumb">{{ crumb }}</div>{% endif %}
    <div class="name">{{ name }}</div>
  </div>
  {%- if right %}
  <div class="right">
    <div class="crumb">OPERATORS</div>
    <div class="name">{{ right }}</div>
  </div>
  {%- endif %}
</div>
{%- endmacro %}
```

- [ ] **Step 2: Verify the macro parses**

Run:
```bash
.venv/Scripts/python.exe -c "
from jinja2 import Environment, FileSystemLoader
env = Environment(loader=FileSystemLoader('src/zira_dashboard/templates'), autoescape=True)
tmpl = env.from_string('{% from \"_tv_header.html\" import tv_header %}{{ tv_header(\"Hi\", crumb=\"X\") }}')
out = tmpl.render()
assert 'class=\"name\">Hi<' in out
assert 'class=\"crumb\">X<' in out
print('OK')
"
```
Expected: `OK`.

- [ ] **Step 3: Commit**

```bash
git add src/zira_dashboard/templates/_tv_header.html
git commit -m "feat(tv-mode): _tv_header.html macro (title left, optional right slot)"
```

---

## Task 3: `/tv/recycling` route + recycling.html plumbing

**Files:**
- Modify: `src/zira_dashboard/routes/value_streams.py` — add new route
- Modify: `src/zira_dashboard/templates/recycling.html` — conditional TV plumbing

- [ ] **Step 1: Find the current `recycling` handler signature**

In `src/zira_dashboard/routes/value_streams.py`, locate the existing handler:

```python
@router.get("/recycling", response_class=HTMLResponse)
def recycling(
    request: Request,
    window: str = Query(default="today"),
    start: str | None = Query(default=None),
    end: str | None = Query(default=None),
):
```

It ends around line 540-543 with `return response`. Read enough context to know how the function is structured. **Do not modify it.** The TV route is added BELOW it.

- [ ] **Step 2: Add the `/tv/recycling` route below the existing `recycling` handler**

Add immediately after the `recycling()` function's closing `return response`:

```python
@router.get("/tv/recycling", response_class=HTMLResponse)
def tv_recycling(request: Request, theme: str | None = Query(default=None)):
    """Read-only TV variant of /recycling. No top nav, no range chips,
    no widget edit buttons, larger fonts. Always shows today.

    Theme: 'dark' (default) or 'light' via ?theme=light. Persisted-config
    theme arrives in sub-project 4 via tv_displays; for now URL-only.
    """
    tv_theme = "light" if theme == "light" else "dark"
    # Delegate to the existing handler with window forced to "today".
    # Then mutate the rendered response context by re-rendering through
    # the template with tv_mode + tv_theme added — but the simpler path
    # is to share the data-prep helper. The existing recycling() function
    # is monolithic; rather than refactor it for one new caller, we call
    # it and patch the response: dangerous if it streams. Instead, just
    # call the underlying template with the prepared context.
    #
    # Practical: re-invoke recycling() directly and pass tv_mode via a
    # context-injection wrapper. To stay surgical, we re-render the
    # template with the same context dict the screen handler builds.
    # Easiest is to call the existing recycling() function and trust its
    # body — since FastAPI handlers are just functions.
    resp = recycling(request, window="today", start=None, end=None)
    # The screen handler returns a TemplateResponse with the full context.
    # Patch its context to add tv_mode + tv_theme, then re-render with
    # the same template.
    ctx = dict(resp.context or {})
    ctx["tv_mode"] = True
    ctx["tv_theme"] = tv_theme
    return templates.TemplateResponse(request, "recycling.html", ctx)
```

- [ ] **Step 3: Modify `recycling.html` — add TV-mode plumbing to `<html>` + `<head>`**

Open `src/zira_dashboard/templates/recycling.html`. Find the top:

```jinja
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="icon" type="image/png" href="/static/gpi-logo.png">
<title>Value Streams — Recycling — GPI Plant Manager</title>
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/gridstack@10.3.1/dist/gridstack.min.css">
<link rel="stylesheet" href="/static/recycling.css?v={{ static_v('recycling.css') }}">
```

Replace with:

```jinja
<!doctype html>
<html lang="en"{% if tv_mode %} data-tv-theme="{{ tv_theme or 'dark' }}"{% endif %}>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="icon" type="image/png" href="/static/gpi-logo.png">
<title>{% if tv_mode %}TV · {% endif %}Value Streams — Recycling — GPI Plant Manager</title>
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/gridstack@10.3.1/dist/gridstack.min.css">
<link rel="stylesheet" href="/static/recycling.css?v={{ static_v('recycling.css') }}">
{% if tv_mode %}
<link rel="stylesheet" href="/static/tv-mode.css?v={{ static_v('tv-mode.css') }}">
<meta http-equiv="refresh" content="60">
{% endif %}
```

Then find the line just before the existing `<body>` opening or just inside `<body>`. Find:

```jinja
<body>
<header>
```

Insert just after `<body>`:

```jinja
<body>
{% if tv_mode %}
{% from "_tv_header.html" import tv_header %}
{{ tv_header("Recycling VS", crumb="VALUE STREAMS · TODAY") }}
{% endif %}
<header>
```

- [ ] **Step 4: Disable gridstack drag when in TV mode**

Still in `recycling.html`, find lines 419-425 — the `const grid = GridStack.init({...})` block ending with `});`. Immediately after the closing `});`, add the conditional disable:

```jinja
  const grid = GridStack.init({
    column: 12,
    cellHeight: 60,
    margin: 8,
    float: false,
    handle: '.grid-stack-item-content > h3, .grid-stack-item-content > .label',
  });
{% if tv_mode %}
  grid.disable();  // TV mode — no drag, no resize, layout is locked.
{% endif %}
```

- [ ] **Step 5: Smoke-test locally**

Run:
```bash
.venv/Scripts/python.exe -c "from zira_dashboard import app; print('OK')"
```
Expected: `OK`.

- [ ] **Step 6: Commit**

```bash
git add src/zira_dashboard/routes/value_streams.py src/zira_dashboard/templates/recycling.html
git commit -m "feat(tv-mode): /tv/recycling route + recycling.html plumbing"
```

---

## Task 4: `/tv/new-vs` route + new_vs.html plumbing

**Files:**
- Modify: `src/zira_dashboard/routes/value_streams.py` — add another route
- Modify: `src/zira_dashboard/templates/new_vs.html` — same conditional plumbing

- [ ] **Step 1: Add the `/tv/new-vs` route**

In `src/zira_dashboard/routes/value_streams.py`, immediately after the existing `new_vs(...)` handler's closing return, add:

```python
@router.get("/tv/new-vs", response_class=HTMLResponse)
def tv_new_vs(request: Request, theme: str | None = Query(default=None)):
    """Read-only TV variant of /new-vs. See tv_recycling for theme rules."""
    tv_theme = "light" if theme == "light" else "dark"
    resp = new_vs(request, day=None)
    ctx = dict(resp.context or {})
    ctx["tv_mode"] = True
    ctx["tv_theme"] = tv_theme
    return templates.TemplateResponse(request, "new_vs.html", ctx)
```

- [ ] **Step 2: Modify `new_vs.html` — same plumbing pattern**

Open `src/zira_dashboard/templates/new_vs.html`. Find the top:

```jinja
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="icon" type="image/png" href="/static/gpi-logo.png">
<title>Value Streams — New — GPI Plant Manager</title>
<link rel="stylesheet" href="/static/new_vs.css?v={{ static_v('new_vs.css') }}">
```

Replace with:

```jinja
<!doctype html>
<html lang="en"{% if tv_mode %} data-tv-theme="{{ tv_theme or 'dark' }}"{% endif %}>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="icon" type="image/png" href="/static/gpi-logo.png">
<title>{% if tv_mode %}TV · {% endif %}Value Streams — New — GPI Plant Manager</title>
<link rel="stylesheet" href="/static/new_vs.css?v={{ static_v('new_vs.css') }}">
{% if tv_mode %}
<link rel="stylesheet" href="/static/tv-mode.css?v={{ static_v('tv-mode.css') }}">
<meta http-equiv="refresh" content="60">
{% endif %}
```

`new_vs.html` does not use gridstack (no draggable widgets) so the `grid.disable()` step from Task 3 doesn't apply here.

Then find the line just inside `<body>` (the `<header>` opening tag). Insert just after `<body>`:

```jinja
<body>
{% if tv_mode %}
{% from "_tv_header.html" import tv_header %}
{{ tv_header("New VS", crumb="VALUE STREAMS · TODAY") }}
{% endif %}
<header>
```

- [ ] **Step 3: Smoke-test locally**

Run:
```bash
.venv/Scripts/python.exe -c "from zira_dashboard import app; print('OK')"
```
Expected: `OK`.

- [ ] **Step 4: Commit**

```bash
git add src/zira_dashboard/routes/value_streams.py src/zira_dashboard/templates/new_vs.html
git commit -m "feat(tv-mode): /tv/new-vs route + new_vs.html plumbing"
```

---

## Task 5: Integration tests for `/tv/recycling` and `/tv/new-vs`

**Files:**
- Create: `tests/test_tv_dashboards_vs.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_tv_dashboards_vs.py`:

```python
"""Integration tests for the TV variants of the value-stream dashboards.

Mirrors the test_dashboards_polish.py pattern: TestClient + monkeypatch
of the data-source helpers so the test doesn't need live Zira / Odoo.
"""
from __future__ import annotations

import os
from datetime import date, datetime, timezone
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from zira_dashboard import staffing
from zira_dashboard.app import app

# The render path transitively hits the work_centers / schedule store DB
# lookups despite the monkeypatches, so we gate on DATABASE_URL the same
# way test_dashboards_polish.py does.
pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set; TV dashboard tests need Postgres",
)


def _stub_data(monkeypatch):
    """Stub the heavy external calls so the route renders quickly."""
    monkeypatch.setattr(staffing, "load_schedule", lambda d: staffing.Schedule(
        day=d, published=True, assignments={"Repair-1": ["Alice"]},
    ))


def test_tv_recycling_renders_with_default_dark_theme(monkeypatch):
    _stub_data(monkeypatch)
    with patch("zira_dashboard.routes.value_streams.leaderboard", return_value=[]), \
         patch("zira_dashboard.routes.value_streams.shift_elapsed_minutes", return_value=60):
        c = TestClient(app)
        r = c.get("/tv/recycling")
    assert r.status_code == 200
    assert 'data-tv-theme="dark"' in r.text
    # Chrome hidden via CSS — but the TV stylesheet must be linked.
    assert "/static/tv-mode.css" in r.text
    # TV header rendered with the dashboard title.
    assert 'class="tv-header"' in r.text
    assert "Recycling VS" in r.text
    # Auto-refresh meta in place.
    assert 'http-equiv="refresh"' in r.text


def test_tv_recycling_supports_light_theme_via_query(monkeypatch):
    _stub_data(monkeypatch)
    with patch("zira_dashboard.routes.value_streams.leaderboard", return_value=[]), \
         patch("zira_dashboard.routes.value_streams.shift_elapsed_minutes", return_value=60):
        c = TestClient(app)
        r = c.get("/tv/recycling?theme=light")
    assert r.status_code == 200
    assert 'data-tv-theme="light"' in r.text


def test_tv_new_vs_renders_with_default_dark_theme(monkeypatch):
    _stub_data(monkeypatch)
    with patch("zira_dashboard.routes.value_streams.leaderboard", return_value=[]), \
         patch("zira_dashboard.routes.value_streams.shift_elapsed_minutes", return_value=60):
        c = TestClient(app)
        r = c.get("/tv/new-vs")
    assert r.status_code == 200
    assert 'data-tv-theme="dark"' in r.text
    assert "/static/tv-mode.css" in r.text
    assert "New VS" in r.text


def test_screen_recycling_unaffected_by_tv_changes(monkeypatch):
    """Regression guard: the screen /recycling route must NOT carry the
    TV attributes after the plumbing changes."""
    _stub_data(monkeypatch)
    with patch("zira_dashboard.routes.value_streams.leaderboard", return_value=[]), \
         patch("zira_dashboard.routes.value_streams.shift_elapsed_minutes", return_value=60):
        c = TestClient(app)
        r = c.get("/recycling")
    assert r.status_code == 200
    assert "data-tv-theme" not in r.text, "screen page must not set data-tv-theme"
    assert "/static/tv-mode.css" not in r.text, "screen page must not link tv-mode.css"
    assert 'class="tv-header"' not in r.text, "screen page must not render TV header"
```

- [ ] **Step 2: Run the tests**

Run: `.venv/Scripts/python.exe -m pytest tests/test_tv_dashboards_vs.py -v`
Expected without `DATABASE_URL`: all 4 skipped cleanly. With `DATABASE_URL`: all 4 pass.

- [ ] **Step 3: Run the full suite to confirm no regressions**

Run: `.venv/Scripts/python.exe -m pytest`
Expected: same pass count as before + 4 new tests (passing or skipping).

- [ ] **Step 4: Commit**

```bash
git add tests/test_tv_dashboards_vs.py
git commit -m "test(tv-mode): integration tests for /tv/recycling + /tv/new-vs"
```

---

## Task 6: CHANGELOG + push

**Files:**
- Modify: `CHANGELOG.md`

- [ ] **Step 1: Get the current time**

Run: `powershell.exe -Command "Get-Date -Format 'h:mm tt'"`
Use that as the section header.

- [ ] **Step 2: Add CHANGELOG entry**

In `CHANGELOG.md`, find the top section (`## 2026-05-13`). Insert a new `### <HH:MM TT>` block at the top of that day's entries:

```markdown
### <HH:MM TT>

- **TV mode for the Recycling + New value-stream dashboards** — two new permanent URLs designed to live on a TV browser: `/tv/recycling` and `/tv/new-vs`. No top nav, no range chips, no per-widget edit buttons, no sub-nav — just the data with bigger fonts. Dashboard title sits top-left ("Recycling VS" / "New VS") so anyone walking by knows what's on screen. Page auto-refreshes every 60 s. Dark theme by default; pass `?theme=light` for a bright-area TV. Gridstack drag is disabled in TV mode so a stray touch can't reshuffle the widgets. The screen versions (`/recycling`, `/new-vs`) are byte-identical to before — TV mode is gated entirely on a `tv_mode` context flag the new routes set. First of four sub-projects in the TV-dashboards spec.
```

- [ ] **Step 3: Commit + push**

```bash
git add CHANGELOG.md
git commit -m "docs(changelog): TV mode for Recycling + New VS dashboards"
git push origin main
```

Railway picks up the push and redeploys. Once it's live, bookmark `https://gpiplantmanager.com/tv/recycling` on the TV browser.

---

## Done

`/tv/recycling` and `/tv/new-vs` ship with chrome stripped, bigger fonts, and a per-display theme via URL. The 6-task scope deliberately doesn't touch DB state — theme persistence and the Settings panel arrive in sub-projects 3 + 4. The per-WC dashboard (the big one) is sub-project 2 in this spec.

If a future surface wants the same TV-mode treatment, the pattern is now:
- Add a thin `/tv/<route>` wrapper that calls the existing handler and patches the context with `tv_mode=True` + `tv_theme=<resolved>`
- In the template, set `data-tv-theme` on `<html>`, link `/static/tv-mode.css`, render `_tv_header.html`, add a refresh meta tag
- Disable any gridstack with `grid.disable()` under `{% if tv_mode %}`
