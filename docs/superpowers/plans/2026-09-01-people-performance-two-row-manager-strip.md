# People Performance Two-Row Manager Strip Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Put People dashboard counts and controls in a wrapping top band and source warnings in a full-width second band, with no overlap or horizontal scrollbar at any supported viewport.

**Architecture:** Add two semantic wrappers to the existing live manager form: `.pp-manager-primary` for the top band and `.pp-manager-actions` for freshness plus controls. Convert the strip to an automatic-height grid and use wrapping flex layouts inside each band; keep `.pp-source-warnings` conditional as the second direct child so it disappears cleanly when sources are healthy. Extend static contract tests and the Playwright preview test to prove the manager groups never scroll horizontally or overlap while retaining the existing sticky, live-refresh, date, and attention behavior.

**Tech Stack:** Jinja2 templates, CSS, pytest, FastAPI test client, Playwright Chromium

## Global Constraints

- The top band contains day totals on the left and updated time, date, and Needs attention controls on the right.
- Source-warning pills occupy a separate full-width band below the top band.
- Counts, controls, and warnings wrap; no manager-bar region uses horizontal scrolling.
- When there are no source warnings, the warning band is not rendered and the strip collapses to the top band only.
- The manager strip remains sticky and may grow vertically to fit its content.
- Do not change count values, warning text, filters, polling, focus restoration, schedule markers, section headers, person rows, calculations, or sorting.
- Preserve `data-pp-control-key`, `data-pp-auto-submit`, `#pp-live-status`, and all current form semantics.
- Preserve unrelated workspace changes and stage only the files named by each task.
- Write the shipped `CHANGELOG.md` note in short, common words that a 10-year-old can understand.

---

## File Map

- `src/zira_dashboard/templates/_people_performance_rows.html`: groups existing manager-strip content into the approved primary and warning bands without changing data or control behavior.
- `src/zira_dashboard/static/people-performance.css`: replaces the fixed single row and manager-local horizontal scrollers with automatic-height grid/flex wrapping.
- `tests/test_people_performance_template.py`: locks down wrapper order, conditional warning rendering, and existing live-control attributes.
- `tests/test_people_performance_static.py`: locks down sticky automatic height, wrapping, and the absence of manager-local horizontal overflow.
- `tests/test_preview_people_performance.py`: verifies real rendered geometry, readability, and no overlap/scrollbar at desktop, tablet, portrait-tablet, and phone widths.
- `CHANGELOG.md`: records the shipped two-row bar in plain language after browser validation passes.

### Task 1: Add Semantic Top and Action Groups

**Files:**
- Modify: `tests/test_people_performance_template.py:116-145`
- Modify: `src/zira_dashboard/templates/_people_performance_rows.html:9-32`

**Interfaces:**
- Consumes: the existing Jinja context values `working_now`, `worked_earlier`, `needs_attention`, `source_warnings`, `as_of`, `day`, `today`, `attention_only`, and `is_today`.
- Produces: `.pp-manager-strip > .pp-manager-primary`, `.pp-manager-primary > .pp-counts`, `.pp-manager-primary > .pp-manager-actions`, `.pp-manager-actions > .pp-updated`, `.pp-manager-actions > .pp-controls`, and optional `.pp-manager-strip > .pp-source-warnings`.

- [ ] **Step 1: Write the failing template-structure assertions**

Extend `test_page_uses_one_compact_live_manager_strip` with exact wrapper and ordering checks:

