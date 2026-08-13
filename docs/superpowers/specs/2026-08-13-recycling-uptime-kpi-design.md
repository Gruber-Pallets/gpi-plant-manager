# Recycling Uptime KPI Design

## Problem

The Recycling dashboard (`/recycling`) has an open grid slot under the
Pallets/hr KPI. Plant uptime is already calculated and shown only as a small
summary on the Downtime Report (`85% up · …m down`). That summary is easy to
miss when scanning the left-hand KPI column.

## Desired behavior

Add a simple KPI card in that open space:

- Widget id: `kpi-uptime`
- Default title: **Uptime**
- Value: the same `uptime_pct` already computed for the Downtime Report,
  displayed as a whole-number percent (e.g. `85%`)
- Value color by band (threshold color always wins; widget color customizer
  must not override the number):
  - **≥ 90%** → green (`--good`)
  - **≥ 80% and &lt; 90%** → orange (`--warn`)
  - **&lt; 80%** → red (`--bad`)
- Keep the existing ⋮ edit controls for title/align like other KPIs
- Default grid size matches the other KPI tiles; default position sits under
  them so a fresh layout fills the open slot. Saved user layouts still win

## Design

Template-only change on `recycling.html`, reusing the existing `uptime_pct`
context value. No new route math, no Downtime Report changes, and no shared
helper unless a later feature needs the band elsewhere.

Implementation sketch:

1. Extend the Recycling KPI list (or add a sibling grid item) with `kpi-uptime`.
2. Format the value as `{{ uptime_pct|round(0)|int }}%` so it matches the
   Downtime Report’s rounded percent.
3. Apply a CSS class or inline style from the three bands above using existing
   `--good` / `--warn` / `--bad` tokens from `recycling.css` / `tokens.css`.
4. Skip `widget_color_style` for this value (same pattern as Pallets/hr forcing
   its own color).

## Alternatives considered

1. **Template-only KPI (chosen).** Smallest change; stays in lockstep with the
   Downtime Report number.
2. **Shared Python helper for the color band.** Better if many callers need the
   band; unnecessary for one card.
3. **Precompute formatted value/color in the route.** Extra wiring when the
   template already has `uptime_pct`.

## Testing

- Static/template test: Recycling HTML includes `gs-id="kpi-uptime"`, shows a
  percent value, and applies the correct band for representative inputs
  (e.g. 95 → good, 85 → warn, 70 → bad; boundaries 90 → good, 80 → warn).
- No change expected to Downtime Report assertions beyond continued presence of
  the shared `uptime_pct`.

## Scope

In scope: `/recycling` (screen and TV layouts that use `recycling.html`).

Out of scope: operator `/wc/...` dashboards, `new_dept.html`, Downtime Report
layout/copy, and any new uptime formula.
