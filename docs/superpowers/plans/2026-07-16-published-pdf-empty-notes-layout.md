# Published PDF Empty-Notes Layout Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Keep the published PDF’s Driving Bay label visible while improving Scheduled and Notes space without changing the live scheduler.

**Architecture:** Update the static Truck Driver Bay label in `staffing.py`. Update only print-media CSS for table proportions and use a `tr:has(.wc-note-print:empty)` selector to let scheduled content visibly occupy an empty Notes cell; actual notes keep their table boundary and wrapping.

**Tech Stack:** Python location configuration, CSS print media, Chromium/Playwright PDF output, pytest.

## Global Constraints

- Preserve the Transportation department and Truck Driver work-center name.
- Keep browser-screen scheduler styling and editing behavior unchanged.
- Keep PDF letter portrait and 9pt schedule type.
- Do not allow scheduled names to overlap a non-empty note.

---

### Task 1: Improve the published PDF’s empty-note rows

**Files:**
- Modify: `src/zira_dashboard/staffing.py`
- Modify: `src/zira_dashboard/static/staffing-print.css`
- Modify: `tests/test_staffing_department_for_wc.py`
- Modify: `tests/test_staffing_static.py`

**Interfaces:**
- Consumes: `LOCATIONS`, the Truck Driver `Location`, and print-only `.wc-note-print` cells emitted by `staffing.html`.
- Produces: a `Driving` Bay label and print-only spacing behavior used by browser and Slack PDFs.

- [ ] **Step 1: Write failing regression tests**

```python
def test_truck_driver_keeps_transportation_department_but_uses_driving_bay():
    location = staffing.location_by_name("Truck Driver")

    assert location is not None
    assert location.bay == "Driving"
    assert location.department == "Transportation"
```

Extend `test_staffing_print_balances_schedule_columns_and_keeps_fitting_name_pairs_inline` with:

```python
assert "table.sched thead th.wc-col { width: 28%; }" in css
assert "table.sched thead th.dept { width: 12%; }" in css
assert "table.sched thead th.sched-col { width: 35%; }" in css
assert "table.sched thead th.wc-note-col { width: 20.5%; }" in css
assert "tr:has(.wc-note-print:empty) .multi-dd .dd-summary-text" in css
assert "white-space: nowrap;" in css
```

- [ ] **Step 2: Verify the tests fail**

Run: `uv run --extra dev pytest tests/test_staffing_department_for_wc.py tests/test_staffing_static.py -q`

Expected: FAIL because Truck Driver still uses the Transportation Bay label and existing print width rules / empty-note selector differ.

- [ ] **Step 3: Make the minimal configuration and print-CSS changes**

In `src/zira_dashboard/staffing.py`, change only the Truck Driver location’s
Bay argument from `"Transportation"` to `"Driving"`.

In `src/zira_dashboard/static/staffing-print.css`, replace the existing
width declarations with:

```css
table.sched thead th.wc-col { width: 28%; }
table.sched thead th.dept { width: 12%; }
table.sched thead th.sched-col { width: 35%; }
table.sched thead th.wc-note-col { width: 20.5%; }

tr:has(.wc-note-print:empty) .multi-dd .dd-summary-text {
  white-space: nowrap;
}
```

- [ ] **Step 4: Verify the focused tests pass**

Run: `uv run --extra dev pytest tests/test_staffing_department_for_wc.py tests/test_staffing_static.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/zira_dashboard/staffing.py src/zira_dashboard/static/staffing-print.css tests/test_staffing_department_for_wc.py tests/test_staffing_static.py
git commit -m "style: improve published PDF empty-note rows"
```
