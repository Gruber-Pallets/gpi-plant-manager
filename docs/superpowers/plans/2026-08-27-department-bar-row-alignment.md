# Department Bar-Row Alignment Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Keep each horizontal department bar and its actual/goal total on one aligned row at every supported widget width.

**Architecture:** The shared `recycling.css` already assigns a three-column grid to horizontal bar rows. Replace its fixed-width total column with intrinsic width, allow the bar column to shrink, and prohibit wrapping on the total itself. A focused static test will lock those CSS contracts without changing the Jinja chart markup, saved widget settings, or vertical-bar mode.

**Tech Stack:** CSS Grid, Jinja-rendered dashboard widgets, pytest static-source tests.

## Global Constraints

- Scope is presentation only; do not change production data, goal calculations, saved widget customization values, vertical bars, or number-position choices.
- Preserve the existing two-line name/work-center label and its ellipsis behavior.
- Keep actual/goal totals as one right-aligned, tabular-number value beside the corresponding bar.
- Add a plain-language, child-friendly `CHANGELOG.md` note for the implementation push.
- Preserve unrelated working-tree changes.

---

## File Structure

- `src/zira_dashboard/static/recycling.css` — shared CSS for Recycling and New department horizontal bar-widget rows.
- `tests/test_recycling_scaling_static.py` — static regression coverage for responsive bar-row grid constraints.
- `CHANGELOG.md` — user-facing description of the layout fix.

### Task 1: Keep horizontal department totals aligned with their bars

**Files:**
- Modify: `tests/test_recycling_scaling_static.py`
- Modify: `src/zira_dashboard/static/recycling.css:262-279`
- Modify: `CHANGELOG.md:12-13`

**Interfaces:**
- Consumes: Jinja's existing `.bar-row.numpos-widget` markup, which renders the label, `.bar-track`, and `.val` total in that order.
- Produces: a responsive three-column CSS Grid layout where `.val` remains a single right-aligned actual/goal value and the middle `.bar-track` may shrink.

- [ ] **Step 1: Write the failing static regression test**

  Add this test after `test_recycling_bar_widgets_default_taller` in `tests/test_recycling_scaling_static.py`:

  ```python
  def test_horizontal_bar_total_stays_beside_its_bar_when_narrow():
      assert (
          ".grid-stack-item-content .bar-row.numpos-widget {\n"
          "    grid-template-columns: minmax(0, clamp(6rem, 22cqw, 11rem)) "
          "minmax(0, 1fr) max-content;\n"
          "  }"
      ) in CSS
      assert (
          ".grid-stack-item-content .bar-row .val {\n"
          "    min-width: max-content;\n"
          "    white-space: nowrap;\n"
          "  }"
      ) in CSS
  ```

- [ ] **Step 2: Run the focused test to verify it fails**

  Run:

  ```bash
  .venv/bin/pytest tests/test_recycling_scaling_static.py::test_horizontal_bar_total_stays_beside_its_bar_when_narrow -v
  ```

  Expected: FAIL because the widget grid still ends in `4rem` and the row-total CSS has no non-wrapping intrinsic-width guard.

- [ ] **Step 3: Apply the minimal responsive grid fix**

  In `src/zira_dashboard/static/recycling.css`, replace the widget-position rule with:

  ```css
  .grid-stack-item-content .bar-row.numpos-widget {
    grid-template-columns: minmax(0, clamp(6rem, 22cqw, 11rem)) minmax(0, 1fr) max-content;
  }
  ```

  Add this row-total rule immediately after the `numpos-*` grid rules:

  ```css
  .grid-stack-item-content .bar-row .val {
    min-width: max-content;
    white-space: nowrap;
  }
  ```

  Leave the `numpos-bar`, `numpos-inside`, and `numpos-hidden` two-column rules unchanged, as those totals are deliberately placed on or inside the bar.

- [ ] **Step 4: Run the focused test to verify it passes**

  Run:

  ```bash
  .venv/bin/pytest tests/test_recycling_scaling_static.py::test_horizontal_bar_total_stays_beside_its_bar_when_narrow -v
  ```

  Expected: PASS (1 passed).

- [ ] **Step 5: Add the What’s New note**

  Under `## 2026-08-27`, before the existing subsections in `CHANGELOG.md`, add:

  ```markdown
  ### Bar totals stay lined up

  #### Fixes

  - **The number next to each work-center bar now stays next to its bar.** This makes it easier to see the work done and the goal together, even in a small box.
  ```

- [ ] **Step 6: Run regression verification**

  Run:

  ```bash
  .venv/bin/pytest tests/test_recycling_scaling_static.py tests/test_recycling_toolbar_static.py tests/test_recycling_operator_links_static.py -v
  git diff --check
  ```

  Expected: every selected pytest test passes; `git diff --check` exits with no output.

- [ ] **Step 7: Commit and push the implementation**

  Run:

  ```bash
  git add CHANGELOG.md src/zira_dashboard/static/recycling.css tests/test_recycling_scaling_static.py
  git commit -m "fix: align department bar totals"
  git push origin main
  ```

  Expected: one implementation commit containing only the changelog, shared CSS, and regression test is pushed to `origin/main`.
