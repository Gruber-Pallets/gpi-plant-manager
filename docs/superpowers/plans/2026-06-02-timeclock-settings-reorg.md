# Timeclock Settings Reorganization — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Reorganize the Settings → Timeclock panel into three sub-tabs (Schedules / Rules / Activity) with one consistent autosave behavior and tighter visual hierarchy — a layout refactor that changes no field names, no endpoints, and nothing about how punches sync to Odoo.

**Architecture:** The Timeclock panel is one `<section>` in a server-rendered Jinja template (`settings.html`). We wrap its existing blocks into three client-side tab panels, reorder the Rules content, wrap each rounding control in a shared `.rounding-card`, and extend the page's existing `attachAutosaver()` to the Rounding / Auto-Lunch / per-schedule forms so every field saves the same way (debounce → toast → Undo/Redo). The save endpoints already accept JSON, so the only server change is appending `#rules` to two redirect targets.

**Tech Stack:** FastAPI + Jinja2 templates, vanilla JS, hand-written CSS, pytest with `fastapi.testclient.TestClient` (Postgres-gated).

**Spec:** `docs/superpowers/specs/2026-06-02-timeclock-settings-reorg-design.md`

---

## File Structure

- `src/zira_dashboard/templates/settings.html` — the `#timeclock-panel` `<section>` (currently lines ~262–607) and the bottom `<script>` block. All markup restructuring + the tab JS + autosave wiring live here.
- `src/zira_dashboard/static/settings.css` — tab strip, `.rounding-card`, unified `.help` text, heading-spacing classes; remove dead rules.
- `src/zira_dashboard/routes/settings.py` — two one-line redirect-target edits (`/work_schedule_rounding/add` line ~588, `/remove` line ~601).
- `tests/test_settings_timeclock_layout.py` — **new** characterization + refactor tests.

**Regression guards that MUST stay green (do not edit these tests):**
- `tests/test_settings_saturday_schedule.py::test_get_settings_renders_saturday_panel` — asserts `"Saturday Default"` in the page.
- `tests/test_settings_auto_lunch.py` — asserts `"Auto-Lunch"` and `name="mode"` in the panel.
- `tests/test_settings_work_schedule_rounding.py` — asserts the three per-schedule endpoints behave and the panel renders 200 when Odoo is down.

**Test execution note:** all settings tests are gated by `pytest.mark.skipif(not os.environ.get("DATABASE_URL"))`. They run in CI / on Railway (where `DATABASE_URL` is set) and **skip cleanly** on a local box without Postgres. Treat "skipped locally, green in CI" as expected. Final visual confirmation is done with the preview workflow against a running dev server (`uvicorn zira_dashboard.app:app`).

---

## Task 1: Lock the field + endpoint contract (characterization tests)

These tests assert exactly what the refactor must NOT change: every `name=` field and `action=` endpoint in the Timeclock panel. They pass against the **current** template, then guard every later task. This is the safety net — write it first.

**Files:**
- Create: `tests/test_settings_timeclock_layout.py`

- [ ] **Step 1: Write the contract tests**

