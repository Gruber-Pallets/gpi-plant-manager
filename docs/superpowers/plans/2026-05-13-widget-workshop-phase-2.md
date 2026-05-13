# Widget Workshop — Phase 2 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add 5 more widget types to the workshop registry from Phase 1: `pallets_banner`, `daily_progress`, `cumulative`, `kpi`, `downtime`. Each becomes selectable in the workshop and droppable on any custom dashboard.

**Architecture:** The Phase 1 registry pattern stays. Each new type is purely additive: one entry in `widget_types.py`'s `_REGISTRY`, one resolver function in `widget_data.py`, one Jinja partial under `templates/widgets/`, one new `elif` branch in `templates/_widget_render.html`, and one resolver unit test. No schema changes, no store changes, no route changes.

**Tech Stack:** Python 3.11+, FastAPI, Jinja2, pytest. Wraps existing helpers in `wc_dashboard_data` and `work_centers_store` — no new data prep written from scratch.

**Spec:** `docs/superpowers/specs/2026-05-13-widget-workshop-and-custom-dashboards-design.md`

**Phase 1 plan (for context):** `docs/superpowers/plans/2026-05-13-widget-workshop-phase-1.md`

---

## File Structure

**Modified files (every task touches these):**
- `src/zira_dashboard/widget_types.py` — append one entry to `_REGISTRY`
- `src/zira_dashboard/widget_data.py` — append one resolver function
- `src/zira_dashboard/templates/_widget_render.html` — add one `elif` branch
- `tests/test_widget_data.py` — append resolver tests

**New files (one per task):**
- `src/zira_dashboard/templates/widgets/_widget_pallets_banner.html` (Task 1)
- `src/zira_dashboard/templates/widgets/_widget_daily_progress.html` (Task 2)
- `src/zira_dashboard/templates/widgets/_widget_cumulative.html` (Task 3)
- `src/zira_dashboard/templates/widgets/_widget_kpi.html` (Task 4)
- `src/zira_dashboard/templates/widgets/_widget_downtime.html` (Task 5)

Final task: CHANGELOG + push.

---

## Conventions

- Python interpreter: `.venv/Scripts/python.exe`.
- Unit tests on resolvers go in `tests/test_widget_data.py` (already exists from Phase 1). Mock the underlying helpers — no Postgres needed.
- Commit messages: `feat(widgets-p2): add <type> widget`.
- Each task ships independently — the dispatcher's `else` branch already renders an "unknown widget type" placeholder, so a half-deployed Phase 2 doesn't break existing dashboards.

**Existing helpers Phase 2 wraps (do NOT modify):**
- `wc_dashboard_data.pallets_banner(wc_name, day)` → `{units_today, target_today, target_full_day, pct_of_target}`
- `wc_dashboard_data.fifteen_min_increments(wc_name, day)` → list of `{bucket_index, minute_offset, units, color, target}`
- `wc_dashboard_data.daily_progress(wc_name, day)` → list of `{bucket_index, minute_offset, cumulative_units}` (yes, "daily_progress" the helper returns cumulative — the Phase 2 `cumulative` widget wraps it)
- `wc_dashboard_data.downtime_report(wc_name, day)` → `{events, total_minutes}`
- `wc_dashboard_data._units_today_for_wc(wc_name, day)` → int (used by `kpi`)
- `work_centers_store.members("group", group_name)` → list of Location (used by group-scoped KPI metrics)
- `awards` and others from Phase 1 already wrapped — no new wrappers needed

---

## Task 1: `pallets_banner` widget (single-WC banner)

**Files:**
- Modify: `src/zira_dashboard/widget_types.py`
- Modify: `src/zira_dashboard/widget_data.py`
- Modify: `src/zira_dashboard/templates/_widget_render.html`
- Create: `src/zira_dashboard/templates/widgets/_widget_pallets_banner.html`
- Modify: `tests/test_widget_data.py`

- [ ] **Step 1: Append registry entry to `widget_types.py`**

In `src/zira_dashboard/widget_types.py`, add this dict to the `_REGISTRY` list (after the existing `ribbons` entry, just before the closing `]`):

