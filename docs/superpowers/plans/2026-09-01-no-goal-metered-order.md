# No-Goal Metered Ordering Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Place every metered People row whose current or final work center has no goal below all metered rows whose current or final work center has a goal.

**Architecture:** Replace the work-center-name-specific production subgroup rank with a goal-based rank derived from the final timeline interval's `ProductionMetric`. Preserve the existing section rank and every attention/result tie-breaker after the subgroup rank.

**Tech Stack:** Python 3.12, frozen dataclasses, pytest, Ruff.

## Global Constraints

- The current interval, or final interval for a completed day, owns the subgroup.
- A finite, positive `goal_units` value means the final metered interval has a goal.
- A missing, zero, negative, or non-finite final goal means the row belongs at the bottom of Metered production.
- Work-center names do not affect placement; Trim Saw follows the same rule as every other metered work center.
- Preserve attention, deficit, rolling-performance, person-name, and employee-ID ordering within each subgroup.
- Preserve the fixed Metered production, Tablet forklift, and Other non-metered people section order.
- Do not change data loading, scoring, goal calculation, attention reasons, summaries, or templates.
- Add a short child-readable `CHANGELOG.md` note with the implementation push.

---

### Task 1: Generalize the metered production subgroup

**Files:**
- Modify: `tests/test_people_performance_rows.py:69-118`
- Modify: `src/zira_dashboard/people_performance.py:491-494,1038-1046`
- Modify: `CHANGELOG.md`

**Interfaces:**
- Consumes: `RoleKey`, `ProductionMetric | None`, `_is_finite_number(value: object) -> bool`, and the final `TimelineInterval.production` value.
- Produces: `_production_subgroup_rank(role: RoleKey, metric: ProductionMetric | None) -> int`, returning `0` for goal-based production and all non-production roles, or `1` for production without a finite positive goal.

- [ ] **Step 1: Replace the Trim-Saw-specific tests with failing goal-based ordering tests**

Replace `test_trim_saw_rows_sort_after_every_other_metered_row()` and `test_final_work_center_owns_trim_saw_subgroup_after_transfer()` in `tests/test_people_performance_rows.py` with:

```python
def test_no_goal_rows_sort_after_goal_based_metered_rows():
    model = _assemble(
        spans=(
            span(80, "No Goal", 0, 480, "Measured Work 1"),
            span(81, "Repair Ahead", 0, 480, "Repair 1"),
            span(82, "Trim Behind", 0, 480, "Trim Saw 1"),
        ),
        scores=(
            score(80, "No Goal", "Measured Work 1", 0, 480, 100, 0),
            score(81, "Repair Ahead", "Repair 1", 0, 480, 110, 100),
            score(82, "Trim Behind", "Trim Saw 1", 0, 480, 50, 100),
        ),
        downtime_by_wc={"Measured Work 1": (), "Repair 1": (), "Trim Saw 1": ()},
        metered_wc_names={"Measured Work 1", "Repair 1", "Trim Saw 1"},
    )

    assert [row.person_name for row in model.rows] == [
        "Trim Behind",
        "Repair Ahead",
        "No Goal",
    ]
    assert all(row.section == "production" for row in model.rows)


def test_final_work_center_goal_owns_subgroup_after_transfer():
    model = _assemble(
        spans=(
            span(83, "Moved To Goal", 0, 60, "Measured Work 1"),
            span(83, "Moved To Goal", 60, 480, "Repair 1"),
            span(84, "Ended Without Goal", 0, 60, "Repair 1"),
            span(84, "Ended Without Goal", 60, 480, "Measured Work 1"),
        ),
        scores=(
            score(83, "Moved To Goal", "Measured Work 1", 0, 60, 5, 0),
            score(83, "Moved To Goal", "Repair 1", 60, 480, 50, 100),
            score(84, "Ended Without Goal", "Repair 1", 0, 60, 5, 10),
            score(84, "Ended Without Goal", "Measured Work 1", 60, 480, 50, 0),
        ),
        downtime_by_wc={"Measured Work 1": (), "Repair 1": ()},
        metered_wc_names={"Measured Work 1", "Repair 1"},
    )

    assert [row.person_name for row in model.rows] == [
        "Moved To Goal",
        "Ended Without Goal",
    ]
    assert model.rows[0].intervals[-1].production.goal_units == 100
    assert model.rows[1].intervals[-1].production.goal_units == 0
```

The first test proves a non-Trim-Saw no-goal row moves below goal-based rows and that a goal-based Trim Saw row is no longer singled out. The second proves only the final interval controls placement in both transfer directions.

- [ ] **Step 2: Run the focused tests and verify RED**

Run:

```bash
PYTHONPATH=src .venv/bin/python -m pytest \
  tests/test_people_performance_rows.py::test_no_goal_rows_sort_after_goal_based_metered_rows \
  tests/test_people_performance_rows.py::test_final_work_center_goal_owns_subgroup_after_transfer -q
```

Expected: both tests fail against the Trim-Saw-specific sorter. The first places `No Goal` before the goal-based rows and demotes `Trim Behind`; the second places `Ended Without Goal` before `Moved To Goal`.

- [ ] **Step 3: Replace the work-center-name check with the final goal check**

Replace `_production_subgroup_rank` in `src/zira_dashboard/people_performance.py` with:

```python
def _production_subgroup_rank(
    role: RoleKey,
    metric: ProductionMetric | None,
) -> int:
    if role != "production":
        return 0
    if metric is None or not _is_finite_number(metric.goal_units):
        return 1
    return 0 if metric.goal_units > 0 else 1
```

Then change the second field in `_assemble_person_row()`'s `sort_key` from:

```python
        _production_subgroup_rank(final_role, final_interval.location_name),
```

to:

```python
        _production_subgroup_rank(final_role, final_interval.production),
```

This keeps the subgroup decision local to the final interval and avoids adding work-center configuration to the assembly interface.

- [ ] **Step 4: Run the row tests and verify GREEN**

Run:

```bash
PYTHONPATH=src .venv/bin/python -m pytest tests/test_people_performance_rows.py -q
```

Expected: all tests pass, including fixed section order, transfer, forklift, and non-metered regressions.

- [ ] **Step 5: Run the full People dashboard regression set and static checks**

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

Expected: pytest reports zero failures, Ruff prints `All checks passed!`, and `git diff --check` prints nothing.

- [ ] **Step 6: Add the child-readable patch note**

Add this entry at the top of the current date in `CHANGELOG.md`:

```markdown
### Put all measured work without goals at the bottom

- **The People page now places any measured work without a goal below measured work that has a goal.** Trim Saw follows the same rule as every other measured work area.
```

- [ ] **Step 7: Commit and push the implementation**

Run:

```bash
git add \
  src/zira_dashboard/people_performance.py \
  tests/test_people_performance_rows.py \
  CHANGELOG.md
git commit -m "fix: sort no-goal metered work last"
git push origin main
```

Expected: the commit and push succeed without staging unrelated worktree files.
