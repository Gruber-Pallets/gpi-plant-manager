# Timeclock Saturday Banner Modal Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Keep the Saturday plan as a banner on the timeclock home screen and make its schedule dialog open and close only on operator interaction.

**Architecture:** The timeclock home template continues to render the banner and its planned-Saturday dialog. A CSS `hidden` rule wins over the overlay's flex layout so the dialog is absent until the banner is selected. The existing person-selection route remains unchanged: it alone routes eligible people to the Yes / No / Decide Later Saturday-offer screen.

**Tech Stack:** FastAPI, Jinja templates, browser JavaScript, pytest.

## Global Constraints

- Do not change the `/timeclock/start/{person_id}` offer-routing priority.
- A planned-Saturday dialog must open only from the top-center banner.
- Close button, backdrop click, and Escape must dismiss the dialog.

---

### Task 1: Make the planned-Saturday dialog initially hidden and dismissible

**Files:**
- Modify: `src/zira_dashboard/templates/timeclock_base.html:139-148`
- Test: `tests/test_timeclock_home_static.py`

**Interfaces:**
- Consumes: `<div id="saturday-schedule-modal" class="k-modal-overlay" hidden>` in `timeclock_home.html`.
- Produces: a CSS rule that preserves the browser's `hidden` state despite `.k-modal-overlay { display:flex; }`.

- [ ] **Step 1: Write the failing test**

```python
def test_hidden_modal_overlay_is_not_displayed():
    html = Path("src/zira_dashboard/templates/timeclock_base.html").read_text()

    assert ".k-modal-overlay[hidden] { display: none; }" in html
```

- [ ] **Step 2: Run test to verify it fails**

Run: `ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_timeclock_home_static.py -q`

Expected: FAIL because the timeclock stylesheet lacks the explicit hidden-overlay rule.

- [ ] **Step 3: Write minimal implementation**

```css
.k-modal-overlay[hidden] { display: none; }
```

Place this immediately after the `.k-modal-overlay` declaration so its selector has greater specificity than the overlay's flex declaration.

- [ ] **Step 4: Run focused tests to verify they pass**

Run: `ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_timeclock_home_static.py tests/test_timeclock_saturday_recruiting.py -q`

Expected: PASS.

- [ ] **Step 5: Verify the entry-point contract**

Run: `ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_timeclock_saturday_recruiting.py -q`

Expected: PASS, including `test_name_tap_routes_eligible_employee_to_offer`, proving the Yes / No / Decide Later flow still starts only after a name is selected.