```python
    {
        "type": "pallets_banner",
        "label": "Pallets Banner (single WC)",
        "data_params_schema": [
            {"key": "wc_name", "label": "Work Center", "input": "select",
             "options_from": "wcs", "required": True},
        ],
        "visual_params_schema": [
            {"key": "color", "label": "Bar color", "input": "color", "default": "#22c55e"},
        ],
        "resolver": "_resolve_pallets_banner",
        "partial": "widgets/_widget_pallets_banner.html",
    },
```

- [ ] **Step 2: Append resolver to `widget_data.py`**

In `src/zira_dashboard/widget_data.py`, add at the bottom of the file:

```python
def _resolve_pallets_banner(params: dict, day: date) -> dict:
    """Single-WC pallets banner: today's units vs prorated daily target.

    Wraps `wc_dashboard_data.pallets_banner`. Returns the same dict
    shape: {units_today, target_today, target_full_day, pct_of_target}.
    """
    from . import wc_dashboard_data
    wc_name = (params or {}).get("wc_name")
    if not wc_name:
        return {"units_today": 0, "target_today": 0,
                "target_full_day": 0, "pct_of_target": None}
    return wc_dashboard_data.pallets_banner(wc_name, day)
```

- [ ] **Step 3: Append resolver tests to `tests/test_widget_data.py`**

At the bottom of `tests/test_widget_data.py`, append:

```python
def test_resolve_pallets_banner_delegates(monkeypatch):
    from zira_dashboard import widget_data, wc_dashboard_data

    monkeypatch.setattr(
        wc_dashboard_data, "pallets_banner",
        lambda wc, d: {
            "units_today": 42, "target_today": 30,
            "target_full_day": 80, "pct_of_target": 140.0,
        } if wc == "Repair 1" else None,
    )
    out = widget_data._resolve_pallets_banner({"wc_name": "Repair 1"}, day=date(2026, 5, 13))
    assert out["units_today"] == 42
    assert out["target_full_day"] == 80


def test_resolve_pallets_banner_missing_wc_returns_empty():
    from zira_dashboard import widget_data
    out = widget_data._resolve_pallets_banner({}, day=date(2026, 5, 13))
    assert out["units_today"] == 0
    assert out["target_today"] == 0
    assert out["pct_of_target"] is None
```

- [ ] **Step 4: Create partial `templates/widgets/_widget_pallets_banner.html`**

```jinja
{# Pallets banner — single WC. Expects:
   data = {units_today, target_today, target_full_day, pct_of_target}
   visual = {color}
#}
<div class="grid-stack-item-content">
  <h3>{{ placement_title or 'Pallets Banner' }}</h3>
  <div class="pallets-banner">
    <div class="pallets-numbers">
      <span class="units">{{ data.units_today or 0 }}</span>
      <span class="target">/ {{ data.target_today or 0 }} goal so far ({{ data.target_full_day or 0 }} full day)</span>
    </div>
    <div class="bar-track">
      <div class="bar-fill"
           style="width: {{ (data.pct_of_target|float)|round(0)|int if data.pct_of_target else 0 }}%;
                  background: {{ visual.color or 'var(--accent)' }}"></div>
    </div>
  </div>
</div>
```

- [ ] **Step 5: Update dispatcher in `templates/_widget_render.html`**

