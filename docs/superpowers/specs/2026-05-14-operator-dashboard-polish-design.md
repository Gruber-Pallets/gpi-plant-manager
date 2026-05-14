# Operator Dashboard Polish — Design

**Date:** 2026-05-14
**Scope:** Iterate on the operator dashboard (`/wc/{slug}` and `/tv/wc/{slug}`) shipped earlier today. Split the KPI row into resizable widgets, add the per-widget edit panel and layout auto-save that `/recycling` already has, make the layout + customizations shared across every WC, scale GOAT/Ribbons text with widget size via CSS container queries, surface scheduled operator names in a page subtitle band, and rebuild the Pallets banner with the same start/now axis ticks `/recycling` uses on its bar rows.

---

## Goal

One sentence: bring `/wc/{slug}` up to feature parity with `/recycling` (resizable per-tile KPIs, auto-saving layout, per-widget edit panels, container-query scaling) while keeping every operator dashboard structurally identical — customize once, applies to every WC.

## Architecture

**One shared layout + customization key.** Today `_render_wc_dashboard` uses `layout_key = f"wc:{slug}"` (one row in `widget_layouts` per WC). This design switches every operator dashboard to a single key, `"operator"` — a single row in `widget_layouts` and a single set of rows in `widget_customizations` covering every WC. Customizing `kpi-units` on Repair 1's dashboard immediately affects every other WC's view.

**Why shared:** the operator dashboards are structurally identical (same widget IDs, same layout shape, just different WC-scoped data). The user's "make it the new default" intent reads naturally as "customize once, applies everywhere." Container queries handle the visual differences (text scales with widget size, not with the WC).

**TV view stays read-only.** `/tv/wc/{slug}` reads the same `"operator"` layout + customizations but renders no edit chrome — no edit-bar, no `⋮` buttons, no subtitle band. The TV view already has its own header (`_tv_header`) that shows the operator name on the right.

**Endpoints unchanged.** Everything reuses the existing `POST /api/layout/{page}` and `POST/DELETE /api/widget/{page}/{widget_id}` routes with `page="operator"`. No new tables, no new endpoints. The widget IDs are operator-namespaced so they don't accidentally collide with recycling's customizations even though the `page` key already separates them.

## Components

### Widget IDs (stable, shared across all WCs)

| ID | What it renders | Default `gs-w` | Default `gs-h` |
|---|---|---|---|
| `kpi-units` | Units today (number) | 3 | 2 |
| `kpi-uptime` | Up Time % | 3 | 2 |
| `kpi-downtime` | Downtime minutes | 3 | 2 |
| `kpi-pph` | Pallets / hr | 3 | 2 |
| `pallets-banner` | Big units, fill bar, start + now ticks | 12 | 2 |
| `progress-15min` | 15-min progress chart | 12 | 5 |
| `cumulative-daily` | Cumulative daily line | 12 | 5 |
| `downtime-row` | Single stacked working/down bar | 12 | 3 |
| `goat-race` | Vs. GOAT Pace (group of this WC) | 12 | 4 |
| `monthly-ribbons` | This month's top 3 in group | 12 | 4 |

### Operator name band

A non-grid header band rendered just under the WC picker, above `.grid-stack`. Screen mode only — gated by `{% if not tv_mode %}`.

```
┌──────────────────────────────────────────────────────────┐
│  Repair 1                                                │
│  👤 Dale Smith · Bob Lee                                 │
└──────────────────────────────────────────────────────────┘
```

- WC name uses dashboard h1-weight (~28 px).
- Operator names below at ~18 px, joined by ` · `, fed by the existing `operators_display` value already built in `_render_wc_dashboard`.
- When `assigned_operators_for_wc(wc_name, today)` returns `[]`: shows `(unassigned)` in `var(--muted)` italic — matches the TV header's wording so the two views read consistently.
- Not a widget, not draggable, not editable.

### Per-widget edit panel

The `⋮` button on each widget opens an inline panel mirroring `/recycling`'s `edit_controls` macro. Backed by the existing `POST /api/widget/operator/{widget_id}` endpoint and the `widget_customizer.save_one` validator.

