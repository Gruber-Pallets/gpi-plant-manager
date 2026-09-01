# Trim Saw Metered Ordering Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Keep every final/current `Trim Saw*` row in Metered production while sorting it below all other metered work centers.

**Architecture:** Extend the existing immutable `PersonRow.sort_key` with one production-only subgroup rank derived from the final interval's canonical location. Preserve the current section, attention, deficit, rolling-performance, name, and employee-ID ranks around it.

**Tech Stack:** Python 3.12, dataclasses, pytest, Ruff.

## Global Constraints

- Match `Trim Saw` as a case-insensitive prefix so numbered Trim Saw work centers are included.
- Use the row's current/final interval; an earlier Trim Saw interval must not demote a person who later transferred elsewhere.
- Keep Trim Saw inside Metered production.
- Keep the fixed Production → Tablet forklift → Other section order.
- Preserve the existing attention order within normal metered rows and within Trim Saw rows.
- Do not change scoring, source loading, attention reasons, templates, or forklift/non-metered sorting.
- Include a short child-readable `CHANGELOG.md` note with the implementation push.

---

### Task 1: Add the Trim Saw production subgroup rank

**Files:**
- Modify: `src/zira_dashboard/people_performance.py:351-355,897-905`
- Modify: `tests/test_people_performance_rows.py:57-67`
- Modify: `CHANGELOG.md`

**Interfaces:**
- Consumes: `_SECTION_RANK`, `RoleKey`, the final `TimelineInterval.location_name`, and the existing `PersonRow.sort_key` tuple.
- Produces: `_production_subgroup_rank(role: RoleKey, location_name: str) -> int` and a sort key ordered as section, production subgroup, then the existing attention fields.

- [ ] **Step 1: Write the failing ordering tests**

Add these tests after `test_fixed_section_order_and_needs_attention_sort_are_stable()` in `tests/test_people_performance_rows.py`:

```python
def test_trim_saw_rows_sort_after_every_other_metered_row():
    model = _assemble(
        spans=(
            span(80, "Trim Behind", 0, 480, "Trim Saw 1"),
            span(81, "Repair Ahead", 0, 480, "Repair 1"),
            span(82, "Trim Ahead", 0, 480, "Trim Saw 2"),
            span(83, "Repair Behind", 0, 480, "Repair 1"),
        ),
        scores=(
            score(80, "Trim Behind", "Trim Saw 1", 0, 480, 50, 100),
            score(81, "Repair Ahead", "Repair 1", 0, 480, 110, 100),
            score(82, "Trim Ahead", "Trim Saw 2", 0, 480, 110, 100),
            score(83, "Repair Behind", "Repair 1", 0, 480, 50, 100),
        ),
        downtime_by_wc={"Repair 1": (), "Trim Saw 1": (), "Trim Saw 2": ()},
        metered_wc_names={"Repair 1", "Trim Saw 1", "Trim Saw 2"},
    )

    assert [row.person_name for row in model.rows] == [
        "Repair Behind",
        "Repair Ahead",
        "Trim Behind",
        "Trim Ahead",
    ]
    assert all(row.section == "production" for row in model.rows)


def test_final_work_center_owns_trim_saw_subgroup_after_transfer():
    model = _assemble(
        spans=(
            span(84, "Moved To Repair", 0, 60, "Trim Saw 1"),
            span(84, "Moved To Repair", 60, 480, "Repair 1"),
            span(85, "Still Trim", 0, 480, "Trim Saw 1"),
        ),
        scores=(
            score(84, "Moved To Repair", "Trim Saw 1", 0, 60, 5, 10),
            score(84, "Moved To Repair", "Repair 1", 60, 480, 50, 100),
            score(85, "Still Trim", "Trim Saw 1", 0, 480, 50, 100),
        ),
        downtime_by_wc={"Repair 1": (), "Trim Saw 1": ()},
        metered_wc_names={"Repair 1", "Trim Saw 1"},
    )

    assert [row.person_name for row in model.rows] == [
        "Moved To Repair",
        "Still Trim",
    ]
    assert model.rows[0].intervals[-1].location_name == "Repair 1"
    assert model.rows[1].intervals[-1].location_name == "Trim Saw 1"
```

- [ ] **Step 2: Run the tests and verify RED**

Run:

```bash
PYTHONPATH=src .venv/bin/python -m pytest \
  tests/test_people_performance_rows.py::test_trim_saw_rows_sort_after_every_other_metered_row \
  tests/test_people_performance_rows.py::test_final_work_center_owns_trim_saw_subgroup_after_transfer -q
```

Expected: the first test fails because the existing attention rank places `Trim Behind` beside the other Behind row instead of after every normal metered row. The second test protects final-location ownership during the implementation.

- [ ] **Step 3: Add the production subgroup helper and sort-key field**

Add this helper immediately after `_SECTION_RANK` in `src/zira_dashboard/people_performance.py`:

```python
def _production_subgroup_rank(role: RoleKey, location_name: str) -> int:
    if role == "production" and location_name.casefold().startswith("trim saw"):
        return 1
    return 0
```

Change the `sort_key` construction in `_assemble_person_row()` to:

```python
    sort_key = (
        _SECTION_RANK[final_role],
        _production_subgroup_rank(final_role, final_interval.location_name),
        attention_rank,
        -deficit,
        rolling_tiebreak,
        person_name.casefold(),
        employee_odoo_id,
    )
```

This inserts the Trim Saw subgroup only inside the production section. Non-production rows receive subgroup `0`, so their existing relative order is unchanged.

- [ ] **Step 4: Run focused tests and verify GREEN**

Run:

```bash
PYTHONPATH=src .venv/bin/python -m pytest tests/test_people_performance_rows.py -q
```

Expected: PASS. The new tests prove normal metered rows precede Trim Saw and that the final work center owns placement.

- [ ] **Step 5: Run dashboard regressions and static checks**

Run:

```bash
PYTHONPATH=src .venv/bin/python -m pytest \
  tests/test_people_performance_production.py \
  tests/test_people_performance_forklift.py \
  tests/test_people_performance_rows.py \
  tests/test_people_performance_data.py \
  tests/test_people_performance_view.py \
  tests/test_people_performance_route.py \
  tests/test_people_performance_template.py -q
.venv/bin/ruff check \
  src/zira_dashboard/people_performance.py \
  tests/test_people_performance_rows.py
git diff --check
```

Expected: all dashboard tests pass, Ruff prints `All checks passed!`, and `git diff --check` prints nothing.

- [ ] **Step 6: Add the patch note, commit, and push**

Add this entry at the top of the current date in `CHANGELOG.md`:

```markdown
### Keep Trim Saw at the bottom of measured work

- **Trim Saw workers now stay below the other measured work areas on the People page.** They still show their results, but the work areas we understand better stay first.
```

Run:

```bash
git add \
  src/zira_dashboard/people_performance.py \
  tests/test_people_performance_rows.py \
  CHANGELOG.md
git commit -m "fix: sort trim saw after metered work"
git push origin main
```

Expected: the commit and push succeed without staging the existing unrelated `.superpowers/sdd/task-7-report.md`, `.cursorignore`, `.python-version`, or `uv.lock` files.
