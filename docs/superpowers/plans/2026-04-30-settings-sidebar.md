# Settings Sidebar Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the settings page's pin-and-scroll model with a left sidebar that switches between two top-level sections (Work Centers & Goals + Company Schedule), with Work Centers & Goals as the default. Also drop the unused Notes column from the Work Centers & Goals table.

**Architecture:** Server adds a `?section=` query param (defaulting to `work_centers`), passes `active_section` into the template. Template renders a fixed-width sidebar plus the active form; non-active form gets `display:none`. POST redirects preserve the section. Pin button HTML / CSS / JS / localStorage code is fully removed.

**Tech Stack:** FastAPI · Jinja2 · vanilla CSS (no JS for sidebar — plain `<a>` links)

---

## File Structure

**Modified files:**
- `src/zira_dashboard/routes/settings.py` — `?section=` query param + redirect targets + drop `"note"` from WC field tuple
- `src/zira_dashboard/templates/settings.html` — sidebar markup + CSS, conditional form display, strip pin code, drop Notes column

No new files.

---

## Task 1: Server-side changes (route handler)

**Files:**
- Modify: `src/zira_dashboard/routes/settings.py`

- [ ] **Step 1: Add `?section=` query param to `settings_page` GET**

Find the `settings_page` GET handler signature (around line 44):

```python
@router.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, saved: int = Query(default=0)):
```

Change to:

```python
@router.get("/settings", response_class=HTMLResponse)
def settings_page(
    request: Request,
    saved: int = Query(default=0),
    section: str = Query(default="work_centers"),
):
    if section not in ("work_centers", "schedule"):
        section = "work_centers"
```

Place the `if section not in (...)` validation as the very first statement of the function body.

- [ ] **Step 2: Pass `active_section` into the template context**

Find the existing `templates.TemplateResponse(...)` call at the end of the handler. Add `"active_section": section,` to the context dict alongside the other keys (e.g., near `"saved": bool(saved),`).

Example final shape (only the new line is added):

```python
    return templates.TemplateResponse(
        request,
        "settings.html",
        {
            ...
            "saved": bool(saved),
            "active_section": section,
            ...
        },
    )
```

- [ ] **Step 3: Update `settings_save_schedule` redirect target**

Find the `settings_save_schedule` POST handler (around line 154). Locate its final redirect:

```python
    return RedirectResponse(url="/settings?saved=1#schedule", status_code=303)
```

Change to:

```python
    return RedirectResponse(url="/settings?saved=1&section=schedule", status_code=303)
```

(Drop the `#schedule` anchor — the sidebar handles in-page navigation now.)

- [ ] **Step 4: Update `settings_save_work_centers` redirect target**

Find the `settings_save_work_centers` POST handler (around line 216). Locate its final redirect:

```python
    return RedirectResponse(url="/settings?saved=1", status_code=303)
```

Change to:

```python
    return RedirectResponse(url="/settings?saved=1&section=work_centers", status_code=303)
```

- [ ] **Step 5: Drop `"note"` from the WC field-name tuple**

In `settings_save_work_centers`, find the line iterating over WC field names (around line 238):

```python
        for field in ("goal_per_day", "min_ops", "max_ops", "note", "value_stream"):
```

Change to:

```python
        for field in ("goal_per_day", "min_ops", "max_ops", "value_stream"):
```

(The DB column stays in schema; this just stops writing to it from the form.)

- [ ] **Step 6: Smoke test**

Run:
```bash
C:\Users\dale.gruber\Projects\zira\.venv\Scripts\python.exe -c "from zira_dashboard.app import app; print(app.title)"
```
Expected: `Zira Station Dashboard`.