- **KPI tiles** (`kpi-*`) expose: `title`, `color`, `align`.
- **Chart widgets** (`progress-15min`, `cumulative-daily`, `goat-race`, `monthly-ribbons`, `downtime-row`): `title`, `color`, plus `show_legend`/`show_target` where they apply.
- **Pallets banner**: `title`, `color`.

To avoid drift between operator and recycling, the `edit_controls` Jinja macro moves into a shared partial `_widget_edit_controls.html`. `recycling.html` is updated to `{% from "_widget_edit_controls.html" import edit_controls %}` and the inline macro deleted; `wc_dashboard.html` imports the same partial. The macro's behavior is identical to today's — no field changes.

Since one shared `page="operator"` key serves all WCs, custom widget titles must be WC-agnostic. Default titles for the operator dashboard:

- `pallets-banner` → `"Today · Pallets"`
- `progress-15min` → `"15-minute progress"`
- `cumulative-daily` → `"Daily progress"`
- `downtime-row` → `"Downtime · green = working, red = down"`
- `goat-race` → `"Vs. GOAT Pace"` (group name still appended dynamically inside the body, not the title)
- `monthly-ribbons` → `"Monthly Ribbons"` (month/year still appended inside the body)
- `kpi-units` → `"Units today"`
- `kpi-uptime` → `"Up Time"`
- `kpi-downtime` → `"Downtime"`
- `kpi-pph` → `"Pallets / hr"`

The WC scoping happens in the operator name band, not the per-widget titles.

### Auto-save layout

Copy `/recycling`'s edit-bar (the "Drag / resize — layout auto-saves" indicator + a Reset Layout button) into the operator dashboard, just above `.grid-stack`. Screen mode only.

```html
<div class="edit-bar">
  <span class="save-indicator" id="save-indicator">Drag / resize — layout auto-saves</span>
  <button type="button" id="reset-layout">Reset Layout</button>
</div>
```

JS behavior, copied verbatim from `recycling.html`:

- `grid.on('change' | 'resizestop' | 'dragstop', persistLayout)` → `POST /api/layout/operator` with the serialized layout.
- On 200: indicator text changes to `"Saved"` for 1500 ms, then back to the default.
- On non-200: `"Save failed"`. On network error: `"Save failed (network)"`.
- Reset Layout button → `POST /api/layout/operator` with `[]`, then `location.reload()` — the hard-coded defaults take over.

GridStack init options match `/recycling`:

```js
const grid = GridStack.init({
  column: 12,
  cellHeight: 60,        // recycling uses 60; today's operator uses 80
  margin: 8,
  float: false,
  handle: '.grid-stack-item-content > h3, .grid-stack-item-content > .label',
});
```

The `handle:` constraint restricts dragging to the widget header — clicking inside the edit panel or on a chart bar will not initiate a drag.

### Pallets banner with start/now ticks

Today the banner shows `units / target_today goal so far (target_full_day full day)` plus a fill bar. This design extends it to mirror `/recycling`'s bar-row axis layout.

```
Today · Pallets
┌────────────────────────────────────────────────────────┐
│ 124   / 240 full day                                   │
│ ████████████████░│░░░░░░░░░░░░░░░░░░░░░░░░             │
│ start · 06:00    now · 11:18                           │
└────────────────────────────────────────────────────────┘
```

- Fill bar width = `units_today / target_full_day * 100`, capped at 100 %.
- Vertical tick (`│`) at `target_today / target_full_day * 100` — the "where we should be right now" marker.
- Axis row below the bar with `start · HH:MM` at 0 % and `now · HH:MM` at the same prorated %, reusing recycling's `.axis-row` / `.axis-tick` / `.axis-track` markup and CSS.
- New helpers added to the render context:
  - `shift_start_label`: `shift_config.shift_start_local(today).strftime("%H:%M")`.
  - `now_label`: `datetime.now(shift_config.SITE_TZ).strftime("%H:%M")` when `today == local today`, else `""` (and the now tick hides on past days).
- The big number stays — that's the bit operators see across the plant.

### Container-query scaling (GOAT Pace + Monthly Ribbons + KPI val + banner number)

Enable `container-type: inline-size` on `.grid-stack-item-content`. Each widget then has its own container, and child font sizes use `clamp(min, Xcqw, max)` so they scale with widget width:

