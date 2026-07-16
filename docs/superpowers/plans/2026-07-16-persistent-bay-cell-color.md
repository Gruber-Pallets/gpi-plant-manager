# Persistent Bay Cell Color Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Keep staffing-schedule bay cells at their full, unchanged visible color regardless of whether their work center is enabled or disabled.

**Architecture:** The schedule replaces broad disabled-row dimming with a selector that dims only non-bay cells. The bay cell is therefore outside the opacity effect, while the existing background override continues to force `--panel-3`; this preserves its rendered background and text colors without affecting print styles or data.

**Tech Stack:** Python 3.11, pytest, Jinja templates, CSS custom properties.

## Global Constraints

- Apply only to the interactive staffing schedule in `src/zira_dashboard/static/staffing.css`.
- Preserve the existing disabled appearance for non-bay cells.
- Do not change print styles, templates, JavaScript, or scheduling data.
- Bay cells must retain their full background and text color when a work center is disabled.

---

### Task 1: Preserve bay-cell color across disabled work-center states

**Files:**
- Modify: `tests/test_staffing_static.py:20-35`
- Modify: `src/zira_dashboard/static/staffing.css:465-469`

**Interfaces:**
- Consumes: `_style() -> str`, which returns the contents of `src/zira_dashboard/static/staffing.css`.
- Produces: `test_staffing_disabled_rows_dim_non_bay_cells_only() -> None`, a static CSS regression test for the disabled-row dimming scope.

- [ ] **Step 1: Write the failing test**

Add this test after `test_staffing_bay_cells_keep_panel_background_across_work_center_states` in `tests/test_staffing_static.py`:

```python
def test_staffing_disabled_rows_dim_non_bay_cells_only():
    css = _style()

    dimmed_non_bay_cells = 'tr.work-center-off td:not(.bay) { opacity: 0.58; }'

    assert dimmed_non_bay_cells in css
    assert 'tr.work-center-off { opacity: 0.58; }' not in css
```

- [ ] **Step 2: Run the focused test to verify it fails**

Run: `pytest tests/test_staffing_static.py::test_staffing_disabled_rows_dim_non_bay_cells_only -v`

Expected: FAIL because disabled-row dimming still targets the entire row.

- [ ] **Step 3: Write the minimal implementation**

Replace the broad disabled-row opacity rule in `src/zira_dashboard/static/staffing.css`:

```css
  tr.work-center-off { opacity: 0.58; }
```

with this selector:

```css
  tr.work-center-off td:not(.bay) { opacity: 0.58; }
```

The selector is intentionally limited to non-bay cells in disabled work-center rows. Because the bay cell is no longer within an opaque ancestor, its background and text retain their full rendered color while non-bay cells remain dimmed.

- [ ] **Step 4: Run the focused test to verify it passes**

Run: `pytest tests/test_staffing_static.py::test_staffing_disabled_rows_dim_non_bay_cells_only -v`

Expected: PASS with `1 passed`.

- [ ] **Step 5: Run the staffing static CSS regression tests**

Run: `pytest tests/test_staffing_static.py -v`

Expected: PASS with no failures.

- [ ] **Step 6: Commit the implementation**

```bash
git add src/zira_dashboard/static/staffing.css tests/test_staffing_static.py
git commit -m "fix: preserve bay cell color"
```
