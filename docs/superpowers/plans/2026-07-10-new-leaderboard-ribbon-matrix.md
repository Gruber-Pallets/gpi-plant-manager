# New-Leaderboard Ribbon Matrix Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the tall Gold Ribbons list with a no-scroll January-to-December calendar matrix whose rows are work centers, freeing vertical space for taller three-family leaderboard panels.

**Architecture:** Keep the existing payload and winner calculations. Jinja sorts `data.ribbons` numerically by `month`, emits month headers once, then emits one row per active family. CSS replaces the old twelve-row grid with a compact fixed 13-column matrix and reserves less height for it on multi-family TVs.

**Tech Stack:** FastAPI, Jinja2, plain CSS, pytest, deterministic preview fixtures.

## Global Constraints

- Do not change production metrics, eligibility, family selection, or award calculations.
- Render January through December in that exact left-to-right order.
- Render work centers as rows and all twelve months with no ribbon scrolling.
- Preserve the one-family side-by-side layout and normal dashboard document-height behavior.
- Keep the existing three-GOAT-chip TV header rules intact.
- Do not touch unrelated files.

---

### Task 1: Render the calendar-ordered ribbon matrix

**Files:**

- Modify: `src/zira_dashboard/templates/new_leaderboard_tv.html:69-80`
- Modify: `tests/test_new_leaderboard_static.py:26-30`

**Interfaces:**

- Consumes: `data.active_families: list[str]` and `data.ribbons`, whose records contain `month: int`, `month_label: str`, and `winners: dict[str, dict | None]`.
- Produces: `.nlb-ribbon-grid` children ordered as corner, twelve month headers, then a work-center label and twelve winner cells for every active family.

- [ ] **Step 1: Write the failing template contract**

Replace the old ribbon-grid test with:

```python
def test_new_leaderboard_ribbon_matrix_uses_calendar_columns_and_family_rows():
    assert "data.ribbons|sort(attribute='month')" in TEMPLATE
    assert "{% for month in calendar_ribbons %}" in TEMPLATE
    assert "{% for family in data.active_families %}" in TEMPLATE
    assert "{% set winner = month.winners[family] %}" in TEMPLATE
    assert "nlb-work-center" in TEMPLATE
```

- [ ] **Step 2: Run the contract to verify it fails**

Run: `PYTHONPATH=src ZIRA_API_KEY=test /Users/dalegruber/Projects/gpi-plant-manager/.venv/bin/python -m pytest -q tests/test_new_leaderboard_static.py::test_new_leaderboard_ribbon_matrix_uses_calendar_columns_and_family_rows`

Expected: FAIL because the current markup iterates monthly rows and family columns.

- [ ] **Step 3: Replace the ribbon loop with calendar headers and family rows**

Use this exact Jinja structure inside `.nlb-ribbon-grid`:

```jinja2
{% set calendar_ribbons = data.ribbons|sort(attribute='month') %}
<span></span>
{% for month in calendar_ribbons %}
  <strong class="nlb-month">{{ month.month_label }}</strong>
{% endfor %}
{% for family in data.active_families %}
  <strong class="nlb-work-center">{{ family }}</strong>
  {% for month in calendar_ribbons %}
    {% set winner = month.winners[family] %}
    <span class="nlb-ribbon-cell">{% if winner %}<strong>{{ winner.name }}</strong><small>{{ winner.day.strftime('%b %-d') }} - {{ "%.0f"|format(winner.amount) }}</small>{% else %}<strong>-</strong>{% endif %}</span>
  {% endfor %}
{% endfor %}
```

Keep the existing title, winner text, and no-winner dash unchanged.

- [ ] **Step 4: Run the static contracts**

Run: `PYTHONPATH=src ZIRA_API_KEY=test /Users/dalegruber/Projects/gpi-plant-manager/.venv/bin/python -m pytest -q tests/test_new_leaderboard_static.py`

Expected: PASS.

- [ ] **Step 5: Commit the template behavior and test**

```bash
git add src/zira_dashboard/templates/new_leaderboard_tv.html tests/test_new_leaderboard_static.py
git commit -m "feat: transpose new leaderboard ribbons"
```

### Task 2: Fit all twelve columns and enlarge the multi-family panels

**Files:**

