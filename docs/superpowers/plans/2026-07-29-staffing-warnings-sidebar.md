# Staffing Warnings Sidebar Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move the live Plant Scheduler warning panel from the center schedule panel into the right sidebar after Notes and the floating Automater.

**Architecture:** Relocate the existing server-rendered warning node without changing its IDs, alert role, Jinja data, or JavaScript renderer. Add sidebar-scoped CSS that fits and scrolls long warning lists, while the existing breakpoint returns Notes, Automater, and warnings to normal source order on narrow screens.

**Tech Stack:** FastAPI/Jinja templates, CSS, vanilla JavaScript, pytest.

## Global Constraints

- The right-sidebar source order is Notes, floating Automater, then schedule warnings.
- Preserve warning generation, live validation, structured issue details, deduplication, severity, ordering, accessible alert behavior, and empty-state visibility.
- Keep the Automater's fixed desktop position and existing responsive return to normal flow.
- Long warning lists must wrap within the sidebar and use bounded vertical scrolling.
- Do not add a duplicate warning view, new API, new persisted data, or new dismissal behavior.
- Add a short child-friendly `CHANGELOG.md` entry in the implementation commit pushed to `origin/main`.

---

## File Structure

| File | Responsibility |
|---|---|
| `src/zira_dashboard/templates/staffing.html` | Own the warning panel's single DOM location and sidebar source order. |
| `src/zira_dashboard/static/staffing.css` | Fit, wrap, and scroll the warning panel in desktop and stacked sidebar layouts. |
| `tests/test_staffing_rotations.py` | Pin the warning node inside the sidebar after Notes and Automater, and cover its CSS contract. |
| `tests/test_staffing_static.py` | Preserve the existing accessible, live, safe-text warning renderer contract. |
| `CHANGELOG.md` | Explain the shipped layout change in simple user-facing words. |

### Task 1: Relocate and constrain the live warning panel

**Files:**

- Modify: `tests/test_staffing_rotations.py:2708-2740`
- Modify: `src/zira_dashboard/templates/staffing.html:230-254,385-424`
- Modify: `src/zira_dashboard/static/staffing.css:769-777,890-894`
- Verify: `tests/test_staffing_static.py:416-444`
- Modify: `CHANGELOG.md`

**Interfaces:**

- Consumes: Jinja `rotation_warnings` and `rotation_issues`; JavaScript
  `renderCoverageIssues(warnings, issues)` lookups of `#rotation-warnings` and
  `#rotation-warning-list`.
- Produces: one unchanged accessible warning node inside `aside.day-context`,
  after `.day-notes` and `.rotation-controls`, plus sidebar overflow rules.

- [ ] **Step 1: Update the template placement test so the current layout fails**

Replace
`test_staffing_keeps_automation_controls_in_the_notes_sidebar` with:

```python
def test_staffing_keeps_automation_controls_and_warnings_in_the_notes_sidebar():
    html = (ROOT / "src/zira_dashboard/templates/staffing.html").read_text()

    sidebar_start = html.index('<aside class="day-context">')
    sidebar_end = html.index("</aside>", sidebar_start)
    sidebar = html[sidebar_start:sidebar_end]
    main_start = html.index('<main class="panel">')
    main_end = html.index("</main>", main_start)
    main = html[main_start:main_end]

    notes_at = sidebar.index('class="day-notes"')
    automater_at = sidebar.index('class="rotation-controls" data-day="{{ day }}"')
    warnings_at = sidebar.index('id="rotation-warnings"')

    assert notes_at < automater_at < warnings_at
    assert 'id="rotation-auto-summary"' in sidebar
    assert 'id="reset-schedule-btn"' in sidebar
    assert 'id="clear-schedule-btn"' in sidebar
    assert 'id="rotation-warnings" role="alert"' in sidebar
    assert 'class="rotation-controls" data-day="{{ day }}"' not in main
    assert 'id="reset-schedule-btn"' not in main
    assert 'id="clear-schedule-btn"' not in main
    assert 'id="rotation-warnings"' not in main
```

- [ ] **Step 2: Update the CSS contract test so the unbounded sidebar fails**

Extend `test_staffing_notes_sidebar_is_sticky_and_mobile_safe` with:

```python
    assert ".day-context .rotation-warning {" in css
    assert "margin-top: 0.75rem; width: 100%;" in css
    assert "max-height: min(32rem, calc(100vh - 18rem));" in css
    assert "overflow-y: auto; overflow-wrap: anywhere;" in css
    assert ".day-context .rotation-warning { max-height: min(32rem, 60vh); }" in css
```

- [ ] **Step 3: Run the focused tests and verify the expected failures**

Run:

```bash
ZIRA_API_KEY=test .venv/bin/python -m pytest \
  tests/test_staffing_rotations.py::test_staffing_keeps_automation_controls_and_warnings_in_the_notes_sidebar \
  tests/test_staffing_rotations.py::test_staffing_notes_sidebar_is_sticky_and_mobile_safe \
  tests/test_staffing_static.py::test_rotation_warning_supports_structured_coverage_issues \
  -q
```

