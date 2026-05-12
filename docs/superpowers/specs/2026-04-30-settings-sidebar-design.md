# Settings Sidebar — Design

**Date:** 2026-04-30
**Status:** Approved (brainstorming → implementation planning)

## Context

The settings page currently stacks two large forms vertically:

1. **Work Centers & Goals** (`data-panel-id="work_centers"`)
2. **Company Schedule** (`data-panel-id="schedule"`)

Each form has a 📌 pin button in its header. Pinning saves the panel
ID into localStorage (`settings-pins`); on next render, pinned forms
re-order to the top and auto-expand. Dale finds the pin pattern
unnecessary — Work Centers & Goals is the section he's almost always
in, so the goal becomes "make Work Centers & Goals the obvious default"
and "navigate between sections with a sidebar instead of pin-and-scroll."

Adjacent ask: drop the Notes column from the Work Centers & Goals
table — Dale doesn't use it.

## Goals

1. Replace the pin-and-scroll model with a left-side sidebar that lists
   the two sections.
2. Show only the active section's form at a time. Sidebar item gets a
   light-green pill highlight when active.
3. Default to Work Centers & Goals on first visit and whenever
   `?section=` is missing from the URL.
4. Persist the active section in the URL (`?section=work_centers` /
   `?section=schedule`) so refresh + sharing keep the user there.
5. Remove all pin functionality (HTML, CSS, JS, localStorage usage).
6. Drop the Notes column from the Work Centers & Goals table (and the
   matching form-input handler in the POST route).

## Non-goals

- Sub-section navigation. The sidebar lists two top-level items only;
  the Work Centers & Goals form keeps its current internal sub-areas
  (WC rows, Groups, Value Streams) as a single scrollable panel.
- Saving the active section in user-specific preferences. URL state is
  the source of truth; nothing in the database.
- Hamburger menu / drawer animations on mobile. Sidebar collapses to a
  horizontal pill strip above the form below ~700px viewport.
- Database migration to drop the `note` column on work centers.
  We stop reading/writing it from the form, but the underlying column
  stays — cleanup belongs in a follow-up if Dale wants to fully retire
  it.

## Design

### Page layout

```
┌───────── Header (logo + nav + Undo/Redo) ─────────┐
├──────────┬────────────────────────────────────────┤
│ Sidebar  │                                        │
│  ──────  │                                        │
│  Work    │   [ Active section's form fills this   │
│  Centers │     pane. Other forms set to           │
│  & Goals │     display: none. ]                   │
│  ──────  │                                        │
│ Company  │                                        │
│ Schedule │                                        │
│          │                                        │
└──────────┴────────────────────────────────────────┘
```

- Sidebar fixed at ~200px wide, sticky to the top of the main area.
- Right pane fills the rest of the width.
- Existing header (logo + Dashboards/Staffing/Settings nav + page-level
  Undo/Redo buttons) remains unchanged at the top of the page.

### Sidebar markup

```html
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
```

CSS shape:

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
    border-bottom: 1px solid var(--border);
    padding-bottom: 0.5rem;
  }
}
```

### Form visibility

Each existing form gains `data-section="<id>"` and a Jinja conditional
`style="display:none"` when not active:

```html
<form method="post" action="/settings/work_centers" id="wc-form"
      data-section="work_centers"
      {% if active_section != 'work_centers' %}style="display:none"{% endif %}>
  ...
</form>
<form method="post" action="/settings/schedule" id="schedule-form"
      data-section="schedule"
      {% if active_section != 'schedule' %}style="display:none"{% endif %}>
  ...
</form>
```

Hiding via `display:none` keeps the form's input state intact in the
DOM (so JS event handlers + the page-level Undo/Redo machinery still
attach), but it doesn't render. Switching sections with the sidebar
just navigates to a new URL — full page re-render — so DOM state
between sections is naturally clean.

### Server side

`settings_page` GET handler in `routes/settings.py`:

```python
@router.get("/settings", response_class=HTMLResponse)
def settings_page(
    request: Request,
    saved: int = Query(default=0),
    section: str = Query(default="work_centers"),
):
    if section not in ("work_centers", "schedule"):
        section = "work_centers"
    ...
    return templates.TemplateResponse(
        request,
        "settings.html",
        {
            ...existing context...
            "active_section": section,
        },
    )