Run:
```bash
C:\Users\dale.gruber\Projects\zira\.venv\Scripts\python.exe -m pytest tests/ -k "settings" -q 2>&1 | tail -5
```
(There may be no settings-specific tests; that's fine.)

- [ ] **Step 7: Commit**

```bash
git add src/zira_dashboard/routes/settings.py
git commit -m "feat(settings): server-side ?section= query param + section-preserving redirects"
```

---

## Task 2: Template — sidebar markup, CSS, conditional form display

**Files:**
- Modify: `src/zira_dashboard/templates/settings.html` — add sidebar wrapper + sidebar markup + CSS rules; add `data-section=...` and conditional `display:none` to the two forms

This task ADDS the new functionality. The pin button + pin code stay in place for now (Task 3 strips them). After this task, sidebar works and pin still works — they coexist briefly.

- [ ] **Step 1: Add sidebar CSS rules**

In `settings.html`, find the existing `<style>` block. Append these new rules (e.g., right before the closing `</style>`):

```css
.settings-shell {
  display: grid;
  grid-template-columns: 200px 1fr;
  gap: 1.5rem;
  align-items: start;
}
.settings-sidebar {
  position: sticky;
  top: 1rem;
  display: flex;
  flex-direction: column;
  gap: 0.25rem;
}
.settings-nav-item {
  display: block;
  padding: 0.55rem 0.85rem;
  border-radius: 8px;
  color: var(--fg);
  text-decoration: none;
  font-size: 0.92rem;
  font-weight: 500;
}
.settings-nav-item:hover {
  background: var(--panel-2);
}
.settings-nav-item.active {
  background: var(--accent-dim);
  color: var(--accent);
  font-weight: 700;
}
@media (max-width: 700px) {
  .settings-shell { grid-template-columns: 1fr; }
  .settings-sidebar {
    position: static;
    flex-direction: row;
    gap: 0.4rem;
    border-bottom: 1px solid var(--border);
    padding-bottom: 0.5rem;
  }
}
```

- [ ] **Step 2: Wrap the two forms in `.settings-shell` + add sidebar**

Find the `<main>` (or whatever container holds the two forms). The forms today look like:

```html
<form method="post" action="/settings/schedule" id="schedule-form" data-panel-id="schedule">
  ...
</form>

<form method="post" action="/settings/work_centers" id="wc-form" data-panel-id="work_centers">
  ...
</form>
```

Wrap them in a `.settings-shell` div with a sidebar `<aside>` as the first child:

```html
<div class="settings-shell">
  <aside class="settings-sidebar" aria-label="Settings sections">
    <a href="?section=work_centers"
       class="settings-nav-item {% if active_section == 'work_centers' %}active{% endif %}">
      Work Centers &amp; Goals
    </a>
    <a href="?section=schedule"
       class="settings-nav-item {% if active_section == 'schedule' %}active{% endif %}">
      Company Schedule
    </a>
  </aside>

  <div class="settings-content">
    <form method="post" action="/settings/schedule" id="schedule-form"
          data-panel-id="schedule" data-section="schedule"
          {% if active_section != 'schedule' %}style="display:none"{% endif %}>
      ...
    </form>

    <form method="post" action="/settings/work_centers" id="wc-form"
          data-panel-id="work_centers" data-section="work_centers"
          {% if active_section != 'work_centers' %}style="display:none"{% endif %}>
      ...
    </form>
  </div>
</div>
```

(Keep `data-panel-id` for now — Task 3 removes it. The new `data-section` is what matters going forward.)

The two forms' inner content (everything between their `<form>` and `</form>`) stays unchanged in this task.

- [ ] **Step 3: Smoke tests**

```bash
C:\Users\dale.gruber\Projects\zira\.venv\Scripts\python.exe -c "from zira_dashboard.app import app; print(app.title)"
```
Expected: `Zira Station Dashboard`.

```bash
C:\Users\dale.gruber\Projects\zira\.venv\Scripts\python.exe -c "from zira_dashboard.deps import templates; templates.env.get_template('settings.html')"
```
Expected: no exception.

If you can run the dev server with a live `DATABASE_URL`, also browse to `/settings` and verify:
- Sidebar shows on the left.
- Work Centers & Goals form is visible by default; Company Schedule is hidden.
- Clicking "Company Schedule" navigates to `?section=schedule` and switches which form shows.
- Active sidebar item gets the green pill highlight.

If you can't run the server, the import + template-compile checks are the substitute.

- [ ] **Step 4: Commit**

```bash
git add src/zira_dashboard/templates/settings.html
git commit -m "feat(settings): left sidebar with section switching (work_centers default)"
```

---

## Task 3: Strip pin functionality + Notes column

**Files:**
- Modify: `src/zira_dashboard/templates/settings.html`

This task is pure removal — the sidebar (Task 2) already replaces the pin UX, and the Notes column was never useful.

- [ ] **Step 1: Strip the pin button HTML from both form headers**

Search the template for `pin-btn` button HTML. Two occurrences:

Around line 506 (Company Schedule):
```html
<button type="button" class="pin-btn" data-panel-id="schedule" title="Pin to top" aria-label="Pin">📌</button>
```

Around line 550 (Work Centers & Goals):
```html
<button type="button" class="pin-btn" data-panel-id="work_centers" title="Pin to top" aria-label="Pin">📌</button>
```

Delete both `<button>` lines. The surrounding `<h2>` heading stays.

- [ ] **Step 2: Strip pin-related CSS rules**

In the `<style>` block, delete every rule that references `.pin-btn` or `.pinned` or `pin-btn:hover` or `pin-btn.pinned`. Search for `pin-btn` and `pinned` in the style block; remove each rule (and its block of declarations).

Specifically the rules listed in the spec:

- `details.collapsible > summary .pin-btn { ... }` (around line 141)
- `.pin-btn { ... }` (around line 144)
- `.pin-btn:hover { ... }` (around line 156)
- `.pin-btn.pinned { ... }` (around line 157)
- `header .page-actions .pin-btn { ... }` (around line 175)
- `form.pinned > details.collapsible { ... }` (around line 337)

Also drop the `data-panel-id="schedule"` and `data-panel-id="work_centers"` attributes from the two `<form>` tags (the pin code referenced them; they're not needed for the sidebar). The `data-section="schedule"` and `data-section="work_centers"` from Task 2 remain.

- [ ] **Step 3: Strip the pin JS block**

Find the `<script>` section containing `// --- Pin panels to top + auto-expand on load ---` (around line 765). Delete the entire IIFE / closure that defines `PINS_KEY`, `readPins`, `writePins`, `applyPins`, and the `.pin-btn` click handlers (around lines 765–810).

The rest of the `<script>` block stays.

- [ ] **Step 4: Drop the Notes column from the Work Centers & Goals table**

In settings.html, find the WC table. It has a `<thead>` row with column headers and a `<tbody>` with one row per WC. Find the column for Notes:

- The `<th>` cell containing the text `Notes` in `<thead>`.
- The corresponding `<td>` cell in each WC row that contains a `<textarea>` named `wc__{{ wc.key }}__note` (or similar).

Delete both. The other columns (work center name, required skills, goal/day, min/max ops, value stream, group, default people) stay.

After this step, the Work Centers form has one fewer column. The form still posts; just without a `note` field, which Task 1 already removed from the server's field-iterator tuple.

- [ ] **Step 5: Smoke tests**

```bash
C:\Users\dale.gruber\Projects\zira\.venv\Scripts\python.exe -c "from zira_dashboard.app import app; print(app.title)"
```
Expected: `Zira Station Dashboard`.

```bash
C:\Users\dale.gruber\Projects\zira\.venv\Scripts\python.exe -c "from zira_dashboard.deps import templates; templates.env.get_template('settings.html')"
```
Expected: no exception.

Verify all pin references are gone:
```bash
grep -nE "pin-btn|applyPins|settings-pins|data-panel-id" src/zira_dashboard/templates/settings.html
```
Expected: zero matches (or, if `data-panel-id` was kept on a tag I missed, only there — but ideally zero).

- [ ] **Step 6: Commit**

```bash
git add src/zira_dashboard/templates/settings.html
git commit -m "feat(settings): remove pin functionality + Notes column from WC table"
```

---

## Task 4: Smoke test on live deploy

This task is operator-only — push and verify in a browser.

- [ ] **Step 1: Push**

```bash
git push
```

Wait for Railway to redeploy (~2-3 min — pure template/route changes).

- [ ] **Step 2: Verify default landing**

Open `/settings` (no query param). Verify:
- Sidebar visible on the left.
- Work Centers & Goals form is shown.
- "Work Centers & Goals" sidebar item highlighted (light-green pill).
- "Company Schedule" sidebar item not highlighted.
- No 📌 pin buttons anywhere.
- Notes column gone from the WC table.

- [ ] **Step 3: Verify section switching**

Click "Company Schedule" in the sidebar. Verify:
- URL becomes `/settings?section=schedule`.
- Work Centers & Goals form hidden.
- Company Schedule form visible.
- "Company Schedule" sidebar item highlighted.

Click "Work Centers & Goals". Verify it returns to `/settings?section=work_centers` and the right form shows.

- [ ] **Step 4: Verify save flow preserves section**

On the Company Schedule form, edit any field (e.g., bump a break time by a minute). Save. Verify:
- After save, URL is `/settings?saved=1&section=schedule`.
- Company Schedule still visible.
- The "saved" toast/banner shows.

Switch back to Work Centers & Goals. Edit a goal value. Save. Verify URL is `/settings?saved=1&section=work_centers` with the right form active.

- [ ] **Step 5: Verify mobile breakpoint**

Resize the browser window narrower than 700px (or use DevTools' device toolbar). Verify:
- Sidebar moves above the form as a horizontal pill strip.
- Both items still clickable.
- Form below it still renders correctly.

- [ ] **Step 6: Done**

If steps 2–5 all pass, the feature is shipped. If anything looks off, follow up with a targeted fix.

---

## Acceptance Recap

After all tasks merge and deploy:

- ✅ `/settings` lands on Work Centers & Goals by default.
- ✅ Sidebar lists both sections; clicking switches which form shows.
- ✅ URL state (`?section=...`) reflects the active section; refresh keeps you there.
- ✅ Save flow (POST → 303 redirect) preserves the active section.
- ✅ No 📌 pin buttons, no pin CSS, no pin JS, no localStorage `settings-pins` reads.
- ✅ Work Centers & Goals table has no Notes column.
- ✅ Sidebar collapses to a horizontal pill strip below 700px viewport.