Expected: the placement test fails because `#rotation-warnings` is absent from
the sidebar, and the CSS contract test fails because no sidebar-scoped warning
size or overflow rules exist. The existing structured warning renderer test
continues to pass.

- [ ] **Step 4: Move the unchanged warning markup after the Automater**

Delete the current warning block between the title bar and
`scheduled_operator_name` macro in `main.panel`. Insert this same block after
the `{% endif %}` that closes `.rotation-controls` and before the right
sidebar's `</aside>`:

```jinja2
    <div class="rotation-warning" id="rotation-warnings" role="alert"
         {% if not rotation_warnings and not rotation_issues %}hidden{% endif %}>
      <ul id="rotation-warning-list">
        {% for issue in rotation_issues %}
          <li class="coverage-issue" data-issue-code="{{ issue.code }}">
            <span>{{ issue.message }}</span>
            {% if issue.rejections %}
              <details class="coverage-why">
                <summary>Why?</summary>
                <ul>
                  {% for rejection in issue.rejections %}
                    <li><strong>{{ rejection.person }}</strong>: {{ rejection.detail }}</li>
                  {% endfor %}
                </ul>
              </details>
            {% endif %}
          </li>
        {% endfor %}
        {% set issue_messages = rotation_issues | map(attribute='message') | list %}
        {% for warning in rotation_warnings %}
          {% if warning not in issue_messages %}
            <li>{{ warning }}</li>
          {% endif %}
        {% endfor %}
      </ul>
    </div>
```

Do not change `id`, `role`, hidden logic, iteration order, structured detail
markup, or deduplication.

- [ ] **Step 5: Add sidebar sizing and responsive overflow CSS**

Add after the base `.rotation-warning li` rule:

```css
  .day-context .rotation-warning {
    margin-top: 0.75rem; width: 100%; min-width: 0;
    max-height: min(32rem, calc(100vh - 18rem));
    overflow-y: auto; overflow-wrap: anywhere;
  }
```

Extend the existing `@media (max-width: 1100px)` block:

```css
    .day-context .rotation-warning { max-height: min(32rem, 60vh); }
```

Keep the existing `.day-context .rotation-controls { position: static; width:
auto; }` breakpoint rule so the stacked view follows DOM order without
overlap.

- [ ] **Step 6: Run the focused tests and verify they pass**

Run:

```bash
ZIRA_API_KEY=test .venv/bin/python -m pytest \
  tests/test_staffing_rotations.py::test_staffing_keeps_automation_controls_and_warnings_in_the_notes_sidebar \
  tests/test_staffing_rotations.py::test_staffing_notes_sidebar_is_sticky_and_mobile_safe \
  tests/test_staffing_static.py::test_rotation_warning_supports_structured_coverage_issues \
  tests/test_staffing_static.py::test_rotation_warning_success_schedules_authoritative_live_validation \
  -q
```

Expected: `4 passed`.

- [ ] **Step 7: Run the complete staffing regression set**

Run:

```bash
ZIRA_API_KEY=test .venv/bin/python -m pytest \
  tests/test_staffing_static.py \
  tests/test_staffing_rotations.py \
  tests/test_staffing_view.py \
  -q
```

Expected: all selected tests pass, with only environment-dependent tests
skipped.

- [ ] **Step 8: Add the child-friendly implementation patch note**

Add this entry above the earlier sidebar warning plan entry in
`CHANGELOG.md`:

```markdown
### 01:40 PM - Easier schedule warnings

- **Schedule warnings have their own spot.** Warnings now sit on the right below Notes and the Auto tools. The schedule stays easy to read, and long warning lists can scroll.
```

- [ ] **Step 9: Run full verification**

Run:

```bash
ZIRA_API_KEY=test .venv/bin/python -m pytest -q
.venv/bin/python -m ruff check .
git diff --check
```

Expected: the full test suite passes with only environment-dependent skips,
Ruff exits `0`, and `git diff --check` exits `0`.

- [ ] **Step 10: Commit and push the implementation**

```bash
git add \
  CHANGELOG.md \
  src/zira_dashboard/templates/staffing.html \
  src/zira_dashboard/static/staffing.css \
  tests/test_staffing_rotations.py
git commit -m "fix: move staffing warnings to sidebar"
git push origin main
```

## Self-review

- Spec coverage: Task 1 covers the exact Notes → Automater → warnings source
  order, unchanged live/accessibility contracts, long-list scrolling, desktop
  floating behavior, narrow-screen normal flow, testing, patch notes, and
  shipping.
- Placeholder scan: no placeholders, deferred steps, or undefined follow-up
  work remain.
- Type consistency: the existing DOM IDs and JavaScript renderer interface are
  unchanged; all new CSS selectors match the relocated template classes and
  test assertions.
