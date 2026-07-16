# Test Baseline Reconciliation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Restore a green repository test baseline by reconciling stale scheduler assertions with the approved safe-partial Auto rebuild contract and updating one isolated template fixture.

**Architecture:** Keep production scheduling and template contracts unchanged. Update tests that still encode the superseded atomic/global scheduler behavior to assert the July 14 greedy safe-partial behavior, correct one stale scheduler docstring, and supply the three route-owned training globals missing from a script-fragment fixture. Treat the Playwright failure as an execution-boundary issue and verify it by running the full suite outside the managed macOS sandbox.

**Tech Stack:** Python 3.12, pytest, Jinja2, Playwright, FastAPI route/template context.

## Global Constraints

- The binding scheduler contract is `docs/superpowers/specs/2026-07-14-partial-auto-schedule-rebuild-design.md`.
- Auto rebuilds keep every safe assignment they can make; unmet minimums, defaults, and unplaced people are soft reporting issues and never roll back safe placements.
- Enabled Auto assignments that are duplicate, unqualified, unavailable, or over capacity are removed before automatic placement; assignments outside enabled Auto centers remain untouched.
- Placement is deterministic and greedy per person, not globally optimized.
- Hard invariants remain: one person in at most one location, required qualifications, configured/static capacity, enabled targets, and valid coupled crews.
- Enabled centers remain present in returned assignment maps, including empty lists.
- Do not change scheduler behavior, routes, solver code, template behavior, or browser launch flags.
- The Playwright geometry test must run outside the managed macOS sandbox; it must not be skipped.

---

### Task 1: Reconcile scheduler tests with the safe-partial contract

**Files:**
- Modify: `tests/test_rotation_suggestions.py`
- Modify: `src/zira_dashboard/rotation_suggestions.py:966-971`

**Interfaces:**
- Consumes: `suggest_recycled_assignments(...) -> RecycledSuggestion` under the July 14 safe-partial contract.
- Produces: scheduler tests that retain hard safety invariants while accepting deterministic partial assignments and soft `placement_issues`.

- [ ] **Step 1: Verify the inherited RED baseline**

Run:

```bash
ZIRA_API_KEY=test /Users/dalegruber/Projects/gpi-plant-manager/.venv/bin/python \
  -m pytest tests/test_rotation_suggestions.py -q -p no:cacheprovider --tb=short
```

Expected: `19 failed, 64 passed`. All failures are assertions that predate commit `d1cb576` and encode the superseded atomic/global contract.

- [ ] **Step 2: Add one order-insensitive placement signature helper**

Add after `empty_history`:

```python
def placement_signatures(out):
    return {
        (issue.code, issue.person, issue.centers)
        for issue in out.placement_issues
    }
```

- [ ] **Step 3: Update the greedy and disabled-default assertions**

Rename `test_engine_assigns_every_available_nonreserve_person` to
`test_greedy_engine_keeps_safe_assignments_when_later_person_is_unplaced` and replace its assertions with:

```python
assert result.assignments == {
    "Repair 1": ["Cross", "Repair A"],
    "Dismantler 1": [],
}
assert result.unused_people == ("Repair B",)
assert result.complete is False
assert placement_signatures(result) == {
    ("person_unplaced", "Repair B", ()),
    ("center_minimum_unmet", None, ("Dismantler 1",)),
}
assert result.unresolved_centers == ("Dismantler 1",)
assert len(result.assignments["Repair 1"]) == 2
```

Rename `test_available_default_with_disabled_target_blocks_complete_result` to
`test_disabled_exact_default_is_reported_without_rolling_back_partial_result` and replace its assertions with:

```python
assert result.assignments == {"Dismantler 1": []}
assert result.unused_people == ("Ana",)
assert result.complete is False
assert placement_signatures(result) == {
    ("exact_default_center_disabled", "Ana", ("Repair 1",)),
    ("person_unplaced", "Ana", ()),
}
```

- [ ] **Step 4: Update partial minimum/capacity assertions**

Rename `test_engine_leaves_two_person_center_empty_when_only_one_qualified_person_exists` to
`test_engine_keeps_safe_under_minimum_assignment_and_reports_shortage` and use:

```python
assert out.assignments == {"Hand Build #2": ["Only Builder"]}
assert out.unused_people == ()
assert out.complete is False
assert placement_signatures(out) == {
    ("center_minimum_unmet", None, ("Hand Build #2",)),
}
assert out.unresolved_centers == ("Hand Build #2",)
```