```python
def test_page_uses_one_compact_live_manager_strip(rendered_html):
    assert 'class="pp-manager-strip"' in rendered_html
    assert 'class="pp-manager-primary"' in rendered_html
    assert 'class="pp-manager-actions"' in rendered_html
    assert rendered_html.index('class="pp-manager-primary"') < rendered_html.index(
        'class="pp-source-warnings"'
    )
    primary = rendered_html.split('<div class="pp-manager-primary">', 1)[1].split(
        '<aside class="pp-source-warnings"', 1
    )[0]
    assert primary.index('class="pp-counts"') < primary.index('class="pp-manager-actions"')
    actions = primary.split('<div class="pp-manager-actions">', 1)[1]
    assert actions.index('class="pp-updated"') < actions.index('class="pp-controls"')
    assert 'id="pp-live-status"' in rendered_html
    assert '<strong>5</strong> working now' in rendered_html
    assert '<strong>1</strong> worked earlier' in rendered_html
    assert '<strong>4</strong> need attention' in rendered_html
    assert "Forklift data unavailable" in rendered_html
    assert 'name="day"' in rendered_html
    assert 'name="attention"' in rendered_html
    assert 'data-pp-control-key="day"' in rendered_html
    assert 'data-pp-control-key="attention"' in rendered_html
    assert 'data-pp-auto-submit' in rendered_html
    assert '>Apply<' not in rendered_html
    assert '>Today<' not in rendered_html
```

Extend the healthy-source test to prove the primary band remains while the second band disappears:

```python
    assert response.status_code == 200
    assert 'class="pp-manager-primary"' in response.text
    assert 'class="pp-manager-actions"' in response.text
    assert 'class="pp-source-warnings"' not in response.text
```

- [ ] **Step 2: Run the focused template tests and verify the new contract fails**

Run:

```bash
.venv/bin/python -m pytest \
  tests/test_people_performance_template.py::test_page_uses_one_compact_live_manager_strip \
  tests/test_people_performance_template.py::test_manager_strip_omits_warning_region_when_sources_are_healthy -q
```

Expected: both tests fail because `pp-manager-primary` and `pp-manager-actions` are not rendered yet.

- [ ] **Step 3: Group the existing form content without changing its behavior**

Replace the manager form at the top of `_people_performance_rows.html` with:

```html
  <form class="pp-manager-strip" action="/people-performance" method="get" aria-label="People dashboard controls">
    <div class="pp-manager-primary">
      <div class="pp-counts" aria-label="Day totals">
        <span><strong>{{ working_now }}</strong> working now</span>
        <span><strong>{{ worked_earlier }}</strong> worked earlier</span>
        <span><strong>{{ needs_attention }}</strong> need attention</span>
      </div>
      <div class="pp-manager-actions">
        <p class="pp-updated" id="pp-live-status" aria-live="polite">Updated {{ as_of }}</p>
        <div class="pp-controls">
          <label class="pp-date-control">
            <span class="sr-only">Date</span>
            <input data-pp-auto-submit data-pp-control-key="day" type="date" name="day" value="{{ day }}" max="{{ today }}">
          </label>
          <label class="pp-check">
            <input data-pp-auto-submit data-pp-control-key="attention" type="checkbox" name="attention" value="1"{% if attention_only %} checked{% endif %}>
            Needs attention
          </label>
          {% if not is_today %}<a data-pp-control-key="today" href="/people-performance">Today</a>{% endif %}
        </div>
      </div>
    </div>
    {% if source_warnings %}
    <aside class="pp-source-warnings" role="status" aria-label="Source warnings">
      {% for warning in source_warnings %}<span>{{ warning }}</span>{% endfor %}
    </aside>
    {% endif %}
  </form>
```

- [ ] **Step 4: Run the template and live-refresh contract tests**

Run:

```bash
.venv/bin/python -m pytest \
  tests/test_people_performance_template.py \
  tests/test_people_performance_static.py::test_refresh_source_contract_places_capture_immediately_before_replacement \
  tests/test_people_performance_static.py::test_controller_runtime_handles_details_races_navigation_and_teardown -q
```

Expected: all selected tests pass, proving the wrappers did not remove the stable control keys or live status.

- [ ] **Step 5: Commit the semantic structure**

```bash
git add tests/test_people_performance_template.py src/zira_dashboard/templates/_people_performance_rows.html
git commit -m "refactor: group people manager strip rows"
```

### Task 2: Replace Manager-Local Scrolling and Prove the Browser Geometry

**Files:**
- Modify: `tests/test_people_performance_static.py:59-72,97-155`
- Modify: `tests/test_preview_people_performance.py:124-219`
- Modify: `src/zira_dashboard/static/people-performance.css:11-124,551-610`

