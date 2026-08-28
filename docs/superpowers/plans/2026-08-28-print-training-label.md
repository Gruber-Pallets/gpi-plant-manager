# Print Training Label Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make a printed Staffing schedule show a small `Training` label beside each active trainee's scheduled name instead of the large Training card.

**Architecture:** Reuse the existing day-scoped `active_training_blocks` template context. The Staffing template derives its trainee names and emits a print-only marker for matching assigned operators. The standard Staffing stylesheet hides that marker on screen, while the print stylesheet reveals it and hides the entire Training sidebar.

**Tech Stack:** Python 3.11, FastAPI/Jinja templates, CSS print media rules, pytest.

## Global Constraints

- Apply this change only to Staffing print/PDF output; leave the on-screen Training sidebar and controls unchanged.
- Mark trainees only; trainers must not receive a label.
- Use the existing `active_training_blocks` data for the displayed day; do not add a new query, route field, or JavaScript path.
- Keep the print label plain and compact: the exact text is `Training`, not a card, bubble, or progress indicator.
- Preserve the test-first red-green cycle and add a plain-language `CHANGELOG.md` entry for the push to `main`.

---

## File Structure

| File | Responsibility |
| --- | --- |
| `src/zira_dashboard/templates/staffing.html` | Derive active trainee names from the existing day-scoped context and place the print marker after a scheduled trainee's name. |
| `src/zira_dashboard/static/staffing.css` | Ensure the marker remains invisible in the ordinary interactive schedule. |
| `src/zira_dashboard/static/staffing-print.css` | Show the compact marker on paper and suppress the entire Training sidebar. |
| `tests/test_staffing_static.py` | Guard the template and stylesheet contract that produces the print behavior. |
| `CHANGELOG.md` | Explain the delivered printout improvement in simple language. |

### Task 1: Replace printed Training cards with inline trainee labels

**Files:**
- Modify: `tests/test_staffing_static.py` after `test_printed_staffing_warnings_expand_instead_of_clipping`
- Modify: `src/zira_dashboard/templates/staffing.html:306-309`
- Modify: `src/zira_dashboard/static/staffing.css` immediately before the Training sidebar rules near line 785
- Modify: `src/zira_dashboard/static/staffing-print.css:43-55` and after the assigned-name print rules near line 123
- Modify: `CHANGELOG.md` below `## 2026-08-28`

**Interfaces:**
- Consumes: `active_training_blocks`, the existing list of active blocks scoped to the schedule day; each item contains a `trainee` name.
- Produces: `<span class="training-print-label">Training</span>` immediately after a matching scheduled trainee's existing name and badges.
- Produces: `.training-print-label` CSS that is hidden on screen and visible only in printed/PDF schedule output.

- [ ] **Step 1: Write the failing static regression test**

Add this test to `tests/test_staffing_static.py`:

```python
def test_printed_staffing_marks_active_trainees_without_training_cards():
    html = _template()
    css = _style()
    print_css = _print_css()

    assert "{% set active_training_trainees = active_training_blocks | map(attribute='trainee') | list %}" in html
    assert (
        '{% if a.name in active_training_trainees %}<span class="training-print-label">Training</span>{% endif %}'
        in html
    )
    assert ".training-print-label { display: none; }" in css
    assert ".training-sidebar," in print_css
    assert ".training-sidebar:not(:has(.training-card))" not in print_css
    assert ".training-print-label {" in print_css
    assert "display: inline;" in print_css
```

This test proves the marker is driven by trainee-only, day-scoped data, stays out of the normal screen UI, and replaces rather than supplements the printed Training cards.

- [ ] **Step 2: Run the focused test to verify it fails for the missing print-label contract**

Run:

```bash
ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_staffing_static.py::test_printed_staffing_marks_active_trainees_without_training_cards -q
```

