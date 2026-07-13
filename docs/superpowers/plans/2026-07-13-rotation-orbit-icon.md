# Rotation Orbit Icon Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the People Matrix scheduling-preferences swap arrows with the selected three-dot Orbit icon.

**Architecture:** This is a template-only visual change. The existing button remains the event target and retains every behavioral and accessible attribute; only its decorative inline SVG changes. A focused static-template assertion protects the icon contract.

**Tech Stack:** Jinja2 HTML template, inline SVG, pytest.

## Global Constraints

- Keep `.rotation-open-btn` at its current size and preserve its current class, data attributes, title, accessible label, dialog semantics, and event handling.
- The new SVG must use `currentColor`, rounded line caps and joins, and fit the existing `viewBox="0 0 24 24"`.
- Do not change CSS, JavaScript, route code, or scheduling behavior.

---

### Task 1: Replace the rotation icon and protect its template contract

**Files:**
- Modify: `tests/test_staffing_rotations.py:623-632`
- Modify: `src/zira_dashboard/templates/skills.html:74-76`

**Interfaces:**
- Consumes: The existing `.rotation-open-btn` button, which `src/zira_dashboard/static/skills-page.js` queries and binds to the Scheduling Preferences dialog.
- Produces: A decorative `.rotation-open-icon` SVG showing a clockwise circular path, arrowhead, and three filled connection dots, with the existing button interface unchanged.

- [ ] **Step 1: Write the failing static-template assertion**

  In `test_skills_matrix_exposes_scheduling_preferences_and_recycled_training`, add the following assertions after the existing `"Scheduling Preferences"` assertion:

  ```python
  assert 'class="rotation-open-btn"' in html
  assert 'aria-label="Scheduling preferences for {{ p.name }}"' in html
  assert 'd="M17 7.5A8.5 8.5 0 1 0 19.5 16"' in html
  assert '<circle cx="12" cy="3.5" r="1.45"' in html
  assert '<circle cx="19.5" cy="15.5" r="1.45"' in html
  assert '<circle cx="6.5" cy="18" r="1.45"' in html
  ```

- [ ] **Step 2: Run the focused test to verify it fails**

  Run:

  ```bash
  ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_staffing_rotations.py::test_skills_matrix_exposes_scheduling_preferences_and_recycled_training -v
  ```

  Expected: FAIL because the current SVG contains the two-way swap path rather than the Orbit path and dots.

- [ ] **Step 3: Replace only the decorative SVG**

  In `src/zira_dashboard/templates/skills.html`, replace the current one-path SVG inside `.rotation-open-btn` with:

  ```html
  <svg class="rotation-open-icon" viewBox="0 0 24 24" aria-hidden="true" focusable="false" fill="none" stroke="currentColor" stroke-linecap="round" stroke-linejoin="round" stroke-width="2">
    <path d="M17 7.5A8.5 8.5 0 1 0 19.5 16"/>
    <path d="M17 7.5h-4m4 0v4"/>
    <circle cx="12" cy="3.5" r="1.45" fill="currentColor" stroke="none"/>
    <circle cx="19.5" cy="15.5" r="1.45" fill="currentColor" stroke="none"/>
    <circle cx="6.5" cy="18" r="1.45" fill="currentColor" stroke="none"/>
  </svg>
  ```

  Do not alter the enclosing button or any other template content.

- [ ] **Step 4: Run focused verification**

  Run:

  ```bash
  ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_staffing_rotations.py::test_skills_matrix_exposes_scheduling_preferences_and_recycled_training -v
  ```

  Expected: PASS.

- [ ] **Step 5: Run the relevant regression file**

  Run:

  ```bash
  ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_staffing_rotations.py -v
  ```

  Expected: PASS.

- [ ] **Step 6: Commit the implementation**

  ```bash
  git add src/zira_dashboard/templates/skills.html tests/test_staffing_rotations.py
  git commit -m "feat: use orbit icon for scheduling preferences"
  ```

## Plan Self-Review

- **Spec coverage:** Task 1 replaces the SVG with the chosen Orbit path and three dots, preserves every button behavior/accessibility attribute, and verifies the template contract.
- **Placeholder scan:** No placeholders or deferred implementation steps remain.
- **Type consistency:** No application interfaces or types change; the plan uses existing template classes and attributes exactly.