**Interfaces:**
- Consumes: the `.pp-manager-primary` and `.pp-manager-actions` wrappers introduced in Task 1.
- Produces: an automatic-height two-band grid; wrapping `.pp-counts`, `.pp-manager-actions`, `.pp-controls`, and `.pp-source-warnings`; no `overflow-x: auto` on any manager-strip child; Playwright proof at all four existing viewport sizes.

- [ ] **Step 1: Replace old fixed-height and mobile-scroller assertions with the new CSS contract**

Replace `test_manager_strip_is_sticky_and_overflow_stays_local` with:

```python
def test_manager_strip_is_sticky_and_manager_groups_wrap_without_scrollbars():
    css = CSS_PATH.read_text(encoding="utf-8")
    strip = re.search(r"\.pp-manager-strip\s*\{([^}]*)\}", css)
    primary = re.search(r"\.pp-manager-primary\s*\{([^}]*)\}", css)
    actions = re.search(r"\.pp-manager-actions\s*\{([^}]*)\}", css)
    warnings = re.search(r"\.pp-source-warnings\s*\{([^}]*)\}", css)
    horizontal = re.search(r"\.pp-horizontal-scroll\s*\{([^}]*)\}", css)

    assert strip
    assert "position: sticky" in strip.group(1)
    assert "display: grid" in strip.group(1)
    assert "height: 2.75rem" not in strip.group(1)
    assert "box-sizing: border-box" in strip.group(1)
    assert "overflow" not in strip.group(1)
    assert primary and "flex-wrap: wrap" in primary.group(1)
    assert actions and "flex-wrap: wrap" in actions.group(1)
    assert warnings and "flex-wrap: wrap" in warnings.group(1)
    assert "overflow-x" not in warnings.group(1)
    assert horizontal and "overflow-x: auto" in horizontal.group(1)
    assert ".pp-page { overflow-x" not in css
```

Replace `test_compact_mobile_strip_uses_two_rows_without_an_empty_warning_cell` with:

```python
def test_manager_groups_wrap_at_all_widths_without_local_horizontal_scroll():
    css = CSS_PATH.read_text(encoding="utf-8")
    controls = re.search(r"\.pp-controls\s*\{([^}]*)\}", css)
    shared = re.search(
        r"\.pp-counts,\s*\.pp-source-warnings,\s*\.pp-controls\s*\{([^}]*)\}",
        css,
    )
    warning_pill = re.search(r"\.pp-source-warnings span\s*\{([^}]*)\}", css)
    mobile = css.split("@media (max-width: 760px)", 1)[1]

    assert shared and "flex-wrap: wrap" in shared.group(1)
    assert controls and "flex-wrap: wrap" in controls.group(1)
    assert "overflow-x" not in controls.group(1)
    assert warning_pill and "white-space: normal" in warning_pill.group(1)
    assert "overflow-wrap: anywhere" in warning_pill.group(1)
    for selector in (".pp-counts", ".pp-source-warnings", ".pp-controls"):
        rule = re.search(rf"{re.escape(selector)}\s*\{{([^}}]*)\}}", mobile)
        assert not rule or "overflow-x" not in rule.group(1)
```

In `test_compact_header_media_queries_preserve_shared_layout_contracts`, remove `mobile_strip` and its assertions about the old two-column mobile grid. Keep the section-row grid checks unchanged.

- [ ] **Step 2: Write the failing browser geometry assertions**

Rename the preview test to `test_preview_fits_all_manager_viewports_with_two_nonoverlapping_bands`.

In the evaluated geometry object, replace `managerRows`, `warningOverflowContained`, and `controlsReachable` with:

```javascript
                      managerGroupsDoNotScroll: [...document.querySelectorAll(
                        '.pp-counts, .pp-source-warnings, .pp-controls'
                      )].every(group => (
                        group.scrollWidth <= group.clientWidth + 0.5
                        && !['auto', 'scroll'].includes(getComputedStyle(group).overflowX)
                      )),
                      primaryAndWarningsDoNotOverlap: (() => {
                        const primary = document.querySelector('.pp-manager-primary');
                        const warnings = document.querySelector('.pp-source-warnings');
                        if (!primary || !warnings) return false;
                        const primaryBox = primary.getBoundingClientRect();
                        const warningBox = warnings.getBoundingClientRect();
                        return warningBox.top >= primaryBox.bottom - 0.5;
                      })(),
                      primaryGroupsDoNotOverlap: (() => {
                        const counts = document.querySelector('.pp-counts');
                        const actions = document.querySelector('.pp-manager-actions');
                        const countBox = counts.getBoundingClientRect();
                        const actionBox = actions.getBoundingClientRect();
                        const separatedHorizontally = countBox.right <= actionBox.left + 0.5
                          || actionBox.right <= countBox.left + 0.5;
                        const separatedVertically = countBox.bottom <= actionBox.top + 0.5
                          || actionBox.bottom <= countBox.top + 0.5;
                        return separatedHorizontally || separatedVertically;
                      })(),
                      warningTextFits: [...document.querySelectorAll('.pp-source-warnings span')]
                        .every(warning => warning.scrollWidth <= warning.clientWidth + 0.5),
                      controlsReachable: [...document.querySelectorAll('.pp-controls > *')]
                        .every(control => {
                          const box = control.getBoundingClientRect();
                          return box.left >= 0 && box.right <= window.innerWidth;
                        }),
```

Replace the old warning-overflow assertion and fixed-height/grid-row assertions with:

```python
                assert geometry["managerGroupsDoNotScroll"] is True
                assert geometry["primaryAndWarningsDoNotOverlap"] is True
                assert geometry["primaryGroupsDoNotOverlap"] is True
                assert geometry["warningTextFits"] is True
                assert geometry["controlsReachable"] is True
```

Retain all document-width, row-width, sticky-position, schedule/timeline overflow, polling, console-error, and screenshot assertions. Remove the `after["height"]` caps because automatic height is required.

- [ ] **Step 3: Run the CSS and browser tests and verify the old layout fails**

Run:

```bash
.venv/bin/python -m pytest \
  tests/test_people_performance_static.py::test_manager_strip_is_sticky_and_manager_groups_wrap_without_scrollbars \
  tests/test_people_performance_static.py::test_manager_groups_wrap_at_all_widths_without_local_horizontal_scroll \
  tests/test_people_performance_static.py::test_compact_header_media_queries_preserve_shared_layout_contracts \
  tests/test_preview_people_performance.py::test_preview_fits_all_manager_viewports_with_two_nonoverlapping_bands -q
```

Expected: the static tests fail because the strip is fixed-height and manager groups declare `overflow-x: auto`; the browser test fails because at least one manager group scrolls or overlaps.

- [ ] **Step 4: Implement the automatic-height two-band layout**

Replace the manager-strip rules through the warning-pill rules with:

```css
.pp-manager-strip {
  position: sticky;
  top: 0;
  z-index: 30;
  display: grid;
  box-sizing: border-box;
  gap: .35rem;
  padding: .3rem .5rem;
  border: 1px solid var(--border);
  border-radius: 8px;
  background: color-mix(in srgb, var(--panel) 96%, transparent);
  backdrop-filter: blur(8px);
}

.pp-manager-primary,
.pp-manager-actions {
  display: flex;
  min-width: 0;
  align-items: center;
  flex-wrap: wrap;
  gap: .4rem;
}

.pp-manager-primary {
  justify-content: space-between;
}

.pp-manager-actions {
  flex: 1 1 22rem;
  justify-content: flex-end;
}

.pp-updated {
  flex: 0 0 auto;
  margin: 0;
  color: var(--muted);
  font-size: .8rem;
  white-space: nowrap;
}

.pp-counts,
.pp-source-warnings,
.pp-controls {
  display: flex;
  min-width: 0;
  align-items: center;
  flex-wrap: wrap;
  gap: .4rem;
  margin: 0;
}

.pp-counts {
  flex: 0 1 auto;
}

.pp-controls {
  justify-content: flex-end;
}
```

