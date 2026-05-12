# Cumulative Progress Widgets — Design

**Date:** 2026-04-29
**Status:** Approved (brainstorming → implementation planning)

## Context

The Recycling VS dashboard has two existing widgets — `dismantler-progress`
and `repair-progress` — that render per-15-minute production as a bar
chart with a per-bucket target tick. Each bar represents that single
15-minute interval's pallets count. Bars rise and fall throughout the
shift.

Dale wants two NEW widgets, one for All Dismantlers and one for All
Repairs, that show the **cumulative daily progress** — bars represent a
running total of pallets from shift start, climbing monotonically through
the day. Each bar has a goal at that point (cumulative target so far),
and is colored green when the cumulative actual is at-or-above the
cumulative goal, red when below. A continuous "Target" line traces the
cumulative goal across the day.

Reference: `Pictures/Screenshots/Screenshot 2026-04-29 144245.png`. Bars
labeled with cumulative pallets count; target line climbs near-linearly
from 0 at shift start to ~2k by 2 PM; bars color green/red against that
line.

The new widgets go on **both** the Recycling VS and New VS dashboards.

## Goals

1. New widget `dismantler-cumulative` ("All Dismantlers — Daily Progress")
   showing cumulative dismantler-group production per 15-minute interval.
2. New widget `repair-cumulative` ("All Repairs — Daily Progress")
   showing cumulative repair-group production per 15-minute interval.
3. Both widgets render a continuous goal line (cumulative target across
   the day) and color each bar green/red based on actual vs target.
4. Place both new widgets on the Recycling VS dashboard, immediately
   below the existing per-bucket dismantler-progress + repair-progress
   widgets.
5. Place both new widgets on the New VS dashboard, after the existing
   Pallets by Work Center + Downtime Report panels.
6. **All dashboard widget titles are fully renamable** — whatever the
   user enters in the edit-controls title box becomes the only visible
   title on the widget. Remove the hardcoded `<span class="sub">…</span>`
   secondary-title text from every widget header; fold the sub text
   into the default title string so the widget still reads sensibly
   without renaming.

## Non-goals

- Replacing or removing the existing per-bucket widgets — they stay.
- A separate page or route for the cumulative view — these are widgets
  on the existing dashboards.
- Backend cumulative computation — running totals are computed in the
  Jinja macro from the existing per-bucket data, no route changes for
  data shape.
- New downtime / breaks logic. The existing `progress_buckets` already
  emits zero-target buckets during breaks; cumulative target plateaus
  through breaks accordingly.
- Cross-dashboard config sharing. The widgets are independent on each
  dashboard (own widget IDs, own edit controls on Recycling).
- Per-station drill-down on hover. Bars show group-level cumulative
  totals only.

## Design

### Widget title behavior (applies to ALL widgets)

Today, each widget renders its title with two parts:

```html
<h3>{{ widget_title('dismantler-progress', 'All Dismantlers') }}<span class="sub">— 15-minute progress</span></h3>
```

`widget_title()` returns either the user's custom title (saved in
`widget_customizations.json`) or the default fallback. The
`<span class="sub">...</span>` is hardcoded HTML appended after, so a
user who renames the widget to "Dismantler Output" still sees the
trailing "— 15-minute progress" sub text. That's the bug Dale reported:
the title box content isn't the full title.

**Fix:** Remove every `<span class="sub">` from widget headers in
`recycling.html` and `new_vs.html`. Roll the sub text into the
**default title** argument passed to `widget_title()`. After the
change, every widget's `<h3>` is simply:

```html
<h3>{{ widget_title('dismantler-progress', 'All Dismantlers — 15-minute progress') }}</h3>
```

Mapping (Recycling, configurable widgets):

| Widget ID | Old default | New default |
|---|---|---|
| `dismantler-bars` | `Pallets by Work Center` | `Pallets by Work Center — Dismantlers` |
| `repair-bars` | `Pallets by Work Center` | `Pallets by Work Center — Repairs` |
| `dismantler-progress` | `All Dismantlers` | `All Dismantlers — 15-minute progress` |
| `repair-progress` | `All Repairs` | `All Repairs — 15-minute progress` |
| `downtime-report` | `Downtime Report` | `Downtime Report — green = working, red = down (shift-scoped)` |

