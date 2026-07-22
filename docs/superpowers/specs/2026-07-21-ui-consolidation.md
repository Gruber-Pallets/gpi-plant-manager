# UI Consolidation — Design Spec

**Date:** 2026-07-21
**Status:** Approved by Dale (chat, 2026-07-21)

## Problem

The UI has grown three parallel rendering systems and heavy chrome duplication:

1. **Jinja inheritance (the good pattern):** `_staffing_base.html` serves 12 pages;
   `timeclock_base.html` serves all 16 kiosk pages.
2. **Standalone hand-rolled documents:** 10 full-page templates each duplicate
   `<!doctype>`/`<head>`/topnav/footer wiring: `staffing.html`, `settings.html`,
   `exceptions.html`, `index.html`, `recycling.html`, `new_dept.html`,
   `wc_dashboard.html`, `new_leaderboard_tv.html`, `recycling_leaderboard_tv.html`,
   `auth_denied.html`.
3. **Python-string HTML:** minor (TV error pages, changelog fragments) — not in scope.

The `:root` color palette is copy-pasted in **9 places** (8 CSS files + inline in
`_staffing_base.html`) and has already drifted (`exceptions.css` uses
`--muted:#64748b` vs `#6b7280` elsewhere).

## Goals

- **Phase 1:** every full-page template extends one of exactly two document shells:
  `_base_app.html` (desktop) or `timeclock_base.html` (kiosk).
- **Phase 2 (separate plan):** one `tokens.css` with the shared palette and common
  component styles; delete the 9 duplicated `:root` blocks. Kiosk CSS excluded.

## Non-goals (deferred, need product decisions)

- Phase 3 IA realignment (merging the four leaderboard surfaces, Diagnostics→/admin).
- Phase 4 route-module regrouping into per-section packages.
- Any change to Python-string HTML fragments.
- Any change to `/settings` UX (it already has a `?section=` sidebar; a code-level
  split of `settings.py`/`settings.html` into per-section modules is a Phase 3/4 item).

## Binding constraints (from Dale, 2026-07-21)

Operator TVs, the Plant Scheduler, and the Timeclock are **live production surfaces**
and must not break:

- **Timeclock: zero changes** in Phases 1–2. Kiosk pages already extend
  `timeclock_base.html`; kiosk CSS is self-contained and stays out of `tokens.css`.
- **Operator TVs:** the TV pages share render functions/templates with desktop
  dashboards via `tv_mode=True`. Their templates (`recycling.html`, `new_dept.html`,
  `wc_dashboard.html`, and the two leaderboard TV templates) are converted **only in
  Wave 2**, after the base pattern is proven — one template per commit, each gated on
  the TV dispatch CI tests, the static scaling guard, and a preview-harness visual
  check of both desktop and TV variants. Pushes that touch dashboard templates deploy
  off-shift (TVs repaint within 60s of a deploy).
- **Plant Scheduler:** `staffing.html` converts **last (Wave 3)**, chrome-only —
  every element id/class inside `<main>` stays identical; `staffing-print.css`'s
  print header (outside `<main>`) must keep working. Pair with the still-pending
  live smoke test of the rotation UI surfaces.
- Anything discovered that needs fixing on those surfaces is reported and scheduled
  with Dale, not changed opportunistically.
- **TV URLs never change (Dale, 2026-07-21).** Every `/tv/*` path — including the
  registry slugs, `?theme=` params, and the legacy `/tv/new-vs` and `/tv/d/{slug}`
  redirects — is pinned by plant-floor TV bookmarks that nobody wants to re-type
  on a TV remote. No wave or phase may rename, move, or delete a `/tv/*` URL; the
  Phase 3 IA realignment applies to desktop URLs only, and the page-usage-driven
  legacy-redirect cleanup explicitly excludes `/tv/*` unless Dale signs off per-URL.

## Phase 1 design

### `_base_app.html`

One desktop document shell. **No inline styles** in Phase 1 (styling stays in each
page's CSS; palette unification is Phase 2). Blocks:

| Block | Purpose |
|---|---|
| `title` | page name, rendered as `{title} — GPI Plant Manager` |
| `head` | page `<link>`/`<style>` tags |
| `topnav` | override to set `active_nav` before including `_topnav.html` |
| `header_extra` | extra controls inside `<header class="app">` (forms, buttons) |
| `header` | whole-header override — TV mode only |
| `subnav` | sub-navigation strip |
| `main_attrs` | attributes for `<main>` |
| `content` | page body |
| `footer` | `_footer.html` by default; TV pages blank it |
| `body_end` | trailing script region owned by intermediate bases |
| `scripts` | page `<script>` tags (nested inside `body_end`) |

The `topnav` block-override pattern (rather than a context variable) is used because
child-template top-level `{% set %}` visibility across inheritance is not guaranteed;
the one-line block override is explicit and safe.

`_staffing_base.html` becomes a child of `_base_app.html`, keeping its inline styles
(until Phase 2), `active`-driven subnav logic, and script region. Its 12 child pages
keep their existing block names (`title`, `styles`, `extra_head`, `content`,
`scripts`) — **zero changes to those 12 templates**. This works because
`_staffing_base` re-declares `styles`/`extra_head` inside its `head` override and
`scripts` inside its `body_end` override.

`auth_denied.html` intentionally stays standalone: it renders for **unauthenticated**
users and must not include `_topnav.html` (which calls `nav_inbox_summary()`).

### Conversion waves

- **Wave 1 (this plan):** ratchet test + `_base_app.html`; convert `index.html`
  (Work Centers), `exceptions.html`, `settings.html`; re-parent `_staffing_base.html`;
  fix two found bugs (below).
- **Wave 2 (follow-up plan):** the five TV-shared templates, with the TV gates above.
- **Wave 3 (follow-up plan):** `staffing.html`.

### Ratchet enforcement

`tests/test_base_app_template.py` asserts every non-underscore template either
extends a base or is on a frozen `ALLOWED_STANDALONE` list, and that converted
templates are **removed** from the list (bidirectional). The list only shrinks.
A second guard asserts every `/static/...` asset referenced by any template exists
on disk.

## Found bugs fixed in Wave 1

1. **Work Centers filter loses its selection:** the Day/Category form posts to
   `action="/"` (`index.html`), but `/` 307-redirects to `/recycling` and drops the
   query string (`routes/dashboard.py::home`). Fix: `action="/work-centers"`.
2. **`auth_denied.html` links a nonexistent stylesheet** (`/static/dashboard.css`
   404s; the card only renders via its inline fallback `var()` defaults). Fix:
   remove the dead `<link>`.

## Reported, deliberately not in this plan

- htmx is loaded by every kiosk page but used by exactly one template
  (`timeclock_time_off_calendar.html`) — removal touches the live kiosk; schedule
  for a quiet window with Dale.
- Unpushed forklift capacity-coverage commits ride local `main`; any push carries
  them. Check `git log origin/main..HEAD` before each push (Dale pushes).

## Verification

- Per task: focused existing tests for the touched page + the new ratchet/chrome
  tests (TDD: shrink the allowlist first, watch it fail, convert, watch it pass).
- Wave end: full suite `ZIRA_API_KEY=test .venv/bin/python -m pytest -q`
  (baseline 2026-07-11: 1,616 passed / 301 skipped).
- Rendered-chrome assertions: converted pages must contain exactly one doctype and
  exactly one `class="brand-row"` topnav, and still include the footer.