Keep the existing label, input, button, count-pill, and warning-color rules, but replace the current `.pp-source-warnings` overflow rule with:

```css
.pp-source-warnings {
  width: 100%;
}
```

Extend the existing `.pp-source-warnings span` rule so long messages remain readable on a phone:

```css
.pp-source-warnings span {
  max-width: 100%;
  background: #ffedd5;
  color: #7c2d12;
  font-size: .78rem;
  white-space: normal;
  overflow-wrap: anywhere;
}
```

Delete the manager-strip, counts, warnings, controls, and old grid-placement rules at the start of `@media (max-width: 760px)`. Keep `main { padding: .7rem; }` and every section-row, summary, identity, timeline, and touch-layout rule that follows.

- [ ] **Step 5: Run the static, template, and browser tests**

Run:

```bash
.venv/bin/python -m pytest \
  tests/test_people_performance_static.py \
  tests/test_people_performance_template.py \
  tests/test_preview_people_performance.py::test_preview_fits_all_manager_viewports_with_two_nonoverlapping_bands -q
```

Expected: all tests pass, including all four viewport iterations in Chromium.

- [ ] **Step 6: Inspect the four generated screenshots**

Inspect:

```text
scripts/_preview_out/people_performance/people-performance-1440x900.png
scripts/_preview_out/people_performance/people-performance-1024x768.png
scripts/_preview_out/people_performance/people-performance-768x1024.png
scripts/_preview_out/people_performance/people-performance-390x844.png
```

Expected: counts and actions form the top band; warnings begin below it and use the full bar width; no group overlaps, clips, or shows a horizontal scrollbar; each long warning is readable; date and Needs attention remain visible.

- [ ] **Step 7: Commit the wrapping layout and browser regression**

```bash
git add \
  tests/test_people_performance_static.py \
  tests/test_preview_people_performance.py \
  src/zira_dashboard/static/people-performance.css
git commit -m "style: wrap people manager strip into two bands"
```

### Task 3: Record the Shipped Change and Run Final Validation

**Files:**
- Modify: `CHANGELOG.md:12`

**Interfaces:**
- Consumes: the validated two-band implementation from Tasks 1-2.
- Produces: a user-facing What's New entry and final repository-level evidence.

- [ ] **Step 1: Add the shipped note above the earlier plan notes**

Add this entry immediately below `## 2026-09-01`; leave the earlier plan notes unchanged:

```markdown
### Keep the People page bar clear

- **Warnings now have their own row in the People page bar.** The totals and controls stay above them, and everything wraps to fit without a sideways scroll bar.
```

- [ ] **Step 2: Run the complete People dashboard regression set**

Run:

```bash
.venv/bin/python -m pytest tests/test_people_performance_*.py tests/test_preview_people_performance.py -q
```

Expected: all selected tests pass with no failures.

- [ ] **Step 3: Run the full test suite and lint**

Run:

```bash
.venv/bin/python -m pytest -q
.venv/bin/ruff check .
```

Expected: the full suite passes and Ruff reports `All checks passed!`.

- [ ] **Step 4: Check the final diff for whitespace errors and scope**

Run:

```bash
git diff --check
git status --short
```

Expected: `git diff --check` prints nothing. Only `CHANGELOG.md` remains uncommitted from this task; any pre-existing unrelated workspace files remain untouched.

- [ ] **Step 5: Commit and push the completed implementation**

```bash
git add CHANGELOG.md
git commit -m "docs: announce two-row people manager strip"
git push origin main
```

Expected: `origin/main` advances through all implementation commits. A pushed plan alone does not count as completion; this final push occurs only after Tasks 1-3 and all validation steps pass.

- [ ] **Step 6: Verify the remote commit and report tracking status**

Run:

```bash
git rev-parse HEAD
git ls-remote --heads origin main
```

Expected: both hashes match. No Odoo improvement was identified in the request, so do not create, guess, or update an Odoo row; report that Odoo completion tracking was not applicable.