For New VS (static `<div class="panel">` blocks, not configurable
widgets), apply the same visual treatment: remove the `<span class="sub">`
markup, fold the text into the `<h3>` directly. This isn't "renaming"
in the configurable sense — it's just removing the dual-title visual
inconsistency. Mapping:

| Panel | Old | New |
|---|---|---|
| Pallets by Work Center | `Pallets by Work Center<span>— New value stream</span>` | `Pallets by Work Center — New value stream` |
| Downtime Report | `Downtime Report<span>— green = working, red = down (shift-scoped)</span>` | `Downtime Report — green = working, red = down (shift-scoped)` |

The CSS for `.sub` (smaller, muted styling) can stay defined for now;
no other code uses it. Removing it is a follow-up cleanup, not part of
this change.

The two **new** cumulative widgets follow the new pattern from day one:
defaults are `All Dismantlers — Daily Progress` and `All Repairs — Daily
Progress`, no sub-span.

User-customized titles already saved in `widget_customizations.json`
are unaffected — `widget_title()` still returns them when present.
Only the **default** changes. A user who never renamed a widget will
see the new combined default; a user who already renamed it sees their
saved name unchanged.

### Data

The Recycling VS route (`routes/value_streams.py`, `recycling_page`
handler) already computes:

- `dismantler_progress` — list of `{actual, target, in_progress, label}`
  buckets covering the shift (15-min granularity).
- `repair_progress` — same shape, for repair stations.
- `dismantler_group_target` and `repair_group_target` — per-hour
  group goals (numeric).

These flow into the existing `progress_chart` macro. The new cumulative
macro consumes the same buckets (no schema changes); it computes
running sums on the fly.

The New VS route (`new_vs` handler) currently does **not** compute
progress buckets. We extend it to:

```python
new_dismantlers = [r for r in results if r.station.category == "Dismantler"]
new_repairs    = [r for r in results if r.station.category == "Repair"]
new_dism_progress = progress_buckets(new_dismantlers, d, now, target_fn=_make_target_fn(new_dismantlers))
new_repair_progress = progress_buckets(new_repairs, d, now, target_fn=_make_target_fn(new_repairs))
new_dism_group_target = _group_goal(new_dismantlers)
new_repair_group_target = _group_goal(new_repairs)
```

Same helpers (`progress_buckets`, `_make_target_fn`, `_group_goal`) the
Recycling route already uses. If a group has zero metered stations, the
template renders an empty-state message (or hides the panel entirely —
see Rendering below).

### Rendering — new Jinja macro