```

The two POST handlers (`/settings/schedule` and `/settings/work_centers`)
update their redirect targets to preserve the section query param:

- `/settings/schedule` redirects to `/settings?saved=1&section=schedule`
- `/settings/work_centers` redirects to `/settings?saved=1&section=work_centers`

So after saving, the user stays on the same section.

### Removals (pin functionality)

1. **Template:** strip the `<button class="pin-btn">📌</button>` from both
   form headers (Company Schedule + Work Centers & Goals).
2. **CSS:** delete `.pin-btn`, `.pin-btn:hover`, `.pin-btn.pinned`,
   `form.pinned > details.collapsible`, the `details.collapsible >
   summary .pin-btn` margin rule, and any header-level pin button styles
   (search for `pin-btn` and `pinned`).
3. **JS:** delete the entire `// --- Pin panels to top + auto-expand on
   load ---` block (`PINS_KEY`, `readPins`, `writePins`, `applyPins`, the
   click handlers wiring it up).
4. **localStorage:** existing `settings-pins` keys in users' browsers
   become orphans. No cleanup code added — it's a few bytes per user
   and Dale's the only user.

### Notes column removal

In the Work Centers & Goals table:

1. **Template:** drop the `<th>` for "Notes" and the per-row `<td>` cell
   containing the `<textarea>` named `wc__<key>__note` (and the
   surrounding wrapper div if it has one).
2. **Server:** in the `/settings/work_centers` POST handler, remove
   `"note"` from the list of fields the loop iterates over (currently:
   `for field in ("goal_per_day", "min_ops", "max_ops", "note", "value_stream"):`).
   The DB column stays in the schema; it just stops being written from
   this form.

## Acceptance criteria

- Visiting `/settings` (no query param) shows the Work Centers & Goals
  form with that sidebar item highlighted; the Company Schedule form
  is not visible.
- Clicking "Company Schedule" in the sidebar navigates to
  `/settings?section=schedule`, hides Work Centers & Goals, shows
  Company Schedule, and highlights the Company Schedule sidebar item.
- Refreshing the page on either URL keeps the user on the same section.
- Saving the Work Centers form returns the user to `/settings?saved=1&section=work_centers`
  with the success toast visible and Work Centers still active.
- Saving the Company Schedule form returns to `/settings?saved=1&section=schedule`.
- No `📌` pin button visible anywhere on the page.
- Pin-related JS / CSS rules are gone (a grep for `pin-btn`, `applyPins`,
  `settings-pins` returns zero matches).
- Work Centers & Goals table no longer has a Notes column. Saving the
  form does not write a `note` field to any work center.
- On a viewport < 700px, the sidebar items render as a horizontal pill
  strip above the form.

## Risks

- **Hidden-form input state quirks.** Setting `display:none` on the
  inactive form keeps its inputs in the DOM but means form-validation
  events (`required`, `submit` handlers) still apply if anything tries
  to submit it. Mitigation: only the visible form has a Save button on
  screen, so this is a non-issue in practice.
- **Page-level Undo/Redo scope.** The Undo/Redo buttons in the header
  currently work across whatever's on the page. They'll still attach
  to both forms even when one is hidden — desirable behavior (you can
  undo a save you made on a different section). Worth verifying after
  ship.
- **localStorage `settings-pins` orphan.** Existing users have a
  per-browser localStorage key that no code reads anymore. Cosmetic
  only; no broken behavior.

## File touch list

- Modify: `src/zira_dashboard/templates/settings.html`
  - Add sidebar markup + grid wrapper.
  - Add `data-section="..."` and conditional `style="display:none"` to
    both forms.
  - Strip `.pin-btn` HTML from both form headers.
  - Strip the Notes column from the WC table (`<th>` + `<td>`).
  - Strip the entire pin-related `<style>` block + the
    `// --- Pin panels to top + auto-expand on load ---` `<script>` block.
  - Add the new `.settings-shell`, `.settings-sidebar`,
    `.settings-nav-item` CSS rules in the `<style>` block.
- Modify: `src/zira_dashboard/routes/settings.py`
  - `settings_page` accepts `?section=` Query param, validates against
    allowed values, passes `active_section` into context.
  - `settings_save_schedule` redirects to `?saved=1&section=schedule`.
  - `settings_save_work_centers` redirects to `?saved=1&section=work_centers`.
  - Drop `"note"` from the WC field-name tuple in the WC POST handler.