Rename `test_engine_never_exceeds_static_capacity_to_reach_minimum` to
`test_engine_fills_only_static_capacity_when_minimum_is_higher` and use:

```python
assert out.assignments == {"Hand Build #1": ["A", "B"]}
assert len(out.assignments["Hand Build #1"]) == 2
assert out.unused_people == ("C",)
assert out.complete is False
assert placement_signatures(out) == {
    ("invalid_center_configuration", None, ("Hand Build #1",)),
    ("person_unplaced", "C", ()),
    ("center_minimum_unmet", None, ("Hand Build #1",)),
}
```

Keep `test_engine_honors_configured_capacity_over_static_location_maximum` and replace its assertions with:

```python
assert fallback.assignments == {"Repair 2": ["A"]}
assert fallback.unused_people == ("B",)
assert fallback.complete is False
assert placement_signatures(fallback) == {
    ("person_unplaced", "B", ()),
}
assert configured.assignments == {"Repair 2": ["A", "B"]}
assert configured.unused_people == ()
assert configured.complete is True
assert configured.placement_issues == ()
```

- [ ] **Step 5: Preserve training and enabled-center invariants under partial output**

In `test_training_green_reservation_respects_center_capacity`, replace the stale diagnostic assertion and retain the capacity checks:

```python
assert out.assignments == {"Hand Build #1": ["Trainee A", "Trainee B"]}
assert len(out.assignments["Hand Build #1"]) <= 2
assert out.unused_people == ("Green",)
assert out.complete is False
assert placement_signatures(out) == {
    ("person_unplaced", "Green", ()),
}
assert out.reason_codes["Hand Build #1"]["Trainee A"] == "training_block"
assert out.reason_codes["Hand Build #1"]["Trainee B"] == "training_block"
```

In `test_empty_assignment_keys_follow_canonical_group_center_order`, use:

```python
assert out.assignments == {"Center Z": [], "Center A": [], "Center M": []}
assert list(out.assignments) == ["Center Z", "Center A", "Center M"]
assert out.unused_people == ()
assert out.complete is False
assert placement_signatures(out) == {
    ("center_minimum_unmet", None, ("Center A",)),
    ("center_minimum_unmet", None, ("Center M",)),
    ("center_minimum_unmet", None, ("Center Z",)),
}
assert out.unresolved_centers == ("Center A", "Center M", "Center Z")
```

Rename `test_unresolved_multi_person_center_has_no_generated_partial_crew` to
`test_under_minimum_multi_person_center_keeps_safe_partial_crew` and use:

```python
assert out.assignments == {"Hand Build #1": ["One Builder"]}
assert out.unused_people == ()
assert out.complete is False
assert placement_signatures(out) == {
    ("center_minimum_unmet", None, ("Hand Build #1",)),
}
assert out.unresolved_centers == ("Hand Build #1",)
```

- [ ] **Step 6: Update Trim Saw partial-result assertions without weakening pair safety**

In `test_training_mode_never_seats_third_person_on_trim_saw`, use:

```python
assert out.assignments == {"Trim Saw 1": ["Green", "One A"]}
assert len(out.assignments["Trim Saw 1"]) == 2
assert _valid_trim_saw_pair(3, 1) is True
assert out.unused_people == ("One B",)
assert out.complete is False
assert placement_signatures(out) == {
    ("person_unplaced", "One B", ()),
}
```

In `test_training_mode_never_creates_invalid_trim_saw_pair`, use:

```python
assert out.assignments == {"Trim Saw 1": ["Green", "Two"]}
assert _valid_trim_saw_pair(3, 2) is True
assert "One" not in out.assignments["Trim Saw 1"]
assert out.unused_people == ("One",)
assert out.complete is False
assert placement_signatures(out) == {
    ("person_unplaced", "One", ()),
}
```

Rename `test_trim_saw_locked_single_without_safe_partner_warns` to
`test_trim_saw_locked_single_without_safe_partner_reports_placement_issue` and use:

```python
assert out.assignments == {"Trim Saw 1": ["Pinned One"]}
assert out.sources["Trim Saw 1"]["Pinned One"] == "manual"
assert "Level Two" not in out.assignments["Trim Saw 1"]
assert out.unused_people == ("Level Two",)
assert out.complete is False
assert placement_signatures(out) == {
    ("no_safe_complete_crew", None, ("Trim Saw 1",)),
    ("person_unplaced", "Level Two", ()),
    ("center_minimum_unmet", None, ("Trim Saw 1",)),
}
```

- [ ] **Step 7: Update greedy multi-group and generic-group assertions**