A new macro `cumulative_progress_chart(buckets, group_target_per_hour, widget_id='')`
lives in `recycling.html` next to the existing `progress_chart` macro
(and is duplicated/copied into `new_vs.html` since the two templates
don't share macros today).

The macro:

1. Iterates `buckets` once to build `cum_actual` and `cum_target`
   running totals per index. Done in Jinja with a `namespace` accumulator
   (matches the existing macro's pattern).
2. Computes `max_cum = max(cum_actual.last, cum_target.last)` for height
   scaling.
3. Renders a column per bucket. Each column has:
   - A `.bar` with `height: cum_actual_at_i / max_cum * 100%`.
   - A class of `hit`, `miss`, or `in-progress` based on
     `cum_actual >= cum_target` (and `b.in_progress` flag).
   - A `.bar-label` text node above the bar showing the cumulative
     actual (formatted with thousands separator: `1,073`).
4. Renders an SVG overlay polyline tracing the cumulative-target points
   (one (x, y) per bucket). Stroke `var(--muted)` (or a near-gray),
   1.5px, `stroke-linecap: round`. Plain text label `Target` near the
   right end of the line.
5. Renders the existing-style x-axis ticks at every-other bucket
   (`07:00 AM`, `08:00 AM`, etc.).
6. Renders a small legend at the bottom — `Target` line swatch +
   `Pallets` filled square — matching the screenshot.

Pseudocode:

```jinja
{% macro cumulative_progress_chart(buckets, group_target_per_hour, widget_id='') -%}
  {% set acc = namespace(actual=[], target=[], a=0, t=0) %}
  {% for b in buckets %}
    {% set acc.a = acc.a + b.actual %}
    {% set acc.t = acc.t + b.target %}
    {% set _ = acc.actual.append(acc.a) %}
    {% set _ = acc.target.append(acc.t) %}
  {% endfor %}
  {% set max_cum = [acc.actual[-1] if acc.actual else 0,
                    acc.target[-1] if acc.target else 0]|max %}
  {% set scale = max_cum if max_cum > 0 else 1 %}
  <div class="cum-progress">
    {# bars + svg overlay + x-ticks + legend #}
    ...
  </div>
{%- endmacro %}
```

The CSS reuses `.col`, `.bar`, `.hit`, `.miss`, `.in-progress` and adds
new rules for `.cum-progress`, `.bar-label`, and the SVG overlay
positioning.

### CSS additions

```css
.cum-progress { /* same shell as .progress */ }
.cum-progress .plot { position: relative; }
.cum-progress .target-line {
  position: absolute;
  inset: 0;
  pointer-events: none;
  overflow: visible;
}
.cum-progress .target-line svg {
  width: 100%;
  height: 100%;
}
.cum-progress .target-line polyline {
  fill: none;
  stroke: var(--muted, #8b949e);
  stroke-width: 1.5;
  stroke-linecap: round;
  stroke-linejoin: round;
}
.cum-progress .target-line .end-label {
  font-size: 0.7rem;
  fill: var(--muted, #8b949e);
}
.cum-progress .bar-label {
  position: absolute;
  bottom: calc(100% + 2px);
  left: 50%;
  transform: translateX(-50%);
  font-size: 0.65rem;
  font-weight: 500;
  font-variant-numeric: tabular-nums;
  color: var(--fg, #e6edf3);
  white-space: nowrap;
  pointer-events: none;
}
```

### Recycling layout

Current Recycling grid-stack layout (y, h):

| Widget | y | h |
|---|---|---|
| KPI tiles row | 0 | 2 |
| dismantler-bars | 2 | 4 |
| repair-bars | 2 | 4 |
| dismantler-progress | 6 | 5 |
| repair-progress | 11 | 5 |
| downtime-report | 16 | 4 |
| (further widgets) | 20+ | ... |

New layout (insert two new widgets, push downtime-report and below):

| Widget | y | h |
|---|---|---|
| KPI tiles row | 0 | 2 |
| dismantler-bars | 2 | 4 |
| repair-bars | 2 | 4 |
| dismantler-progress | 6 | 5 |
| repair-progress | 11 | 5 |
| **dismantler-cumulative** | **16** | **5** |
| **repair-cumulative** | **21** | **5** |
| downtime-report | 26 | 4 |
| (further widgets) | 30+ | ... |

Widget IDs: `dismantler-cumulative`, `repair-cumulative`. Both get the
standard `widget_attrs`, `widget_title`, `widget_color_style`,
`edit_controls`, etc. — first-class widgets, fully customizable.

The widget-customization config (`widget_customizations.json`) doesn't
need updates; new widgets get default config on first render.

`widget_layouts.json` stores per-user layouts keyed by widget ID. New
widget IDs will not appear in any saved layout, so the existing
`widget_attrs` macro must default to the spec'd `(x, y, w, h)` from
the template when no layout entry exists. Implementer must verify
this default-behavior path works for the new IDs; if it doesn't, the
fix is a small addition to whatever code reads `widget_layouts.json`.

### New VS layout

Append two new `<div class="panel">` blocks after the existing Pallets
by Work Center + Downtime Report panels:

```html
<div class="panel">
  <h3>All Dismantlers<span class="sub">— Daily Progress</span></h3>
  {% if new_dism_progress and new_dismantlers %}
    {{ cumulative_progress_chart(new_dism_progress, new_dism_group_target) }}
  {% else %}
    <div class="empty-state">No metered dismantler stations on the New value stream.</div>
  {% endif %}
</div>

<div class="panel">
  <h3>All Repairs<span class="sub">— Daily Progress</span></h3>
  {% if new_repair_progress and new_repairs %}
    {{ cumulative_progress_chart(new_repair_progress, new_repair_group_target) }}
  {% else %}
    <div class="empty-state">No metered repair stations on the New value stream.</div>
  {% endif %}
</div>
```

The macro is duplicated into `new_vs.html` since the two templates
don't share macros today. Future cleanup could extract it to a
`_progress_macros.html` partial; not in scope here.

### Color rule

Per bucket index `i`:

```
hit      = cum_actual[i] >= cum_target[i]   (green)
miss     = cum_actual[i] <  cum_target[i]   (red)
in_progress = bucket.in_progress
```

If `in_progress`, the bar gets a class to lower opacity to ~0.7 so it
reads as "still climbing." Color (green/red) still applies based on
the same hit/miss rule — supervisors see at a glance whether the
in-flight interval is on pace.

### Empty / no-data states

- Recycling: existing `dismantler_progress` is empty when there's no
  shift data. Macro check is the same `{% if buckets %}` pattern as
  the existing widget — falls back to "No shift data for this day."
- New VS: if a group has zero metered stations, **hide** the panel
  entirely (avoid visual clutter on a sparse dashboard). If stations
  exist but the day has no shift data yet (e.g., before 7:00 AM,
  weekend), **show** the panel with the empty-state message
  "No shift data for this day." The two conditions are distinct:
  one is "feature not configured for this VS" (hide), the other is
  "no data for this day" (empty-state).

## Acceptance criteria

- Recycling VS dashboard renders four progress widgets (per-bucket
  dismantler + repair, then cumulative dismantler + repair) in that
  vertical order.
- Cumulative widgets show one bar per 15-minute bucket, height
  representing cumulative actual.
- Each bar is colored green (cum_actual ≥ cum_target) or red (below).
- A continuous gray "Target" line traces the cumulative goal across
  the chart, lining up with each bar's bottom-relative goal point.
- Each bar shows the cumulative actual count above it as a small text
  label (e.g., `1,073`).
- Numbers are formatted with thousands separators where appropriate.
- New VS dashboard renders the two cumulative panels at the bottom
  of the page; missing data falls back to the empty-state message.
- Both new widgets on Recycling are first-class grid-stack widgets:
  drag-reorder, custom title, custom color via the existing edit
  controls.

## Risks

- **Goal line drawing precision.** SVG polyline coordinates depend on
  bucket positions matching CSS-grid bar positions. Misalignment by a
  few pixels would make the line look slightly off the bar tops.
  Mitigation: position the SVG inside `.plot` (the bar grid container),
  use percentage-based x coordinates aligned to bar centers, and
  height-scale matching the bars. Plain unit-test risk is low; visual
  smoke test on deploy is the gate.
- **Width on small widget heights.** A grid-stack widget can be resized.
  Cumulative widgets at h=5 have plenty of room; if a user resizes to
  h=2 the number labels will overlap. Acceptable — the existing
  per-bucket widget has the same constraint, and Dale doesn't typically
  shrink these.
- **New VS sparse data.** Most New VS stations aren't metered yet, so
  the cumulative widgets there will frequently render the empty-state.
  Acceptable per Q1's "A" answer — Dale's goal is to have the widgets
  in place for the day they ramp up.
- **Macro duplication.** The macro lives in both `recycling.html` and
  `new_vs.html` because the templates don't share macros today.
  Acceptable trade-off; future refactor could extract to a partial.
- **Cumulative target through breaks.** The existing per-bucket data
  has `b.target = 0` during break intervals, so the cumulative target
  line plateaus during breaks (a flat segment). This matches the
  screenshot's apparent flat segment around 11:00–11:30. Confirmed
  visually.

## File touch list

- Modified: `src/zira_dashboard/routes/value_streams.py` — extend
  `new_vs` handler to compute `new_dism_progress`, `new_repair_progress`,
  `new_dism_group_target`, `new_repair_group_target`; pass into context.
- Modified: `src/zira_dashboard/templates/recycling.html`:
  - Remove `<span class="sub">…</span>` from each widget's `<h3>` (5
    widgets); fold the sub text into the default title argument.
  - Add the `cumulative_progress_chart` macro.
  - Add the two new widget blocks at y=16 and y=21; bump
    `downtime-report` y to 26.
- Modified: `src/zira_dashboard/templates/new_vs.html`:
  - Remove `<span class="sub">…</span>` from the two static panels;
    fold the sub text into the `<h3>` directly.
  - Duplicate the `cumulative_progress_chart` macro.
  - Add the two new panel blocks after the Downtime Report panel.
- Modified: shared CSS in both templates — add `.cum-progress`,
  `.target-line`, `.bar-label` rules (duplicated where needed).
- New (optional, follow-up): `tests/test_cumulative_macro.py` — render
  the macro with a fixture-buckets list and assert key elements appear
  in the output (e.g., correct number of bars, target line points
  match cumulative target). Not strictly required if smoke-test on
  deploy is acceptable.
