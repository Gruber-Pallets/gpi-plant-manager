# Recycling Leaderboard TV Header Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move the Recycling leaderboard date ranges beside the TV title and add one large decorative goat icon beside the two Current GOAT tiles.

**Architecture:** Add optional `title_meta` and `right_icon` inputs to the shared Jinja TV-header macro, then opt into both only from the Recycling leaderboard TV template. Keep desktop markup unchanged with an `is_tv` conditional, and use scoped CSS hooks for responsive positioning without affecting the New leaderboard or other header callers.

**Tech Stack:** Python 3, FastAPI TestClient, Jinja2 templates, CSS, pytest

## Global Constraints

- Apply the layout only to `/tv/recycling-leaderboard`.
- Leave the desktop Recycling leaderboard and both New leaderboard modes unchanged.
- Render the range at approximately `0.7` opacity immediately to the right of the title.
- Render exactly one decorative `🐐` icon to the left of the two Current GOAT tiles and hide it from assistive technology.
- Preserve the existing GOAT tile data, copy, colors, and ordering.

---

### Task 1: Add the TV-header metadata and icon hooks

**Files:**
- Modify: `tests/test_recycling_leaderboard_tv.py`
- Modify: `src/zira_dashboard/templates/_tv_header.html`
- Modify: `src/zira_dashboard/templates/recycling_leaderboard_tv.html`

**Interfaces:**
- Consumes: `tv_header(name, crumb=None, right=None, right_label="OPERATORS", right_items=None, right_class=None)` and the existing leaderboard date fields.
- Produces: optional `title_meta` and `right_icon` macro arguments; `.tv-header-title-line`, `.tv-header-title-meta`, `.tv-header-right-has-icon`, `.tv-header-right-icon`, and `.tv-header-right-content` hooks.

- [ ] **Step 1: Write failing route-render tests**

Add these tests to `tests/test_recycling_leaderboard_tv.py`:

```python
def test_tv_recycling_range_moves_into_header_and_goat_group_has_one_icon(monkeypatch):
    monkeypatch.setattr(
        "zira_dashboard.routes.recycling_leaderboard._leaderboard_payload",
        lambda today: _fake_recycling_leaderboard_data(),
    )

    response = TestClient(app).get("/tv/recycling-leaderboard")

    assert response.status_code == 200
    assert response.text.count('class="tv-header-title-meta"') == 1
    assert "YTD: Jan 1-Jul 9" in response.text
    assert "L30: Jun 10-Jul 9" in response.text
    assert 'class="rlb-range"' not in response.text
    assert response.text.count('class="tv-header-right-icon"') == 1
    assert '<span class="tv-header-right-icon" aria-hidden="true">🐐</span>' in response.text


def test_desktop_recycling_range_and_header_stay_unchanged(monkeypatch):
    monkeypatch.setattr(
        "zira_dashboard.routes.recycling_leaderboard._leaderboard_payload",
        lambda today: _fake_recycling_leaderboard_data(),
    )

    response = TestClient(app).get("/recycling-leaderboard")

    assert response.status_code == 200
    assert 'class="rlb-range"' in response.text
    assert 'class="tv-header-title-meta"' not in response.text
    assert 'class="tv-header-right-icon"' not in response.text
```

- [ ] **Step 2: Run the focused tests and verify RED**

Run:

```bash
pytest -q \
  tests/test_recycling_leaderboard_tv.py::test_tv_recycling_range_moves_into_header_and_goat_group_has_one_icon \
  tests/test_recycling_leaderboard_tv.py::test_desktop_recycling_range_and_header_stay_unchanged
```

Expected: the TV test fails because `.tv-header-title-meta` and `.tv-header-right-icon` are absent; the desktop test passes.

- [ ] **Step 3: Extend the shared header macro**

Change the macro signature in `src/zira_dashboard/templates/_tv_header.html` to:

```jinja2
{% macro tv_header(name, crumb=None, right=None, right_label="OPERATORS", right_items=None, right_class=None, title_meta=None, right_icon=None) -%}
```

Replace the existing title `<div class="name">` with this conditional block:

```jinja2
    {%- if title_meta %}
    <div class="tv-header-title-line">
      <div class="name">{{ name }}</div>
      <div class="tv-header-title-meta">{{ title_meta }}</div>
    </div>
    {%- else %}
    <div class="name">{{ name }}</div>
    {%- endif %}
```

Append the icon-state class to the right container, emit the optional decorative icon, and wrap the existing right-side label/list content:

```jinja2
  <div class="right{% if right_items %} tv-header-right-items{% endif %}{% if right_class %} {{ right_class }}{% endif %}{% if right_icon %} tv-header-right-has-icon{% endif %}">
    {%- if right_icon %}
    <span class="tv-header-right-icon" aria-hidden="true">{{ right_icon }}</span>
    <div class="tv-header-right-content">
    {%- endif %}
    <div class="crumb">{{ right_label }}</div>
    {%- if right_items %}
    <div class="tv-header-right-list">
      {%- for item in right_items %}
      <div class="tv-header-right-chip">
        <span class="tv-header-right-role">{{ item.label }}</span>
        <span class="tv-header-right-name">{{ item.name or "No GOAT yet" }}</span>
        {%- if item.units is defined and item.units is not none %}
        <span class="tv-header-right-meta">{{ "%.0f"|format(item.units) }} pallets</span>
        {%- endif %}
      </div>
      {%- endfor %}
    </div>
    {%- else %}
    <div class="name">{{ right }}</div>
    {%- endif %}
    {%- if right_icon %}
    </div>
    {%- endif %}
  </div>
```