```css
.grid-stack-item-content { container-type: inline-size; }

/* KPI numbers */
.wc-dashboard .kpi .val   { font-size: clamp(1.8rem, 8cqw, 4rem); }
.wc-dashboard .kpi .label { font-size: clamp(0.75rem, 2cqw, 1rem); }

/* Pallets banner big number */
.wc-dashboard .pallets-banner .units  { font-size: clamp(2rem, 9cqw, 5rem); }
.wc-dashboard .pallets-banner .target { font-size: clamp(0.85rem, 2.4cqw, 1.3rem); }

/* GOAT race */
.wc-dashboard .goat-race .race-stats  { font-size: clamp(0.95rem, 4cqw, 2.2rem); }
.wc-dashboard .goat-race .status-pill {
  font-size: clamp(0.85rem, 3cqw, 1.6rem);
  padding: clamp(2px, 0.6cqw, 8px) clamp(6px, 1.2cqw, 14px);
}
.wc-dashboard .goat-race .goat-meta   { font-size: clamp(0.8rem, 2.4cqw, 1.3rem); }

/* Monthly ribbons */
.wc-dashboard .ribbons-list li      { font-size: clamp(0.95rem, 3.5cqw, 2rem); gap: clamp(4px, 1cqw, 14px); }
.wc-dashboard .ribbons-list .medal  { font-size: clamp(1.2rem, 5cqw, 3rem); }
.wc-dashboard .ribbons-list .units  { font-size: clamp(1rem, 4cqw, 2.4rem); }
```

`cqw` = 1 % of the widget's own width. Floors keep small widgets readable; ceilings keep full-width widgets from looking cartoonish. The 15-min progress chart, cumulative line, and downtime stacked bar are already percentage-driven so no extra work is needed beyond enabling `container-type`.

### KPI text color (theme-aware) — operator only

```css
.wc-dashboard .kpi .val                              { color: #000; }
html[data-tv-theme="dark"] .wc-dashboard .kpi .val   { color: #fff; }
```

Black on light theme, white on dark theme. Theme is signaled by the `data-tv-theme` attribute that `_tv_header` already sets on `<html>` in TV mode (`"dark"` or `"light"`); screen mode has no attribute and renders light, so it falls through to the black default.

Scoped via a `wc-dashboard` class added to `<body>` on the operator template only (`<body class="wc-dashboard">` in both `/wc/{slug}` and `/tv/wc/{slug}` renders). `/recycling`'s KPI tiles are untouched. The per-widget `color` customization still wins because `widget_color_style` writes inline `style="color: …"` which has higher specificity than either rule above.

## Data flow

```
GET /wc/{slug}
  └─> _render_wc_dashboard
        ├─ wc_dashboard_data.assigned_operators_for_wc(wc_name, today)   ──> operator band content
        ├─ wc_dashboard_data.kpi_tiles(wc_name, today)                   ──> 4 KPI widgets
        ├─ wc_dashboard_data.pallets_banner(wc_name, today)              ──> pallets-banner widget
        ├─ wc_dashboard_data.fifteen_min_progress_buckets(...)           ──> progress-15min + cumulative-daily
        ├─ wc_dashboard_data.downtime_report(...)                        ──> downtime-row
        ├─ wc_dashboard_data.goat_race(...)                              ──> goat-race
        ├─ wc_dashboard_data.monthly_ribbons(...)                        ──> monthly-ribbons
        ├─ shift_config.shift_start_local / now in SITE_TZ                ──> banner axis ticks
        ├─ layout_store.layout_map("operator")                            ──> per-widget positions
        └─ widget_customizer.load_all("operator")                         ──> per-widget customizations
```

Customizer fetches are TTL-cached (30 s, already in place in `widget_customizer`). No new caching needed.

```
POST /api/layout/operator           [from gridstack change/resizestop/dragstop]
  └─> layout_store.save("operator", items)   ──> widget_layouts row UPSERT

POST /api/widget/operator/kpi-units [from edit panel Save]
  └─> widget_customizer.save_one("operator", "kpi-units", payload)
        └─> widget_customizations row UPSERT (or DELETE if payload empty after validation)

DELETE /api/widget/operator/kpi-units [from edit panel Reset]
  └─> widget_customizer.reset_one("operator", "kpi-units")
        └─> widget_customizations row DELETE
```

