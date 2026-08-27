# Timeclock What's New Panel Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the timeclock's bottom Feedback bar with the shared top-right light-bulb control and full What's New panel used in GPI Plant Manager.

**Architecture:** The timeclock base will render the existing `_footer.html` component, which owns the shared modal, feedback controls, styling, and scripts. `footer.js` will treat `.k-header` as a valid control host and re-inject the shared control after HTMX replaces the timeclock screen.

**Tech Stack:** Jinja templates, vanilla JavaScript, CSS, HTMX, pytest static-template tests.

## Global Constraints

- Reuse `_footer.html`, `footer.css`, `footer.js`, and `_feedback.html`; do not create a timeclock-specific panel or feedback form.
- Use the existing accessible 44px outline light-bulb button, its `What's new` label, and its unread dot unchanged.
- Place the trigger in the upper-right header area: inside `.k-header-actions` when present, otherwise in `.k-header`.
- Keep the panel outside `#timeclock-screen` so HTMX navigation cannot remove it.
- Remove `.k-feedback-bar` and `#timeclock-feedback-open`; the old bottom control must not reserve screen space.
- Preserve the `gpi:feedback-opened` and `gpi:feedback-closed` idle-redirect behavior.
- Add only plain-language plan notes to `CHANGELOG.md`; do not describe this planned work as already visible in the timeclock.

---

## File Structure

- `src/zira_dashboard/templates/timeclock_base.html` — owns the timeclock document shell and will render the shared footer outside the HTMX swap target.
- `src/zira_dashboard/static/footer.js` — owns the shared light-bulb trigger and will recognize and refresh timeclock header hosts.
- `tests/test_timeclock_feedback_static.py` — verifies the timeclock shell uses the shared surface and retains its idle-redirect event contract.
- `tests/test_whatsnew_panel_static.py` — verifies the shared trigger supports both main-app and timeclock header layouts.

### Task 1: Render the shared What's New surface in the timeclock shell

**Files:**
- Modify: `src/zira_dashboard/templates/timeclock_base.html:218-275`
- Modify: `tests/test_timeclock_feedback_static.py:7-32`

**Interfaces:**
- Consumes: `_footer.html`, which renders `#changelog-modal`, includes `_feedback.html`, and loads `footer.css` and `footer.js`.
- Produces: a single shared What's New and feedback surface immediately after `#timeclock-screen`; the existing idle-redirect listener remains available to `feedback.js` events.

- [ ] **Step 1: Replace the old feedback-bar assertions with a failing shared-footer assertion**

```python
def test_timeclock_renders_shared_whats_new_panel_outside_htmx_swap():
    html = _html()

    screen_end = html.index("</div>", html.index('<div id="timeclock-screen">'))
    footer = html.index("{% include '_footer.html' %}")

    assert footer > screen_end
    assert "k-feedback-bar" not in html
    assert "timeclock-feedback-open" not in html
    assert "{% include '_feedback.html' %}" not in html
```

- [ ] **Step 2: Run the focused test and verify it fails**

Run: `pytest tests/test_timeclock_feedback_static.py -v`

Expected: FAIL because `timeclock_base.html` still contains `.k-feedback-bar`, `#timeclock-feedback-open`, and the direct `_feedback.html` include.

- [ ] **Step 3: Replace the private bottom bar with the existing shared footer component**

Delete the `.k-feedback-bar` and `.k-feedback-trigger` CSS blocks from `timeclock_base.html`. Replace the old persistent markup and direct feedback include after `#timeclock-screen` with this exact include:

```jinja
{% include '_footer.html' %}
```

Keep that include after the closing `</div>` for `#timeclock-screen` and before the existing idle-redirect script. Do not change the `feedbackPaused` event listeners.

- [ ] **Step 4: Run the focused test and verify it passes**

Run: `pytest tests/test_timeclock_feedback_static.py -v`

Expected: PASS, including `test_timeclock_idle_redirect_pauses_while_feedback_is_open`.

- [ ] **Step 5: Commit the self-contained template change**