Rename `test_optimized_covers_multiple_groups_with_multi_skill_green` to
`test_optimized_greedy_assignment_reports_later_unplaced_person`, remove the obsolete global-scarcity comment, and use:

```python
assert out.assignments == {
    "Dismantler 1": ["Alice"],
    "Repair 1": ["Carl"],
}
assert out.unused_people == ("Bob",)
assert out.complete is False
assert placement_signatures(out) == {
    ("person_unplaced", "Bob", ()),
}
assert out.staffed_centers == ("Dismantler 1", "Repair 1")
assert sum(name == "Alice" for names in out.assignments.values() for name in names) == 1
```

Rename `test_generic_group_locations_keep_an_under_minimum_hand_build_crew_empty` to
`test_generic_groups_keep_safe_assignments_when_one_center_remains_under_minimum` and use:

```python
assert out.assignments == {
    "Hand Build #1": ["Hand Builder"],
    "Junior #1": ["Junior Pro"],
}
assert out.unused_people == ()
assert out.complete is False
assert placement_signatures(out) == {
    ("center_minimum_unmet", None, ("Hand Build #1",)),
}
assert out.staffed_centers == ("Junior #1",)
assert out.unresolved_centers == ("Hand Build #1",)
```

Rename `test_invalid_minimum_above_capacity_is_configuration_issue_even_with_headcount` to
`test_invalid_minimum_above_capacity_keeps_capacity_safe_partial_result` and use:

```python
assert out.assignments == {"Repair 1": ["Green A"]}
assert len(out.assignments["Repair 1"]) == 1
assert out.unused_people == ("Green B",)
assert out.complete is False
assert placement_signatures(out) == {
    ("invalid_center_configuration", None, ("Repair 1",)),
    ("person_unplaced", "Green B", ()),
    ("center_minimum_unmet", None, ("Repair 1",)),
}
```

- [ ] **Step 8: Update enabled-lock sanitization assertions**

Replace `_assert_duplicate_protected_lock_blocks_rebuild` with:

```python
def _assert_duplicate_enabled_lock_is_sanitized(out):
    assert out.assignments == {
        "Repair 1": ["Duplicated Lock", "Backfill A"],
        "Repair 2": ["Backfill B"],
    }
    assert out.unused_people == ()
    assert out.complete is True
    assert out.placement_issues == ()
    assert any(
        "Duplicated Lock was removed from Repair 2" in warning
        for warning in out.warnings
    )
    assert sum(
        name == "Duplicated Lock"
        for names in out.assignments.values()
        for name in names
    ) == 1
    assert out.sources["Repair 1"]["Duplicated Lock"] == "manual"
```

Rename the two callers to:

```python
def test_duplicate_manual_lock_keeps_first_copy_and_removes_conflicting_copy():
    ...
    _assert_duplicate_enabled_lock_is_sanitized(out)

def test_duplicate_default_lock_keeps_first_copy_and_removes_conflicting_copy(monkeypatch):
    ...
    _assert_duplicate_enabled_lock_is_sanitized(out)
```

Rename `test_enabled_lock_conflicts_with_preserved_pass_through_assignment` to
`test_pass_through_assignment_owns_person_and_conflicting_enabled_lock_is_removed` and use:

```python
assert out.assignments == {
    "Disabled Bench": ["Duplicated Lock"],
    "Repair 1": ["Backfill"],
}
assert out.unused_people == ()
assert out.complete is True
assert out.placement_issues == ()
assert any(
    "Duplicated Lock was removed from Repair 1" in warning
    for warning in out.warnings
)
assert sum(
    name == "Duplicated Lock"
    for names in out.assignments.values()
    for name in names
) == 1
```

Rename `test_staffed_center_reports_invalid_protected_assignment` to
`test_unqualified_enabled_lock_is_removed_and_center_is_backfilled` and use:

```python
assert out.assignments == {"Repair 1": ["Qualified"]}
assert out.unused_people == ("Protected Zero",)
assert out.complete is False
assert out.staffed_centers == ("Repair 1",)
assert out.issues == ()
assert placement_signatures(out) == {
    ("person_unplaced", "Protected Zero", ()),
}
assert any(
    "Protected Zero was removed from Repair 1" in warning
    for warning in out.warnings
)
assert out.sources["Repair 1"]["Qualified"] == "generated"
```

- [ ] **Step 9: Update generic diagnostic-channel assertions**

Rename `test_level_zero_only_alerts_that_training_is_required` to
`test_level_zero_person_remains_unplaced_and_center_reports_minimum_shortage` and use:

