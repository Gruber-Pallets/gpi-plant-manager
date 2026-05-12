# Light Mode Conversion — Design

**Date:** 2026-04-30
**Status:** Approved (brainstorming → implementation planning)

## Context

The GPI Plant Manager dashboard currently ships in a dark theme — palette
defined per-template via CSS custom properties (`--bg: #0e1116`, `--panel:
#161b22`, etc.). Dale wants the whole app converted to a light theme.

The print/PDF view (in `staffing.html`) is already light (white background,
black text) and stays untouched.

## Goals

1. Convert the entire dashboard UI from dark to light mode (full switch, no
   user-facing toggle).
2. Apply the agreed palette (visual companion confirmed):
   - Soft-gray app background, white panels, vibrant green/red accents.
3. Darken cert-badge colors for white-background readability.
4. No regressions — the green/red color-grading on bars, the cert-badge
   contrast, and the print/PDF view all remain readable and meaningful.

## Non-goals

- A user-facing dark/light toggle. Single-mode app. Could be added later
  if needed.
- Print/PDF style changes. Print mode already overrides to a light theme;
  the screen palette change does not affect it.
- Brand identity overhaul. Same accent green hue, just shade-adjusted for
  the new background.
- Reworking layout, spacing, or typography. Pure recolor.
- Migrating colors to a single canonical theme module. The existing
  per-template `:root` definitions are the surface; we update each. A
  follow-up could consolidate, but that's a separate refactor.

## Design

### Palette tokens

The dashboard's per-template `:root` blocks currently define these
variables. After this change, every `:root` block defining these names
adopts the light values:

| Token | Old (dark) | New (light) | Purpose |
|---|---|---|---|
| `--bg` | `#0e1116` | `#f1f4f7` | Page / app background (soft gray) |
| `--panel` | `#161b22` | `#ffffff` | Primary panel/card background (white) |
| `--panel-2` | `#1c232c` | `#f1f4f7` | Secondary surface (input bg, muted panel) |
| `--panel-3` | `#222a33` | `#e3e8ee` | Tertiary surface (hover, opened picker bg) |
| `--border` | `#30363d` | `#d8dee5` | Borders / dividers |
| `--fg` | `#e6edf3` | `#1f2937` | Primary text |
| `--muted` | `#8b949e` | `#6b7280` | Secondary / hint text |
| `--good` | `#4ade80` | `#16a34a` | "Hit goal" / running / OK accent |
| `--good-color` | `#4ade80` | `#16a34a` | Alias used in some downtime widgets |
| `--bad` | `#ef4444` | `#ef4444` | Below-goal / down / error (unchanged — vibrant red works on both) |
| `--accent` | `#4ade80` | `#16a34a` | Brand accent (Publish button, links) |
| `--accent-dim` | `#14532d` | `#dcfce7` | Accent's "dim" backdrop (active states, pill bg) |
| `--accent-dim` text-on color | (white text on dark bg) | `#15803d` (dark green text on the light dim bg) | Pills/buttons that use `--accent` text on `--accent-dim` bg need text-color flipped from white to a darker green so they read on the new light dim background |
| `--warn` | `#facc15` | `#a16207` | Warning text (testing-day pill, blocked banner heading) |
| `--warn-dim` | `#3f2f00` | `#fef3c7` | Warning backdrop |
| `--neutral-pill` | `#222a33` | `#e3e8ee` | Time-off pill / lvl-2 pill bg |
| `--bad-dim` | `#3a1212` | `#fee2e2` | Below-goal/lvl-0 backdrop |

The actual variables used vary slightly per template — implementation
inspects each template's `:root` block and updates the names that exist
there. Names not in the table above (rare) get judgment-call light
analogs at implementation time.

### Cert badge colors

Existing cert-badge palette (in `_cert_badges.html` `cert_badges_css()` macro):

| Cert slug | Old color | New color |
|---|---|---|
| `cert-badge-forklift` | `#facc15` (yellow-400) | `#a16207` (yellow-700) |
| `cert-badge-spotter` | `#22c55e` (green-500) | `#15803d` (green-700) |
| `cert-badge-cdl-manual` | `#3b82f6` (blue-500) | `#1d4ed8` (blue-700) |
| `cert-badge-cdl-auto` | `#a855f7` (purple-500) | `#7e22ce` (purple-700) |

All four are darkened to 700-shade equivalents so they're clearly
readable against the new white panel background while remaining
recognizably their original hues.

### Hard-coded color hunt