Expected: `FAILED` because the template does not derive `active_training_trainees`, neither stylesheet defines `.training-print-label`, and the print stylesheet still uses the conditional `.training-sidebar:not(:has(.training-card))` selector.

- [ ] **Step 3: Add the smallest template and stylesheet changes that satisfy the test**

In `src/zira_dashboard/templates/staffing.html`, add this immediately inside `<span class="dd-summary-text">`, before the `visible_assigned` condition:

```jinja2
{% set active_training_trainees = active_training_blocks | map(attribute='trainee') | list %}
```

In the existing `visible_assigned` loop, append this exact marker after `scheduled_operator_name`, certificate badges, and GOAT badges, before Saturday-availability and partial-hours badges:

```jinja2
{% if a.name in active_training_trainees %}<span class="training-print-label">Training</span>{% endif %}
```

In `src/zira_dashboard/static/staffing.css`, add the screen-only hide rule directly before the Training sidebar block:

```css
/* Printed schedules show this beside an active trainee's name. */
.training-print-label { display: none; }
```

In `src/zira_dashboard/static/staffing-print.css`, replace the conditional Training-sidebar selector in the hide list and remove its now-redundant child selectors:

```css
.day-notes:has(#notes-textarea:placeholder-shown),
.training-sidebar,
.timeoff .ts-note,
```

Then add the print-only label rule after the existing level-colour reset for `.multi-dd .dd-summary-text`:

```css
.training-print-label {
  display: inline;
  margin-left: 0.25em;
  font-size: 8pt;
  font-weight: 700;
}
```

The parent `.training-sidebar` hide makes `.training-start-toggle`, `.training-sidebar-create`, and `.training-card-actions` unreachable on paper, so remove those selectors from the same print hide list.

Add this entry above the existing 2026-08-28 planning note in `CHANGELOG.md`:

```markdown
### Cleaner training printouts

- **Printed schedules now put a small Training label beside a trainee’s name.** The big Training box stays out of the printout, so the schedule is easier to read.
```

- [ ] **Step 4: Run the focused test to verify it passes**

Run:

```bash
ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_staffing_static.py::test_printed_staffing_marks_active_trainees_without_training_cards -q
```

Expected: `1 passed`.

- [ ] **Step 5: Run the related Staffing static test file and inspect the patch**

Run:

```bash
ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_staffing_static.py -q
git diff --check
git diff -- tests/test_staffing_static.py src/zira_dashboard/templates/staffing.html src/zira_dashboard/static/staffing.css src/zira_dashboard/static/staffing-print.css CHANGELOG.md
```

Expected: the Staffing static suite passes, `git diff --check` has no output, the only functional changes are the trainee-only print label and the fully hidden printed sidebar, and the changelog says the same in plain language.

- [ ] **Step 6: Commit and push the completed change**

Run:

```bash
git add CHANGELOG.md tests/test_staffing_static.py src/zira_dashboard/templates/staffing.html src/zira_dashboard/static/staffing.css src/zira_dashboard/static/staffing-print.css
git commit -m "fix: simplify printed training details"
git push origin main
```

Expected: the commit contains only the five listed implementation files and is pushed to `origin/main`.

## Plan Self-Review

### Spec coverage

- Small label beside a scheduled trainee: Task 1, steps 1 and 3.
- Trainer remains unmarked: Task 1 uses only the `trainee` field from `active_training_blocks`.
- Large Training card omitted from print/PDF: Task 1, step 3 replaces the conditional sidebar hide with an unconditional parent hide.
- Interactive schedule stays unchanged: Task 1 hides the marker in the ordinary stylesheet and does not alter sidebar JavaScript or controls.
- Regression coverage and verification: Task 1, steps 1, 2, 4, and 5.
- Plain-language release note and required delivery: Task 1, steps 3 and 6.

### Placeholder and consistency check

The plan specifies every changed file, selector, template marker, test body, command, expected result, commit message, and push target. It introduces no new functions, route fields, queries, or JavaScript interfaces.
