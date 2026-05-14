# Operator Dashboard + Workshop Tear-Down

**Date:** 2026-05-14
**Status:** Draft — pending user review
**Goal:** Pull the widget workshop + custom dashboards experiment out of the system and replace it with a single Operator dashboard sub-tab. The Operator dashboard mirrors `/recycling`'s visual style but is scoped to a single work center, picked via a dropdown at the top of the page. TV view works the same way.

## Problem

The widget workshop / custom-dashboard direction (built across multiple ships earlier this week) hasn't worked out. The visual treatment never matched `/recycling`, the data-shape mismatches caused repeated visual regressions, and the abstraction (widget types + resolvers + partials + dispatcher) carried too much complexity for what's effectively four built-in views.

The actual need is simpler: an "operator" dashboard that looks exactly like `/recycling` but is scoped to one work center, with a dropdown to switch which WC's data it pulls from. That dashboard becomes a TV view the same way the existing VS dashboards do.

## Strategy

Tear out the workshop + custom-dashboard scaffolding entirely. Keep the four built-in dashboards (`/recycling`, `/new-vs`, the operator dashboard at `/wc/{slug}`, `/work-centers`). Sub-nav becomes a fixed four-tab strip. The existing `/wc/{slug}` route + template is rewritten to mirror `/recycling`'s layout with widgets scoped to one WC.

The simplification is the design. No widget registry, no resolver framework, no dispatcher partial — just inline HTML using the same CSS classes `/recycling` defines.

## Components

### Tear-out

**Routes removed:** `/widgets`, `/dashboards`, `/dashboards/{slug}`, `/tv/dashboards/{slug}`, `/api/widget-defs*`, `/api/widgets/*`, `/api/dashboards*`, `/api/placements/*`, `/api/tv-templates*`, `/api/pinned-dashboards`.

**Python modules removed:**
- `src/zira_dashboard/widget_types.py`
- `src/zira_dashboard/widget_data.py`
- `src/zira_dashboard/widget_definitions_store.py`
- `src/zira_dashboard/custom_dashboards_store.py`
- `src/zira_dashboard/tv_templates_store.py`
- `src/zira_dashboard/pinned_dashboards_store.py`
- `src/zira_dashboard/dashboard_catalog.py`
- `src/zira_dashboard/routes/widgets.py`
- `src/zira_dashboard/routes/custom_dashboards.py`
- `src/zira_dashboard/routes/tv_templates.py`

**Templates removed:**
- `templates/widgets.html`
- `templates/dashboards.html`
- `templates/custom_dashboard.html`
- All eight `templates/widgets/_widget_*.html` partials
- `templates/_widget_render.html`
- The current `templates/_dashboards_subnav.html` (replaced — see below)

**Static assets removed:**
- `static/dashboards-subnav.css` (replaced — see below)

**Tests removed:**
- `tests/test_widget_types.py`
- `tests/test_widget_data.py`
- `tests/test_widget_definitions_store.py`
- `tests/test_widgets_routes.py`
- `tests/test_custom_dashboards_store.py`
- `tests/test_custom_dashboards_routes.py`
- `tests/test_pinned_dashboards_store.py`
- `tests/test_dashboard_catalog.py`
- `tests/test_tv_templates_store.py`
- `tests/test_tv_templates_routes.py`

**App boot cleanup (`app.py`):** drop the `tv_displays`, `widgets`, `custom_dashboards`, `tv_templates` route-module imports that no longer exist; keep `tv_displays` (the registry, separate from the deleted dashboard routes); drop `pinned_dashboards_store.seed_defaults_if_empty()` and `widget_definitions_store.seed_defaults_if_empty()` calls from the lifespan.

