# TV Dashboards

**Date:** 2026-05-13
**Status:** Draft — pending user review
**Goal:** Permanent-URL, read-only dashboards designed to live on TVs throughout the plant. Two flavors out of the gate: existing Value-Stream dashboards (Recycling, New VS) stripped down for TV mode, and a brand-new per-work-center dashboard so each WC's TV shows its own production. A Settings panel lists every configured TV with its bookmarkable URL and lets you arrange the per-WC widgets.

## Problem

`/recycling` and `/new-vs` are designed for a manager glancing at a laptop — they carry the top nav, the range chips, the sub-nav, and per-widget edit buttons. None of that belongs on a TV mounted on a wall. There's also no per-work-center view at all today; an operator at Repair 1 has nowhere to look up to see how they're doing in real time against their target.

This spec adds three pieces:

1. **TV-mode variants** of the Recycling and New VS dashboards — same data, chrome stripped, font sizes scaled up for viewing from across a bay, auto-refresh kept.
2. **A new per-WC dashboard** assembled from six gridstack widgets (pallets-by-WC banner, daily progress, GOAT race, monthly ribbons, 15-min increments, downtime). The dashboard has two routes: an editor view (drag/resize/edit widgets, save layout) and a TV view (renders the saved layout, read-only).
3. **A Settings → TV Displays panel** that lists every configured TV, generates the bookmarkable URL, and lets Dale add/remove entries.

## Strategy

New `/tv/*` route subtree carries all read-only TV variants. The page handlers reuse the existing template logic with a `tv_mode=True` context flag that:

- Hides the top nav, sub-nav, range chips, edit buttons, "add" buttons, and any other interactive UI
- Bumps font sizes via a TV-mode CSS file loaded only when the flag is set
- Adds a dashboard-name header (top-left) and operator-name header (top-right, per-WC dashboards only)
- Keeps the existing auto-refresh JS

The editor view (`/wc/{wc_slug}`) reuses the same template without the flag — gridstack edit handles + save button + per-widget config visible.

Widget layouts persist in the existing `widget_layouts` table, keyed by dashboard kind. Per-WC dashboards get one layout key per WC (e.g., `wc:repair-1`) so each can be arranged independently.

## Components

### Route map

| Route | Purpose | View mode |
|---|---|---|
| `/tv/recycling` | Recycling VS dashboard on TV | read-only, stripped, large |
| `/tv/new-vs` | New VS dashboard on TV | read-only, stripped, large |
| `/wc/{wc_slug}` | Per-WC dashboard editor | editable (gridstack) |
| `/tv/wc/{wc_slug}` | Per-WC dashboard on TV | read-only, stripped, large |
| `/settings/tv-displays` (a sub-section of `/settings`) | Configure the list of TVs | editable |
| `POST /api/tv-displays` | Add/edit/remove a TV entry | API |
| `POST /api/widget-layout/{dashboard_key}` | Save gridstack layout for a dashboard (extends existing API) | API |

`wc_slug` is a URL-safe version of the WC name: lowercased, spaces and special chars → hyphens. Generated once when the WC is added to settings and stored alongside the WC name. Reverse-lookup on the request to find the canonical name.

### TV mode flag

A context boolean `tv_mode` threaded through the templates. Driven by the route — `/tv/*` routes set it True. Templates check it to:

- Conditionally render `<header class="app">` (skipped when tv_mode)
- Conditionally render the range chips / sub-nav / edit buttons (skipped when tv_mode)
- Conditionally include `static/tv-mode.css` (extra rules: big fonts, no hover affordances, hide buttons)

`static/tv-mode.css` is opt-in via a `<link>` tag conditioned on `tv_mode`. Keeps the screen-mode page bytes unchanged.

### Per-WC dashboard widgets

Six widgets, all built fresh in `templates/wc_dashboard.html`:

| Widget | Backed by | Notes |
|---|---|---|
| **Header** (always-on, not a gridstack widget) | static | WC name (top-left, breadcrumb above), operators (top-right, big), both rendered through existing `goat_badges` + `cert_badges` macros |
| **Pallets banner** | Today's Zira meter data for this WC, prorated against `leaderboard_wc_settings.expected_units_per_day` | Single-bar horizontal progress, same visual language as the existing VS "Pallets by Work Center" widget but scoped to one WC |
| **Daily progress chart** | Cumulative per-15-min meter readings | SVG area chart, "120 goal" dashed line, time on x-axis from shift-start to shift-end |
| **GOAT race widget** | `awards.goat(group_of_this_wc)` + today's elapsed-minutes-prorated GOAT pace | Status pill (ON PACE / BEHIND / AHEAD), progress bar with avg + GOAT markers |
| **Monthly ribbons** | `awards.monthly_badges(group, year, month)` | Top-3 person-days for the WC's group this month, 🥇🥈🥉 |
| **15-min increments** | Bucketed meter readings per 15-min interval | 28 bars across the shift, color-coded (green ≥ target, amber ≥ 75%, red < 75%) |
| **Downtime report** | Zira-derived downtime events for this WC today | List of `{time, reason, duration}` rows, total minutes in the header |

