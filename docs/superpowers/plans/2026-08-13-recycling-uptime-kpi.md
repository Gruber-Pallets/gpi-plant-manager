# Recycling Uptime KPI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Put an **Uptime** KPI card on the Recycling dashboard that shows the same whole-number uptime percent as the Downtime Report, colored green / orange / red by band.

**Architecture:** Template + CSS only on `recycling.html` / `recycling.css`. Reuse the existing `uptime_pct` context value already computed for the Downtime Report. Reintroduce widget id `kpi-uptime` (removed May 2025) so saved layouts that still store that id drop the tile into the open slot under Pallets/hr. Threshold colors always win over the widget color customizer.

**Tech Stack:** Jinja2, CSS custom properties (`--good` / `--warn` / `--bad`), pytest static + template-render tests, FastAPI Recycling page

## Global Constraints

- Scope: `/recycling` via `recycling.html` only (screen and TV that use this template).
- Widget id: `kpi-uptime`; default title: `Uptime`; value: whole-number percent like `85%`.
- Same number as Downtime Report: `uptime_pct|round(0)|int` then `%`.
- Bands on the displayed percent: `>= 90` → `--good`; `>= 80` and `< 90` → `--warn`; `< 80` → `--bad`.
- Threshold color always wins; do not call `widget_color_style` for this value.
- No new route math, no Downtime Report copy/layout changes, no `/wc/...` or `new_dept.html` changes.
- Add no dependencies.
- What's New copy must be kid-friendly per `AGENTS.md`.

## File map

| File | Responsibility |
|------|----------------|
| `src/zira_dashboard/templates/recycling.html` | Add `kpi-uptime` KPI; format value; apply band class |
| `src/zira_dashboard/static/recycling.css` | Band color rules with `!important` so customizer cannot override |
| `tests/test_recycling_uptime_kpi.py` | Static + render tests for widget, format, and bands |
| `CHANGELOG.md` | Kid-friendly What's New entry under today's date |

---

### Task 1: Add the Uptime KPI with traffic-light colors

**Files:**
- Create: `tests/test_recycling_uptime_kpi.py`
- Modify: `src/zira_dashboard/templates/recycling.html` (KPI block ~81–98 and the comment above it)
- Modify: `src/zira_dashboard/static/recycling.css` (add band rules near the other `.val` rules ~108–130)
- Modify: `CHANGELOG.md` (under `## 2026-08-13`)

**Interfaces:**
- Consumes: existing template context `uptime_pct: float` from `routes/departments.py` (already passed for Downtime Report).
- Produces: grid widget `kpi-uptime` with classes `val band-good` / `band-warn` / `band-bad` on the value element.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_recycling_uptime_kpi.py`:

```python
"""Recycling dashboard Uptime KPI — presence, format, and color bands."""

from pathlib import Path

from starlette.requests import Request

from zira_dashboard.deps import templates


ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "src" / "zira_dashboard" / "templates" / "recycling.html"
CSS = ROOT / "src" / "zira_dashboard" / "static" / "recycling.css"


def _src() -> str:
    return TEMPLATE.read_text(encoding="utf-8")


def _css() -> str:
    return CSS.read_text(encoding="utf-8")


def _render(*, uptime_pct: float) -> str:
    """Render recycling.html with the minimum context the KPI block needs."""
    request = Request({"type": "http", "method": "GET", "path": "/recycling", "headers": []})
    return templates.get_template("recycling.html").render(
        request=request,
        static_v=lambda path: "test",
        tv_mode=False,
        tv_theme="dark",
        window="today",
        custom_range_active=False,
        start="2026-08-13",
        end="2026-08-13",
        layout={},
        customs={},
        total_units=1459,
        pph_per_person=63.6,
        pph_per_person_ex_d4=57.1,
        dismantler_bars=[],
        repair_bars=[],
        downtime_rows=[],
        elapsed_minutes=0,
        uptime_pct=uptime_pct,
        dismantler_people=0,
        repair_people=0,
        is_range=False,
        dismantler_progress=[],
        repair_progress=[],
        dismantler_group_target=0,
        repair_group_target=0,
        range_includes_today=False,
        refreshed_at="9:00:00 AM",
        all_active_people=[],
        goat_alerts_active=[],
        goat_contenders=[],
    )


def test_recycling_template_declares_kpi_uptime_widget():
    html = _src()
    assert "kpi-uptime" in html
    assert "'Uptime'" in html or '"Uptime"' in html
    assert "band-good" in html and "band-warn" in html and "band-bad" in html


def test_recycling_css_locks_uptime_band_colors():
    css = _css()
    assert 'gs-id="kpi-uptime"' in css or "[gs-id='kpi-uptime']" in css or '[gs-id="kpi-uptime"]' in css
    assert "band-good" in css and "var(--good)" in css
    assert "band-warn" in css and "var(--warn)" in css
    assert "band-bad" in css and "var(--bad)" in css
    # Threshold colors must win over any customizer inline color.
    assert css.count("!important") >= 3


def test_uptime_kpi_renders_whole_percent_and_warn_band():
    html = _render(uptime_pct=85.4)
    assert 'gs-id="kpi-uptime"' in html
    assert ">85%<" in html or ">85 %</" not in html  # prefer tight "85%"
    assert "85%" in html
    assert 'class="val band-warn"' in html or "val band-warn" in html


def test_uptime_kpi_band_boundaries():
    assert "band-good" in _render(uptime_pct=90.0)
    assert "band-warn" in _render(uptime_pct=80.0)
    assert "band-bad" in _render(uptime_pct=79.9)
    assert "band-good" in _render(uptime_pct=99.9)
    assert "band-bad" in _render(uptime_pct=0.0)