Variables cover most cases, but there are scattered hard-coded color
strings in templates (e.g., inline `style="background:#161b22"`, hex
values inside `<style>` blocks that don't reference variables). The
implementer must:

1. Grep every template + extracted CSS file for hex codes (`#[0-9a-fA-F]{3,6}`)
   and `rgb(`/`rgba(` literals.
2. For each hit: classify as either "should use a variable" (replace),
   "intentional hard-code that needs a light analog" (replace with a new
   value), or "color-grading that's correct in both themes" (leave).
3. Common offenders to expect: chart colors (yellow goal lines, blue
   downtime fills), per-cert badge colors (already covered above), `lvl-0`
   through `lvl-3` pill backgrounds (use `--accent-dim`/`--neutral-pill`
   etc. — those already cascade), inline hex on the testing-day pill,
   the `posted-badge` styling.

### Print/PDF view (unchanged)

The print stylesheet in `staffing.html` (the `@media print { ... }` block)
explicitly overrides `body { background: white; color: black; }` and
forces white panels, black text, gray borders throughout. **No changes
needed for print mode** — it already renders as a light theme regardless
of screen palette.

### File touch list

Templates with their own `:root` palette (one per static-extracted CSS
file, plus inline-styled templates):

- `src/zira_dashboard/static/recycling.css`
- `src/zira_dashboard/static/staffing.css`
- `src/zira_dashboard/static/leaderboards.css`
- `src/zira_dashboard/static/new_vs.css`
- `src/zira_dashboard/static/skills.css`
- `src/zira_dashboard/templates/_staffing_base.html` (inline `<style>`)
- `src/zira_dashboard/templates/settings.html` (inline `<style>` — fully inline since not extracted)
- `src/zira_dashboard/templates/index.html` (work-centers page — inline `<style>`)
- `src/zira_dashboard/templates/past_schedules.html` (inline `<style>`)
- `src/zira_dashboard/templates/time_off.html` (inline `<style>`)
- `src/zira_dashboard/templates/player_card.html` (inline `<style>`)
- `src/zira_dashboard/templates/_cert_badges.html` (the cert-badge color block in `cert_badges_css()` macro)

Implementation reads each file, finds the palette variables AND any
hard-coded hex colors, replaces per the table above. Each template is
its own commit-sized chunk.

## Acceptance criteria

- Every dashboard page (`/recycling`, `/new-vs`, `/work-centers`,
  `/staffing`, `/staffing/skills`, `/staffing/leaderboards`,
  `/staffing/past`, `/staffing/time-off`, `/settings`, player card)
  renders with white panels on a soft-gray background and dark text on
  white.
- The Publish button, "Posted" pill, and other accent-colored UI elements
  use the new vibrant green (`#16a34a`) and read clearly on white.
- Below-goal bars, cert-badge "miss" states, and error toasts use a
  vibrant red that's clearly distinguishable from green at a glance.
- All four cert-badge icon colors render with strong contrast against
  the white panel background.
- The print preview / PDF Slack post are visually unchanged from the
  pre-conversion state (already light, no regressions).
- No leftover dark backgrounds in any UI surface (e.g., a forgotten
  inline `style="background:#161b22"` showing up as a dark patch).

## Risks

- **Hard-coded hex sweep is the riskiest step.** A single missed
  `style="background:#161b22"` produces a visible dark patch. Mitigation:
  systematic grep, page-by-page visual smoke test on deploy.
- **Color-graded data may need re-tuning on white.** The bar charts'
  HSL-interpolated colors (in `routes/value_streams.py` `_color()`
  helper) compute `hsl(hue, sat, light)` against a dark background;
  some shades may look washed out on white. Mitigation: test on a real
  page, adjust the lightness floor in `_color()` if needed (one helper
  function).
- **Cert-badge color choice on white.** All four are 700-shade
  equivalents. If any feels too dark/muddy in practice, swap to
  600-shade (`#ca8a04`, `#16a34a`, `#2563eb`, `#9333ea`). Easy
  one-line fix.
- **Per-template palette drift.** Each template currently maintains
  its own `:root` block. If a future template forgets to define a
  variable that other templates use, it won't inherit the global
  palette. Long-term cleanup: extract a single shared `_palette.css`
  partial. Out of scope for this change but worth noting for follow-up.

## Out-of-scope follow-ups (future)

- Single shared `_palette.css` partial (DRY the per-template `:root`).
- Optional dark/light toggle with localStorage persistence.
- Color-grading helper retune on white (`_color()` lightness floor).
