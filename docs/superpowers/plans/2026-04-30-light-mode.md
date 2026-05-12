# Light Mode Conversion Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Convert the GPI Plant Manager dashboard from dark mode to light mode (soft-gray background, white panels, vibrant green/red accents) without breaking the print/PDF view or any color-graded data.

**Architecture:** Replace per-`:root` palette tokens with light-mode values in the 6 files that define palettes (3 extracted CSS + 3 templates). Templates that extend `_staffing_base.html` (leaderboards, skills, past_schedules, time_off) inherit the flip automatically. Then darken cert-badge colors for white-bg readability and sweep for hard-coded hex literals that bypass the variable system.

**Tech Stack:** CSS custom properties · Jinja2 inheritance · static asset extraction (already done in Round-1 #5)

---

## File Structure

**Files with their own `:root` palette (must be updated):**
- `src/zira_dashboard/static/recycling.css`
- `src/zira_dashboard/static/staffing.css`
- `src/zira_dashboard/static/new_vs.css`
- `src/zira_dashboard/templates/_staffing_base.html` (`<style>` block; covers leaderboards/skills/past/time_off via Jinja extends)
- `src/zira_dashboard/templates/index.html` (the work-centers page)
- `src/zira_dashboard/templates/settings.html`

**Files with cert-badge colors:**
- `src/zira_dashboard/templates/_cert_badges.html` (`cert_badges_css()` macro)

**Files to sweep for hard-coded hex literals:**
- All of the above, plus: `static/leaderboards.css`, `static/skills.css`, all other templates in `src/zira_dashboard/templates/`.

---

## Token mapping (used by every task below)

```
--bg            #0e1116  →  #f1f4f7   (page background, soft gray)
--panel         #161b22  →  #ffffff   (primary panel bg, white)
--panel-2       #1c232c  →  #f1f4f7   (secondary surface)
--panel-3       #222a33  →  #e3e8ee   (tertiary surface)
--border        #30363d  →  #d8dee5   (borders / dividers)
--fg            #e6edf3  →  #1f2937   (primary text)
--muted         #8b949e  →  #6b7280   (secondary text)
--good          #4ade80  →  #16a34a   (hit/OK accent)
--good-color    #4ade80  →  #16a34a   (alias used in some downtime widgets)
--bad           #ef4444  →  #ef4444   (UNCHANGED — vibrant red works on both)
--accent        #4ade80  →  #16a34a   (brand accent)
--accent-dim    #14532d  →  #dcfce7   (accent backdrop, light)
--warn          #facc15  →  #a16207   (warning text — darkened for white bg)
--warn-dim      #3f2f00  →  #fef3c7   (warning backdrop, light)
--neutral-pill  #222a33  →  #e3e8ee   (lvl-2 pill bg)
--bad-dim       #3a1212  →  #fee2e2   (lvl-0 / below-goal backdrop)
```

Variables that don't appear in a particular file's `:root`: leave alone. Variables in `:root` that aren't in this table: leave alone (judgment-call adjustment in Task 3 if any are misbehaving on white).

---

## Task 1: Update palette in all 6 `:root` blocks

**Files:**
- Modify: `src/zira_dashboard/static/recycling.css`
- Modify: `src/zira_dashboard/static/staffing.css`
- Modify: `src/zira_dashboard/static/new_vs.css`
- Modify: `src/zira_dashboard/templates/_staffing_base.html`
- Modify: `src/zira_dashboard/templates/index.html`
- Modify: `src/zira_dashboard/templates/settings.html`

For each file:

- [ ] **Step 1: Read the file's `:root` block**

Find the `:root { ... }` block in the file. Note which tokens are defined there — most files define a subset of the full table.

- [ ] **Step 2: Replace token values**

For each token that exists in the `:root` block AND in the mapping table above, change the value to the new (light) value. Keep the variable name unchanged.

Example: in `static/recycling.css`, find:

```css
:root {
  --bg: #0e1116;
  --panel: #161b22;
  ...
}
```

Change to:

```css
:root {
  --bg: #f1f4f7;
  --panel: #ffffff;
  ...
}
```

(Continuing for every variable that's both defined AND in the table.)

- [ ] **Step 3: Smoke test**

```bash
C:\Users\dale.gruber\Projects\zira\.venv\Scripts\python.exe -c "from zira_dashboard.app import app; print(app.title)"
```
Expected: `Zira Station Dashboard`.

```bash
C:\Users\dale.gruber\Projects\zira\.venv\Scripts\python.exe -c "from zira_dashboard.deps import templates; [templates.env.get_template(t) for t in ['recycling.html','staffing.html','leaderboards.html','new_vs.html','skills.html','settings.html','index.html','past_schedules.html','time_off.html']]; print('all templates ok')"
```
Expected: `all templates ok`.

- [ ] **Step 4: Commit**

```bash
git add src/zira_dashboard/static/recycling.css src/zira_dashboard/static/staffing.css src/zira_dashboard/static/new_vs.css src/zira_dashboard/templates/_staffing_base.html src/zira_dashboard/templates/index.html src/zira_dashboard/templates/settings.html
git commit -m "feat(theme): light-mode palette in :root blocks (soft gray + white panels + vibrant accents)"
```

---

## Task 2: Darken cert-badge colors for white-bg readability

**Files:**
- Modify: `src/zira_dashboard/templates/_cert_badges.html` — the four `.cert-badge-*` color rules inside the `cert_badges_css()` macro

- [ ] **Step 1: Find the cert-badge color rules**

In `_cert_badges.html`, the `cert_badges_css()` macro contains four lines like:

```css
.cert-badge-forklift   { color: #facc15; } /* yellow */
.cert-badge-spotter    { color: #22c55e; } /* green  */
.cert-badge-cdl-manual { color: #3b82f6; } /* blue   */
.cert-badge-cdl-auto   { color: #a855f7; } /* purple */
```

- [ ] **Step 2: Replace with darker (700-shade) values**

Change to:

```css
.cert-badge-forklift   { color: #a16207; } /* yellow-700 */
.cert-badge-spotter    { color: #15803d; } /* green-700  */
.cert-badge-cdl-manual { color: #1d4ed8; } /* blue-700   */
.cert-badge-cdl-auto   { color: #7e22ce; } /* purple-700 */
```

(Comments updated to reflect the shade level.)

- [ ] **Step 3: Smoke test**

```bash
C:\Users\dale.gruber\Projects\zira\.venv\Scripts\python.exe -c "from zira_dashboard.deps import templates; templates.env.get_template('_cert_badges.html')"
```
Expected: no exception.

- [ ] **Step 4: Commit**

```bash
git add src/zira_dashboard/templates/_cert_badges.html
git commit -m "feat(theme): darken cert-badge colors to 700-shade for white-bg readability"
```

---

## Task 3: Hard-coded hex sweep + fix

**Files:**
- Audit + modify any of: `src/zira_dashboard/static/*.css`, `src/zira_dashboard/templates/*.html` — fix hard-coded color literals that don't use the `--bg`/`--panel`/etc. variables.

Variables cover most surfaces but some templates have inline hex codes that bypass them. These need to be either (a) replaced with `var(--token)` references, or (b) updated to a light analog if they're intentional one-off colors.

- [ ] **Step 1: Sweep for hex literals in extracted CSS**

Run:
```bash
grep -nE "#[0-9a-fA-F]{3,6}" src/zira_dashboard/static/*.css | grep -v "rgba" | head -100
```

For each match, classify:
- **Should use a variable** — e.g., `background: #161b22` should be `background: var(--panel)`. Replace with the var.
- **Intentional hard-code that needs a light analog** — e.g., a chart goal-line color hard-coded as `#facc15` for visibility on dark. Update to a light-bg analog (likely a `--muted` gray or a darkened hue).
- **Color-graded data, leave alone** — e.g., `#16a34a` / `#ef4444` literals already in light-mode-friendly shades; or per-cert-slug colors that we just darkened in Task 2.

- [ ] **Step 2: Sweep for hex literals in templates**

Run:
```bash
grep -nE "#[0-9a-fA-F]{3,6}" src/zira_dashboard/templates/*.html | grep -v "rgba" | head -100
```

Apply the same classification and fix.

Pay particular attention to:
- Inline `style="..."` attributes on elements (these bypass variables entirely)
- The `style` attributes in macros like `edit_controls` (`recycling.html`)
- Hard-coded background/border/color values in widgets (e.g., the testing-day pill at the top of `staffing.html`)
- The toast helper in `staffing.html` uses inline hex (`#fee`/`#efe`/`#900`/etc.) — these are FINE on white (light reds/greens for error/success toasts); leave alone unless they look wrong on the new bg.

- [ ] **Step 3: Verify no dark backgrounds slip through**

Search for the SPECIFIC dark colors that were the old palette's panel/bg values:
```bash
grep -nE "#0e1116|#161b22|#1c232c|#222a33|#30363d" src/zira_dashboard/static/*.css src/zira_dashboard/templates/*.html
```

Expected: ZERO matches after Task 1 + this sweep are complete. If any remain, replace with the light-mode equivalent or `var(--bg)` / `var(--panel)` / etc.

- [ ] **Step 4: Smoke test**

```bash
C:\Users\dale.gruber\Projects\zira\.venv\Scripts\python.exe -c "from zira_dashboard.app import app; print(app.title)"
```
Expected: `Zira Station Dashboard`.

```bash
C:\Users\dale.gruber\Projects\zira\.venv\Scripts\python.exe -c "from zira_dashboard.deps import templates; [templates.env.get_template(t) for t in ['recycling.html','staffing.html','leaderboards.html','new_vs.html','skills.html','settings.html','index.html','past_schedules.html','time_off.html','player_card.html']]; print('all templates ok')"
```
Expected: `all templates ok`.

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "feat(theme): sweep hard-coded dark hex literals to light analogs"
```

(The `git add -A` here is OK — by this point the only modified files should be templates/CSS touched during the sweep. Double-check `git status` shows nothing unexpected.)

In your commit report, list each file you actually modified and a one-line note on what kind of fix went in (e.g., "settings.html: 3 inline `background:#161b22` → `var(--panel)`").

---

## Task 4: Smoke test on live deploy

This task is operator-only — push and walk the pages.

- [ ] **Step 1: Push**

```bash
git push
```

Wait for Railway to redeploy (~2-3 min — pure template/CSS changes, no Docker layer churn).

- [ ] **Step 2: Walk every page**

For each, verify: white panels on soft-gray bg, vibrant green/red accents, dark text on white panels, NO dark patches anywhere:
- `/recycling`
- `/new-vs`
- `/work-centers`
- `/staffing`
- `/staffing/skills`
- `/staffing/leaderboards`
- `/staffing/past`
- `/staffing/time-off`
- `/settings`
- A player-card view (click a name on `/staffing/leaderboards`)

If any page shows a dark patch or unreadable color combination, screenshot + iterate.

- [ ] **Step 3: Smoke-test cert badges**

Open `/staffing` (any day with assignments). Verify the four cert-badge icon colors all render with strong contrast against the white panel:
- Yellow forklift (now darker `#a16207`)
- Green spotter (`#15803d`)
- Blue CDL-manual (`#1d4ed8`)
- Purple CDL-auto (`#7e22ce`)
- Dot certified — currentColor accent (`#16a34a`)

- [ ] **Step 4: Smoke-test print/PDF**

On `/staffing`, click the printer-icon button. Print preview should look unchanged from the pre-conversion state — white paper, black text, gray borders. The print stylesheet hard-codes those values regardless of screen palette.

Click the Slack-logo button. The PDF posted to `#mgmt-sups` should look identical to before.

- [ ] **Step 5: Done**

If steps 2–4 all pass, the conversion is shipped. If anything looks off, follow up with a targeted fix.

---

## Acceptance Recap

After all tasks merge and deploy:

- ✅ Every dashboard page renders with white panels on a soft-gray background.
- ✅ Publish / Posted / Save accents use vibrant green (`#16a34a`); below-goal bars use vibrant red (`#ef4444`).
- ✅ All four cert-badge icons read clearly on white (yellow-700 / green-700 / blue-700 / purple-700).
- ✅ Print preview / PDF Slack post unchanged from pre-conversion appearance.
- ✅ No leftover dark backgrounds (a `grep` for `#0e1116|#161b22|#1c232c|#222a33|#30363d` returns zero matches in templates/static).
