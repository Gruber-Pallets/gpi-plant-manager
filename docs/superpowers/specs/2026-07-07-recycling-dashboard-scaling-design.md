# Recycling Dashboard — Scale Correctly on Any TV / Screen

**Date:** 2026-07-07
**Status:** Design — awaiting review
**Scope:** Both views — the wall-TV display (`/tv/recycling`) and the interactive
editor (`/recycling`). Target screens: 16:9 HDTVs (1080p → 4K) and laptop/desktop
browsers. Proactive hardening — no single reported bug, the goal is confidence it
looks right on any of those screens.

## Goal

Make the recycling dashboard render correctly and legibly at every 16:9 TV
resolution (720p/1080p/1440p/4K) and every common laptop/desktop window size,
with **no content clipped, no content overflowing its widget, and no dead
whitespace** — without changing the layout, the widgets, or the editing workflow.

## Non-goals (YAGNI)

- Ultrawide (21:9) and portrait/rotated TV layouts — not in use.
- Phone/tablet reflow (restacking the 12-column grid into one column).
- Redesigning any widget or the Gridstack layout model.
- TV overscan compensation (some TVs crop ~2–5% of edges). Flagged as a known
  limitation; deferred unless it shows up on a real screen.

## Current architecture (unchanged by this work)

- **TV view:** `dashboard-grid.js` runs `fitGridToViewport()` — it measures the
  saved layout's true row extent and the live TV-header height, then sets
  Gridstack `cellHeight` so all rows fit `window.innerHeight` with no scroll
  (`html`/`body` are `overflow:hidden`). Root font scales with width:
  `clamp(16px, 1.1vw, 40px)`. Widget internals scale via container queries
  (`cqh`/`cqw`) and `clamp()`.
- **Editor view:** draggable/resizable Gridstack at a fixed `cellHeight: 60`;
  layout persists per page; the page scrolls normally.

The rendered DOM is **identical regardless of screen size** — only the container
queries, `clamp()`, and the TV `fitGridToViewport` calc respond to size. So this
is purely a CSS + JS scaling audit; the data layer and templates are untouched.

## Root causes to fix

### 1. Fixed pixel floors fight the fit-to-viewport math (primary issue)

`fitGridToViewport` shrinks `cellHeight` to fit the viewport, but several widget
internals hold hard pixel minimums that don't shrink with it:

- `.grid-stack-item-content .bar-track { min-height: 14px; max-height: 200px }`
  — `recycling.css:155`
- `.progress .plot { min-height: 60px }` — `recycling.css:589`
- `.cum-progress .plot { min-height: 80px }` — `recycling.css:725`

On a 1080p TV `cellHeight` lands ≈ 30px, so a 5-row chart widget's plot area,
after the title / goal line / x-ticks, can fall below these floors. The floor
then forces the content taller than the widget, and `overflow:hidden` silently
clips the x-axis or the bottom of the chart. Every resolution below 1080p (720p
TV, a maximized 1366×768 laptop) makes the gap larger.

**Fix:** make these internal heights fully proportional. Replace the hard pixel
floors with `min-height: 0` (a 1–2px sliver where a zero-value bar must still be
visible) so flex distribution governs size and no child can ever exceed its
widget. This is strictly an improvement: it only changes behavior in exactly the
cases that currently clip; on a correctly-fitted screen each row already gets its
proportional share. The `max-height: 200px` cap on `.bar-track` is removed for the
same reason (it capped bar thickness on large 4K widgets, wasting vertical space).

### 2. Dead and one harmful legacy media query

The `@media (max-width: 1400px)` and `@media (max-width: 600px)` blocks predate the
Gridstack rewrite and mostly target classes this template no longer renders
(`.kpi`, `.kpi-row`, `.panel`). Two problems:

- **Harmful:** `@media (max-width: 600px) { .progress .bars { height: 110px } }`
  (`recycling.css:685`) pins the gridstack chart's bars to a fixed 110px on any
  narrow window, overriding their `height:100%` flex-fill.
- **Dead weight:** the `.kpi` / `.panel` sub-rules in both blocks style nothing on
  this page.

**Fix:** delete the harmful `.progress .bars` rule and the dead `.kpi`/`.panel`
sub-rules. **Keep** the live, still-useful layout tightening in the 1400px block
(`header`, `.sub-nav`, `main` padding for 13" laptops) — that genuinely helps the
editor view on small laptops.

### 3. `fitGridToViewport` resilience

With the floors gone, the calc no longer produces overflow. Keep JS changes
minimal and defensive:

- Re-fit on `document.fonts.ready` (in addition to the existing `rAF` + `resize`
  listener) so a late font metric can't leave `cellHeight` measured against a
  stale header height. Cheap; harmless with system fonts.
- Keep the existing `Math.max(16, …)` floor on `cellHeight` and the `overflow:
  hidden` belt-and-suspenders.
- No behavior change to the editor view (it intentionally scrolls at
  `cellHeight: 60`).

## Verification plan (evidence before we ship)

Render the **real** template with representative data through the existing test
harness (`AUTH_DISABLED=1` + monkeypatched `leaderboard`/`load_schedule`, pgserver
`DATABASE_URL`), write the HTML to disk, serve it alongside the real
`/static` assets, and drive it in a headless browser at each size — capturing a
screenshot and asserting no widget overflows (`scrollHeight <= clientHeight` for
every `.grid-stack-item-content`, and content within widget bounds).

Matrix (both `/recycling` and `/tv/recycling`; TV in dark **and** light theme):

| Screen              | Size        |
|---------------------|-------------|
| 13" laptop          | 1280 × 800  |
| Small laptop (max)  | 1366 × 768  |
| 15" laptop          | 1440 × 900  |
| 1080p TV            | 1920 × 1080 |
| 4K TV               | 3840 × 2160 |

Include a "busy" data fixture (6 dismantler bars + full downtime row set) so the
tightest per-row slices are exercised, and an "empty/weekend" fixture (the
`No shift data` / `No elapsed shift minutes` empty states) so those don't break
either.

## Regression guard (tests)

Follow the existing `tests/test_recycling_toolbar_static.py` precedent — add
static assertions over `recycling.css` that lock the fixes so a future edit can't
silently reintroduce them:

- no `min-height: 60px` / `min-height: 80px` plot floors,
- no `max-height: 200px` on `.bar-track`,
- no `.progress .bars { height: 110px }` inside a `max-width` media query.

## Files touched

- `src/zira_dashboard/static/recycling.css` — proportional internals, media-query
  cleanup.
- `src/zira_dashboard/static/dashboard-grid.js` — `fonts.ready` re-fit.
- `src/zira_dashboard/static/tv-mode.css` — only if the audit surfaces a
  TV-specific floor not covered above.
- `tests/test_recycling_toolbar_static.py` (or a new sibling) — regression guard.

No Python route / data / template changes.
