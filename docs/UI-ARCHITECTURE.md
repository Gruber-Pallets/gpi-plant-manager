# UI Architecture

How the Plant Manager UI is put together, for someone seeing the codebase
for the first time. (Consolidated 2026-07-21 — spec:
`docs/superpowers/specs/2026-07-21-ui-consolidation.md`.)

## The three surfaces

| Surface | Audience | Entry points | Auth |
|---|---|---|---|
| **Desktop app** | managers/office | topnav: Performance, Staffing, Inbox, Settings (`/` → `/recycling`) | Azure AD login |
| **TV displays** | plant floor, read-only, auto-refresh 60s | `/tv/...` (registry-dispatched via `/tv/{slug}`) | none (IP allowlist) |
| **Timeclock kiosk** | shop-floor touchscreens | `/timeclock...` | device tokens |

**TV URLs are permanent.** Never rename, move, or delete a `/tv/*` path —
plant TVs bookmark them and there is no keyboard on a TV remote. This is a
binding product decision (Dale, 2026-07-21, recorded in the spec).

**Performance** (Dale, 2026-07-22) is the one home for every "how are we
doing" page: the department dashboards, the operator view, the work-center
grid, the department leaderboard pages, the plant leaderboards, and the
Trophy Case — all tabs of `_performance_subnav.html`. URLs did not change
(e.g. `/staffing/leaderboards` keeps its path; only the nav moved).

## Templates: two base layouts, nothing else

Every full-page template extends one of exactly two document shells —
enforced by the ratchet test `tests/test_base_app_template.py`:

- **`_base_app.html`** — every desktop page. Blocks: `title` (rendered as
  "{title} — GPI Plant Manager"), `title_tag` (full override for odd
  titles), `head`, `html_attrs`/`body_attrs`/`main_attrs`, `topnav`
  (override to set the active menu item), `header_extra`, `header` (whole
  header — TV pages override it with `_tv_header.html`), `subnav`,
  `content`, `footer`, `body_end`/`scripts`.
- **`timeclock_base.html`** — every kiosk page. Touch-first, no-scroll,
  self-contained styling. Deliberately isolated: shares nothing with the
  desktop shell, and `tokens.css` is not loaded there.
- `_staffing_base.html` is an *intermediate* base (extends `_base_app`)
  adding the Staffing/Trophy-Case sub-nav and shared panel styles; the
  staffing-family pages extend it.
- `auth_denied.html` is the one permanent standalone document: it renders
  for unauthenticated users, so it must not include the topnav (which
  calls `nav_inbox_summary()`).

The dashboard pages (`recycling.html`, `new_dept.html`, `wc_dashboard.html`,
the two leaderboard templates) serve BOTH desktop and TV from one template:
routes pass `tv_mode=True` (or the page defaults TV-first via
`is_tv = tv_mode | default(true)`), and the template's `header`/`footer`
block overrides strip the desktop chrome. `tv-mode.css` re-themes colors
under `html[data-tv-theme="dark|light"]`.

## CSS: one palette, page-specific extras

- **`static/tokens.css`** is the single source of the shared light-theme
  palette (`--bg`, `--panel`, `--fg`, `--muted`, `--accent`, …). Linked by
  `_base_app.html` before every other stylesheet.
- Page stylesheets keep only page-specific variables (e.g. `index.css`
  `--running`, `exceptions.css` `--info`). `exceptions.css` deliberately
  overrides three tokens for its denser text UI — documented in the file.
- `tv-mode.css` overrides the palette for TVs via `html[data-tv-theme]`
  (higher specificity than `:root`, so load order doesn't matter).
- Guard: `test_shared_palette_lives_only_in_tokens_css` fails if anyone
  redefines a shared token elsewhere or adds a `:root` block to a template.

## Where things live

- Routes: one module per feature in `src/zira_dashboard/routes/`; mounted
  flat in `app.py` (no prefixes — each module declares full paths).
- The TV registry: Settings → TVs manages named displays; `/tv/{slug}`
  (`routes/tv_displays.py`) dispatches to the same render functions the
  desktop pages use. Add a display in Settings, not in code.
- Settings: `/settings?section=...` — nine sections in one sidebar page
  (work_centers, roster_filter, integrations, api, tvs, timeclock,
  time_off, forklift, diagnostics). Feature-local settings deliberately
  live ON the feature's page (skills auto-level goals on the Skills
  Matrix, leaderboard visibility on Leaderboards, widget layout on each
  dashboard, rotation preferences in the People Matrix).
- Widget layout persistence: `layout_store` via `routes/api_layout.py`;
  dashboards use gridstack + `static/dashboard-grid.js`.
- `static/footer.js` is (historically) more than the footer: the XHR
  helper `gpiFetch`, the changelog modal, the feedback modal, and the
  inbox badge polling.

## Verification habits for UI changes

- Run the guards: `tests/test_base_app_template.py` (template ratchet,
  static-reference existence, palette ratchet, per-page chrome tests).
- Anything touching the dashboard templates: also run
  `tests/test_tv_dashboards_vs.py`, `tests/test_wc_dashboard.py`,
  `tests/test_tv_displays_routes.py` (the TV dispatch path 500'd all
  plant TVs once — 2026-07-10 — these tests exist because of it), and
  `tests/test_recycling_scaling_static.py`.
- Visual check without live Odoo/Zira: `scripts/preview_recycling.py`
  renders stubbed snapshots into `scripts/_preview_out/` (served by the
  `recycling-preview` launch config) for the recycling + operator pages,
  desktop and TV variants.
- Deploys repaint the plant TVs within ~60s (they probe `/tv/ping`, then
  reload). Push dashboard-affecting changes off-shift when possible, and
  spot-check a `/tv/*` URL right after the deploy goes live.

## Test environment in one line

`DATABASE_URL=<embedded pgserver> ZIRA_API_KEY=test .venv/bin/python -m pytest`
(`tests/conftest.py` sets `AUTH_DISABLED=1`; see the pgserver recipe in the
team's dev notes — CI provides Postgres itself.)