**DB schema migrations (idempotent, in `_SCHEMA_DDL` as `DROP TABLE IF EXISTS`):**
```sql
DROP TABLE IF EXISTS dashboard_widgets;
DROP TABLE IF EXISTS custom_dashboards;
DROP TABLE IF EXISTS widget_definitions;
DROP TABLE IF EXISTS tv_dashboard_templates;
DROP TABLE IF EXISTS pinned_dashboards;
DELETE FROM tv_displays WHERE kind = 'custom';
```

`DROP TABLE` must come before tables that reference them via FK — `dashboard_widgets` first (references both `custom_dashboards` and `widget_definitions`).

**Kept:**
- `tv_displays` table + store + routes — the TV registry is still useful. Settings → TVs panel kept; the `custom` kind option in the picker is removed; existing custom-kind rows are deleted on this deploy.
- All `wc_dashboard_data` helpers (`pallets_banner`, `fifteen_min_increments`, `daily_progress`, `goat_race`, `monthly_ribbons`, `downtime_report`, `assigned_operators_for_wc`).
- `awards.goat`, `awards.monthly_badges`.
- `_cumulative_progress_chart.html` (shared macro used by `/recycling`, `/new-vs`, and the operator dashboard).

### Operator dashboard

The existing `/wc/{slug}` and `/tv/wc/{slug}` routes become the Operator dashboard. The template gets a rewrite to mirror `/recycling`'s layout exactly, with widgets scoped to a single WC.

**Layout (12-column gridstack, top to bottom):**