- [ ] **Step 4: Opt the Recycling TV view into the new hooks**

In the `is_tv` branch of `src/zira_dashboard/templates/recycling_leaderboard_tv.html`, capture the range markup and pass both new arguments:

```jinja2
  {% set range_meta -%}
    <span>YTD: {{ data.ytd_start.strftime('%b %-d') }}-{{ data.ytd_end.strftime('%b %-d') }}</span>
    <span>L30: {{ data.l30_start.strftime('%b %-d') }}-{{ data.l30_end.strftime('%b %-d') }}</span>
  {%- endset %}
  {{ tv_header(
      "Recycling-leaderboard",
      crumb="RECYCLING",
      right_label="CURRENT GOATS",
      right_items=data.current_goats | default([]),
      right_class="rlb-goat-banner",
      title_meta=range_meta,
      right_icon="🐐",
  ) }}
```

Guard the existing below-header range so it remains desktop-only:

```jinja2
    {% if not is_tv %}
    <div class="rlb-range">
      <span>YTD: {{ data.ytd_start.strftime('%b %-d') }}-{{ data.ytd_end.strftime('%b %-d') }}</span>
      <span>L30: {{ data.l30_start.strftime('%b %-d') }}-{{ data.l30_end.strftime('%b %-d') }}</span>
    </div>
    {% endif %}
```

- [ ] **Step 5: Run the focused tests and verify GREEN**

Run the Step 2 command again.

Expected: both tests pass.

- [ ] **Step 6: Commit the markup behavior**

```bash
git add tests/test_recycling_leaderboard_tv.py \
  src/zira_dashboard/templates/_tv_header.html \
  src/zira_dashboard/templates/recycling_leaderboard_tv.html
git commit -m "feat: group recycling leaderboard tv header"
```

### Task 2: Style the range and unified GOAT section

**Files:**
- Modify: `tests/test_recycling_leaderboard_static.py`
- Modify: `src/zira_dashboard/static/recycling_leaderboard.css`

**Interfaces:**
- Consumes: the `.tv-header-title-line`, `.tv-header-title-meta`, `.tv-header-right-has-icon`, `.tv-header-right-icon`, and `.tv-header-right-content` hooks from Task 1.
- Produces: scoped responsive header layout for `body.recycling-leaderboard-tv:not(.new-leaderboard-tv)`.

- [ ] **Step 1: Write a failing static-style test**

Add this test to `tests/test_recycling_leaderboard_static.py`:

```python
def test_tv_range_and_goat_group_have_scoped_responsive_styles():
    title_start = CSS.index("html[data-tv-theme] .tv-header-title-line")
    title_end = CSS.index(
        "html[data-tv-theme] .tv-header-title-meta", title_start
    )
    title_block = CSS[title_start:title_end]
    assert "display: flex" in title_block
    assert "align-items: baseline" in title_block

    meta_start = title_end
    meta_end = CSS.index(
        "html[data-tv-theme] .tv-header .right.rlb-goat-banner", meta_start
    )
    meta_block = CSS[meta_start:meta_end]
    assert "opacity: 0.7" in meta_block
    assert "white-space: nowrap" in meta_block
    assert "font-size: clamp(" in meta_block

    icon_selector = (
        "html[data-tv-theme] .tv-header .right.rlb-goat-banner"
        ".tv-header-right-has-icon"
    )
    icon_layout_start = CSS.index(icon_selector)
    icon_layout_end = CSS.index("}", icon_layout_start)
    icon_layout = CSS[icon_layout_start:icon_layout_end]
    assert "grid-template-columns: auto minmax(0, 1fr)" in icon_layout
    assert "align-items: center" in icon_layout

    icon_start = CSS.index("html[data-tv-theme] .rlb-goat-banner .tv-header-right-icon")
    icon_end = CSS.index("}", icon_start)
    assert "font-size: clamp(" in CSS[icon_start:icon_end]

    assert "body.recycling-leaderboard-tv:not(.new-leaderboard-tv)" in CSS
```

- [ ] **Step 2: Run the static test and verify RED**

Run:

```bash
pytest -q tests/test_recycling_leaderboard_static.py::test_tv_range_and_goat_group_have_scoped_responsive_styles
```

Expected: FAIL because the new selectors do not exist.

- [ ] **Step 3: Add the scoped responsive CSS**

Add the following near the existing `.rlb-range` and GOAT-banner rules in `src/zira_dashboard/static/recycling_leaderboard.css`:

```css
html[data-tv-theme] body.recycling-leaderboard-tv:not(.new-leaderboard-tv) {
  display: flex;
  flex-direction: column;
}
html[data-tv-theme] body.recycling-leaderboard-tv:not(.new-leaderboard-tv) .tv-header {
  flex: 0 0 auto;
}
html[data-tv-theme] body.recycling-leaderboard-tv:not(.new-leaderboard-tv) .rlb-main {
  flex: 1 1 auto;
  min-height: 0;
}
html[data-tv-theme] body.recycling-leaderboard-tv:not(.new-leaderboard-tv) .rlb-grid {
  height: 100%;
}
html[data-tv-theme] .tv-header-title-line {
  display: flex;
  align-items: baseline;
  gap: clamp(0.45rem, 0.8vw, 1rem);
  min-width: 0;
}
html[data-tv-theme] .tv-header-title-meta {
  display: flex;
  gap: 0.5rem;
  color: var(--muted);
  font-size: clamp(0.65rem, 0.8vw, 0.95rem);
  font-weight: 700;
  line-height: 1;
  opacity: 0.7;
  white-space: nowrap;
}
```

Update the existing banner width rule and add the icon layout immediately after it:

```css
html[data-tv-theme] .tv-header .right.rlb-goat-banner {
  align-self: center;
  min-width: min(40vw, 30rem);
  max-width: min(52vw, 40rem);
}
html[data-tv-theme] .tv-header .right.rlb-goat-banner.tv-header-right-has-icon {
  display: grid;
  grid-template-columns: auto minmax(0, 1fr);
  align-items: center;
  column-gap: clamp(0.35rem, 0.7vw, 0.75rem);
}
html[data-tv-theme] .rlb-goat-banner .tv-header-right-icon {
  font-size: clamp(2.4rem, 4vw, 4.5rem);
  line-height: 1;
  filter: drop-shadow(0 0 0.55rem rgba(251, 191, 36, 0.24));
}
html[data-tv-theme] .rlb-goat-banner .tv-header-right-content {
  min-width: 0;
}
```

- [ ] **Step 4: Run the static test and verify GREEN**

Run the Step 2 command again.

Expected: PASS.

- [ ] **Step 5: Run all Recycling/New leaderboard tests**

Run:

```bash
pytest -q \
  tests/test_recycling_leaderboard_tv.py \
  tests/test_recycling_leaderboard_static.py \
  tests/test_new_leaderboard_routes.py \
  tests/test_new_leaderboard_static.py
```

Expected: all tests pass, confirming the shared macro change does not alter New leaderboard behavior.

- [ ] **Step 6: Commit the responsive styling**

```bash
git add tests/test_recycling_leaderboard_static.py \
  src/zira_dashboard/static/recycling_leaderboard.css
git commit -m "style: refine recycling leaderboard tv header"
```

### Task 3: Verify the rendered TV layout

**Files:**
- Verify: `src/zira_dashboard/templates/recycling_leaderboard_tv.html`
- Verify: `src/zira_dashboard/static/recycling_leaderboard.css`

**Interfaces:**
- Consumes: the completed template and CSS changes from Tasks 1-2.
- Produces: visual evidence that the requested layout renders correctly in dark TV mode.

- [ ] **Step 1: Start the local dashboard using the repository's documented development command**

Run:

```bash
AUTH_DISABLED=1 .venv/bin/python -m uvicorn zira_dashboard.app:app \
  --host 127.0.0.1 --port 8000
```

Expected: uvicorn reports that it is serving on `http://127.0.0.1:8000`. Open `http://127.0.0.1:8000/tv/recycling-leaderboard?theme=dark` and confirm it returns HTTP 200. If port 8000 is already occupied, use port 8001 in both commands.

- [ ] **Step 2: Inspect the TV view in the in-app browser**

Verify all of the following at the available TV viewport:

- The YTD/L30 text sits immediately right of `Recycling-leaderboard` in the top header.
- The range is legible but less prominent than the title.
- One large `🐐` appears left of both GOAT tiles.
- Both tiles remain readable and aligned.
- No separate range row remains below the header.
- The leaderboard grid fills the remaining viewport without clipping or scrollbars.

- [ ] **Step 3: Run final automated verification**

Run:

```bash
pytest -q \
  tests/test_recycling_leaderboard_tv.py \
  tests/test_recycling_leaderboard_static.py \
  tests/test_new_leaderboard_routes.py \
  tests/test_new_leaderboard_static.py
git diff --check
```

Expected: all tests pass and `git diff --check` produces no output.

- [ ] **Step 4: Review the final diff**

Run:

```bash
git status --short
git diff --stat HEAD~2..HEAD
git diff HEAD~2..HEAD -- \
  src/zira_dashboard/templates/_tv_header.html \
  src/zira_dashboard/templates/recycling_leaderboard_tv.html \
  src/zira_dashboard/static/recycling_leaderboard.css \
  tests/test_recycling_leaderboard_tv.py \
  tests/test_recycling_leaderboard_static.py
```

Expected: only the scoped header templates, stylesheet, and tests differ from the implementation commits; unrelated `.claude/` files remain untouched.