```bash
git add src/zira_dashboard/templates/timeclock_base.html tests/test_timeclock_feedback_static.py && git commit -m "refactor: share timeclock whats new panel" && git push origin main
```

### Task 2: Mount the shared bulb in timeclock headers and restore it after HTMX navigation

**Files:**
- Modify: `src/zira_dashboard/static/footer.js:67-98,207-213`
- Modify: `tests/test_whatsnew_panel_static.py:148-156`

**Interfaces:**
- Consumes: `#timeclock-screen` as the HTMX swap target and `.k-header-actions` as the home-screen right-side action group.
- Produces: `injectButton()` mounts exactly one `.whatsnew-btn` in a main-app `<header>` or a timeclock `.k-header`, and refreshes it after timeclock swaps.

- [ ] **Step 1: Add failing static assertions for timeclock header mounting and HTMX refresh**

Replace `test_footer_js_uses_dedicated_header_slot_for_trigger` and add this second test:

```python
def test_footer_js_uses_dedicated_header_slot_for_trigger():
    js = JS.read_text(encoding="utf-8")

    assert "slot.className = 'whatsnew-slot'" in js
    assert "var slotParent = header.querySelector('.k-header-actions') || header;" in js
    assert "slotParent.appendChild(slot)" in js
    assert "header.children[header.children.length - 1].appendChild(btn)" not in js


def test_footer_js_mounts_timeclock_trigger_after_htmx_swaps():
    js = JS.read_text(encoding="utf-8")

    assert "document.querySelector('header, .k-header')" in js
    assert "document.body.addEventListener('htmx:afterSwap'" in js
    assert "event.target.id !== 'timeclock-screen'" in js
    assert "injectButton();" in js
    assert "refreshDot();" in js
```

- [ ] **Step 2: Run the focused test and verify it fails**

Run: `pytest tests/test_whatsnew_panel_static.py -v`

Expected: FAIL because `footer.js` currently searches only for `<header>`, appends its slot directly to that element, and does not listen for HTMX swaps.

- [ ] **Step 3: Add the smallest shared trigger extension**

In `injectButton()`, use this header and slot-parent selection:

```javascript
var header = document.querySelector('header, .k-header');
if (!header || header.querySelector('.whatsnew-btn')) return;
var slotParent = header.querySelector('.k-header-actions') || header;
slotParent.appendChild(slot);
```

Keep the existing `slot.appendChild(btn)` call and click handler. After the initial `DOMContentLoaded` branch, add a body listener that only responds when the timeclock swap target was replaced:

```javascript
document.body.addEventListener('htmx:afterSwap', function (event) {
  if (!event.target || event.target.id !== 'timeclock-screen') return;
  injectButton();
  refreshDot();
});
```

Do not modify the light-bulb SVG, its ARIA label, read-state keys, modal wiring, feedback controls, or TV-mode guard.

- [ ] **Step 4: Run shared-panel and timeclock regression coverage**

Run: `pytest tests/test_timeclock_feedback_static.py tests/test_whatsnew_panel_static.py -v`

Expected: PASS with the shared-footer test, timeclock-header assertions, icon contract, feedback-modal stacking, and idle-redirect tests all green.

- [ ] **Step 5: Run the full test suite**

Run: `pytest -q`

Expected: PASS with no failed tests.

- [ ] **Step 6: Commit and push the shared trigger change**

```bash
git add src/zira_dashboard/static/footer.js tests/test_whatsnew_panel_static.py && git commit -m "feat: add timeclock whats new trigger" && git push origin main
```

## Plan Self-Review

- Spec coverage: Task 1 removes the bottom bar, renders the shared panel outside HTMX content, and retains the idle-redirect contract. Task 2 provides the top-right host placement, full shared panel behavior, unread dot, and post-navigation trigger restoration.
- Scope: No feedback storage, changelog route, icon, modal, or timeclock punch behavior changes are included.
- Test coverage: Each production change begins with a focused failing static test, then runs focused regression coverage. The final full test suite verifies no broader regressions.
- Naming consistency: Every task uses the existing `injectButton`, `refreshDot`, `.k-header`, `.k-header-actions`, and `#timeclock-screen` names from the codebase.