```python
"""Characterization + refactor tests for the Timeclock settings panel.

Tasks 1's two contract tests lock the field names and form endpoints that the
sub-tabs/autosave refactor must NOT change — they are the contract between the
settings UI and the punch -> Odoo hr.attendance sync path. They pass against the
PRE-refactor template and must stay green through every task. The remaining tests
are added by later tasks (each fails before its task, passes after).

Postgres-backed, same gate as the sibling settings tests.
"""
import os

import pytest
from fastapi.testclient import TestClient

from zira_dashboard.app import app
from zira_dashboard import db, work_schedule_store, odoo_client

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"), reason="needs Postgres"
)

client = TestClient(app)
CAL_ID = 990077


def _seed_override():
    db.execute("DELETE FROM work_schedules WHERE resource_calendar_id = %s", (CAL_ID,))
    work_schedule_store.create(CAL_ID, "Contract-Test Schedule")
    work_schedule_store.reload()


def _drop_override():
    db.execute("DELETE FROM work_schedules WHERE resource_calendar_id = %s", (CAL_ID,))
    work_schedule_store.reload()


def test_timeclock_panel_preserves_core_field_contract():
    r = client.get("/settings?section=timeclock")
    assert r.status_code == 200
    html = r.text
    # Company Schedule + Saturday Default fields
    for name in ("shift_start", "shift_end", "weekday_0", "weekday_6"):
        assert f'name="{name}"' in html, name
    # Default rounding windows
    for name in ("in_before_min", "in_after_min", "out_before_min", "out_after_min"):
        assert f'name="{name}"' in html, name
    # Auto-Lunch fields
    for name in ("mode", "flex_after_hours", "flex_minutes"):
        assert f'name="{name}"' in html, name
    # Always-rendered form endpoints
    for action in ("/settings/schedule", "/settings/saturday_schedule",
                   "/settings/rounding", "/settings/auto_lunch"):
        assert f'action="{action}"' in html, action


def test_timeclock_panel_preserves_per_schedule_contract(monkeypatch):
    # Seed one override (so its card + remove form render) and stub Odoo so the
    # "Add a schedule" form renders too.
    _seed_override()
    monkeypatch.setattr(
        odoo_client, "fetch_work_schedules",
        lambda: [{"id": CAL_ID + 1, "name": "Another Schedule"}],
    )
    try:
        r = client.get("/settings?section=timeclock")
        assert r.status_code == 200
        html = r.text
        assert 'name="resource_calendar_id"' in html
        for action in ("/settings/work_schedule_rounding",
                       "/settings/work_schedule_rounding/add",
                       "/settings/work_schedule_rounding/remove"):
            assert f'action="{action}"' in html, action
    finally:
        _drop_override()
```

- [ ] **Step 2: Run the tests to verify they pass against the current template**

Run: `pytest tests/test_settings_timeclock_layout.py -v`
Expected (CI / with `DATABASE_URL`): 2 passed. (Locally without Postgres: 2 skipped — also acceptable; the assertions are verified in CI.)

- [ ] **Step 3: Commit**

```bash
git add tests/test_settings_timeclock_layout.py
git commit -m "test(timeclock): characterize settings field + endpoint contract

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 2: Wrap the Timeclock panel into three sub-tabs

Wrap the existing blocks (unchanged internally, current order) into three tab panels, add the tab strip, and the switch JS. Default tab = Schedules; the URL hash reflects the active tab.

**Files:**
- Modify: `src/zira_dashboard/templates/settings.html` (the `#timeclock-panel` section + bottom `<script>`)
- Modify: `src/zira_dashboard/static/settings.css`
- Modify: `tests/test_settings_timeclock_layout.py`

- [ ] **Step 1: Write the failing test (tabs render)**

Append to `tests/test_settings_timeclock_layout.py`:

```python
def test_timeclock_panel_renders_subtabs():
    r = client.get("/settings?section=timeclock")
    assert r.status_code == 200
    html = r.text
    for marker in ('data-tc-tab="schedules"',
                   'data-tc-tab="rules"',
                   'data-tc-tab="activity"'):
        assert marker in html, marker
    for pid in ('id="tc-tab-schedules"',
                'id="tc-tab-rules"',
                'id="tc-tab-activity"'):
        assert pid in html, pid
```

- [ ] **Step 2: Run it to verify it fails**

