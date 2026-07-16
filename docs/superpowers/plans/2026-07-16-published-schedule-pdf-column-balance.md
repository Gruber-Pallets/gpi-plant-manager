# Published Schedule PDF Column Balance Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Tighten the published-schedule PDF columns and allow fitting pairs of scheduled names to share a line.

**Architecture:** Modify only `staffing-print.css`, which is loaded with `media="print"` and inlined before Playwright produces the Slack PDF. Add a static regression assertion to lock the print-specific table geometry and inline-name behavior while leaving the interactive scheduler stylesheet untouched.

**Tech Stack:** Jinja2 template, CSS print media, Playwright PDF renderer, pytest.

## Global Constraints

- Keep the PDF letter portrait, 9pt schedule type, and current Notes-column width.
- Do not change `src/zira_dashboard/static/staffing.css` or interactive scheduling behavior.
- Two names share a line only when natural browser wrapping permits it; do not reduce type size or clip names.

---

### Task 1: Balance published-PDF schedule columns

**Files:**
- Modify: `tests/test_staffing_static.py`
- Modify: `src/zira_dashboard/static/staffing-print.css`

**Interfaces:**
- Consumes: `.sched`, `.bay`, `.station`, `.dept`, `.sched-cell`, and `.dd-summary-text > span[class^="lvl-"]` selectors already emitted by `src/zira_dashboard/templates/staffing.html`.
- Produces: print-only CSS declarations used by the browser print dialog and the published Slack PDF.

- [ ] **Step 1: Write the failing static regression test**

```python
def test_staffing_print_balances_schedule_columns_and_keeps_fitting_name_pairs_inline():
    css = _print_css()

    assert "table.sched { table-layout: fixed; }" in css
    assert "table.sched th.n, table.sched td.bay { width: 4.5rem; }" in css
    assert "table.sched th.wc-col { width: 31%; }" in css
    assert "table.sched th.dept { width: 14%; }" in css
    assert "table.sched th.sched-col { width: 34%; }" in css
    assert "table.sched th.wc-note-col { width: 16.5%; }" in css
    assert "table.sched th.wc-col,\ntable.sched td.station { padding-right: 2pt; }" in css
    assert "table.sched th.dept,\ntable.sched td.dept { padding-left: 2pt; padding-right: 2pt; }" in css
    assert "display: inline;" in css
    assert "margin-right: 0.45em;" in css
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `pytest tests/test_staffing_static.py::test_staffing_print_balances_schedule_columns_and_keeps_fitting_name_pairs_inline -v`

Expected: FAIL because the current print CSS has no explicit column geometry and forces each name span to `display: block`.

- [ ] **Step 3: Add the minimal print-only CSS**

Replace the existing print-only assigned-name rule with:

```css
.multi-dd .dd-summary-text > span[class^="lvl-"] {
  display: inline;
  margin-right: 0.45em;
}
```

Add the following after `table.sched { font-size: 9pt; }`:

```css
table.sched { table-layout: fixed; }
table.sched th.n, table.sched td.bay { width: 4.5rem; }
table.sched th.wc-col { width: 31%; }
table.sched th.dept { width: 14%; }
table.sched th.sched-col { width: 34%; }
table.sched th.wc-note-col { width: 16.5%; }
table.sched th.wc-col,
table.sched td.station { padding-right: 2pt; }
table.sched th.dept,
table.sched td.dept { padding-left: 2pt; padding-right: 2pt; }
```

- [ ] **Step 4: Run the focused regression test**

Run: `pytest tests/test_staffing_static.py::test_staffing_print_balances_schedule_columns_and_keeps_fitting_name_pairs_inline -v`

Expected: PASS.

- [ ] **Step 5: Run the staffing static test module**

Run: `pytest tests/test_staffing_static.py -v`

Expected: PASS with no failures.

- [ ] **Step 6: Commit**

```bash
git add tests/test_staffing_static.py src/zira_dashboard/static/staffing-print.css
git commit -m "style: balance published schedule PDF columns"
```