Open `src/zira_dashboard/templates/_widget_render.html`. The existing chain has branches for `pallets_by_wc`, `goat_race`, `ribbons`. Insert a new `elif` for `pallets_banner` (alphabetical order isn't required — place it right after the `pallets_by_wc` branch for grouping):

Find:

```jinja
{% if placement.type == 'pallets_by_wc' %}
  {% include "widgets/_widget_pallets_by_wc.html" %}
{% elif placement.type == 'goat_race' %}
```

Replace with:

```jinja
{% if placement.type == 'pallets_by_wc' %}
  {% include "widgets/_widget_pallets_by_wc.html" %}
{% elif placement.type == 'pallets_banner' %}
  {% include "widgets/_widget_pallets_banner.html" %}
{% elif placement.type == 'goat_race' %}
```

- [ ] **Step 6: Run tests + parse check**

```
.venv/Scripts/python.exe -m pytest tests/test_widget_types.py tests/test_widget_data.py -v 2>&1 | tail -10
.venv/Scripts/python.exe -c "from jinja2 import Environment, FileSystemLoader; env = Environment(loader=FileSystemLoader('src/zira_dashboard/templates'), autoescape=True); env.parse(open('src/zira_dashboard/templates/_widget_render.html', encoding='utf-8').read()); env.parse(open('src/zira_dashboard/templates/widgets/_widget_pallets_banner.html', encoding='utf-8').read()); print('parse OK')"
```

Expected: all tests PASS (new + existing). Parse OK. Registry now has 4 types and all resolver-function-name + partial-path assertions still hold.

- [ ] **Step 7: Commit**

```
git add src/zira_dashboard/widget_types.py src/zira_dashboard/widget_data.py src/zira_dashboard/templates/_widget_render.html src/zira_dashboard/templates/widgets/_widget_pallets_banner.html tests/test_widget_data.py
git commit -m "$(cat <<'EOF'
feat(widgets-p2): add pallets_banner widget (single WC)

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 2: `daily_progress` widget (per-15-min bar chart, color-coded)

**Files:**
- Modify: `src/zira_dashboard/widget_types.py`
- Modify: `src/zira_dashboard/widget_data.py`
- Modify: `src/zira_dashboard/templates/_widget_render.html`
- Create: `src/zira_dashboard/templates/widgets/_widget_daily_progress.html`
- Modify: `tests/test_widget_data.py`

This widget wraps `wc_dashboard_data.fifteen_min_increments`, which emits per-bucket data with green/amber/red color flags against the per-bucket target.

- [ ] **Step 1: Append registry entry**

In `_REGISTRY` in `widget_types.py`, after the `pallets_banner` entry just added:

```python
    {
        "type": "daily_progress",
        "label": "Daily Progress (15-min bars)",
        "data_params_schema": [
            {"key": "wc_name", "label": "Work Center", "input": "select",
             "options_from": "wcs", "required": True},
        ],
        "visual_params_schema": [],
        "resolver": "_resolve_daily_progress",
        "partial": "widgets/_widget_daily_progress.html",
    },
```

- [ ] **Step 2: Append resolver to `widget_data.py`**

```python
def _resolve_daily_progress(params: dict, day: date) -> dict:
    """Per-15-min bar chart with target-based color (green/amber/red).

    Wraps `wc_dashboard_data.fifteen_min_increments`. Returns
    {buckets: [...], target}.
    """
    from . import wc_dashboard_data
    wc_name = (params or {}).get("wc_name")
    if not wc_name:
        return {"buckets": [], "target": 0}
    buckets = wc_dashboard_data.fifteen_min_increments(wc_name, day) or []
    target = buckets[0]["target"] if buckets else 0
    return {"buckets": buckets, "target": target}
```

- [ ] **Step 3: Append resolver tests**

```python
def test_resolve_daily_progress_returns_buckets(monkeypatch):
    from zira_dashboard import widget_data, wc_dashboard_data

    monkeypatch.setattr(
        wc_dashboard_data, "fifteen_min_increments",
        lambda wc, d: [
            {"bucket_index": 0, "minute_offset": 0, "units": 5, "color": "green", "target": 4},
            {"bucket_index": 1, "minute_offset": 15, "units": 2, "color": "red", "target": 4},
        ] if wc == "Repair 1" else [],
    )
    out = widget_data._resolve_daily_progress({"wc_name": "Repair 1"}, day=date(2026, 5, 13))
    assert len(out["buckets"]) == 2
    assert out["target"] == 4
    assert out["buckets"][0]["color"] == "green"


def test_resolve_daily_progress_missing_wc_returns_empty():
    from zira_dashboard import widget_data
    out = widget_data._resolve_daily_progress({}, day=date(2026, 5, 13))
    assert out == {"buckets": [], "target": 0}
```

- [ ] **Step 4: Create partial `templates/widgets/_widget_daily_progress.html`**

```jinja
{# Per-15-min bar chart with color-coded bars (green ≥ target, amber ≥ 75%, red < 75%).
   data = {buckets: [{bucket_index, minute_offset, units, color, target}, ...], target}
#}
<div class="grid-stack-item-content">
  <h3>{{ placement_title or 'Daily Progress' }}{% if data.target %} (target {{ data.target }}/15 min){% endif %}</h3>
  <div class="fifteen-min-bars">
    {% for b in data.buckets %}
      <div class="bar bar-{{ b.color }}"
           style="--units: {{ b.units }}; --target: {{ b.target }};"
           title="{{ b.units }} units · target {{ b.target }}"></div>
    {% else %}
      <div class="empty-state">No data yet.</div>
    {% endfor %}
  </div>
</div>
```

- [ ] **Step 5: Update dispatcher**

In `templates/_widget_render.html`, after the `pallets_banner` branch just added, insert:

Find:

```jinja
{% elif placement.type == 'pallets_banner' %}
  {% include "widgets/_widget_pallets_banner.html" %}
{% elif placement.type == 'goat_race' %}
```

Replace with:

```jinja
{% elif placement.type == 'pallets_banner' %}
  {% include "widgets/_widget_pallets_banner.html" %}
{% elif placement.type == 'daily_progress' %}
  {% include "widgets/_widget_daily_progress.html" %}
{% elif placement.type == 'goat_race' %}
```

- [ ] **Step 6: Run tests + parse**

```
.venv/Scripts/python.exe -m pytest tests/test_widget_types.py tests/test_widget_data.py -v 2>&1 | tail -10
.venv/Scripts/python.exe -c "from jinja2 import Environment, FileSystemLoader; env = Environment(loader=FileSystemLoader('src/zira_dashboard/templates'), autoescape=True); env.parse(open('src/zira_dashboard/templates/_widget_render.html', encoding='utf-8').read()); env.parse(open('src/zira_dashboard/templates/widgets/_widget_daily_progress.html', encoding='utf-8').read()); print('parse OK')"
```

Expected: all PASS. Registry has 5 types.

- [ ] **Step 7: Commit**

```
git add src/zira_dashboard/widget_types.py src/zira_dashboard/widget_data.py src/zira_dashboard/templates/_widget_render.html src/zira_dashboard/templates/widgets/_widget_daily_progress.html tests/test_widget_data.py
git commit -m "$(cat <<'EOF'
feat(widgets-p2): add daily_progress widget (15-min bars)

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 3: `cumulative` widget (cumulative line chart)

**Files:** same shape as Task 2.

The Phase 2 `cumulative` widget wraps the (confusingly-named) `wc_dashboard_data.daily_progress` helper, which returns the CUMULATIVE bucket data. The widget renders an SVG line/area chart from shift-start to shift-end.

- [ ] **Step 1: Append registry entry**

```python
    {
        "type": "cumulative",
        "label": "Cumulative Progress (line chart)",
        "data_params_schema": [
            {"key": "wc_name", "label": "Work Center", "input": "select",
             "options_from": "wcs", "required": True},
        ],
        "visual_params_schema": [
            {"key": "color", "label": "Line color", "input": "color", "default": "#22c55e"},
            {"key": "show_target", "label": "Show goal line",
             "input": "select", "options": [
                 {"value": "true", "label": "Yes"}, {"value": "false", "label": "No"},
             ], "default": "true"},
        ],
        "resolver": "_resolve_cumulative",
        "partial": "widgets/_widget_cumulative.html",
    },
```

- [ ] **Step 2: Append resolver**

```python
def _resolve_cumulative(params: dict, day: date) -> dict:
    """Cumulative bucket data + the WC's full-day target for the goal line.

    Wraps `wc_dashboard_data.daily_progress` (which returns cumulative
    per bucket) and pulls the full-day goal from `pallets_banner`.
    """
    from . import wc_dashboard_data
    wc_name = (params or {}).get("wc_name")
    if not wc_name:
        return {"points": [], "max_y": 0}
    points = wc_dashboard_data.daily_progress(wc_name, day) or []
    banner = wc_dashboard_data.pallets_banner(wc_name, day) or {}
    max_y = banner.get("target_full_day") or 0
    return {"points": points, "max_y": max_y}
```

- [ ] **Step 3: Append resolver tests**

```python
def test_resolve_cumulative_combines_points_and_target(monkeypatch):
    from zira_dashboard import widget_data, wc_dashboard_data

    monkeypatch.setattr(
        wc_dashboard_data, "daily_progress",
        lambda wc, d: [
            {"bucket_index": 0, "minute_offset": 0, "cumulative_units": 0},
            {"bucket_index": 1, "minute_offset": 15, "cumulative_units": 5},
            {"bucket_index": 2, "minute_offset": 30, "cumulative_units": 11},
        ] if wc == "Repair 1" else [],
    )
    monkeypatch.setattr(
        wc_dashboard_data, "pallets_banner",
        lambda wc, d: {"target_full_day": 80} if wc == "Repair 1" else {},
    )
    out = widget_data._resolve_cumulative({"wc_name": "Repair 1"}, day=date(2026, 5, 13))
    assert len(out["points"]) == 3
    assert out["max_y"] == 80
    assert out["points"][-1]["cumulative_units"] == 11


def test_resolve_cumulative_missing_wc_returns_empty():
    from zira_dashboard import widget_data
    out = widget_data._resolve_cumulative({}, day=date(2026, 5, 13))
    assert out == {"points": [], "max_y": 0}
```

- [ ] **Step 4: Create partial `templates/widgets/_widget_cumulative.html`**

```jinja
{# Cumulative line chart (SVG). Expects:
   data = {points: [{bucket_index, minute_offset, cumulative_units}, ...], max_y}
   visual = {color, show_target}
#}
<div class="grid-stack-item-content">
  <h3>{{ placement_title or 'Cumulative Progress' }}</h3>
  {% if data.points %}
    {%- set max_y = data.max_y or 1 -%}
    {%- set n = data.points|length or 1 -%}
    {%- set color = visual.color or 'var(--accent)' -%}
    {%- set show_target = (visual.show_target|default('true')) != 'false' -%}
    {%- set pts = [] -%}
    {%- for p in data.points -%}
      {%- set _ = pts.append("%.1f,%.1f"|format(loop.index0 / (n-1 if n>1 else 1) * 200, 80 - (p.cumulative_units / max_y) * 80 if max_y else 80)) -%}
    {%- endfor -%}
    <svg class="daily-progress-chart" viewBox="0 0 200 80" preserveAspectRatio="none">
      <polyline fill="none" stroke="{{ color }}" stroke-width="1.5" points="{{ pts|join(' ') }}"/>
      {% if show_target %}
        <line x1="0" y1="0" x2="200" y2="0" stroke="var(--muted)" stroke-dasharray="3 3" stroke-width="0.5"/>
        <text x="2" y="6" fill="var(--muted)" font-size="6">goal {{ max_y }}</text>
      {% endif %}
    </svg>
  {% else %}
    <div class="empty-state">No data yet.</div>
  {% endif %}
</div>
```

- [ ] **Step 5: Update dispatcher**

Find:

```jinja
{% elif placement.type == 'daily_progress' %}
  {% include "widgets/_widget_daily_progress.html" %}
{% elif placement.type == 'goat_race' %}
```

Replace with:

```jinja
{% elif placement.type == 'daily_progress' %}
  {% include "widgets/_widget_daily_progress.html" %}
{% elif placement.type == 'cumulative' %}
  {% include "widgets/_widget_cumulative.html" %}
{% elif placement.type == 'goat_race' %}
```

- [ ] **Step 6: Run tests + parse**

```
.venv/Scripts/python.exe -m pytest tests/test_widget_types.py tests/test_widget_data.py -v 2>&1 | tail -10
.venv/Scripts/python.exe -c "from jinja2 import Environment, FileSystemLoader; env = Environment(loader=FileSystemLoader('src/zira_dashboard/templates'), autoescape=True); env.parse(open('src/zira_dashboard/templates/_widget_render.html', encoding='utf-8').read()); env.parse(open('src/zira_dashboard/templates/widgets/_widget_cumulative.html', encoding='utf-8').read()); print('parse OK')"
```

Expected: all PASS. Registry has 6 types.

- [ ] **Step 7: Commit**

```
git add src/zira_dashboard/widget_types.py src/zira_dashboard/widget_data.py src/zira_dashboard/templates/_widget_render.html src/zira_dashboard/templates/widgets/_widget_cumulative.html tests/test_widget_data.py
git commit -m "$(cat <<'EOF'
feat(widgets-p2): add cumulative widget (line chart)

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 4: `kpi` widget (single big number)

**Files:** same shape.

KPI is the only generic one — picks a metric. Metrics supported in Phase 2:
- `units_today_wc` — today's units for one WC (param: `wc_name`)
- `units_today_group` — today's units across every WC in a group (param: `group`)
- `downtime_minutes_wc` — today's downtime minutes for one WC (param: `wc_name`)

- [ ] **Step 1: Append registry entry**

```python
    {
        "type": "kpi",
        "label": "KPI Tile",
        "data_params_schema": [
            {"key": "metric", "label": "Metric", "input": "select",
             "options": [
                 {"value": "units_today_wc",     "label": "Units today (single WC)"},
                 {"value": "units_today_group",  "label": "Units today (group sum)"},
                 {"value": "downtime_minutes_wc", "label": "Downtime minutes (single WC)"},
             ], "default": "units_today_wc", "required": True},
            {"key": "wc_name", "label": "Work Center (for *_wc metrics)",
             "input": "select", "options_from": "wcs"},
            {"key": "group", "label": "Group (for *_group metrics)",
             "input": "select", "options_from": "groups"},
        ],
        "visual_params_schema": [
            {"key": "label", "label": "Display label (overrides default)", "input": "text"},
            {"key": "color", "label": "Number color", "input": "color", "default": "#22c55e"},
        ],
        "resolver": "_resolve_kpi",
        "partial": "widgets/_widget_kpi.html",
    },
```

- [ ] **Step 2: Append resolver**

```python
def _resolve_kpi(params: dict, day: date) -> dict:
    """KPI tile — single big number with a label.

    Returns: {label, value, suffix}. The widget partial concatenates
    "{value}{suffix}" and renders `label` above it.
    """
    from . import wc_dashboard_data
    params = params or {}
    metric = params.get("metric") or "units_today_wc"
    if metric == "units_today_wc":
        wc = params.get("wc_name")
        if not wc:
            return {"label": "Units today", "value": 0, "suffix": ""}
        units = wc_dashboard_data._units_today_for_wc(wc, day)
        return {"label": f"Units · {wc}", "value": units, "suffix": ""}
    if metric == "units_today_group":
        group = params.get("group")
        if not group:
            return {"label": "Units today (group)", "value": 0, "suffix": ""}
        units = _units_today_for_group(group, day)
        return {"label": f"Units · {group}", "value": units, "suffix": ""}
    if metric == "downtime_minutes_wc":
        wc = params.get("wc_name")
        if not wc:
            return {"label": "Downtime today", "value": 0, "suffix": "m"}
        report = wc_dashboard_data.downtime_report(wc, day) or {}
        return {"label": f"Downtime · {wc}", "value": int(report.get("total_minutes", 0)), "suffix": "m"}
    return {"label": f"Unknown metric: {metric}", "value": 0, "suffix": ""}
```

- [ ] **Step 3: Append resolver tests**

```python
def test_resolve_kpi_units_today_wc(monkeypatch):
    from zira_dashboard import widget_data, wc_dashboard_data
    monkeypatch.setattr(
        wc_dashboard_data, "_units_today_for_wc",
        lambda wc, d: 42 if wc == "Repair 1" else 0,
    )
    out = widget_data._resolve_kpi(
        {"metric": "units_today_wc", "wc_name": "Repair 1"}, day=date(2026, 5, 13),
    )
    assert out["value"] == 42
    assert out["label"] == "Units · Repair 1"


def test_resolve_kpi_units_today_group(monkeypatch):
    from zira_dashboard import widget_data
    monkeypatch.setattr(widget_data, "_units_today_for_group", lambda g, d: 200)
    out = widget_data._resolve_kpi(
        {"metric": "units_today_group", "group": "Repairs"}, day=date(2026, 5, 13),
    )
    assert out["value"] == 200
    assert out["label"] == "Units · Repairs"


def test_resolve_kpi_downtime_minutes(monkeypatch):
    from zira_dashboard import widget_data, wc_dashboard_data
    monkeypatch.setattr(
        wc_dashboard_data, "downtime_report",
        lambda wc, d: {"events": [], "total_minutes": 17} if wc == "Repair 1" else {},
    )
    out = widget_data._resolve_kpi(
        {"metric": "downtime_minutes_wc", "wc_name": "Repair 1"}, day=date(2026, 5, 13),
    )
    assert out["value"] == 17
    assert out["suffix"] == "m"


def test_resolve_kpi_unknown_metric_returns_placeholder():
    from zira_dashboard import widget_data
    out = widget_data._resolve_kpi({"metric": "garbage"}, day=date(2026, 5, 13))
    assert out["value"] == 0
    assert "garbage" in out["label"]
```

- [ ] **Step 4: Create partial `templates/widgets/_widget_kpi.html`**

```jinja
{# KPI tile — single big number with a label.
   data = {label, value, suffix}
   visual = {label (override), color}
#}
<div class="grid-stack-item-content kpi">
  <div class="label">{{ visual.label or data.label }}</div>
  <div class="val" style="color: {{ visual.color or 'var(--accent)' }}">
    {{ data.value }}{{ data.suffix }}
  </div>
</div>
```

- [ ] **Step 5: Update dispatcher**

Find:

```jinja
{% elif placement.type == 'cumulative' %}
  {% include "widgets/_widget_cumulative.html" %}
{% elif placement.type == 'goat_race' %}
```

Replace with:

```jinja
{% elif placement.type == 'cumulative' %}
  {% include "widgets/_widget_cumulative.html" %}
{% elif placement.type == 'kpi' %}
  {% include "widgets/_widget_kpi.html" %}
{% elif placement.type == 'goat_race' %}
```

- [ ] **Step 6: Run tests + parse**

```
.venv/Scripts/python.exe -m pytest tests/test_widget_types.py tests/test_widget_data.py -v 2>&1 | tail -10
.venv/Scripts/python.exe -c "from jinja2 import Environment, FileSystemLoader; env = Environment(loader=FileSystemLoader('src/zira_dashboard/templates'), autoescape=True); env.parse(open('src/zira_dashboard/templates/_widget_render.html', encoding='utf-8').read()); env.parse(open('src/zira_dashboard/templates/widgets/_widget_kpi.html', encoding='utf-8').read()); print('parse OK')"
```

Expected: all PASS. Registry has 7 types.

- [ ] **Step 7: Commit**

```
git add src/zira_dashboard/widget_types.py src/zira_dashboard/widget_data.py src/zira_dashboard/templates/_widget_render.html src/zira_dashboard/templates/widgets/_widget_kpi.html tests/test_widget_data.py
git commit -m "$(cat <<'EOF'
feat(widgets-p2): add kpi widget (single big number tile)

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 5: `downtime` widget (event list + total)

**Files:** same shape.

- [ ] **Step 1: Append registry entry**

```python
    {
        "type": "downtime",
        "label": "Downtime Report",
        "data_params_schema": [
            {"key": "wc_name", "label": "Work Center", "input": "select",
             "options_from": "wcs", "required": True},
        ],
        "visual_params_schema": [],
        "resolver": "_resolve_downtime",
        "partial": "widgets/_widget_downtime.html",
    },
```

- [ ] **Step 2: Append resolver**

```python
def _resolve_downtime(params: dict, day: date) -> dict:
    """Downtime report — list of gap events + total minutes.

    Wraps `wc_dashboard_data.downtime_report`. Returns the same shape:
    {events: [{time, duration_minutes}, ...], total_minutes}.
    """
    from . import wc_dashboard_data
    wc_name = (params or {}).get("wc_name")
    if not wc_name:
        return {"events": [], "total_minutes": 0}
    return wc_dashboard_data.downtime_report(wc_name, day) or {"events": [], "total_minutes": 0}
```

- [ ] **Step 3: Append resolver tests**

```python
def test_resolve_downtime_delegates(monkeypatch):
    from zira_dashboard import widget_data, wc_dashboard_data
    monkeypatch.setattr(
        wc_dashboard_data, "downtime_report",
        lambda wc, d: {
            "events": [{"time": "9:42a", "duration_minutes": 7}],
            "total_minutes": 17,
        } if wc == "Repair 1" else None,
    )
    out = widget_data._resolve_downtime({"wc_name": "Repair 1"}, day=date(2026, 5, 13))
    assert out["total_minutes"] == 17
    assert len(out["events"]) == 1
    assert out["events"][0]["duration_minutes"] == 7


def test_resolve_downtime_missing_wc_returns_empty():
    from zira_dashboard import widget_data
    out = widget_data._resolve_downtime({}, day=date(2026, 5, 13))
    assert out == {"events": [], "total_minutes": 0}
```

- [ ] **Step 4: Create partial `templates/widgets/_widget_downtime.html`**

```jinja
{# Downtime report — event list + header total.
   data = {events: [{time, duration_minutes}, ...], total_minutes}
#}
<div class="grid-stack-item-content">
  <h3>{{ placement_title or 'Downtime' }} · {{ data.total_minutes or 0 }}m total</h3>
  <ul class="downtime-list">
    {% for e in data.events %}
      <li>
        <span class="time">{{ e.time }}</span>
        <span class="duration">{{ e.duration_minutes }}m</span>
      </li>
    {% else %}
      <li class="empty">no downtime</li>
    {% endfor %}
  </ul>
</div>
```

- [ ] **Step 5: Update dispatcher**

Find:

```jinja
{% elif placement.type == 'ribbons' %}
  {% include "widgets/_widget_ribbons.html" %}
{% else %}
```

Replace with:

```jinja
{% elif placement.type == 'ribbons' %}
  {% include "widgets/_widget_ribbons.html" %}
{% elif placement.type == 'downtime' %}
  {% include "widgets/_widget_downtime.html" %}
{% else %}
```

- [ ] **Step 6: Run tests + parse**

```
.venv/Scripts/python.exe -m pytest tests/test_widget_types.py tests/test_widget_data.py -v 2>&1 | tail -10
.venv/Scripts/python.exe -c "from jinja2 import Environment, FileSystemLoader; env = Environment(loader=FileSystemLoader('src/zira_dashboard/templates'), autoescape=True); env.parse(open('src/zira_dashboard/templates/_widget_render.html', encoding='utf-8').read()); env.parse(open('src/zira_dashboard/templates/widgets/_widget_downtime.html', encoding='utf-8').read()); print('parse OK')"
```

Expected: all PASS. Registry has 8 types — Phase 2 widget set complete.

- [ ] **Step 7: Commit**

```
git add src/zira_dashboard/widget_types.py src/zira_dashboard/widget_data.py src/zira_dashboard/templates/_widget_render.html src/zira_dashboard/templates/widgets/_widget_downtime.html tests/test_widget_data.py
git commit -m "$(cat <<'EOF'
feat(widgets-p2): add downtime widget (event list + total)

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 6: CHANGELOG + push

**Files:**
- Modify: `CHANGELOG.md`

- [ ] **Step 1: Run the full test suite**

```
.venv/Scripts/python.exe -m pytest 2>&1 | tail -3
```

Expected: pass count grew by 12 (2 per widget × 5 widgets, plus the registry's partial-paths test still passing). No new failures.

- [ ] **Step 2: Get the current time**

```
powershell.exe -Command "Get-Date -Format 'h:mm tt'"
```

- [ ] **Step 3: Add CHANGELOG entry**

In `CHANGELOG.md`, insert a new `### <HH:MM TT>` block at the top of today's `## 2026-05-13` section:

```markdown
### <HH:MM TT>

- **Widget Workshop Phase 2 — 5 more widget types** — Pallets Banner (single-WC), Daily Progress (15-min color-coded bars), Cumulative Progress (cumulative SVG line chart with optional goal line), KPI Tile (units-today by WC or group, or downtime minutes), and Downtime Report (event list + total). All five join the Workshop alongside the Phase 1 trio (Pallets by WC, Vs Goat Pace, Monthly Ribbons), so any custom dashboard at `/dashboards/{slug}` can now drop in 8 different widget types. KPI metrics in Phase 2 are limited to today's units (per WC or group sum) and today's downtime minutes; more metrics can be added later by appending to the resolver's metric list. Phase 3 (TV Displays integration so a custom dashboard can be saved as a TV, plus per-placement data-override UI) still to ship.
```

- [ ] **Step 4: Commit + push**

```
git add CHANGELOG.md
git commit -m "$(cat <<'EOF'
docs(changelog): widget workshop phase 2 — 5 more types

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
git push origin main
```

Railway picks up the push. After deploy:
1. Visit `/widgets`. Create a "Repair 1 Banner" widget: type = Pallets Banner, WC = Repair 1.
2. Visit any dashboard from `/dashboards/{slug}`, click **Add** on the new palette entry — single-WC banner appears.
3. Repeat with `kpi` (metric = downtime_minutes_wc, WC = Repair 1), `daily_progress`, `cumulative`, `downtime` — all drop in cleanly.

---

## Done

Phase 2 ships. The workshop has 8 widget types total. Phase 3 remains: TV Displays integration (`tv_displays.kind = 'custom'` + cascading slug picker) so a custom dashboard can live in the TVs settings list, plus per-placement data-override UI (a per-widget "⋮" popover on the dashboard editor that PATCHes `data_overrides` without reload).