1. **WC picker bar** (screen mode only — hidden in TV mode). A `<select>` with every entry in `staffing.LOCATIONS`. Change → navigate to `/wc/{slug}` for the picked WC.
2. **KPI tiles row** (12 × 2 in grid units) — four tiles side-by-side:
   - Units today (today's pallet count for this WC)
   - Up Time % (`(elapsed - downtime) / elapsed`)
   - Downtime (total minutes today)
   - Pallets/hr (`units / hours_elapsed`, 1-decimal)
3. **Pallets banner** (12 × 2) — big number (today's units), goal-so-far + full-day goal, progress bar showing pct.
4. **15-min progress chart** (12 × 5) — same `progress_chart` markup as `/recycling`, scoped to this WC. Bars are green when hit, red when miss; in-progress bar highlighted; target tick on each bar.
5. **Cumulative Daily Progress** (12 × 5) — uses the `cumulative_progress_chart` macro from `_cumulative_progress_chart.html` (shared with `/recycling`).
6. **Downtime stacked bar** (12 × 3) — single `bar-row` with `.stacked-track` (working green + down red), label = WC name, val = down minutes.
7. **Vs. GOAT Pace** (12 × 4) — status pill (`AHEAD` / `ON_PACE` / `BEHIND` / `no record yet`) + race stats (today's units, GOAT pace now, GOAT meta line with name/units/date). Scoped to this WC's group (`work_centers_store.groups(loc)[0]`).
8. **Monthly Ribbons** (12 × 4) — 🥇🥈🥉 top-3 person-days for the group this month. Hyperlinked names → person card. Scoped to this WC's group.

All widget markup is inline in `wc_dashboard.html` (no workshop partials). Each widget is a `<div class="grid-stack-item">` with `gs-id` + `gs-x/y/w/h` so gridstack can still drag/resize and the saved layout persists in `widget_layouts.page = 'wc:{slug}'` (same key the existing autosave uses).

**Data prep (in `_render_wc_dashboard`):** uses existing `wc_dashboard_data` helpers — `pallets_banner`, `fifteen_min_increments`, `daily_progress`, `downtime_report`, `goat_race`, `monthly_ribbons`. KPI tile values computed via a small new helper `wc_dashboard_data.kpi_tiles(wc_name, day)` that returns `{units_today, up_time_pct, downtime_minutes, pallets_per_hour, hours_elapsed}` from the same Zira data the other widgets use.

**WC picker behavior:** the picker shows `loc.name` for every entry in `staffing.LOCATIONS`. On `change`, JS sets `location.href = "/wc/" + slug_for_wc(value)`. The picker is wrapped in `{% if not tv_mode %}` so TV mode doesn't show it. Active option auto-selects the current WC via `selected` on the matching `<option>`.

### `/operator` redirect

A new route in `routes/wc_dashboard.py`:

```python
@router.get("/operator")
def operator_default():
    """Entry point for the Operator dashboard sub-tab.

    Redirects to the first work center's /wc/{slug} URL. Order is
    staffing.LOCATIONS order — usually alphabetical by name.
    """
    from .. import staffing
    from ..wc_dashboard_data import slug_for_wc
    if not staffing.LOCATIONS:
        return JSONResponse(
            {"error": "no work centers configured — set them up in Settings"},
            status_code=404,
        )
    first = staffing.LOCATIONS[0]
    return RedirectResponse(url=f"/wc/{slug_for_wc(first.name)}", status_code=302)
```

The "Operator" sub-nav tab points at `/operator`. Once on a per-WC page, the picker handles WC switching.

### New `_dashboards_subnav.html`

Drop the pinning logic. Fixed 4-tab strip:

```jinja
{# Dashboards sub-nav. Fixed 4-tab strip — no pinning, no workshop.
   Context required:
     active_dashboard_key — one of 'vs_recycling', 'vs_new',
                            'vs_work_centers', or a string starting with
                            'wc:' (any WC = Operator tab active).
#}
<nav class="dash-subnav">
  <a href="/recycling"
     class="subnav-item {% if active_dashboard_key == 'vs_recycling' %}active{% endif %}">
    Recycling VS
  </a>
  <a href="/new-vs"
     class="subnav-item {% if active_dashboard_key == 'vs_new' %}active{% endif %}">
    New VS
  </a>
  <a href="/operator"
     class="subnav-item {% if active_dashboard_key and active_dashboard_key.startswith('wc:') %}active{% endif %}">
    Operator
  </a>
  <a href="/work-centers"
     class="subnav-item {% if active_dashboard_key == 'vs_work_centers' %}active{% endif %}">
    Work Centers
  </a>
</nav>
```

The `active_dashboard_key` context value is already set by every page (`vs_recycling`, `vs_new`, `vs_work_centers`, `wc:<name>`). On the `/wc/{slug}` page, the key starts with `wc:` so the Operator tab is highlighted regardless of which WC.

**CSS:** keep the same look as today's sub-nav. `static/dashboards-subnav.css` is rewritten with a simpler ruleset — no `.pinned-tabs` / `.meta-tabs` split, just a flex row of `.subnav-item` tabs.

### Page-template cleanups

- **`index.html` / `recycling.html` / `new_vs.html` / `_staffing_base.html` / `settings.html`:** drop the `<a href="/dashboards">My Dashboards</a>` link from the top nav (every page).
- **`settings.html`:** drop any leftover sidebar links to `/widgets` or `/dashboards`.
- **`wc_dashboard.html`:** complete rewrite of the body (see Operator dashboard above).

### Settings → TVs panel

Stays. The flat-picker UI keeps working with three optgroups removed of `custom`:

- **Built-in:** Recycling VS, New VS, Work Centers, plus every entry in `staffing.LOCATIONS`.
- ~~**Custom:**~~ optgroup removed.

`routes/settings.py` drops the `dashboard_catalog` import; the picker options are built inline from `staffing.LOCATIONS` + the three hard-coded vs entries. `_settings_tvs.html` drops the "Custom" optgroup. `routes/tv_displays.py` drops the `kind='custom'` branch in `/tv/d/{slug}`. The `kind` CHECK on `tv_displays` keeps `'custom'` in its allowed set (no migration to tighten — harmless leftover).

## Data flow

**Visiting `/wc/Repair 1`:**
1. `_render_wc_dashboard(slug='repair-1')` looks up the WC via `wc_by_slug`.
2. Calls each data helper for this WC: `pallets_banner`, `fifteen_min_increments`, `daily_progress`, `downtime_report`, `goat_race`, `monthly_ribbons`, `kpi_tiles` (new), `assigned_operators_for_wc`.
3. Renders `wc_dashboard.html` with `active_dashboard_key='wc:Repair 1'` so the sub-nav highlights "Operator".

**Switching WCs via the picker:**
1. User selects "Repair 2" from the dropdown.
2. JS: `location.href = '/wc/repair-2'`.
3. Full page load with the new slug.

**Visiting `/operator`:**
1. Redirect to `/wc/{first WC slug}`.

**Visiting `/tv/wc/Repair 1`:**
1. Same handler with `tv_mode=True`.
2. Template renders the TV header + same widgets, hides the picker bar + sub-nav.

## Edge cases

- **No WCs configured:** `/operator` returns a 404 with a "set them up in Settings" message. `/wc/{slug}` already returns 404 if the slug doesn't match a WC.
- **WC removed after a TV display references it:** existing tv_displays handling already covers this (the route shows a "work center removed" page).
- **Custom-kind tv_displays rows after the deploy:** deleted by the migration. Anyone who bookmarked a `/tv/d/{slug}` for a custom row gets a 404.
- **Saved widget_layouts for a `wc:{slug}` page (pre-rewrite):** the new template uses the same gridstack widget IDs (`wc-kpi-row`, `wc-pallets-banner`, etc.). Existing layout rows that reference the OLD widget IDs (`wc-daily-progress`, `wc-goat-race`, etc.) won't match any new IDs; gridstack will fall back to the `gs-y` defaults in the template. Acceptable — the layout reset is the price of the visual overhaul.
- **Dashboard routes deleted with active sessions:** anyone with `/widgets` or `/dashboards` open during the deploy gets a 404 on next interaction. Acceptable.

## Testing

- **Route smoke:** `/operator` redirects to the first WC's `/wc/{slug}`; `/wc/{slug}` renders 200 with the new widget set; `/tv/wc/{slug}` renders 200 with TV chrome stripped and the picker hidden.
- **Sub-nav active state:** rendering any of the 4 dashboard pages produces a sub-nav with the right tab highlighted.
- **KPI tile helper:** `kpi_tiles(wc_name, day)` returns the expected dict shape; division-by-zero (no elapsed time on a weekend morning) returns `0` rather than raising.
- **No regressions:** `/recycling`, `/new-vs`, `/work-centers` keep rendering correctly. `/tv/recycling`, `/tv/new-vs`, `/tv/wc/{slug}` keep working.
- **Settings TVs panel:** flat picker renders with Built-in only, no Custom optgroup; existing rows render with their target auto-selected.

## Out of scope

- **Restoring any custom dashboards Dale built.** None of the custom dashboards are recoverable post-tear-out (table is dropped). Acceptable given how few were created and how broken they were.
- **Saving per-WC widget layouts.** Gridstack still autosaves to `widget_layouts.page='wc:{slug}'` so users can rearrange the operator dashboard; layout persists per WC. Cross-WC layout templates are NOT brought back.
- **Replacement for the workshop preview feature.** Gone. Building new widgets is no longer something the user does.

## Rollout

One deploy. Big single-commit push (or two: tear-out then operator rebuild). After deploy:
- Top nav: Dashboards · Trophy Case · Staffing · Settings (unchanged).
- Dashboards sub-nav: Recycling VS · New VS · Operator · Work Centers (fixed 4 tabs).
- `/widgets`, `/dashboards`, `/dashboards/*` all 404.
- `/wc/{slug}` renders the new operator layout.
- `/operator` redirects to the first WC.
- `/tv/wc/{slug}` keeps working in TV mode.

Code base shrinks meaningfully: ~30 files removed, 5 DB tables dropped, the widget framework abstraction is gone. Maintenance surface is back to a manageable size.