```python
assert out.assignments == {"Dismantler 1": []}
assert out.unused_people == ("Potential Trainee",)
assert out.complete is False
assert placement_signatures(out) == {
    ("person_unplaced", "Potential Trainee", ()),
    ("center_minimum_unmet", None, ("Dismantler 1",)),
}
assert out.unresolved_centers == ("Dismantler 1",)
```

- [ ] **Step 10: Correct the stale scheduler docstring**

In `suggest_recycled_assignments`, replace the statement that manual/default locks always pass through unchanged with:

```python
"""Suggest safe Recycled assignments for enabled Auto work centers.

Assignments outside enabled Auto centers pass through unchanged. Within an
enabled center, valid unique assignments and locks are retained; duplicate,
unavailable, unqualified, or over-capacity assignments are removed before
greedy safe partial placement. Minimum/default/unplaced conditions are reported
without rolling back safe assignments.
"""
```

Retain any parameter documentation that follows the existing summary; change only wording that contradicts the July 14 contract.

- [ ] **Step 11: Verify GREEN and commit**

Run:

```bash
ZIRA_API_KEY=test /Users/dalegruber/Projects/gpi-plant-manager/.venv/bin/python \
  -m pytest tests/test_rotation_suggestions.py -q -p no:cacheprovider
```

Expected: `83 passed`.

Then run:

```bash
git diff --check
git add src/zira_dashboard/rotation_suggestions.py tests/test_rotation_suggestions.py
git commit -m "test: reconcile partial scheduler expectations"
```

---

### Task 2: Repair the isolated staffing template fixture

**Files:**
- Modify: `tests/test_staffing_trim_saw_defaults.py:17-36`

**Interfaces:**
- Consumes: the route/template contract requiring `active_training_blocks`, `training_protocol_people`, and `training_protocol_work_centers` in the extracted global script.
- Produces: a self-contained fragment-render fixture matching the real route context.

- [ ] **Step 1: Verify the inherited RED fixture failure**

Run:

```bash
ZIRA_API_KEY=test /Users/dalegruber/Projects/gpi-plant-manager/.venv/bin/python \
  -m pytest tests/test_staffing_trim_saw_defaults.py::test_staffing_template_exposes_smart_defaults \
  -q -p no:cacheprovider
```

Expected: one failure with `TypeError: Object of type Undefined is not JSON serializable` at the extracted script's first missing training global.

- [ ] **Step 2: Supply the three route-owned globals**

Add these arguments to the existing `render(...)` call after `forklift_live_model`:

```python
active_training_blocks=[],
training_protocol_people=[],
training_protocol_work_centers=[],
```

Do not add template-side defaults; the real route already guarantees these values.

- [ ] **Step 3: Verify GREEN and commit**

Run:

```bash
ZIRA_API_KEY=test /Users/dalegruber/Projects/gpi-plant-manager/.venv/bin/python \
  -m pytest tests/test_staffing_trim_saw_defaults.py -q -p no:cacheprovider
```

Expected: `5 passed`.

Then run:

```bash
git diff --check
git add tests/test_staffing_trim_saw_defaults.py
git commit -m "test: refresh staffing template fixture"
```

---

### Task 3: Verify the complete repository outside the browser sandbox

**Files:**
- No file changes.

**Interfaces:**
- Consumes: commits from Tasks 1 and 2 plus the local Playwright/Chromium installation.
- Produces: a trustworthy full-suite result in an environment where Chromium can register its macOS Mach rendezvous service.

- [ ] **Step 1: Run focused staffing-warning verification**

```bash
ZIRA_API_KEY=test /Users/dalegruber/Projects/gpi-plant-manager/.venv/bin/python \
  -m pytest tests/test_staffing_rotations.py -q -p no:cacheprovider
```

Expected: `97 passed`.

- [ ] **Step 2: Run the full suite outside the managed sandbox**

From the worktree, run with approved unsandboxed execution:

```bash
env PYTHONDONTWRITEBYTECODE=1 ZIRA_API_KEY=test AUTH_DISABLED=1 PYTHONPATH=src \
  /Users/dalegruber/Projects/gpi-plant-manager/.venv/bin/python \
  -m pytest -q -p no:cacheprovider
```

Expected: `1846 passed, 304 skipped` with zero failures.

- [ ] **Step 3: Verify repository state**

```bash
git diff --check
git status --short
git log --oneline c3a5cc8339806b4f88425a2161b9244591b024c7..HEAD
```

Expected: clean worktree and only the scoped staffing-warning and baseline-reconciliation commits.