Each widget is a `<div class="grid-stack-item">` wrapped with the existing `widget_attrs` macro. The TV view sets `gridstack.disable()` in JS so the layout is locked but still rendered.

### Dashboard layout templates

Once Dale has spent ten minutes arranging Repair 1's widgets, he should be able to clone that arrangement to Repair 2, Repair 3, and every other WC without dragging each one. A template is a named, saved snapshot of a widget-layout JSON. Two operations:

1. **Save current layout as a template.** From the WC editor view (`/wc/{slug}`), a "Save as template…" button next to the existing layout-save controls prompts for a name and stores the current `widget_layouts.layout_json` as a `tv_dashboard_templates` row.
2. **Apply a template.** Same editor view has an "Apply template…" dropdown that lists every existing template. Picking one fills the current dashboard's layout from the template (and saves it as the WC's layout). A second "Apply to…" option fans out: pick a group (e.g., Repairs) or "All WCs", and the template's layout is upserted into every matching WC's `widget_layouts` row.

```sql
CREATE TABLE IF NOT EXISTS tv_dashboard_templates (
  id          SERIAL PRIMARY KEY,
  name        TEXT NOT NULL UNIQUE,
  layout_json JSONB NOT NULL,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

API:

- `POST /api/tv-templates` — body `{name, layout}`. Upserts by name.
- `GET /api/tv-templates` — returns the list (name + id) for the dropdown.
- `POST /api/tv-templates/{id}/apply` — body `{targets: ["wc:repair-1", "wc:repair-2", ...] | "group:Repairs" | "all"}`. Resolves to a concrete set of WCs, then UPSERTs each WC's `widget_layouts.layout_json` from the template.

The template UI lives on the WC editor view (not on the Settings panel) because that's where you're already arranging widgets — the "Save as template" + "Apply to…" buttons sit next to the Save button. The Settings → TV Displays panel gets one secondary section showing the list of templates with a Delete button each, for cleanup.

### Settings → TV Displays panel

New section in `/settings`, sits in the existing sub-nav as "TV Displays". Renders a table from a new `tv_displays` config table:

```sql
CREATE TABLE IF NOT EXISTS tv_displays (
  id         SERIAL PRIMARY KEY,
  name       TEXT NOT NULL,
  kind       TEXT NOT NULL CHECK (kind IN ('vs_recycling', 'vs_new', 'wc')),
  wc_name    TEXT,
  slug       TEXT NOT NULL,
  sort_order INTEGER NOT NULL DEFAULT 0,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX ON tv_displays (slug);
```

- `kind = 'vs_recycling'` and `kind = 'vs_new'` are seeded once and editable for display-name only (their URL is hard-coded `/tv/recycling` / `/tv/new-vs`).
- `kind = 'wc'` rows have a `wc_name` (FK semantically to `work_centers.name`) and a generated `slug`. URL is `/tv/wc/{slug}`.

The panel renders each row with a Copy-URL button that puts the full `https://gpiplantmanager.com/tv/...` URL on the clipboard. Add Display button opens an inline form: pick kind, pick WC if kind=wc, auto-derive slug, save.

The underlying `/tv/...` routes work regardless of whether a `tv_displays` row exists — the table is a config / convenience UI, not a permission gate. Removing a row from the panel just removes it from Dale's list; the URL still resolves if anyone has it bookmarked.

### TV-mode CSS

`static/tv-mode.css` is a small overrides file applied only when `tv_mode = True`. Patterns:

```css
/* Hide every chrome thing. */
header.app, .sub-nav, .range-chips, .widget-edit-btn,
.widget-edit, .no-assign-btn, .lb-toolbar, .rc-toolbar, .save-block {
  display: none !important;
}

/* Bigger everything. */
body { font-size: 18px; }
.kpi .val { font-size: 3rem; }
.bar-row .name-primary { font-size: 1.5rem; }
table.sched { font-size: 1.2rem; }

/* TV header: WC name + operators across the top. */
.tv-header {
  display: grid;
  grid-template-columns: 1fr auto;
  align-items: end;
  padding: 14px 24px;
  border-bottom: 2px solid var(--border);
}
.tv-header .name { font-size: 38px; font-weight: 900; }
.tv-header .crumb { font-size: 11px; letter-spacing: 2px; opacity: .55; }
```

Same file used by Recycling/New-VS TV mode and per-WC TV view.

## Data flow

### Per-WC dashboard data resolution

When a request hits `/wc/{slug}` or `/tv/wc/{slug}`:

1. Resolve slug → WC name via `work_centers_store` lookup.
2. Fetch today's per-WC meter readings from Zira (cached path — `leaderboard.cached_leaderboard`).
3. For the GOAT widget: `awards.goat(group_of_wc)` returns the all-time record holder for the WC's group. Prorate the GOAT's units against elapsed shift minutes to produce "GOAT pace today".
4. For monthly ribbons: `awards.monthly_badges(group_of_wc, year, month)` — already returns top-3 with overrides applied.
5. For 15-min increments: query `zira_daily_cache.payload` for today, bucket readings into 15-min windows.
6. For daily progress: same data as 15-min, but cumulative.
7. For downtime: extract downtime events from the same Zira payload.
8. For operators assigned: `staffing.load_schedule(today).assignments.get(wc_name, [])`.
9. Layout: `db.query("SELECT layout_json FROM widget_layouts WHERE dashboard_key = %s", (f"wc:{slug}",))`.

Steps 2-7 share a single Zira fetch — the cached_leaderboard call returns enough to derive everything. Add one helper in `routes/wc_dashboard.py` that pulls the per-WC payload and decomposes into widget-ready dicts.

### Layout save

POST to existing `/api/widget-layout/{dashboard_key}` (extension of the existing recycling save). Body: `{layout: [{id, x, y, w, h}, ...]}`. Server-side validates IDs against the WC dashboard's widget set, upserts into `widget_layouts`.

### Auto-refresh

`<meta http-equiv="refresh" content="60">` on the TV variants. Matches the existing `setTimeout(() => location.reload(), 60000)` cadence on `/recycling`. The plant doesn't run third shift, but a midnight rollover is still needed to clear today's numbers; the next refresh after midnight naturally resolves `today` to the new date.

## Out of scope (v1)

- **Per-display widget overrides** — every TV showing the same WC sees the same arrangement. If a future TV needs a different layout for the same WC, we'll add a per-display override layer.
- **WebSocket streaming** — page refresh is good enough. The warmer keeps the underlying data fresh every 45s.
- **TV-specific themes / colors** — single dark theme everywhere on the TV variants. Light-mode TVs would need a separate sheet.
- **Multi-WC TVs** (one screen showing 2 WCs side-by-side) — not in v1. If demanded later, the same widget pattern composes cleanly.
- **Audio / alerts** when behind pace — TVs are silent.

## Testing

- **Helper unit tests** for the per-WC data decomposition (Zira payload → 15-min buckets, downtime list, cumulative daily progress).
- **Slug derivation** test: "Repair 1" → "repair-1", "Hand Build #1" → "hand-build-1".
- **Render smoke tests** for each new template: `/tv/recycling`, `/tv/new-vs`, `/wc/{slug}`, `/tv/wc/{slug}` all return 200 with the expected widgets present.
- **TV-mode flag plumbing**: the same handler with `tv_mode=True` produces a page without `<header class="app">`.
- **Layout persistence**: POST a layout, GET the same dashboard, assert the order and sizes round-trip.

## Rollout

Four sub-projects, shipped in this order so each provides value on its own:

1. **TV mode for existing VS dashboards** — smallest change, immediate value (Dale can mount a TV on the wall the same day). Adds `tv_mode` flag + `/tv/recycling` + `/tv/new-vs` + `tv-mode.css`. No new widgets, no DB changes.
2. **Per-WC dashboard** — the new template + widget set + editor (`/wc/{slug}`) + TV view (`/tv/wc/{slug}`) + the layout-save extension. Biggest scope, biggest visible new feature.
3. **Layout templates** — `tv_dashboard_templates` table + Save-as / Apply-to UI on the WC editor + bulk-apply API. Lets you arrange one WC then propagate.
4. **Settings → TV Displays panel** — config table + sub-section UI + add/remove flow + template management. Pure quality-of-life; the URLs from steps 1 + 2 work without it.

Each sub-project gets its own implementation plan when we get to executing.
