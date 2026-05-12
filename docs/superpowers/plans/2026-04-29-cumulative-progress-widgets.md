# Cumulative Progress Widgets Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add cumulative-daily-progress widgets (one for All Dismantlers, one for All Repairs) to the Recycling and New VS dashboards, and clean up widget-title rendering so the title-box content is the only visible title on every widget.

**Architecture:** A new Jinja macro `cumulative_progress_chart` consumes the existing per-15-min progress buckets (no schema/route changes for Recycling), runs Jinja-side accumulators for cumulative totals, and renders bars + an SVG polyline target line + per-bar number labels. The widget title rename removes hardcoded `<span class="sub">` markup from every header and folds that text into the default title argument so user-renames replace the entire visible title. The New VS route is extended to compute the same per-bucket data using a simpler flat target function (sparse data — break-aware target machinery isn't needed there).

**Tech Stack:** FastAPI · Jinja2 · inline SVG (target line) · CSS grid (bars)

---

## File Structure

**Modified files:**
- `src/zira_dashboard/templates/recycling.html` — remove sub-spans from 5 widget headers, fold text into defaults; add `cumulative_progress_chart` macro + CSS; add 2 new widget blocks; bump downtime-report y to 26.
- `src/zira_dashboard/templates/new_vs.html` — remove sub-spans from 2 panel headers; duplicate the cumulative macro + CSS; add 2 new panel blocks (gated by data availability).
- `src/zira_dashboard/routes/value_streams.py` — extend the `new_vs` GET handler to compute `new_dism_progress`, `new_repair_progress`, `new_dism_group_target`, `new_repair_group_target` and pass them into the template context.

No new files. No tests — this is a pure presentation/UI change; smoke testing on deploy is the gate.

---

## Task 1: Widget title rename — fold sub spans into defaults

**Files:**
- Modify: `src/zira_dashboard/templates/recycling.html` — 5 widget headers
- Modify: `src/zira_dashboard/templates/new_vs.html` — 2 panel headers

- [ ] **Step 1: Recycling — fold each `<span class="sub">` into the title default**

Edit each `<h3>` block in `src/zira_dashboard/templates/recycling.html`. Five widgets to update — find each by searching for `<span class="sub">`:

Change:

```html
<h3>{{ widget_title('dismantler-bars', 'Pallets by Work Center') }}<span class="sub">— Dismantlers</span></h3>
```

to:

```html
<h3>{{ widget_title('dismantler-bars', 'Pallets by Work Center — Dismantlers') }}</h3>
```

Apply the same transformation to the other four:

| Widget ID | New default title argument |
|---|---|
| `dismantler-bars` | `Pallets by Work Center — Dismantlers` |
| `repair-bars` | `Pallets by Work Center — Repairs` |
| `dismantler-progress` | `All Dismantlers — 15-minute progress` |
| `repair-progress` | `All Repairs — 15-minute progress` |
| `downtime-report` | `Downtime Report — green = working, red = down (shift-scoped)` |

For each: remove the entire `<span class="sub">…</span>` element, append its inner text (with the leading em-dash + space preserved) to the default title argument of the surrounding `widget_title(...)` call.

- [ ] **Step 2: New VS — fold each `<span class="sub">` into the `<h3>` text**

Edit `src/zira_dashboard/templates/new_vs.html`. Two `<h3>` blocks. Change:

```html
<h3>Pallets by Work Center<span class="sub">— New value stream</span></h3>
```

to:

```html
<h3>Pallets by Work Center — New value stream</h3>
```

And:

```html
<h3>Downtime Report<span class="sub">— green = working, red = down (shift-scoped)</span></h3>
```

to:

```html
<h3>Downtime Report — green = working, red = down (shift-scoped)</h3>
```

- [ ] **Step 3: Verify all sub spans removed**

Run: `grep -rn '<span class="sub">' src/zira_dashboard/templates/recycling.html src/zira_dashboard/templates/new_vs.html` (or use the editor's search). Expected: zero matches.

- [ ] **Step 4: Smoke tests**

```bash
C:\Users\dale.gruber\Projects\zira\.venv\Scripts\python.exe -c "from zira_dashboard.app import app; print(app.title)"
```
Expected: `Zira Station Dashboard`, no traceback.

```bash
C:\Users\dale.gruber\Projects\zira\.venv\Scripts\python.exe -c "from zira_dashboard.deps import templates; templates.env.get_template('recycling.html'); templates.env.get_template('new_vs.html')"
```
Expected: no exception.

- [ ] **Step 5: Commit**

```bash
git add src/zira_dashboard/templates/recycling.html src/zira_dashboard/templates/new_vs.html
git commit -m "feat(dashboards): fold widget sub-titles into the title-box default"
```

---

## Task 2: Cumulative progress macro + CSS (in recycling.html)

**Files:**
- Modify: `src/zira_dashboard/templates/recycling.html` — add new macro after the existing `progress_chart` macro; add CSS rules in the existing `<style>` block

- [ ] **Step 1: Add the CSS rules**

Edit `src/zira_dashboard/templates/recycling.html`. Find the existing `<style>` block (search for `:root {` or `.progress {` to locate the dashboard-wide styles area). Append:

```css
/* ---- Cumulative progress widget ---- */
.cum-progress {
  display: flex;
  flex-direction: column;
  height: 100%;
  font-size: 0.78rem;
  color: var(--fg);
}
.cum-progress .legend {
  display: flex;
  gap: 1.25rem;
  align-items: center;
  justify-content: center;
  padding: 0 0 0.4rem 0;
  color: var(--muted);
  font-size: 0.72rem;
}
.cum-progress .legend .swatch-line {
  display: inline-block;
  width: 18px;
  height: 0;
  border-top: 1.5px solid var(--muted);
  margin-right: 4px;
  vertical-align: middle;
}
.cum-progress .legend .swatch-dot {
  display: inline-block;
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: var(--fg);
  margin-right: 4px;
  vertical-align: middle;
}
.cum-progress .plot {
  position: relative;
  flex: 1 1 auto;
  min-height: 80px;
  margin-top: 14px;            /* room for number labels */
  border-bottom: 1px solid var(--border);
}
.cum-progress .bars {
  display: grid;
  grid-template-columns: repeat(var(--n-cols, 24), 1fr);
  gap: 2px;
  height: 100%;
  align-items: end;
}
.cum-progress .col {
  position: relative;
  height: 100%;
  display: flex;
  align-items: end;
}
.cum-progress .bar {
  width: 100%;
  background: var(--good);
  border-radius: 2px 2px 0 0;
  min-height: 1px;
}
.cum-progress .col.miss .bar { background: var(--bad); }
.cum-progress .col.in-progress .bar { opacity: 0.7; }
.cum-progress .bar-label {
  position: absolute;
  bottom: calc(100% + 1px);
  left: 50%;
  transform: translateX(-50%);
  font-size: 0.6rem;
  font-weight: 500;
  font-variant-numeric: tabular-nums;
  color: var(--fg);
  white-space: nowrap;
  pointer-events: none;
}
.cum-progress .target-line {
  position: absolute;
  inset: 0;
  pointer-events: none;
  overflow: visible;
}
.cum-progress .target-line svg {
  width: 100%;
  height: 100%;
  overflow: visible;
}
.cum-progress .target-line polyline {
  fill: none;
  stroke: var(--muted);
  stroke-width: 1.5;
  stroke-linecap: round;
  stroke-linejoin: round;
  vector-effect: non-scaling-stroke;
}
.cum-progress .target-line .end-label {
  font-size: 9px;
  fill: var(--muted);
}
.cum-progress .x-ticks {
  display: grid;
  grid-template-columns: repeat(var(--n-cols, 24), 1fr);
  gap: 2px;
  margin-top: 4px;
  color: var(--muted);
  font-size: 0.65rem;
}
.cum-progress .x-ticks span {
  text-align: center;
  white-space: nowrap;
  overflow: hidden;
}
```

- [ ] **Step 2: Add the macro**

Find the existing `{% macro progress_chart(...) -%}` macro (around line 685 in recycling.html). Immediately AFTER its `{%- endmacro %}` (around line 721), insert the new macro:

```jinja
{% macro cumulative_progress_chart(buckets) -%}
  {# Running totals computed in-template via namespace accumulator. #}
  {% set acc = namespace(actual=[], target=[], a=0, t=0) %}
  {% for b in buckets %}
    {% set acc.a = acc.a + b.actual %}
    {% set acc.t = acc.t + b.target %}
    {% set _ = acc.actual.append(acc.a) %}
    {% set _ = acc.target.append(acc.t) %}
  {% endfor %}
  {% set max_a = acc.actual[-1] if acc.actual else 0 %}
  {% set max_t = acc.target[-1] if acc.target else 0 %}
  {% set scale = max_a if max_a > max_t else max_t %}
  {% if scale <= 0 %}{% set scale = 1 %}{% endif %}
  {% set n = buckets|length %}

  <div class="cum-progress" style="--n-cols: {{ n }}">
    <div class="legend">
      <span><span class="swatch-line"></span>Target</span>
      <span><span class="swatch-dot"></span>Pallets</span>
    </div>
    <div class="plot">
      <div class="bars">
        {% for b in buckets %}
          {% set i = loop.index0 %}
          {% set cum_a = acc.actual[i] %}
          {% set cum_t = acc.target[i] %}
          {% set hit = cum_a >= cum_t %}
          {% set h = (cum_a / scale * 100.0) if scale else 0 %}
          <div class="col {% if hit %}hit{% else %}miss{% endif %} {% if b.in_progress %}in-progress{% endif %}"
               title="{{ b.label }} · {{ '{:,}'.format(cum_a|int) }} cumulative (target {{ '{:,}'.format(cum_t|int) }})">
            <span class="bar-label">{{ '{:,}'.format(cum_a|int) }}</span>
            <div class="bar" style="height: {{ h }}%"></div>
          </div>
        {% endfor %}
      </div>
      <div class="target-line" aria-hidden="true">
        <svg viewBox="0 0 100 100" preserveAspectRatio="none">
          {% set pts = namespace(s='') %}
          {% for i in range(n) %}
            {% set x = ((i + 0.5) / n * 100.0) %}
            {% set y = (100.0 - (acc.target[i] / scale * 100.0)) %}
            {% set pts.s = pts.s + ('%.3f,%.3f ' % (x, y)) %}
          {% endfor %}
          <polyline points="{{ pts.s|trim }}"/>
          {% if n > 0 %}
            {% set last_x = ((n - 0.5) / n * 100.0) %}
            {% set last_y = (100.0 - (acc.target[n-1] / scale * 100.0)) %}
            <text class="end-label" x="{{ '%.2f'|format(last_x) }}" y="{{ '%.2f'|format(last_y - 1.5) }}" text-anchor="end">Target</text>
          {% endif %}
        </svg>
      </div>
    </div>
    <div class="x-ticks">
      {% for b in buckets %}
        {% if loop.index0 % 2 == 0 %}<span>{{ b.label }}</span>{% else %}<span></span>{% endif %}
      {% endfor %}
    </div>
  </div>
{%- endmacro %}
```

- [ ] **Step 3: Smoke tests**

```bash
C:\Users\dale.gruber\Projects\zira\.venv\Scripts\python.exe -c "from zira_dashboard.deps import templates; templates.env.get_template('recycling.html')"
```
Expected: no exception.

```bash
C:\Users\dale.gruber\Projects\zira\.venv\Scripts\python.exe -c "from zira_dashboard.app import app; print(app.title)"
```
Expected: `Zira Station Dashboard`.

(The macro isn't called yet in this task; just defined. The smoke test is mainly checking the template still parses.)

- [ ] **Step 4: Commit**

```bash
git add src/zira_dashboard/templates/recycling.html
git commit -m "feat(dashboards): add cumulative_progress_chart macro + CSS"
```

---

## Task 3: Add the two new cumulative widgets to recycling.html

**Files:**
- Modify: `src/zira_dashboard/templates/recycling.html` — insert two new `grid-stack-item` blocks; bump downtime-report's y from 16 to 26

- [ ] **Step 1: Insert dismantler-cumulative widget**

Find the `repair-progress` widget block in `src/zira_dashboard/templates/recycling.html` (around line 737, the block with `widget_attrs('repair-progress', 0, 11, 12, 5)`). Immediately AFTER its closing `</div>` (the outer `grid-stack-item` div), insert:

```html
    <div class="grid-stack-item" {{ widget_attrs('dismantler-cumulative', 0, 16, 12, 5) }}>
      <div class="grid-stack-item-content" {% set pc = customs.get('dismantler-cumulative', {}).get('color') %}{% if pc %}style="--good: {{ pc }}"{% endif %}>
        {{ edit_controls('dismantler-cumulative', 'All Dismantlers — Daily Progress', 'progress') }}
        <h3>{{ widget_title('dismantler-cumulative', 'All Dismantlers — Daily Progress') }}</h3>
        <div class="widget-body">
          {% if dismantler_progress %}
            {{ cumulative_progress_chart(dismantler_progress) }}
          {% else %}
            <div style="color:var(--muted);font-size:0.85rem">No shift data for this day.</div>
          {% endif %}
        </div>
      </div>
    </div>

    <div class="grid-stack-item" {{ widget_attrs('repair-cumulative', 0, 21, 12, 5) }}>
      <div class="grid-stack-item-content" {% set pc = customs.get('repair-cumulative', {}).get('color') %}{% if pc %}style="--good: {{ pc }}"{% endif %}>
        {{ edit_controls('repair-cumulative', 'All Repairs — Daily Progress', 'progress') }}
        <h3>{{ widget_title('repair-cumulative', 'All Repairs — Daily Progress') }}</h3>
        <div class="widget-body">
          {% if repair_progress %}
            {{ cumulative_progress_chart(repair_progress) }}
          {% else %}
            <div style="color:var(--muted);font-size:0.85rem">No shift data for this day.</div>
          {% endif %}
        </div>
      </div>
    </div>
```

- [ ] **Step 2: Bump downtime-report y from 16 to 26**

Find the `downtime-report` widget block (around line 751, the block with `widget_attrs('downtime-report', 0, 16, 12, 4)`). Change `0, 16, 12, 4` to `0, 26, 12, 4`:

Before:

```html
<div class="grid-stack-item" {{ widget_attrs('downtime-report', 0, 16, 12, 4) }}>
```

After:

```html
<div class="grid-stack-item" {{ widget_attrs('downtime-report', 0, 26, 12, 4) }}>
```

- [ ] **Step 3: Check for any other widgets with y >= 16**

Search recycling.html for `widget_attrs(...` calls with a y >= 16 (e.g., `widget_attrs('xyz', 0, 17, ...`). For each, bump y by 10 to maintain the original visual ordering. (If `downtime-report` is the only one at or below y=16, no other changes needed — verify by grepping.)

Quick check command:
```bash
grep -nE "widget_attrs\([^)]*, [0-9]+, ([1-9][0-9]+|[2-9][0-9])," src/zira_dashboard/templates/recycling.html
```

For any widget with y in 16+ besides downtime-report, add 10 to its y.

- [ ] **Step 4: Smoke tests**

```bash
C:\Users\dale.gruber\Projects\zira\.venv\Scripts\python.exe -c "from zira_dashboard.deps import templates; templates.env.get_template('recycling.html')"
```
Expected: no exception.

```bash
C:\Users\dale.gruber\Projects\zira\.venv\Scripts\python.exe -c "from zira_dashboard.app import app; print(app.title)"
```
Expected: `Zira Station Dashboard`.

If you can run the dev server with a live DATABASE_URL, also open `/recycling` and verify the two new cumulative widgets render (or show "No shift data for this day" if the day has no buckets).

- [ ] **Step 5: Commit**

```bash
git add src/zira_dashboard/templates/recycling.html
git commit -m "feat(dashboards): add cumulative All Dismantlers + All Repairs widgets to Recycling"
```

---

## Task 4: Extend new_vs route with progress buckets

**Files:**
- Modify: `src/zira_dashboard/routes/value_streams.py` — `new_vs` handler

The Recycling route's `_make_target_fn` is a closure over locals (`people_by_wc`, `productive_by_wc`, `grace_end_local`) that the New VS route doesn't compute. For New VS we use a simpler flat per-bucket target — sufficient for sparse data, accurate enough that the cumulative line is approximately linear (matches the screenshot shape).

- [ ] **Step 1: Add the progress-bucket computation**

In `src/zira_dashboard/routes/value_streams.py`, find the `new_vs` handler (around line 304). Locate the line where `bars` is finished being assigned (`bars.sort(key=lambda x: -x["units"])` near line 383). After that line and before the `downtime_rows = []` line (around line 385), insert:

```python
    # ---- Per-bucket dismantler / repair progress (cumulative widgets) ----
    # New VS has sparse metering, so we use a flat target function instead of
    # the full break-aware machinery the Recycling route uses. Each 15-min
    # bucket gets a target of (sum of group hourly targets) * (bucket_min/60).
    new_dismantlers = [r for r in results if r.station.category == "Dismantler"]
    new_repairs    = [r for r in results if r.station.category == "Repair"]

    def _flat_target_fn(group):
        def fn(b_start_local, b_end_local):
            bucket_min = (b_end_local - b_start_local).total_seconds() / 60.0
            total_hourly = sum(settings_store.station_target(r.station) for r in group)
            return total_hourly * bucket_min / 60.0
        return fn

    new_dism_progress = (
        progress_buckets(new_dismantlers, d, now, target_fn=_flat_target_fn(new_dismantlers))
        if new_dismantlers else []
    )
    new_repair_progress = (
        progress_buckets(new_repairs, d, now, target_fn=_flat_target_fn(new_repairs))
        if new_repairs else []
    )

    elapsed_hours_for_avg = (elapsed / 60.0) if elapsed else 0.0
    def _flat_group_goal(rows):
        if not rows:
            return 0.0
        return sum(settings_store.station_target(r.station) for r in rows)
    new_dism_group_target = _flat_group_goal(new_dismantlers)
    new_repair_group_target = _flat_group_goal(new_repairs)
```

- [ ] **Step 2: Pass into template context**

Find the `templates.TemplateResponse(... "new_vs.html", { ... })` call (around line 398). Inside the context dict, add four new keys (alongside the existing ones like `"bars"`, `"downtime_rows"`, etc.):

```python
            "has_dismantlers": bool(new_dismantlers),
            "has_repairs": bool(new_repairs),
            "new_dism_progress": new_dism_progress,
            "new_repair_progress": new_repair_progress,
            "new_dism_group_target": new_dism_group_target,
            "new_repair_group_target": new_repair_group_target,
```

- [ ] **Step 3: Smoke test the route still imports + handler is callable**

```bash
C:\Users\dale.gruber\Projects\zira\.venv\Scripts\python.exe -c "from zira_dashboard.app import app; print(app.title)"
```
Expected: `Zira Station Dashboard`, no traceback.

- [ ] **Step 4: Commit**

```bash
git add src/zira_dashboard/routes/value_streams.py
git commit -m "feat(new-vs): compute per-bucket dismantler + repair progress for cumulative widgets"
```

---

## Task 5: Cumulative widgets on new_vs.html

**Files:**
- Modify: `src/zira_dashboard/templates/new_vs.html` — add the cumulative macro + CSS; add 2 new panel blocks

- [ ] **Step 1: Add CSS rules**

Edit `src/zira_dashboard/templates/new_vs.html`. Find the existing `<style>` block in the head. Before its closing `</style>`, append the same CSS block from Task 2 Step 1 (paste verbatim — the rules use only generic CSS variables and class names, identical to the Recycling version):

```css
/* ---- Cumulative progress widget ---- */
.cum-progress {
  display: flex;
  flex-direction: column;
  height: 100%;
  font-size: 0.78rem;
  color: var(--fg);
}
.cum-progress .legend {
  display: flex;
  gap: 1.25rem;
  align-items: center;
  justify-content: center;
  padding: 0 0 0.4rem 0;
  color: var(--muted);
  font-size: 0.72rem;
}
.cum-progress .legend .swatch-line {
  display: inline-block;
  width: 18px;
  height: 0;
  border-top: 1.5px solid var(--muted);
  margin-right: 4px;
  vertical-align: middle;
}
.cum-progress .legend .swatch-dot {
  display: inline-block;
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: var(--fg);
  margin-right: 4px;
  vertical-align: middle;
}
.cum-progress .plot {
  position: relative;
  flex: 1 1 auto;
  min-height: 220px;
  margin-top: 14px;
  border-bottom: 1px solid var(--border);
}
.cum-progress .bars {
  display: grid;
  grid-template-columns: repeat(var(--n-cols, 24), 1fr);
  gap: 2px;
  height: 100%;
  align-items: end;
}
.cum-progress .col {
  position: relative;
  height: 100%;
  display: flex;
  align-items: end;
}
.cum-progress .bar {
  width: 100%;
  background: var(--good);
  border-radius: 2px 2px 0 0;
  min-height: 1px;
}
.cum-progress .col.miss .bar { background: var(--bad); }
.cum-progress .col.in-progress .bar { opacity: 0.7; }
.cum-progress .bar-label {
  position: absolute;
  bottom: calc(100% + 1px);
  left: 50%;
  transform: translateX(-50%);
  font-size: 0.6rem;
  font-weight: 500;
  font-variant-numeric: tabular-nums;
  color: var(--fg);
  white-space: nowrap;
  pointer-events: none;
}
.cum-progress .target-line {
  position: absolute;
  inset: 0;
  pointer-events: none;
  overflow: visible;
}
.cum-progress .target-line svg {
  width: 100%;
  height: 100%;
  overflow: visible;
}
.cum-progress .target-line polyline {
  fill: none;
  stroke: var(--muted);
  stroke-width: 1.5;
  stroke-linecap: round;
  stroke-linejoin: round;
  vector-effect: non-scaling-stroke;
}
.cum-progress .target-line .end-label {
  font-size: 9px;
  fill: var(--muted);
}
.cum-progress .x-ticks {
  display: grid;
  grid-template-columns: repeat(var(--n-cols, 24), 1fr);
  gap: 2px;
  margin-top: 4px;
  color: var(--muted);
  font-size: 0.65rem;
}
.cum-progress .x-ticks span {
  text-align: center;
  white-space: nowrap;
  overflow: hidden;
}
```

(The `min-height: 220px` on `.plot` differs from the Recycling version's `80px` — New VS panels aren't grid-stack-resizable, so we set a fixed comfortable plot height directly.)

- [ ] **Step 2: Add the macro**

Find the very top of `<body>` in `src/zira_dashboard/templates/new_vs.html`. Insert the macro definition there, immediately after the opening `<body>` tag (or anywhere in the body before its first use):

```jinja
{% macro cumulative_progress_chart(buckets) -%}
  {% set acc = namespace(actual=[], target=[], a=0, t=0) %}
  {% for b in buckets %}
    {% set acc.a = acc.a + b.actual %}
    {% set acc.t = acc.t + b.target %}
    {% set _ = acc.actual.append(acc.a) %}
    {% set _ = acc.target.append(acc.t) %}
  {% endfor %}
  {% set max_a = acc.actual[-1] if acc.actual else 0 %}
  {% set max_t = acc.target[-1] if acc.target else 0 %}
  {% set scale = max_a if max_a > max_t else max_t %}
  {% if scale <= 0 %}{% set scale = 1 %}{% endif %}
  {% set n = buckets|length %}

  <div class="cum-progress" style="--n-cols: {{ n }}">
    <div class="legend">
      <span><span class="swatch-line"></span>Target</span>
      <span><span class="swatch-dot"></span>Pallets</span>
    </div>
    <div class="plot">
      <div class="bars">
        {% for b in buckets %}
          {% set i = loop.index0 %}
          {% set cum_a = acc.actual[i] %}
          {% set cum_t = acc.target[i] %}
          {% set hit = cum_a >= cum_t %}
          {% set h = (cum_a / scale * 100.0) if scale else 0 %}
          <div class="col {% if hit %}hit{% else %}miss{% endif %} {% if b.in_progress %}in-progress{% endif %}"
               title="{{ b.label }} · {{ '{:,}'.format(cum_a|int) }} cumulative (target {{ '{:,}'.format(cum_t|int) }})">
            <span class="bar-label">{{ '{:,}'.format(cum_a|int) }}</span>
            <div class="bar" style="height: {{ h }}%"></div>
          </div>
        {% endfor %}
      </div>
      <div class="target-line" aria-hidden="true">
        <svg viewBox="0 0 100 100" preserveAspectRatio="none">
          {% set pts = namespace(s='') %}
          {% for i in range(n) %}
            {% set x = ((i + 0.5) / n * 100.0) %}
            {% set y = (100.0 - (acc.target[i] / scale * 100.0)) %}
            {% set pts.s = pts.s + ('%.3f,%.3f ' % (x, y)) %}
          {% endfor %}
          <polyline points="{{ pts.s|trim }}"/>
          {% if n > 0 %}
            {% set last_x = ((n - 0.5) / n * 100.0) %}
            {% set last_y = (100.0 - (acc.target[n-1] / scale * 100.0)) %}
            <text class="end-label" x="{{ '%.2f'|format(last_x) }}" y="{{ '%.2f'|format(last_y - 1.5) }}" text-anchor="end">Target</text>
          {% endif %}
        </svg>
      </div>
    </div>
    <div class="x-ticks">
      {% for b in buckets %}
        {% if loop.index0 % 2 == 0 %}<span>{{ b.label }}</span>{% else %}<span></span>{% endif %}
      {% endfor %}
    </div>
  </div>
{%- endmacro %}
```

- [ ] **Step 3: Add the two new panel blocks**

Find the existing Downtime Report panel (`<div class="panel">` block ending around line 160 of the original new_vs.html). Immediately after its closing `</div>`, BEFORE the `<div class="footer">` line, insert:

```jinja
  {% if has_dismantlers %}
  <div class="panel">
    <h3>All Dismantlers — Daily Progress</h3>
    {% if new_dism_progress %}
      {{ cumulative_progress_chart(new_dism_progress) }}
    {% else %}
      <div class="empty-state">No shift data for this day.</div>
    {% endif %}
  </div>
  {% endif %}

  {% if has_repairs %}
  <div class="panel">
    <h3>All Repairs — Daily Progress</h3>
    {% if new_repair_progress %}
      {{ cumulative_progress_chart(new_repair_progress) }}
    {% else %}
      <div class="empty-state">No shift data for this day.</div>
    {% endif %}
  </div>
  {% endif %}
```

The `{% if has_dismantlers %}` / `{% if has_repairs %}` outer guards hide the entire panel when the New VS has zero metered stations of that category — avoids visual clutter on a sparse dashboard. The inner `{% if new_dism_progress %}` shows the empty-state message when stations exist but the day has no shift data yet (per the spec's distinction between the two cases).

- [ ] **Step 4: Smoke tests**

```bash
C:\Users\dale.gruber\Projects\zira\.venv\Scripts\python.exe -c "from zira_dashboard.deps import templates; templates.env.get_template('new_vs.html')"
```
Expected: no exception.

```bash
C:\Users\dale.gruber\Projects\zira\.venv\Scripts\python.exe -c "from zira_dashboard.app import app; print(app.title)"
```
Expected: `Zira Station Dashboard`.

If you can run the dev server: open `/new-vs` and verify the new panels render (or are hidden if there are no metered dismantler/repair stations on the New VS).

- [ ] **Step 5: Commit**

```bash
git add src/zira_dashboard/templates/new_vs.html
git commit -m "feat(new-vs): add cumulative All Dismantlers + All Repairs panels"
```

---

## Task 6: Manual smoke test + verification

This task is operator-only (push + visual verification on production).

- [ ] **Step 1: Push**

```bash
git push
```

Wait for Railway to redeploy.

- [ ] **Step 2: Visual checks on `/recycling`**

Open the Recycling dashboard. Verify:
- 5 widget headers no longer have a separate sub-title; the title-box content (default or user-renamed) is the entire visible heading.
- Two new widgets appear below the existing `dismantler-progress` + `repair-progress` widgets, in this order:
  1. All Dismantlers — Daily Progress (cumulative)
  2. All Repairs — Daily Progress (cumulative)
- Cumulative bars rise monotonically left-to-right, except where the in-progress bucket is still climbing (lighter opacity).
- Continuous gray "Target" line traces the cumulative goal across the chart, with `Target` text label near the right end.
- Each bar shows the cumulative actual count above it (e.g., `74`, `152`, `1,073`, `2,109`).
- Bars at-or-above target are green (`var(--good)`); below-target bars are red (`var(--bad)`).
- Downtime Report widget moved down accordingly (no longer overlaps the new widgets).

- [ ] **Step 3: Visual checks on `/new-vs`**

Open the New VS dashboard. Verify:
- 2 panel headers no longer have a separate sub-title; the title is one cohesive line.
- If the New VS has metered dismantler stations: an "All Dismantlers — Daily Progress" panel appears at the bottom. Otherwise: no panel (hidden).
- Same for repair stations: panel renders if stations exist, hidden if none.
- Same visual properties as the Recycling cumulative widgets (bars, target line, color-grading, number labels).

- [ ] **Step 4: Rename smoke test**

On `/recycling`, click the edit-controls (pencil/gear icon) on any widget. Rename it to a custom value (e.g., "Test Rename"). Save. The widget should now show "Test Rename" — and ONLY "Test Rename" — as its heading. No trailing sub-text. Restore the original name when done.

- [ ] **Step 5: Done**

If all visual checks pass, the feature is shipped. If any look broken, follow up with a targeted fix.