```

If full-template render fails for missing context vars during Step 2, extend `_render` with empty/default stubs for whatever Jinja reports — do not weaken the band assertions.

- [ ] **Step 2: Run tests and verify RED**

Run:

```bash
ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_recycling_uptime_kpi.py -q
```

Expected: failures because `kpi-uptime` is absent from `recycling.html` and band CSS is missing (and/or render errors from missing widget).

- [ ] **Step 3: Implement the KPI in the template**

In `src/zira_dashboard/templates/recycling.html`:

1. Replace the outdated comment above `kpi_defs` with a short note that `kpi-uptime` is back as a dedicated KPI (Downtime Report still shows the same percent in its header).

2. Extend `kpi_defs` and the value markup so the third tile is uptime. Prefer keeping the existing loop and branching on `kid`, same pattern as `kpi-palletshr`:

```jinja
    {% set kpi_defs = [
      ('kpi-pallets',   'Total Pallets Processed', '{:,}'.format(total_units)),
      ('kpi-palletshr', 'pallets/hr/person',       pph_per_person),
      ('kpi-uptime',    'Uptime',                  (uptime_pct|round(0)|int)|string ~ '%'),
    ] %}
```

3. In the `.val` div, apply band class for `kpi-uptime` and skip `widget_color_style`:

```jinja
          {% if kid == 'kpi-uptime' %}
            {% set uptime_band = 'good' if uptime_pct >= 90 else ('warn' if uptime_pct >= 80 else 'bad') %}
          {% endif %}
          <div class="val{% if kid == 'kpi-uptime' %} band-{{ uptime_band }}{% endif %}"
            {%- if kid == 'kpi-palletshr' %} style="color: var(--fg) !important"
            {%- elif kid == 'kpi-uptime' %}
            {%- else %} {{ widget_color_style(kid, 'color') }}
            {%- endif -%}>{{ kval }}{% if kid == 'kpi-palletshr' %}<span class="val-secondary" title="Same metric but Dismantler 4's pallets are excluded from the numerator (their man-hours are still in the denominator). D4 reprocesses reject material that the ERP doesn't count as new throughput.">({{ pph_per_person_ex_d4 }} excl. D4)</span>{% endif %}</div>
```

Default grid position comes from the existing loop (`kdef_x = loop.index0 * 3` → `(6, 0, 3, 2)` for a fresh layout). Saved layouts that still contain `kpi-uptime` coordinates win via `widget_attrs` / `layout_map` and fill the open slot under Pallets/hr.

Keep `edit_controls(kid, ktitle, 'kpi')` so title/align customization still works; only the number color is locked.

- [ ] **Step 4: Add CSS band rules**

In `src/zira_dashboard/static/recycling.css`, near the other `.grid-stack-item-content > .val` rules, add:

```css
  /* Uptime KPI: traffic-light bands always win over widget color customizer. */
  .grid-stack-item[gs-id="kpi-uptime"] .val.band-good {
    color: var(--good) !important;
  }
  .grid-stack-item[gs-id="kpi-uptime"] .val.band-warn {
    color: var(--warn) !important;
  }
  .grid-stack-item[gs-id="kpi-uptime"] .val.band-bad {
    color: var(--bad) !important;
  }
```

- [ ] **Step 5: Run the new tests and verify GREEN**

Run:

```bash
ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_recycling_uptime_kpi.py -q
```

Expected: all passed. If render is missing a context key, add the stub in `_render` and re-run until green without changing band rules.

- [ ] **Step 6: Run related Recycling static checks**

Run:

```bash
ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_recycling_toolbar_static.py tests/test_recycling_scaling_static.py tests/test_recycling_uptime_kpi.py -q
```

Expected: all passed.

- [ ] **Step 7: Add What's New copy**

Under the existing `## 2026-08-13` section in `CHANGELOG.md`, add a features group (create a new `###` deploy heading if needed):

```markdown
### Recycling uptime at a glance

#### Features

- **Recycling now shows total uptime as a big number.** It uses green when uptime is 90% or higher, orange from 80% up to 90%, and red below 80%, so you can see plant health quickly without hunting in the downtime chart.
```

- [ ] **Step 8: Commit**

```bash
git add tests/test_recycling_uptime_kpi.py \
  src/zira_dashboard/templates/recycling.html \
  src/zira_dashboard/static/recycling.css \
  CHANGELOG.md
git commit -m "$(cat <<'EOF'
feat: add Recycling uptime KPI with traffic-light bands

EOF
)"
git push origin main
```

---

## Spec coverage self-review

| Spec requirement | Task |
|------------------|------|
| Widget `kpi-uptime`, title Uptime, value `N%` | Task 1 Steps 3–5 |
| Same `uptime_pct` as Downtime Report, whole number | Task 1 Step 3 |
| ≥90 good / ≥80 warn / &lt;80 bad; customizer cannot override | Task 1 Steps 3–4 |
| Default size matches other KPIs; saved layout fills open slot | Task 1 Step 3 (`widget_attrs` + shared id) |
| Template-only; no new formula / no operator or new_dept | Global Constraints + File map |
| Tests for presence + bands + boundaries | Task 1 Steps 1–5 |
| Kid-friendly What's New | Task 1 Step 7 |

## Placeholder / consistency check

- No TBD/TODO placeholders.
- Band class names `band-good` / `band-warn` / `band-bad` match across template, CSS, and tests.
- Widget id `kpi-uptime` matches spec and historical layout key.