- Modify: `src/zira_dashboard/static/new_leaderboard.css:29-99,109-120,154-172`
- Modify: `tests/test_new_leaderboard_static.py`
- Modify: `tests/test_preview_new_leaderboard.py`

**Interfaces:**

- Consumes: `.nlb-ribbon-grid`, `.nlb-work-center`, `.nlb-month`, and `.nlb-ribbon-cell` emitted by Task 1.
- Produces: a no-scroll 13-column calendar matrix and a multi-family TV grid whose upper leaderboard region has more height than its lower ribbon region.

- [ ] **Step 1: Write failing CSS and preview contracts**

Add tests requiring the fixed geometry and verifying generated three-family fixture order:

```python
def test_new_leaderboard_ribbon_matrix_fits_all_months_without_scrolling():
    assert "grid-template-columns: minmax(4.5rem, 0.7fr) repeat(12, minmax(0, 1fr));" in CSS
    assert "grid-template-rows: auto repeat(var(--nlb-family-count), minmax(0, 1fr));" in CSS
    assert "overflow: hidden;" in CSS
    assert ".nlb-work-center" in CSS

def test_preview_three_family_fixture_contains_calendar_ribbon_headers():
    env = os.environ | {"ZIRA_API_KEY": "test", "AUTH_DISABLED": "1", "PYTHONPATH": str(ROOT / "src")}
    subprocess.run([sys.executable, "scripts/preview_new_leaderboard.py"], cwd=ROOT, env=env, check=True)
    html = (OUT / "tv-dark-three-families.html").read_text(encoding="utf-8")
    assert html.index(">Jan<") < html.index(">Dec<")
    for family in ("Juniors", "Woodpecker", "Hand Build"):
        assert f'class="nlb-work-center">{family}</strong>' in html
```

- [ ] **Step 2: Run focused contracts to verify they fail**

Run: `PYTHONPATH=src ZIRA_API_KEY=test /Users/dalegruber/Projects/gpi-plant-manager/.venv/bin/python -m pytest -q tests/test_new_leaderboard_static.py tests/test_preview_new_leaderboard.py`

Expected: FAIL because the current CSS is a month-label column with variable family columns and twelve month rows.

- [ ] **Step 3: Implement the compact matrix and taller panel allocation**

Replace `.nlb-ribbon-grid` geometry with:

```css
.nlb-ribbon-grid {
  display: grid;
  grid-template-columns: minmax(4.5rem, 0.7fr) repeat(12, minmax(0, 1fr));
  grid-template-rows: auto repeat(var(--nlb-family-count), minmax(0, 1fr));
  gap: 0.18rem;
  overflow: hidden;
}
```

Style `.nlb-work-center` in the existing amber header style with ellipsis. Keep winner name and amount single-line with ellipsis; reduce padding and type through `clamp()` rather than introducing scroll or wrapping months. For `.nlb-family-count-2` and `.nlb-family-count-3`, use `grid-template-rows: minmax(0, 1fr) minmax(8.5rem, 0.18fr);`. Keep normal-dashboard `height: auto`, but reduce its ribbon minimum from 22rem to a compact content-based size. In the `max-width: 1100px` rules, retain the 13-column matrix and do not add overflow or a second month row.

- [ ] **Step 4: Verify previews and focused tests**

Run:

```bash
PYTHONPATH=src ZIRA_API_KEY=test /Users/dalegruber/Projects/gpi-plant-manager/.venv/bin/python scripts/preview_new_leaderboard.py
PYTHONPATH=src ZIRA_API_KEY=test /Users/dalegruber/Projects/gpi-plant-manager/.venv/bin/python -m pytest -q tests/test_new_leaderboard_static.py tests/test_preview_new_leaderboard.py tests/test_new_leaderboard_routes.py
```

Expected: PASS; the three-family preview contains Jan through Dec in order and Juniors, Woodpecker, and Hand Build as row headings.

- [ ] **Step 5: Run the full suite and commit**

Run: `PYTHONPATH=src ZIRA_API_KEY=test /Users/dalegruber/Projects/gpi-plant-manager/.venv/bin/python -m pytest -q`

Expected: PASS with only environment-dependent skips.

```bash
git add src/zira_dashboard/static/new_leaderboard.css tests/test_new_leaderboard_static.py tests/test_preview_new_leaderboard.py
git commit -m "feat: compact new leaderboard ribbon matrix"
```