Run: `pytest tests/test_settings_timeclock_layout.py::test_timeclock_panel_renders_subtabs -v`
Expected: FAIL (the `data-tc-tab` markers don't exist yet).

- [ ] **Step 3: Insert the tab strip + three wrapper divs**

In `settings.html`, inside `<section id="timeclock-panel">`, keep the heading + intro paragraph + the two "Open Timeclock" buttons (the `<h2>` through the `</div>` that closes the buttons row, ~lines 263–282) exactly as-is. **Immediately after that buttons `</div>`**, insert:

```html
    <!-- Sub-tabs: Schedules / Rules / Activity -->
    <div class="tc-tabs" role="tablist" aria-label="Timeclock settings">
      <button type="button" class="tc-tab active" data-tc-tab="schedules" role="tab" aria-selected="true">Schedules</button>
      <button type="button" class="tc-tab" data-tc-tab="rules" role="tab" aria-selected="false">Rules</button>
      <button type="button" class="tc-tab" data-tc-tab="activity" role="tab" aria-selected="false">Activity</button>
    </div>

    <div id="tc-tab-schedules" class="tc-panel" role="tabpanel" aria-label="Schedules">
      <!-- (Step 4 moves Company Schedule + Saturday Default here) -->
    </div>

    <div id="tc-tab-rules" class="tc-panel" role="tabpanel" aria-label="Rules" style="display:none">
      <!-- (Step 4 moves Rounding + Auto-Lunch + Per-schedule here, current order) -->
    </div>

    <div id="tc-tab-activity" class="tc-panel" role="tabpanel" aria-label="Activity" style="display:none">
      <!-- (Step 4 moves Sync status + Recent punches + Schedule variances here) -->
    </div>
```

- [ ] **Step 4: Move the existing blocks into the wrappers (no internal changes)**

Cut-and-paste, preserving each block's markup verbatim:
- Into `#tc-tab-schedules`: `<form id="schedule-form" …>` (Company Schedule) and `<form id="saturday-schedule-form" …>` (Saturday Default).
- Into `#tc-tab-rules` (keep current order for now): `<form id="rounding-form" …>`, then `<form id="auto-lunch-form" …>`, then `<div class="per-schedule-rounding" …>`.
- Into `#tc-tab-activity`: the `<h3>Sync status…</h3>` block + its `{% if %}` body, the `<h3>Recent punches</h3>` block + table, and the `<h3>Schedule variances …</h3>` block + table.

The `<section id="timeclock-panel">` still closes after `#tc-tab-activity`. Do not move anything out of the section.

- [ ] **Step 5: Add the tab CSS**

Append to `settings.css`:

```css
/* Timeclock sub-tabs (Schedules / Rules / Activity) */
.tc-tabs {
  display: flex; gap: 0.25rem;
  border-bottom: 1px solid var(--border);
  margin: 0.5rem 0 1.1rem;
}
.tc-tab {
  background: transparent; color: var(--muted);
  border: none; border-bottom: 2px solid transparent; border-radius: 0;
  padding: 0.5rem 0.95rem; margin-bottom: -1px;
  font: inherit; font-size: 0.9rem; font-weight: 600; cursor: pointer;
}
.tc-tab:hover { color: var(--fg); }
.tc-tab.active { color: var(--accent); border-bottom-color: var(--accent); }
```

- [ ] **Step 6: Add the tab-switch JS**

In the bottom `<script>` of `settings.html`, append (after the `initGroupQuickAdd` IIFE is fine):

```javascript
  // ---------- Timeclock sub-tabs (Schedules / Rules / Activity) ----------
  (function initTimeclockTabs() {
    const tablist = document.querySelector('.tc-tabs');
    if (!tablist) return;
    const tabs = [...tablist.querySelectorAll('[data-tc-tab]')];
    const panels = {
      schedules: document.getElementById('tc-tab-schedules'),
      rules:     document.getElementById('tc-tab-rules'),
      activity:  document.getElementById('tc-tab-activity'),
    };
    function activate(name) {
      if (!panels[name]) name = 'schedules';
      tabs.forEach(t => {
        const on = t.dataset.tcTab === name;
        t.classList.toggle('active', on);
        t.setAttribute('aria-selected', on ? 'true' : 'false');
      });
      for (const [k, el] of Object.entries(panels)) {
        if (el) el.style.display = (k === name) ? '' : 'none';
      }
    }
    tabs.forEach(t => t.addEventListener('click', () => {
      activate(t.dataset.tcTab);
      if (history.replaceState) history.replaceState(null, '', '#' + t.dataset.tcTab);
    }));
    const initial = (location.hash || '').replace('#', '');
    activate(panels[initial] ? initial : 'schedules');
  })();
```

- [ ] **Step 7: Run the tab test + the contract tests**

Run: `pytest tests/test_settings_timeclock_layout.py -v`
Expected: 3 passed (the two contract tests still pass — field names/endpoints unmoved; the new tab test passes).

- [ ] **Step 8: Commit**

```bash
git add src/zira_dashboard/templates/settings.html src/zira_dashboard/static/settings.css tests/test_settings_timeclock_layout.py
git commit -m "feat(timeclock): split settings panel into Schedules/Rules/Activity sub-tabs

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 3: Reorganize the Rules tab (reorder + IN/OUT pairing)

Within `#tc-tab-rules`: reorder to Rounding → Per-schedule rounding → Auto-Lunch, and wrap the default rounding grid plus each per-schedule grid in a shared `.rounding-card` so the IN/OUT columns read as one paired control.

**Files:**
- Modify: `src/zira_dashboard/templates/settings.html`
- Modify: `src/zira_dashboard/static/settings.css`
- Modify: `tests/test_settings_timeclock_layout.py`

- [ ] **Step 1: Write the failing test (Auto-Lunch comes after Per-schedule)**

Append to `tests/test_settings_timeclock_layout.py`:

```python
def test_rules_tab_orders_autolunch_after_per_schedule():
    r = client.get("/settings?section=timeclock")
    assert r.status_code == 200
    html = r.text
    # "Per-schedule rounding" heading must appear before the "Auto-Lunch" heading.
    assert "Per-schedule rounding" in html
    assert "Auto-Lunch" in html
    assert html.index("Per-schedule rounding") < html.index("Auto-Lunch"), \
        "Auto-Lunch should sit below the rounding block"
```

- [ ] **Step 2: Run it to verify it fails**

Run: `pytest tests/test_settings_timeclock_layout.py::test_rules_tab_orders_autolunch_after_per_schedule -v`
Expected: FAIL (currently Auto-Lunch is rendered before Per-schedule rounding).

- [ ] **Step 3: Reorder the Rules blocks**

In `#tc-tab-rules`, move `<form id="auto-lunch-form" …>` so it comes **after** the `<div class="per-schedule-rounding" …>` block. New order inside `#tc-tab-rules`:
1. `<form id="rounding-form" …>` (Default rounding)
2. `<div class="per-schedule-rounding" …>` (Per-schedule)
3. `<form id="auto-lunch-form" …>` (Auto-Lunch)

- [ ] **Step 4: Wrap the default rounding grid in a `.rounding-card`**

Inside `#rounding-form`, wrap the existing `<div class="rounding-grid"> … </div>` so it becomes:

```html
      <div class="rounding-card">
        <div class="rounding-grid">
          <!-- existing IN column (.rounding-col) and OUT column (.rounding-col) UNCHANGED -->
        </div>
      </div>
```

- [ ] **Step 5: Convert each per-schedule card to the same `.rounding-card` and wrap its grid**

In the `{% for ws in work_schedules %}` loop, change the card wrapper from its inline style to the class, and wrap its grid the same way:

Replace:
```html
      <div class="ws-rounding-card"
           style="border:1px solid var(--border);border-radius:8px;padding:0.8rem;margin:0.6rem 0">
```
with:
```html
      <div class="rounding-card ws-rounding-card">
```
and inside that card, wrap the per-schedule `<div class="rounding-grid"> … </div>` in `<div class="rounding-card-inner"> … </div>` is **not** needed — the `.rounding-card` already provides the frame; leave the per-schedule `.rounding-grid` as the card's direct child (the name/hours header row stays above it). Keep the hidden `resource_calendar_id` input, the IN/OUT columns, and both `<form>`s (window + remove) unchanged in this task.

- [ ] **Step 6: Add the `.rounding-card` + pairing CSS**

Append to `settings.css`:

```css
/* Rounding control card — shared by the default window and each per-schedule
   override so they read as one family. The two IN/OUT columns are visibly paired. */
.rounding-card {
  border: 1px solid var(--border);
  border-radius: 8px;
  background: var(--panel-2);
  padding: 0.7rem 0.9rem;
  margin: 0.5rem 0 1rem;
}
.rounding-card .rounding-grid { margin: 0.2rem 0 0; gap: 0; }
.rounding-card .rounding-col { padding: 0 1.2rem; }
.rounding-card .rounding-col + .rounding-col { border-left: 1px solid var(--border); }
@media (max-width: 700px) {
  .rounding-card .rounding-col { padding: 0; }
  .rounding-card .rounding-col + .rounding-col {
    border-left: none; border-top: 1px solid var(--border);
    margin-top: 0.6rem; padding-top: 0.6rem;
  }
}
```

(Visual judgment call deferred in the spec: if the vertical divider reads as too heavy against the card border, drop `border-left` and rely on the column padding alone — decide from a screenshot in Task 7.)

- [ ] **Step 7: Run the tests**

Run: `pytest tests/test_settings_timeclock_layout.py -v`
Expected: 4 passed (contract tests + tabs + the new ordering test).

- [ ] **Step 8: Commit**

```bash
git add src/zira_dashboard/templates/settings.html src/zira_dashboard/static/settings.css tests/test_settings_timeclock_layout.py
git commit -m "feat(timeclock): pair default + per-schedule rounding; Auto-Lunch below

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 4: Autosave the Rules forms; remove the three Save buttons

Extend `attachAutosaver` with a submit guard (so Enter no longer reloads these button-less forms), wire it to the Rounding / Auto-Lunch / per-schedule window forms, and delete the three explicit Save buttons and their flash markup.

**Files:**
- Modify: `src/zira_dashboard/templates/settings.html`
- Modify: `tests/test_settings_timeclock_layout.py`

- [ ] **Step 1: Write the failing test (Save buttons gone, autosave class present)**

Append to `tests/test_settings_timeclock_layout.py`:

```python
def test_rules_forms_have_no_explicit_save_buttons(monkeypatch):
    _seed_override()
    monkeypatch.setattr(
        odoo_client, "fetch_work_schedules",
        lambda: [{"id": CAL_ID + 1, "name": "Another Schedule"}],
    )
    try:
        r = client.get("/settings?section=timeclock")
        assert r.status_code == 200
        html = r.text
        # The old explicit Save buttons are gone (autosave replaces them).
        assert "Save Rounding" not in html
        assert "Save Auto-Lunch" not in html
        # The per-schedule window form is tagged for the autosaver.
        assert "ws-rounding-fields" in html
        # Structural action buttons remain.
        assert "Add a schedule" in html or 'action="/settings/work_schedule_rounding/add"' in html
        assert 'action="/settings/work_schedule_rounding/remove"' in html
    finally:
        _drop_override()
```

- [ ] **Step 2: Run it to verify it fails**

Run: `pytest tests/test_settings_timeclock_layout.py::test_rules_forms_have_no_explicit_save_buttons -v`
Expected: FAIL ("Save Rounding" is still in the page).

- [ ] **Step 3: Add the submit guard to `attachAutosaver`**

In `settings.html`, inside `attachAutosaver(form, url)`, alongside the existing `form.addEventListener('change', schedule);` / `form.addEventListener('input', …)` listeners, add:

```javascript
    // Button-less autosaved forms: Enter must save in place, never native-submit
    // (full reload). data-section forms are handled by the submit interceptor
    // above, so skip them here to avoid a double save.
    form.addEventListener('submit', (e) => {
      if (form.dataset.section) return;
      e.preventDefault();
      save();
    });
```

- [ ] **Step 4: Remove the Default-rounding Save button + flash**

In `#rounding-form`, delete the entire trailing block:

```html
      <div class="rounding-actions">
        {% if saved and active_section == 'timeclock' %}
          <span class="saved-flash">Saved.</span>
        {% endif %}
        <button type="submit">Save Rounding</button>
      </div>
```

Keep the `<p class="effective-note">…</p>` above it.

- [ ] **Step 5: Remove the Auto-Lunch Save button + flash**

In `#auto-lunch-form`, delete the same trailing `<div class="rounding-actions"> … <button type="submit">Save Auto-Lunch</button> </div>` block. Keep its `<p class="effective-note">…</p>`.

- [ ] **Step 6: Tag the per-schedule window form and drop its Save button**

In the `{% for ws in work_schedules %}` loop, change the window-edit form opening tag:

```html
        <form method="post" action="/settings/work_schedule_rounding">
```
to:
```html
        <form method="post" action="/settings/work_schedule_rounding" class="ws-rounding-fields">
```
and delete the `<button type="submit">Save</button>` inside that form. Leave the separate `…/remove` form and its Remove button untouched.

- [ ] **Step 7: Wire the autosavers**

In the bottom `<script>`, directly after the existing three `attachAutosaver(...)` calls (`schedule-form`, `saturday-schedule-form`, `wc-form`), add:

```javascript
  attachAutosaver(document.getElementById('rounding-form'), '/settings/rounding');
  attachAutosaver(document.getElementById('auto-lunch-form'), '/settings/auto_lunch');
  document.querySelectorAll('form.ws-rounding-fields').forEach(f => {
    attachAutosaver(f, '/settings/work_schedule_rounding');
  });
```

- [ ] **Step 8: Run the layout tests + the endpoint tests**

Run: `pytest tests/test_settings_timeclock_layout.py tests/test_settings_auto_lunch.py tests/test_settings_work_schedule_rounding.py -v`
Expected: all passed. The endpoint tests prove the save routes still persist; the layout test proves the buttons are gone and the autosave class is present.

- [ ] **Step 9: Commit**

```bash
git add src/zira_dashboard/templates/settings.html tests/test_settings_timeclock_layout.py
git commit -m "feat(timeclock): autosave Rounding/Auto-Lunch/per-schedule; drop Save buttons

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 5: Activity tab restyle + unified helper text & heading spacing

Replace the Activity tables' inline `style=` attributes with the page's standard table CSS, and standardize the under-heading helper text (`.help`) and heading spacing across the panel. No data or query changes.

**Files:**
- Modify: `src/zira_dashboard/templates/settings.html`
- Modify: `src/zira_dashboard/static/settings.css`
- Modify: `tests/test_settings_timeclock_layout.py`

- [ ] **Step 1: Write the failing test (help text unified)**

Append to `tests/test_settings_timeclock_layout.py`:

```python
def test_helper_text_unified_to_help_class():
    r = client.get("/settings?section=timeclock")
    assert r.status_code == 200
    html = r.text
    # The ad-hoc rounding blurb class is replaced by the shared .help style.
    assert 'class="rounding-blurb"' not in html
    assert 'class="help"' in html
```

- [ ] **Step 2: Run it to verify it fails**

Run: `pytest tests/test_settings_timeclock_layout.py::test_helper_text_unified_to_help_class -v`
Expected: FAIL (`class="rounding-blurb"` is still present).

- [ ] **Step 3: Add the `.help` + heading-spacing CSS**

Append to `settings.css`:

```css
/* One helper-text style for descriptions under headings inside the timeclock tabs */
.help {
  color: var(--muted);
  font-size: 0.8rem;
  line-height: 1.45;
  margin: 0.15rem 0 0.7rem;
}
/* Consistent heading rhythm within each tab panel */
.tc-panel h3 {
  margin: 1.1rem 0 0.3rem;
  font-size: 0.95rem; font-weight: 600; color: var(--fg);
}
.tc-panel > form:first-child h3,
.tc-panel > h3:first-child { margin-top: 0.2rem; }
.tc-panel table { width: 100%; border-collapse: collapse; }
```

- [ ] **Step 4: Unify the under-heading helper paragraphs to `.help`**

In the timeclock panel, change these to `class="help"` (drop inline styles and the `.rounding-blurb` class):
- The Rounding intro `<p class="note">Note: Some jurisdictions…</p>` and the `<p class="rounding-blurb">When clocking in or out…</p>`.
- The Auto-Lunch `<p class="note">Automatically signs employees out…</p>` and `<p class="rounding-blurb">For employees on an Odoo flexible schedule…</p>`.
- The Per-schedule `<p class="rounding-blurb">Give a specific Odoo work schedule…</p>`.
- The Saturday Default `<p class="note">Applied to Saturdays…</p>`.

Leave the two `<p class="effective-note">` lines as-is (they're a distinct footnote style, not under a heading). Leave the `.rounding-subhead` / `.rounding-col h4` headings as-is.

- [ ] **Step 5: Replace the Activity tables' inline styles with classes**

In `#tc-tab-activity`:
- Sync status: leave the `<ul>` content/logic untouched; if it carries an inline `style=` for layout, replace with a small `class="help"`-adjacent treatment or leave functional inline numerics. Do not change any `{{ … }}` expressions.
- Recent punches table: remove the inline `style="width:100%;border-collapse:collapse;font-size:0.85rem"` from `<table>` (now covered by `.tc-panel table`), and remove the inline `style="…"` from its `<thead><tr>` / `<th>` / `<tr>` / `<td>` — rely on the existing global `th, td` rules. Keep every `{{ p.* }}` expression and the `✓`/`⏳` synced markers exactly.
- Schedule variances table: same inline-style removal, keep all `{{ v.* }}` expressions and the heading text (`Schedule variances` + the `— "I'm somewhere else" overrides` small note).

- [ ] **Step 6: Remove now-dead CSS**

In `settings.css`, delete the unused `.rounding-blurb` rule, and the `.rounding-actions` / `.rounding-actions button` / `.saved-flash` rules (their markup was removed in Task 4). Leave `.effective-note` and `.note`.

- [ ] **Step 7: Run the full settings test set**

Run: `pytest tests/test_settings_timeclock_layout.py tests/test_settings_saturday_schedule.py tests/test_settings_auto_lunch.py tests/test_settings_work_schedule_rounding.py -v`
Expected: all passed.

- [ ] **Step 8: Commit**

```bash
git add src/zira_dashboard/templates/settings.html src/zira_dashboard/static/settings.css tests/test_settings_timeclock_layout.py
git commit -m "refactor(timeclock): unify helper text/heading spacing; restyle Activity tables

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 6: Return to the Rules tab after per-schedule Add / Remove

The per-schedule Add and Remove buttons round-trip the server (full reload). Append `#rules` to their redirect targets so the user lands back on the Rules tab.

**Files:**
- Modify: `src/zira_dashboard/routes/settings.py`
- Modify: `tests/test_settings_timeclock_layout.py`

- [ ] **Step 1: Write the failing tests (redirect lands on #rules)**

Append to `tests/test_settings_timeclock_layout.py`:

```python
def test_add_redirects_to_rules_tab(monkeypatch):
    monkeypatch.setattr(odoo_client, "fetch_work_schedules",
                        lambda: [{"id": CAL_ID, "name": "Drivers"}])
    monkeypatch.setattr(odoo_client, "fetch_calendar_hours",
                        lambda ids: {CAL_ID: {"0": ["05:45", "14:30"]}})
    _drop_override()
    try:
        r = client.post("/settings/work_schedule_rounding/add",
                        data={"resource_calendar_id": str(CAL_ID)},
                        follow_redirects=False)
        assert r.status_code == 303
        assert r.headers["location"].endswith("#rules")
    finally:
        _drop_override()


def test_remove_redirects_to_rules_tab():
    work_schedule_store.create(CAL_ID, "Drivers")
    work_schedule_store.reload()
    try:
        r = client.post("/settings/work_schedule_rounding/remove",
                        data={"resource_calendar_id": str(CAL_ID)},
                        follow_redirects=False)
        assert r.status_code == 303
        assert r.headers["location"].endswith("#rules")
    finally:
        _drop_override()
```

- [ ] **Step 2: Run them to verify they fail**

Run: `pytest tests/test_settings_timeclock_layout.py::test_add_redirects_to_rules_tab tests/test_settings_timeclock_layout.py::test_remove_redirects_to_rules_tab -v`
Expected: FAIL (current Location ends with `section=timeclock`, not `#rules`).

- [ ] **Step 3: Append `#rules` to the two redirect targets**

In `routes/settings.py`:
- In `settings_add_work_schedule` (the `/settings/work_schedule_rounding/add` handler), change the final return from
  `return RedirectResponse(url="/settings?saved=1&section=timeclock", status_code=303)`
  to
  `return RedirectResponse(url="/settings?saved=1&section=timeclock#rules", status_code=303)`
- In `settings_remove_work_schedule` (the `/settings/work_schedule_rounding/remove` handler), make the identical `#rules` change to its `RedirectResponse`.

Leave every other redirect (`/schedule`, `/saturday_schedule`, `/rounding`, `/auto_lunch`) unchanged — those use the JSON autosave path and don't redirect when JS is on.

- [ ] **Step 4: Run the redirect tests + the per-schedule endpoint tests**

Run: `pytest tests/test_settings_timeclock_layout.py tests/test_settings_work_schedule_rounding.py -v`
Expected: all passed (the existing `test_add_creates_override` / `test_remove_deletes_override` only assert status `303`, so the `#rules` suffix doesn't break them).

- [ ] **Step 5: Commit**

```bash
git add src/zira_dashboard/routes/settings.py tests/test_settings_timeclock_layout.py
git commit -m "feat(timeclock): per-schedule add/remove returns to the Rules tab

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 7: Full verification, visual polish, and ship

**Files:** none (verification) — then `CHANGELOG.md` for the ship step.

- [ ] **Step 1: Run the full affected test set**

Run: `pytest tests/test_settings_timeclock_layout.py tests/test_settings_saturday_schedule.py tests/test_settings_auto_lunch.py tests/test_settings_work_schedule_rounding.py -v`
Expected: all passed (in CI / with `DATABASE_URL`).

- [ ] **Step 2: Prove the Odoo-sync contract is byte-stable**

Confirm no field name or save endpoint drifted versus `main` (only the two `#rules` redirect suffixes should differ in the route):

Run: `git diff main -- src/zira_dashboard/routes/settings.py`
Expected: the ONLY logic-relevant change is `#rules` appended to the two `RedirectResponse` URLs. No `name=`/`action=`/handler changes.

- [ ] **Step 3: Visual verification via the preview workflow**

Start the dev server (`preview_start`, command `uvicorn zira_dashboard.app:app`, requires `DATABASE_URL`). Then:
- Load `/settings?section=timeclock`; confirm the three tabs render and **Schedules** is active by default.
- Click **Rules** and **Activity** — the visible panel switches; the URL hash updates; reloading `…#rules` lands on Rules.
- On **Rules**, confirm Default rounding and each per-schedule override render as the same bordered `.rounding-card` with IN/OUT clearly paired, and Auto-Lunch sits below them. Judge the IN/OUT divider against the screenshot; if heavy, drop `.rounding-card .rounding-col + .rounding-col { border-left }`.
- Edit a Default-rounding minute field, an Auto-Lunch field, and a per-schedule window field — each pops the top-center "Saved" toast within ~1s, persists across reload, and arms the header Undo; Undo reverts and re-saves. No Save buttons appear on those sections.
- Confirm **+ Add break**, per-schedule **Add a schedule**, and **Remove** (with its confirm) still work and Add/Remove return to the Rules tab.
- Capture `preview_screenshot` of each tab at desktop width and at ~1280px.

- [ ] **Step 4: Plan self-review against the spec**

Confirm every spec Done-criterion maps to a task: sub-tabs (T2), reorder + IN/OUT pairing (T3), autosave + button removal (T4), Activity grouping + unified help/spacing (T5), `#rules` return (T6), contract intact (T1 + T7 Step 2). Fix any gap before shipping.

- [ ] **Step 5: Ship (CHANGELOG + push — confirm with Dale first; it's a deploy)**

Add a new `### <TIME>` entry under the existing `## 2026-06-02` header in `CHANGELOG.md` (newest entries first within the day), e.g.:

```markdown
### <H:MM AM/PM>

- **Settings → Timeclock is reorganized into Schedules / Rules / Activity tabs** — the one long Timeclock settings panel is now three sub-tabs: **Schedules** (Company Schedule + Saturday Default), **Rules** (default + per-schedule rounding shown as one paired IN/OUT control, plus Auto-Lunch), and **Activity** (sync status, recent punches, schedule variances). Saving is now consistent everywhere — every field autosaves with the same "Saved" toast + Undo/Redo the schedules already used, so the old per-section Save buttons are gone (Add break / Add-or-Remove schedule stay as buttons). Pure layout/organization: no field, endpoint, or punch → Odoo `hr.attendance` sync behavior changed.
```

Then:
```bash
git add CHANGELOG.md
git commit -m "docs(changelog): Timeclock settings reorganized into tabs + consistent autosave

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
git push
```

(Push deploys to Railway. Per house rule, the CHANGELOG entry is required for the push to be meaningful — it drives the unread-indicator UX.)

---

## Self-Review

- **Spec coverage:** sub-tabs → T2; reorder + IN/OUT pairing → T3; autosave-everywhere + remove 3 buttons → T4; Activity grouping + unified help/spacing → T5; `#rules` return → T6; "field names + endpoints + Odoo sync untouched" → T1 contract + T7 Step 2 diff check. All Done-criteria mapped.
- **Placeholder scan:** none — every step has concrete code/commands and expected output.
- **Type/name consistency:** `data-tc-tab`, `#tc-tab-{schedules,rules,activity}`, `.tc-panel`, `.tc-tabs`/`.tc-tab`, `.rounding-card`, `.ws-rounding-fields`, `.help` are used identically across the template, CSS, JS, and tests. The autosave wiring matches the existing `attachAutosaver(form, url)` signature; the submit-guard's `form.dataset.section` check aligns with the existing `form[data-section]` interceptor so no form double-saves.
