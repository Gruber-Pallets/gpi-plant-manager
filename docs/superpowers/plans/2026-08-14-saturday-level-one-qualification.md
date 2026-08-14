# Saturday Level-One Qualification Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make Saturday recruiting and publish validation accept operators whose required skills are level 1 or higher, matching weekday scheduling.

**Architecture:** The pure Saturday domain module owns both decisions. Update its opening-eligibility predicate and its publish-time predicate from the Saturday-only level-2-or-3 rule to the weekday-compatible level-at-least-one rule. Regression tests cover the acceptance boundary and retain the level-zero rejection boundary.

**Tech Stack:** Python 3.12, pytest, FastAPI domain module.

## Global Constraints

- Every required skill must be at least level 1; a missing or level-0 skill remains ineligible.
- Apply the same threshold while offering Saturday work and while validating publication.
- Do not change commitment, availability, time-off, duplicate-assignment, or requested-coverage behavior.
- Preserve existing user-facing error messages for genuinely unqualified operators.

---

## File Structure

- `src/zira_dashboard/saturday_recruiting.py` — Pure rules that determine opening eligibility and publish-time qualification.
- `tests/test_saturday_recruiting.py` — Unit boundary tests for employee eligibility before a Saturday commitment.
- `tests/test_staffing_saturday_recruiting.py` — Publish-validation boundary tests for a level-1 assigned operator.

### Task 1: Align Saturday opening eligibility with the weekday threshold

**Files:**
- Modify: `tests/test_saturday_recruiting.py:121-125`
- Modify: `src/zira_dashboard/saturday_recruiting.py:104-113`

**Interfaces:**
- Consumes: `eligible_work_centers(skill_levels: Mapping[str, int], openings: Sequence[Opening]) -> frozenset[int]`.
- Produces: Opening IDs for which each required skill is level 1 or above.

- [ ] **Step 1: Write the failing acceptance-boundary test**

Replace the existing level-two-only test with this test, preserving the
multi-skill opening so every prerequisite is exercised:

```python
def test_eligibility_accepts_level_one_in_every_skill():
    openings = [_opening(10, 1, "Repair", "Forklift")]

    assert sr.eligible_work_centers({"Repair": 3, "Forklift": 1}, openings) == {10}
    assert sr.eligible_work_centers({"Repair": 3, "Forklift": 0}, openings) == set()
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `pytest tests/test_saturday_recruiting.py::test_eligibility_accepts_level_one_in_every_skill -q`

Expected: FAIL because the current rule rejects `"Forklift": 1`.

- [ ] **Step 3: Implement the minimal eligibility change**

Update the function documentation and predicate to match weekday qualification,
which accepts every level at or above 1:

```python
def eligible_work_centers(
    skill_levels: Mapping[str, int], openings: Sequence[Opening]
) -> frozenset[int]:
    """Return work centers whose every required skill is at least level 1."""
    return frozenset(
        opening.wc_id
        for opening in openings
        if opening.required_skills
        and all(int(skill_levels.get(skill, 0)) >= 1 for skill in opening.required_skills)
    )
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `pytest tests/test_saturday_recruiting.py::test_eligibility_accepts_level_one_in_every_skill -q`

Expected: PASS.

### Task 2: Align Saturday publish validation and coverage counting

**Files:**
- Modify: `tests/test_staffing_saturday_recruiting.py:264-277`
- Modify: `src/zira_dashboard/saturday_recruiting.py:176-192`

**Interfaces:**
- Consumes: `validate_publish(bundle, assignments, people_by_name, full_day_off_names, available_names=None, require_coverage=True) -> list[str]`.
- Produces: No qualification reason and one qualified coverage count when a committed, active, present operator has each opening skill at level 1 or above.

- [ ] **Step 1: Write the failing publish-boundary test**

Add this standalone test immediately before the parameterized blocker test:

```python
def test_publish_accepts_level_one_qualified_operator():
    people = _people(ana_repair=1)

    assert sr.validate_publish(
        _bundle(),
        {"Repair 1": ["Ana"], "Dismantle": ["Bob"]},
        people,
        set(),
    ) == []
```

Then keep the existing blocker matrix but change its qualification case to
`ana_repair=0`, preserving the assertion that a level-0 operator is blocked:

```python
({"Repair 1": ["Ana"], "Dismantle": ["Bob"]}, _people(ana_repair=0), set(), "Ana is no longer qualified for Repair."),
```

- [ ] **Step 2: Run the focused publish tests to verify the new acceptance test fails**

Run: `pytest tests/test_staffing_saturday_recruiting.py::test_publish_accepts_level_one_qualified_operator tests/test_staffing_saturday_recruiting.py::test_publish_validation_reports_each_saturday_blocker -q`

Expected: The new level-1 acceptance test FAILS with the current
`"Ana is no longer qualified for Repair."` reason; the level-0 blocker case
still passes.

- [ ] **Step 3: Implement the minimal publish validation change**

Change only the qualification predicate in `validate_publish`:

```python
is_qualified = person is not None and all(
    person.level(skill) >= 1 for skill in opening.required_skills
)
```

Do not alter the surrounding `elif`: it must continue to count only
committed/available, active, present, qualified people toward requested
Saturday coverage.

- [ ] **Step 4: Run the focused publish tests to verify they pass**

Run: `pytest tests/test_staffing_saturday_recruiting.py::test_publish_accepts_level_one_qualified_operator tests/test_staffing_saturday_recruiting.py::test_publish_validation_reports_each_saturday_blocker -q`

Expected: PASS; the level-1 operator publishes and the level-0 operator stays
blocked.

- [ ] **Step 5: Run the full relevant regression suites**

Run: `pytest tests/test_saturday_recruiting.py tests/test_staffing_saturday_recruiting.py -q`

Expected: PASS with no failures.

- [ ] **Step 6: Commit the implementation and user-facing release note**

Add a short, child-friendly `CHANGELOG.md` entry explaining that level-1
trained workers can now volunteer for and be scheduled on Saturday, just as
they can during the week. Stage only the implementation, tests, and this
release note, then commit:

```bash
git add CHANGELOG.md src/zira_dashboard/saturday_recruiting.py \
  tests/test_saturday_recruiting.py tests/test_staffing_saturday_recruiting.py
git commit -m "fix: align Saturday qualifications with weekdays"
```

Push the resulting commit to `origin/main`.