## Schema cleanup

The earlier per-WC keys (`page = 'wc:{slug}'`) become orphaned once the operator dashboard switches to `"operator"`. They're harmless but clutter. The schema bootstrap gets a final idempotent cleanup statement:

```sql
DELETE FROM widget_layouts        WHERE page LIKE 'wc:%';
DELETE FROM widget_customizations WHERE page LIKE 'wc:%';
```

Safe to re-run on every boot — once empty, it's a no-op.

## Error handling

| Scenario | Behavior |
|---|---|
| `assigned_operators_for_wc` returns `[]` | Operator band shows `(unassigned)` in muted italic. |
| `assigned_operators_for_wc` raises | Returned as `[]` (the helper already swallows exceptions). Band reads `(unassigned)`. |
| `POST /api/layout/operator` 4xx/5xx | `save-indicator` shows `"Save failed"`. Layout state still lives in gridstack client-side — next drag/resize retries. |
| `POST /api/layout/operator` network error | `save-indicator` shows `"Save failed (network)"`. |
| `POST /api/widget/operator/{id}` invalid field | `widget_customizer.save_one` ignores unknown keys; valid-but-empty payload deletes the row (effectively a reset). |
| `widget_customizer` cache stale after save | `save_one` / `reset_one` already call `_CACHE.invalidate(page)`. |
| Container queries unsupported (legacy browser) | `clamp(min, Xcqw, max)` collapses to `min` — widgets render at the floor size, still readable. |
| Banner now-tick on a past day | `now_label` is empty; the now-tick element is suppressed via `{% if now_label %}`. |

## Testing

| Test | File | Asserts |
|---|---|---|
| `test_operator_layout_key_is_shared` | `tests/test_wc_dashboard_routes.py` | Render `/wc/repair-1` and `/wc/dismantler-2` — both reads call `layout_store.load("operator")`, never `layout_store.load("wc:repair-1")`. |
| `test_operator_dashboard_uses_widget_customizer` | same file | After `widget_customizer.save_one("operator", "kpi-units", {"title": "Pallets Done"})`, every WC render contains `"Pallets Done"` in the rendered HTML. |
| `test_operator_dashboard_renders_operator_band` | same file | With `assigned_operators_for_wc` returning `["Dale", "Bob"]`, HTML contains `Dale · Bob` in a `.operator-band` block. |
| `test_operator_dashboard_unassigned_band` | same file | When operators is `[]`, the band shows `(unassigned)`. |
| `test_operator_dashboard_split_kpi_widgets` | same file | Rendered HTML contains four separate `gs-id="kpi-units"`, `kpi-uptime`, `kpi-downtime`, `kpi-pph` grid-stack-items (regex on the gs-id attribute). |
| `test_operator_dashboard_edit_controls_present` | same file | Each widget has a `.widget-edit-btn` button and a `.widget-edit[hidden]` panel. |
| `test_tv_wc_dashboard_omits_edit_chrome` | same file | `/tv/wc/repair-1` HTML does not contain `widget-edit-btn`, `edit-bar`, or `operator-band` markers. |
| `test_pallets_banner_has_axis_ticks` | same file | Rendered HTML contains `start ·` and `now ·` substrings inside the pallets banner. |
| `test_bootstrap_drops_legacy_wc_layouts` | `tests/test_db.py` | After `db.bootstrap_schema()`, no rows exist in `widget_layouts` or `widget_customizations` with `page LIKE 'wc:%'`. |

CSS-only behaviors (container query scaling, black KPI color) are visual — verify in a browser by resizing widgets at the plant TV viewport, no automated test.

## Out of scope (YAGNI)

- Per-WC overrides on the shared layout (Section 1 Q1 option C). Adding overrides means extra schema and a merge layer at render time. If you later find one WC needs a different layout we can revisit.
- Cross-page customization (sharing customizations between `/recycling` and `/wc/{slug}`). The page namespaces stay separate.
- Editing the operator band content from the UI. The band shows whatever the Plant Scheduler says; if you want to change assignments, do it in the scheduler.
- Mobile/responsive sweep for the operator dashboard. The dashboard is a plant-TV / desktop target; the container-query scaling already handles a wide range of widget sizes.
