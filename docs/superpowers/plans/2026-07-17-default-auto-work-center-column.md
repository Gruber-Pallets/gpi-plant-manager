# Default Auto Work Center Column Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Align every default Auto work-center checkbox with its work-center row in Settings.

**Architecture:** Move the existing checkbox controls from their standalone Settings grid into a new `Auto` column in the existing `wc-table`. Preserve the submitted field names and hidden presence indicator, so no route or persistence code changes.

**Tech Stack:** Jinja2, FastAPI, pytest.

## Global Constraints

- Keep the `default_auto_work_centers` field name and `default_auto_work_centers_present=1` submission contract unchanged.
- Each checkbox must render within the corresponding `wc_rows` table row.
- The setting continues to apply only when a new staffing day is created.
- Do not change Settings route, persistence, or daily Auto work-center behavior.

---

### Task 1: Put the default Auto control in the work-center table

**Files:**
- Modify: `src/zira_dashboard/templates/settings.html:73-105,194-198`
- Modify: `tests/test_settings_auto_work_centers.py:4-9`

**Interfaces:**
- Consumes: `wc_rows` and `default_auto_work_centers` provided by the existing Settings page context.
- Produces: one `default_auto_work_centers` checkbox in the `Auto` column for every work-center row.
- Preserves: the Settings form's `default_auto_work_centers_present` hidden input and all existing POST field names.

- [ ] **Step 1: Write the failing template regression test**

  Replace the first test in `tests/test_settings_auto_work_centers.py` with:

  ```python
  def test_work_center_settings_render_auto_checkbox_column():
      html = Path("src/zira_dashboard/templates/settings.html").read_text()

      assert "<th>Auto</th>" in html
      assert '<input type="hidden" name="default_auto_work_centers_present" value="1">' in html
      assert html.index('<th>Auto</th>') < html.index('<th>Bay</th>')
      checkbox = 'name="default_auto_work_centers" value="{{ r.name }}"'
      assert checkbox in html
      assert html.index(checkbox) > html.index('{% for r in wc_rows %}')
      assert "default-auto-centers-grid" not in html
  ```

- [ ] **Step 2: Run the focused test to verify it fails**

  Run:

  ```bash
  ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_settings_auto_work_centers.py \
    -k render_auto_checkbox_column -q
  ```

  Expected: FAIL because the template has no `Auto` table header and still contains `default-auto-centers-grid`.

- [ ] **Step 3: Render the checkbox in an Auto table cell**

  In `src/zira_dashboard/templates/settings.html`, remove the standalone
  `default-auto-centers` section. Keep the hidden input immediately inside the
  form panel. Insert the new header after `Work Center`:

  ```jinja2
  <th>Work Center</th>
  <th>Auto</th>
  <th>Bay</th>
  ```

  In each `{% for r in wc_rows %}` row, insert the checkbox immediately after
  the work-center name cell:

  ```jinja2
  <td>{{ r.name }}</td>
  <td class="auto-cell">
    <input type="checkbox" name="default_auto_work_centers" value="{{ r.name }}"
           aria-label="Enable {{ r.name }} for Auto by default"
           {% if r.name in default_auto_work_centers %}checked{% endif %}>
  </td>
  <td class="hint">{{ r.bay }}</td>
  ```

  Update the table note to start with:

  ```jinja2
  <div class="note">Auto defaults are used only when a new staffing day is created. Saved days keep their own On/Off choices. Max blank = unlimited.
  ```

- [ ] **Step 4: Run focused Settings tests**

  Run:

  ```bash
  ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_settings_auto_work_centers.py -q
  ```

  Expected: PASS with all tests in the file passing.

- [ ] **Step 5: Run the related static template suite**

  Run:

  ```bash
  ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_settings_auto_work_centers.py tests/test_settings_group_defaults.py -q
  ```

  Expected: PASS with no failures.

- [ ] **Step 6: Commit the implementation**

  ```bash
  git add src/zira_dashboard/templates/settings.html tests/test_settings_auto_work_centers.py
  git commit -m "fix: align default auto work center controls"
  ```
