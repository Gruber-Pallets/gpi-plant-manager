# Complete Auto Scheduling and Group Defaults Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make every successful schedule-goal rebuild place every available non-reserve employee across enabled Auto work centers, preserve exact defaults, add evenly rotated user-group defaults, and leave the previous schedule untouched when complete safe placement is impossible.

**Architecture:** Add atomic exact/group-default persistence and expose it in Work Center Settings. Extend the pure scheduler with a complete-assignment min-cost flow that enforces every-person placement plus work-center lower/upper bounds, while enumerating bounded coupled-crew choices such as Trim Saw. Feed exact and group defaults into the global candidate graph, validate the complete result independently, and make rebuild/reset transactional at the existing schedule-save boundary.

**Tech Stack:** Python 3.12, FastAPI, psycopg2/PostgreSQL, Jinja2, browser JavaScript, pytest.

## Global Constraints

- Auto generates assignments only in work centers whose Auto checkbox is enabled.
- Available people are active, non-reserve employees without full-day time off.
- A successful rebuild assigns every available person exactly once and satisfies every enabled center minimum.
- Manual assignments, work-center maximums, qualifications, time off, training blocks, and Trim Saw safety remain hard constraints.
- Exact defaults are fixed work-center constraints; group defaults are fixed user-group constraints with the member center chosen globally.
- One person may have only one default target across exact work-center and group defaults.
- Group-default rotation fairness applies only after feasibility and safety, and never leaves a person unassigned.
- Infeasible rebuilds perform zero schedule writes and preserve the previous schedule and metadata.
- Reserves are never generated automatically.
- Do not add a third-party optimizer dependency.
- Start execution from the then-current `origin/main`; at planning time the global coverage solver is at `94f9f90`.
- Preserve the user's unrelated changes in `src/zira_dashboard/routes/skills.py`, automated-skill files/tests, untracked plans, and `uv.lock`.

---

## File Structure

- `src/zira_dashboard/_schema.py` — additive `group_default_people` table.
- `src/zira_dashboard/work_centers_store.py` — exact/group default reads, atomic replacement, conflict validation, and group rename/delete lifecycle.
- `src/zira_dashboard/settings_context.py` — pure group-default picker rows and conflict presentation.
- `src/zira_dashboard/routes/settings.py` — load and atomically save both default kinds.
- `src/zira_dashboard/templates/settings.html` — group Default People picker and error banner.
- `src/zira_dashboard/static/settings.css` — group-picker/conflict layout only.
- `src/zira_dashboard/schedule_solver.py` — complete-assignment problem/result types and pure min-cost flow.
- `src/zira_dashboard/rotation_suggestions.py` — build complete candidate graph, exact/group constraints, coupled crews, rotation costs, reasons, and infeasibility details.
- `src/zira_dashboard/routes/staffing.py` — gather user-group/default inputs and provide complete-schedule page context.
- `src/zira_dashboard/routes/rotations.py` — transactional rebuild response and completeness validation.
- `src/zira_dashboard/static/staffing.js` — failed-rebuild preservation and server-side Reset to defaults.
- `src/zira_dashboard/templates/staffing.html` — concise kept-schedule failure copy; existing structured “Why?” UI remains authoritative.
- `scripts/replay_schedule_solver.py` — report complete feasibility and unplaced-person blockers without writes.
- `CLAUDE.md` — scheduler invariants after rollout.
- `tests/test_group_default_people_store.py` — default normalization/persistence/lifecycle.
- `tests/test_settings_context.py` — group picker eligibility/conflicts.
- `tests/test_settings_group_defaults.py` — Settings route/template contract.
- `tests/test_schedule_solver.py` — deterministic complete-flow examples.
- `tests/test_schedule_solver_properties.py` — exhaustive feasibility oracle and invariants.
- `tests/test_rotation_suggestions.py` — defaults, swaps, rotation, modes, and coupled crews.
- `tests/test_staffing_rotations.py` — route input wiring, no-write failure, and success metadata.
- `tests/test_staffing_static.py` — browser contract for reset/failure rendering.

---

### Task 1: Persist exact and user-group default targets atomically

**Files:**
- Modify: `src/zira_dashboard/_schema.py:128-143`
- Modify: `src/zira_dashboard/work_centers_store.py:40-420`
- Create: `tests/test_group_default_people_store.py`

**Interfaces:**
- Produces: `InvalidDefaultTargets`, `group_default_people(group_name: str) -> list[str]`, `group_defaults_map() -> dict[str, list[str]]`, `default_target_conflicts() -> dict[str, tuple[str, ...]]`, and `replace_default_targets(*, exact_by_center: Mapping[str, Sequence[str]], group_by_name: Mapping[str, Sequence[str]]) -> None`.
- Preserves: `default_people(loc)` and `save_one(loc, {"default_people": names})` for existing callers.

- [ ] **Step 1: Write failing normalization and lifecycle tests**

Create `tests/test_group_default_people_store.py` with DB-free normalization tests and DB-backed lifecycle tests guarded by `DATABASE_URL`:

```python
import os

import pytest

from zira_dashboard import db, work_centers_store


def test_normalize_default_targets_rejects_cross_target_duplicates():
    with pytest.raises(work_centers_store.InvalidDefaultTargets) as caught:
        work_centers_store._normalize_default_targets(
            exact_by_center={"Repair 1": ["Ana"], "Repair 2": ["Luis"]},
            group_by_name={"Repair": ["Ana"]},
        )

    assert caught.value.conflicts == {
        "Ana": ("group:Repair", "work_center:Repair 1"),
    }


def test_normalize_default_targets_cleans_and_preserves_order():
    exact, groups = work_centers_store._normalize_default_targets(
        exact_by_center={"Repair 1": [" Ana ", "", "Ana", "Luis"]},
        group_by_name={"Repair": [" Zoe ", "Zoe"]},
    )

    assert exact == {"Repair 1": ("Ana", "Luis")}
    assert groups == {"Repair": ("Zoe",)}


pytestmark_db = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"), reason="needs Postgres"
)


@pytestmark_db
def test_group_defaults_follow_rename_and_delete():
    person_name = "__group_default_test_person__"
    old_group = "__group_default_old__"
    new_group = "__group_default_new__"
    db.execute(
        "INSERT INTO people (name, active, excluded) VALUES (%s, TRUE, FALSE) "
        "ON CONFLICT (name) DO UPDATE SET active = TRUE, excluded = FALSE",
        (person_name,),
    )
    work_centers_store.add_group(old_group)
    try:
        work_centers_store.replace_default_targets(
            exact_by_center={}, group_by_name={old_group: [person_name]}
        )
        assert work_centers_store.group_default_people(old_group) == [person_name]

        work_centers_store.rename_group(old_group, new_group)
        assert work_centers_store.group_default_people(old_group) == []
        assert work_centers_store.group_default_people(new_group) == [person_name]

        work_centers_store.delete_group(new_group)
        assert work_centers_store.group_default_people(new_group) == []
    finally:
        db.execute("DELETE FROM groups WHERE name IN (%s, %s)", (old_group, new_group))
        db.execute("DELETE FROM people WHERE name = %s", (person_name,))
        work_centers_store._invalidate_caches()
```

- [ ] **Step 2: Run the new tests and verify RED**

Run:

```bash
ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_group_default_people_store.py -q
```

Expected: FAIL because `InvalidDefaultTargets`, `_normalize_default_targets`, and the group-default store functions do not exist.

- [ ] **Step 3: Add the schema after the `groups` table**

In `src/zira_dashboard/_schema.py`, place the new table after `CREATE TABLE IF NOT EXISTS groups` so its foreign key target already exists:

```sql
CREATE TABLE IF NOT EXISTS group_default_people (
  group_name      TEXT NOT NULL REFERENCES groups(name) ON DELETE CASCADE,
  person_id       INTEGER NOT NULL REFERENCES people(id) ON DELETE CASCADE,
  sort_order      INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (group_name, person_id)
);
```

- [ ] **Step 4: Implement normalization, reads, and atomic replacement**

Add the following public shape to `work_centers_store.py`:

```python
from collections.abc import Mapping, Sequence


class InvalidDefaultTargets(ValueError):
    def __init__(self, conflicts: dict[str, tuple[str, ...]]):
        self.conflicts = conflicts
        rendered = "; ".join(
            f"{person}: {', '.join(targets)}"
            for person, targets in sorted(conflicts.items(), key=lambda item: item[0].lower())
        )
        super().__init__(f"Each person may have only one default target. {rendered}")


def _clean_names(values: Sequence[str]) -> tuple[str, ...]:
    seen: set[str] = set()
    cleaned: list[str] = []
    for value in values or ():
        name = str(value or "").strip()
        if name and name not in seen:
            seen.add(name)
            cleaned.append(name)
    return tuple(cleaned)


def _normalize_default_targets(*, exact_by_center, group_by_name):
    exact = {
        str(center): _clean_names(names)
        for center, names in (exact_by_center or {}).items()
    }
    groups = {
        str(group): _clean_names(names)
        for group, names in (group_by_name or {}).items()
    }
    targets: dict[str, list[str]] = {}
    for center, names in exact.items():
        for person in names:
            targets.setdefault(person, []).append(f"work_center:{center}")
    for group, names in groups.items():
        for person in names:
            targets.setdefault(person, []).append(f"group:{group}")
    conflicts = {
        person: tuple(sorted(person_targets, key=str.lower))
        for person, person_targets in targets.items()
        if len(person_targets) > 1
    }
    if conflicts:
        raise InvalidDefaultTargets(conflicts)
    return exact, groups


def group_default_people(group_name: str) -> list[str]:
    from . import db
    rows = db.query(
        "SELECT pe.name FROM group_default_people gdp "
        "JOIN people pe ON pe.id = gdp.person_id "
        "WHERE gdp.group_name = %s ORDER BY gdp.sort_order, lower(pe.name)",
        (group_name,),
    )
    return [row["name"] for row in rows]


def group_defaults_map() -> dict[str, list[str]]:
    from . import db
    rows = db.query(
        "SELECT gdp.group_name, pe.name FROM group_default_people gdp "
        "JOIN people pe ON pe.id = gdp.person_id "
        "ORDER BY lower(gdp.group_name), gdp.sort_order, lower(pe.name)"
    )
    result: dict[str, list[str]] = {}
    for row in rows:
        result.setdefault(row["group_name"], []).append(row["name"])
    return result


def replace_default_targets(*, exact_by_center, group_by_name) -> None:
    exact, groups = _normalize_default_targets(
        exact_by_center=exact_by_center, group_by_name=group_by_name
    )
    from . import db
    with db.cursor() as cur:
        cur.execute("DELETE FROM work_center_default_people")
        cur.execute("DELETE FROM group_default_people")
        for center, names in exact.items():
            for sort_order, person in enumerate(names):
                cur.execute(
                    "INSERT INTO work_center_default_people (wc_id, person_id, sort_order) "
                    "SELECT wc.id, pe.id, %s FROM work_centers wc, people pe "
                    "WHERE wc.name = %s AND pe.name = %s",
                    (sort_order, center, person),
                )
        for group, names in groups.items():
            for sort_order, person in enumerate(names):
                cur.execute(
                    "INSERT INTO group_default_people (group_name, person_id, sort_order) "
                    "SELECT %s, pe.id, %s FROM people pe WHERE pe.name = %s",
                    (group, sort_order, person),
                )
    _invalidate_caches()
```

Implement `default_target_conflicts()` by loading all exact and group rows and returning the same conflict mapping without raising. Extract `_target_index(exact, groups)` from `_normalize_default_targets` so both paths use identical labels and ordering:

```python
def default_target_conflicts() -> dict[str, tuple[str, ...]]:
    exact = {
        loc.name: tuple(default_people(loc))
        for loc in LOCATIONS
    }
    groups = {
        group: tuple(names)
        for group, names in group_defaults_map().items()
    }
    targets = _target_index(exact, groups)
    return {
        person: tuple(sorted(person_targets, key=str.lower))
        for person, person_targets in targets.items()
        if len(person_targets) > 1
    }
```

Update `save_one(loc, {"default_people": names})` to load all current exact/group targets, replace only `loc.name` in the in-memory exact map, validate with `_normalize_default_targets`, and write that center only after validation. This preserves its existing single-center contract without deleting defaults elsewhere.

- [ ] **Step 5: Preserve defaults through group rename/delete**

Inside the existing `rename_group` transaction, copy defaults before deleting the old group:

```python
cur.execute(
    "INSERT INTO group_default_people (group_name, person_id, sort_order) "
    "SELECT %s, person_id, sort_order FROM group_default_people "
    "WHERE group_name = %s ON CONFLICT (group_name, person_id) DO NOTHING",
    (new, old),
)
```

The existing delete of the old `groups` row then cascades old rows. `delete_group` needs no additional delete beyond verifying the foreign key cascade in the test.

- [ ] **Step 6: Run focused and existing store tests**

Run:

```bash
ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_group_default_people_store.py tests/test_work_centers_store_required_skills.py -q
```

Expected: PASS (DB-backed cases skip when `DATABASE_URL` is absent).

- [ ] **Step 7: Commit Task 1**

```bash
git add src/zira_dashboard/_schema.py src/zira_dashboard/work_centers_store.py tests/test_group_default_people_store.py
git commit -m "feat: persist group default people"
```

---

### Task 2: Add group defaults to Work Center Settings

**Files:**
- Modify: `src/zira_dashboard/settings_context.py:23-73`
- Modify: `src/zira_dashboard/routes/settings.py:282-306,880-935`
- Modify: `src/zira_dashboard/templates/settings.html:70-247`
- Modify: `src/zira_dashboard/static/settings.css:125-190,370-395`
- Modify: `tests/test_settings_context.py`
- Create: `tests/test_settings_group_defaults.py`

**Interfaces:**
- Consumes: `work_centers_store.group_default_people`, `replace_default_targets`, `default_target_conflicts`.
- Produces: group rows containing `default_people`, `default_pool`, and `default_conflicts`.

- [ ] **Step 1: Write failing pure context tests**

Append to `tests/test_settings_context.py`:

```python
def test_group_default_rows_include_only_nonreserve_people_qualified_somewhere():
    repair_1 = SimpleNamespace(name="Repair 1")
    repair_2 = SimpleNamespace(name="Repair 2")
    people = [
        SimpleNamespace(name="Ana", reserve=False, level=lambda skill: 3),
        SimpleNamespace(name="Learner", reserve=False, level=lambda skill: 0),
        SimpleNamespace(name="Reserve", reserve=True, level=lambda skill: 3),
    ]
    rows = [{"name": "Repair", "count": 2}]

    result = settings_context.with_group_default_context(
        rows,
        people,
        members_for=lambda kind, name: [repair_1, repair_2],
        required_skills_for=lambda loc: ("Repair",),
        defaults_for=lambda name: ["Ana"],
        conflicts={"Ana": ("group:Repair", "work_center:Repair 1")},
    )

    assert result[0]["default_people"] == ["Ana"]
    assert result[0]["default_pool"] == [
        {"name": "Ana", "eligible_centers": ("Repair 1", "Repair 2")}
    ]
    assert result[0]["default_conflicts"] == {
        "Ana": ("group:Repair", "work_center:Repair 1")
    }
```

- [ ] **Step 2: Run context test and verify RED**

Run:

```bash
ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_settings_context.py::test_group_default_rows_include_only_nonreserve_people_qualified_somewhere -q
```

Expected: FAIL because `with_group_default_context` does not exist.

- [ ] **Step 3: Implement the pure group-row builder**

Add `with_group_default_context(rows, active_people, *, members_for, required_skills_for, defaults_for, conflicts)` to `settings_context.py`. It must return copied rows, compute each person's tuple of eligible member-center names using `all(person.level(skill) >= 1 for skill in required_skills_for(member))`, exclude reserves, preserve stale selected names in `default_people`, and attach only conflicts involving selected names.

Use this exact returned shape:

```python
{
    **row,
    "default_people": list(defaults_for(row["name"])),
    "default_pool": [
        {"name": person.name, "eligible_centers": tuple(eligible_centers)}
        for person, eligible_centers in eligible_people
    ],
    "default_conflicts": selected_conflicts,
}
```

- [ ] **Step 4: Add failing route/template contract tests**

Create `tests/test_settings_group_defaults.py` with static and route-focused tests:

```python
from pathlib import Path


def test_settings_group_table_has_default_people_picker():
    html = Path("src/zira_dashboard/templates/settings.html").read_text()
    assert "group_default_people_present__{{ g.name }}" in html
    assert 'name="group_default_people__{{ g.name }}"' in html
    assert "g.default_people" in html
    assert "g.default_conflicts" in html
    assert "protected anchors used when Auto runs" in html


def test_settings_route_uses_one_atomic_default_replacement():
    source = Path("src/zira_dashboard/routes/settings.py").read_text()
    handler = source.split("async def settings_save_work_centers", 1)[1].split(
        "@router.post(\"/settings\")", 1
    )[0]
    assert "replace_default_targets(" in handler
    assert "group_default_people__" in handler
    assert 'updates["default_people"]' not in handler
```

- [ ] **Step 5: Wire group rows and atomic save in the route**

After `group_summary`, wrap the rows:

```python
group_rows = settings_context.with_group_default_context(
    group_rows,
    active_people_objs,
    members_for=work_centers_store.members,
    required_skills_for=lambda loc: tuple(work_centers_store.required_skills(loc)),
    defaults_for=work_centers_store.group_default_people,
    conflicts=work_centers_store.default_target_conflicts(),
)
```

In `_work`, collect all posted exact/group defaults, call `save_one` without a `default_people` update, and then call exactly one atomic replacement:

```python
exact_defaults: dict[str, list[str]] = {}
group_defaults: dict[str, list[str]] = {}

# Inside the work-center loop:
if (prefix + "default_people_present") in form:
    exact_defaults[loc.name] = form.getlist(prefix + "default_people")
else:
    exact_defaults[loc.name] = work_centers_store.default_people(loc)

# After registry edits and work-center saves:
for group_name in work_centers_store.registered_groups():
    marker = f"group_default_people_present__{group_name}"
    field = f"group_default_people__{group_name}"
    group_defaults[group_name] = (
        form.getlist(field)
        if marker in form
        else work_centers_store.group_default_people(group_name)
    )
work_centers_store.replace_default_targets(
    exact_by_center=exact_defaults,
    group_by_name=group_defaults,
)
```

Catch `InvalidDefaultTargets`. JSON requests return `422` with `{"ok": False, "error": str(exc), "conflicts": exc.conflicts}`. Browser form requests redirect to the same section with a URL-encoded `defaults_error`; render that value in an `aria-live="assertive"` error banner.

- [ ] **Step 6: Render the group picker and conflict state**

Add a `Default people` column to the user-managed Groups table. Reuse `.skills-picker.default-people-picker`, with field names from the static test. Show each option's eligible centers in a muted suffix. Mark selected conflicts with `class="default-conflict"` and render the target list. Update the note to:

```html
<div class="note">Max blank = unlimited. Required skills control safe automatic placement. Default people are protected anchors used when Auto runs; group defaults rotate among qualified enabled member work centers.</div>
```

Add only the width, muted suffix, banner, and conflict-color rules needed in `settings.css`; reuse existing picker behavior from `settings.js`.

- [ ] **Step 7: Run settings tests**

Run:

```bash
ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_settings_context.py tests/test_settings_group_defaults.py tests/test_settings_api_keys.py -q
```

Expected: PASS.

- [ ] **Step 8: Commit Task 2**

```bash
git add src/zira_dashboard/settings_context.py src/zira_dashboard/routes/settings.py src/zira_dashboard/templates/settings.html src/zira_dashboard/static/settings.css tests/test_settings_context.py tests/test_settings_group_defaults.py
git commit -m "feat: manage group default people"
```

---

### Task 3: Add the pure complete-assignment solver

**Files:**
- Modify: `src/zira_dashboard/schedule_solver.py:1-424`
- Modify: `tests/test_schedule_solver.py`
- Modify: `tests/test_schedule_solver_properties.py`

**Interfaces:**
- Consumes: existing `CandidateEdge`, `CrewOption`, and stable candidate rank costs.
- Produces: `CompleteCenter`, `CompleteScheduleResult`, `PlacementIssue`, and `solve_complete_schedule(*, people: Sequence[str], centers: Sequence[CompleteCenter], candidates: Sequence[CandidateEdge]) -> CompleteScheduleResult`.
- Preserves: `solve_minimum_coverage(requirements: Sequence[CenterRequirement]) -> CoverageResult` for compatibility until Task 4 switches its caller.

- [ ] **Step 1: Write failing complete-flow examples**

Append to `tests/test_schedule_solver.py`:

```python
from zira_dashboard.schedule_solver import CompleteCenter, solve_complete_schedule


def test_complete_solver_places_every_person_and_meets_center_minimums():
    result = solve_complete_schedule(
        people=("Cross Trained", "Repair Only", "Extra Repair"),
        centers=(
            CompleteCenter("Dismantler 1", "Dismantler", minimum=1, capacity=1),
            CompleteCenter("Repair 1", "Repair", minimum=1, capacity=2),
        ),
        candidates=(
            edge("Cross Trained", "Dismantler 1", level=1, rank=20),
            edge("Cross Trained", "Repair 1", level=3, rank=0),
            edge("Repair Only", "Repair 1", level=1, rank=5),
            edge("Extra Repair", "Repair 1", level=1, rank=10),
        ),
    )

    assert result.complete is True
    assert {(item.center, item.person) for item in result.decisions} == {
        ("Dismantler 1", "Cross Trained"),
        ("Repair 1", "Repair Only"),
        ("Repair 1", "Extra Repair"),
    }
    assert result.unplaced_people == ()


def test_complete_solver_returns_failure_instead_of_partial_schedule():
    result = solve_complete_schedule(
        people=("Qualified", "No Edge"),
        centers=(CompleteCenter("Repair 1", "Repair", minimum=1, capacity=1),),
        candidates=(edge("Qualified", "Repair 1"),),
    )

    assert result.complete is False
    assert result.decisions == ()
    assert result.unplaced_people == ("No Edge",)
    assert result.issues[0].code == "person_no_enabled_qualified_center"


def test_complete_solver_honors_variable_complete_crew_options():
    result = solve_complete_schedule(
        people=("Green", "Learner", "Repair"),
        centers=(
            CompleteCenter(
                "Trim Saw 1",
                "Trim Saw",
                minimum=2,
                capacity=2,
                crew_options=(CrewOption(
                    "Trim Saw 1",
                    (edge("Green", "Trim Saw 1", level=3), edge("Learner", "Trim Saw 1", level=1)),
                ),),
            ),
            CompleteCenter("Repair 1", "Repair", minimum=1, capacity=1),
        ),
        candidates=(edge("Repair", "Repair 1"),),
    )

    assert result.complete is True
    assert {item.person for item in result.decisions} == {"Green", "Learner", "Repair"}
```

- [ ] **Step 2: Run examples and verify RED**

Run:

```bash
ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_schedule_solver.py -q
```

Expected: collection FAIL because the complete-solver interfaces are missing.

- [ ] **Step 3: Add immutable complete-solver result types**

Add:

```python
@dataclass(frozen=True)
class CompleteCenter:
    center: str
    group: str
    minimum: int
    capacity: int
    protected_people: tuple[str, ...] = ()
    crew_options: tuple[CrewOption, ...] = ()


@dataclass(frozen=True)
class PlacementIssue:
    code: str
    message: str
    person: str | None = None
    centers: tuple[str, ...] = ()
    rejections: tuple[CandidateRejection, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "code": self.code,
            "message": self.message,
            "person": self.person,
            "centers": list(self.centers),
            "rejections": [item.to_dict() for item in self.rejections],
        }


@dataclass(frozen=True)
class CompleteScheduleResult:
    complete: bool
    decisions: tuple[AssignmentDecision, ...]
    placed_people: tuple[str, ...]
    unplaced_people: tuple[str, ...]
    staffed_centers: tuple[str, ...]
    issues: tuple[PlacementIssue, ...]
    total_cost: int = 0
```

Validate unique center names, nonnegative minimums, `minimum <= capacity`, unique requested people, level 1-3 edges, edge centers that exist, and edge people that exist.

- [ ] **Step 4: Implement min-cost full-person flow for non-coupled centers**

Reuse `_Arc`, `_add_arc`, and the existing shortest augmenting-path loop. Build:

```text
source -> each person (capacity 1, cost 0)
person -> eligible center (capacity 1, lexicographic edge cost)
center -> required sink lane (capacity remaining minimum, large negative reward)
center -> optional sink lane (capacity remaining capacity - remaining minimum, cost 0)
```

Continue augmenting until no source-to-sink path. A branch is feasible only when flow equals the number of remaining people and every required lane is saturated. Set the required-lane reward greater than the maximum possible sum of all edge costs:

```python
ordinary_bound = 1 + sum(max(0, edge.rank_cost) + edge.override_cost for edge in candidates)
required_reward = ordinary_bound * (len(people) + 1)
```

Encode preference override ahead of rank and deterministic person/center tie-breaks exactly as `_match_single_requirements` does. Return decisions only for a feasible full flow. For failure, retain the best maximum-flow diagnostic internally but return `decisions=()`.

- [ ] **Step 5: Enumerate bounded coupled crews around the flow**

Sort coupled centers by option count. For each complete crew option, reserve its people and capacity, then solve all remaining people/centers through the flow. Also permit a zero-generated crew only when protected occupants already meet the center minimum. Reject an option whose members overlap protected or already chosen people.

Compare only complete branches by:

```python
(
    sum(item.preference == "never" for item in decisions),
    sum(item.rank_cost for item in decisions),
    tuple((item.center.lower(), item.person.lower()) for item in decisions),
)
```

If no complete branch exists, choose the diagnostic branch with the most placed people, then fewest unsatisfied minimum slots, to produce deterministic unplaced names and `person_all_qualified_centers_full` issues.

- [ ] **Step 6: Add exhaustive feasibility property tests**

In `tests/test_schedule_solver_properties.py`, add a compact exhaustive oracle that enumerates each person's candidate center and returns whether any assignment satisfies all min/max bounds:

```python
from itertools import product


def exhaustive_complete(people, centers, candidates):
    choices = {
        person: tuple(sorted(
            {edge.center for edge in candidates if edge.person == person},
            key=str.lower,
        ))
        for person in people
    }
    if any(not choices[person] for person in people):
        return False
    by_center = {center.center: center for center in centers}
    for selected in product(*(choices[person] for person in people)):
        counts = {name: 0 for name in by_center}
        for center_name in selected:
            counts[center_name] += 1
        if all(
            center.minimum <= counts[center.center] <= center.capacity
            for center in centers
        ):
            return True
    return False
```

Generate deterministic fixtures up to five people and three non-coupled centers. Assert:

```python
result.complete == oracle_complete
```

For complete results assert every requested person appears once, all center bounds hold, no unknown edge is chosen, and input order permutations return identical results.

- [ ] **Step 7: Run solver tests**

Run:

```bash
ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_schedule_solver.py tests/test_schedule_solver_properties.py -q
```

Expected: PASS.

- [ ] **Step 8: Commit Task 3**

```bash
git add src/zira_dashboard/schedule_solver.py tests/test_schedule_solver.py tests/test_schedule_solver_properties.py
git commit -m "feat: solve complete staffing assignments"
```

---

### Task 4: Build complete candidates with exact/group defaults and even rotation

**Files:**
- Modify: `src/zira_dashboard/rotation_suggestions.py:39-1414`
- Modify: `tests/test_rotation_suggestions.py`

**Interfaces:**
- Consumes: `solve_complete_schedule`, exact defaults by center, group defaults by user group, and user-group center membership.
- Produces: `RecycledSuggestion.complete`, `available_people`, `placed_people`, `placement_issues`, and complete assignments/reasons.

- [ ] **Step 1: Write failing exact/group and all-person tests**

Add focused tests to `tests/test_rotation_suggestions.py`:

```python
def test_engine_assigns_every_available_nonreserve_person():
    result = suggest_recycled_assignments(
        TARGET_DAY,
        "normal",
        roster=[
            staffing.Person("Cross", True, False, {"Repair": 3, "Dismantle": 1}),
            staffing.Person("Repair A", True, False, {"Repair": 1}),
            staffing.Person("Repair B", True, False, {"Repair": 1}),
        ],
        group_locations={"Repair": ("Repair 1",), "Dismantler": ("Dismantler 1",)},
        group_required_skills={"Repair": ("Repair",), "Dismantler": ("Dismantle",)},
        center_minimums={"Repair 1": 1, "Dismantler 1": 1},
        center_capacities={"Repair 1": 2, "Dismantler 1": 1},
    )

    assert result.complete is True
    assert result.unused_people == ()
    assert result.assigned_people == {"Cross", "Repair A", "Repair B"}


def test_group_default_rotates_over_qualified_enabled_members():
    history = RecycledHistory(
        center_counts={("Ana", "Repair 1"): 2, ("Ana", "Repair 2"): 1},
        last_center_by_person_group={("Ana", "User Group:Repair Line"): "Repair 2"},
    )
    result = suggest_recycled_assignments(
        TARGET_DAY,
        "normal",
        roster=[staffing.Person("Ana", True, False, {"Repair": 3})],
        group_locations={"Repair": ("Repair 1", "Repair 2", "Repair 3")},
        group_required_skills={"Repair": ("Repair",)},
        center_minimums={"Repair 1": 0, "Repair 2": 0, "Repair 3": 0},
        center_capacities={"Repair 1": 1, "Repair 2": 1, "Repair 3": 1},
        group_defaults={"Repair Line": ("Ana",)},
        user_group_centers={"Repair Line": ("Repair 1", "Repair 2", "Repair 3")},
        history=history,
    )

    assert result.complete is True
    assert result.assignments["Repair 3"] == ["Ana"]
    assert result.reason_codes["Repair 3"]["Ana"] == "group_default"


def test_available_default_with_disabled_target_blocks_complete_result():
    result = suggest_recycled_assignments(
        TARGET_DAY,
        "normal",
        roster=[staffing.Person("Ana", True, False, {"Repair": 3})],
        group_locations={"Dismantler": ("Dismantler 1",)},
        group_required_skills={"Dismantler": ("Dismantle",)},
        exact_defaults={"Repair 1": ("Ana",)},
        center_minimums={"Dismantler 1": 0},
        center_capacities={"Dismantler 1": 1},
    )

    assert result.complete is False
    assert result.placement_issues[0].code == "exact_default_center_disabled"
    assert result.assignments == {}
```

These examples use `staffing.Person` directly because the file's `_person` helper intentionally grants the same level in every Recycled group and would hide the cross-skill edge cases.

- [ ] **Step 2: Run focused tests and verify RED**

Run the three new test node IDs. Expected: FAIL because the new arguments/result fields do not exist and the current engine allows unused people.

- [ ] **Step 3: Extend suggestion result and inputs**

Add defaulted fields to `RecycledSuggestion`:

```python
complete: bool = False
available_people: tuple[str, ...] = ()
placed_people: tuple[str, ...] = ()
placement_issues: tuple[schedule_solver.PlacementIssue, ...] = ()
default_assignments: dict[str, str] = field(default_factory=dict)
```

Keep `complete=False` as the fail-safe default. Update every existing direct `RecycledSuggestion(...)` test fixture that represents a successful engine result to pass `complete=True`; fixtures representing an unresolved/partial result must now assert failure and zero persistence.

Add keyword inputs to `suggest_recycled_assignments`:

```python
exact_defaults: Mapping[str, Sequence[str]] | None = None,
group_defaults: Mapping[str, Sequence[str]] | None = None,
user_group_centers: Mapping[str, Sequence[str]] | None = None,
```

- [ ] **Step 4: Validate defaults before solving**

Create pure helpers that map each default person to one target, returning these stable issues before any decisions:

```python
schedule_solver.PlacementIssue(
    code="exact_default_center_disabled",
    person=name,
    centers=(center,),
    message=f"{name}'s default work center {center} is not enabled. Previous schedule kept.",
)
```

Use the analogous approved codes for unqualified exact targets, no enabled group member, no qualified group member, and conflicts. Absent, inactive, and reserve default names do not become daily requirements. Filter a default person's candidate edges to the exact center or eligible enabled user-group member centers.

- [ ] **Step 5: Build one candidate edge for every safe person/center pair**

Replace the Stage-2-minimum plus optional-fill loops with one graph build. For every active, non-reserve roster person not already protected, inspect every enabled scheduling target and center. Add an edge only if all effective required skills are level 1+. Do not filter `never`; encode it through `CandidateEdge.preference` so the complete solver can override it only when necessary.

Rank costs must be nonnegative and encode, in order:

1. group-default center imbalance and most-recent-center penalty for group-default people;
2. selected mode skill/preference score;
3. ordinary group and center history;
4. canonical name/center tie-breaks handled by the solver.

Give exact defaults `reason_code="exact_default"`, group defaults `reason_code="group_default"`, required-minimum placements `minimum_coverage`, and remaining placements the existing mode reason code. If `never` is used, `preference_override` remains the displayed reason.

Extend `_load_recycled_history` (or its pure post-processing helper) so user-managed groups get `last_center_by_person_group[(name, f"User Group:{group_name}")]` from the most recent scheduled member center. `center_counts[(name, center)]` remains the primary even-rotation counter and already spans all centers.

- [ ] **Step 6: Generate full coupled-crew options**

For Trim Saw and any other coupled center, enumerate every safe final generated crew size from the remaining minimum through remaining capacity around protected occupants. Every option must contain all generated members for that center, not only the members needed to reach minimum. Keep `_valid_trim_saw_pair` authoritative. This prevents the complete solver from adding an unsafe optional third path after a minimum pair.

- [ ] **Step 7: Apply only complete solver output**

Call `solve_complete_schedule`. On `complete=False`, return a suggestion with no regenerated managed assignments, `complete=False`, structured placement issues, and all available names that are not protected in `unused_people`. On success, place all decisions, set every enabled center key, and assert:

```python
set(result.placed_people) == available_names
not result.unused_people
```

Preserve non-managed assignments and manual locks in both outcomes.

- [ ] **Step 8: Add rotation, modes, reserve, absence, and pairing regressions**

Add tests proving:

- three successive history inputs rotate one group default Repair 1 -> 2 -> 3;
- only qualified enabled group members participate;
- capacity may repeat a center without losing the person;
- reserves and full-day-absent people are excluded from `available_people`;
- a `never` edge is used only when required for complete placement;
- all three modes return the same complete headcount and minimum coverage;
- Trim Saw never returns an unsafe crew;
- an impossible crew returns no regenerated partial schedule.

- [ ] **Step 9: Run rotation and solver tests**

Run:

```bash
ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_rotation_suggestions.py tests/test_schedule_solver.py tests/test_schedule_solver_properties.py -q
```

Expected: PASS.

- [ ] **Step 10: Commit Task 4**

```bash
git add src/zira_dashboard/rotation_suggestions.py tests/test_rotation_suggestions.py
git commit -m "feat: require complete automatic schedules"
```

---

### Task 5: Wire default inputs and make rebuilds transactional

**Files:**
- Modify: `src/zira_dashboard/routes/staffing.py:200-770`
- Modify: `src/zira_dashboard/routes/rotations.py:32-360`
- Modify: `tests/test_staffing_rotations.py`

**Interfaces:**
- Consumes: complete suggestion result and default store APIs.
- Produces: rebuild success only for complete validated schedules; `422` structured failure with zero writes. Rebuild callers pass manual locks and saved defaults separately so a default is never duplicated as a manual lock.

- [ ] **Step 1: Write failing route tests for no-write failure and complete success**

Add tests around `rebuild_rotation` using the existing `_rotations_client` and `_stub_recommendation_inputs` helpers:

```python
def test_rebuild_infeasible_keeps_previous_schedule_and_does_not_save(monkeypatch):
    client, rotations = _rotations_client(monkeypatch)
    staffing_route = _stub_recommendation_inputs(monkeypatch)
    saved = []
    monkeypatch.setattr(rotations.staffing, "save_schedule", saved.append)
    monkeypatch.setattr(
        rotations.staffing,
        "load_roster",
        lambda: [_person("Gerardo Garcia", 1)],
    )
    monkeypatch.setattr(
        rotations.staffing,
        "load_schedule",
        lambda day: staffing.Schedule(day=day),
    )
    monkeypatch.setattr(
        staffing_route,
        "_recycled_suggestion_for_day",
        lambda *args, **kwargs: rotation_suggestions.RecycledSuggestion(
            assignments={},
            sources={},
            reasons={},
            warnings=(),
            complete=False,
            available_people=("Gerardo Garcia",),
            placed_people=(),
            unused_people=("Gerardo Garcia",),
            placement_issues=(schedule_solver.PlacementIssue(
                code="person_no_enabled_qualified_center",
                person="Gerardo Garcia",
                centers=(),
                message="Gerardo Garcia has no qualified enabled work center. Previous schedule kept.",
            ),),
        ),
    )

    response = client.post(
        "/api/rotations/rebuild",
        json={"day": "2026-07-15", "mode": "normal"},
    )

    assert response.status_code == 422
    assert saved == []
    assert response.json()["schedule_kept"] is True
    assert response.json()["placement"]["unplaced_people"] == ["Gerardo Garcia"]
```

Add this success regression beside it:

```python
def test_rebuild_complete_result_saves_once_and_preserves_metadata(monkeypatch):
    client, rotations = _rotations_client(monkeypatch)
    staffing_route = _stub_recommendation_inputs(monkeypatch)
    prior = staffing.Schedule(
        day=TARGET_DAY,
        assignments={"Truck Driver": ["Manual Driver"]},
        notes="keep",
        wc_notes={"Truck Driver": "keep"},
        custom_hours={"start": "06:00", "end": "14:30", "breaks": []},
        published_snapshot={"assignments": {"Truck Driver": ["Manual Driver"]}},
    )
    saved = []
    monkeypatch.setattr(
        rotations.staffing,
        "load_roster",
        lambda: [_person("A", 1), _person("B", 1), _person("C", 1)],
    )
    monkeypatch.setattr(rotations.staffing, "load_schedule", lambda day: prior)
    monkeypatch.setattr(rotations.staffing, "save_schedule", saved.append)
    monkeypatch.setattr(
        staffing_route,
        "_recycled_suggestion_for_day",
        lambda *args, **kwargs: rotation_suggestions.RecycledSuggestion(
            assignments={"Repair 1": ["A", "B", "C"]},
            sources={"Repair 1": {name: "generated" for name in ("A", "B", "C")}},
            reasons={"Repair 1": {name: "complete assignment" for name in ("A", "B", "C")}},
            warnings=(),
            complete=True,
            available_people=("A", "B", "C"),
            placed_people=("A", "B", "C"),
            unused_people=(),
        ),
    )

    response = client.post(
        "/api/rotations/rebuild",
        json={"day": TARGET_DAY.isoformat(), "mode": "normal"},
    )

    assert response.status_code == 200
    assert len(saved) == 1
    assert saved[0].assignments["Truck Driver"] == ["Manual Driver"]
    assert saved[0].notes == "keep"
    assert saved[0].wc_notes == {"Truck Driver": "keep"}
    assert saved[0].custom_hours == prior.custom_hours
    assert saved[0].published_snapshot == prior.published_snapshot
    assert response.json()["placement"]["unplaced_people"] == []
```

- [ ] **Step 2: Run route tests and verify RED**

Run the two new test node IDs. Expected: FAIL because incomplete suggestions are currently saved and the response lacks placement details.

- [ ] **Step 3: Gather exact/group inputs once in Staffing route helpers**

Add a helper returning:

```python
def _default_inputs(strict: bool = False):
    exact = {
        loc.name: tuple(work_centers_store.default_people(loc))
        for loc in staffing.LOCATIONS
    }
    groups = {
        name: tuple(people)
        for name, people in work_centers_store.group_defaults_map().items()
    }
    members = {
        name: tuple(loc.name for loc in work_centers_store.members("group", name))
        for name in groups
    }
    return exact, groups, members
```

When `strict=False`, log and return empty maps on read failure. When `strict=True`, propagate the failure so a rebuild returns `503` without saving. Pass these maps through `_recycled_suggestion_for_day`, `_recycled_context_for_day`, and all route callers.

Refactor `_protected_locks` to accept `include_saved_defaults: bool = True`. Existing display/advisory callers retain the default. Complete rebuild callers pass `include_saved_defaults=False`, then pass `exact_defaults`, `group_defaults`, and `user_group_centers` separately to the suggestion engine:

```python
manual_locks = _protected_locks(
    sched.assignment_sources,
    sched.assignments,
    allowed_centers=enabled_centers,
    strict_default_reads=True,
    include_saved_defaults=False,
)
```

Remove `_append_auto_expansion_warning` from the successful rebuild path. “Enable more centers” is no longer a non-blocking success warning: insufficient enabled capacity is a structured incomplete result and keeps the previous schedule. Auto-center selection may still display the same placement issue as an advisory before the user clicks a goal button.

- [ ] **Step 4: Add independent completeness validation**

In `routes/rotations.py`, add this exact boundary:

```python
def _validate_complete_rebuild(
    *,
    available_people: Sequence[str],
    protected_assignments: Mapping[str, Sequence[str]],
    enabled_centers: Sequence[str],
    center_minimums: Mapping[str, int],
    center_capacities: Mapping[str, int | None],
    required_skills: Mapping[str, Sequence[str]],
    roster: Sequence[staffing.Person],
    exact_defaults: Mapping[str, Sequence[str]],
    group_defaults: Mapping[str, Sequence[str]],
    user_group_centers: Mapping[str, Sequence[str]],
    proposed_assignments: Mapping[str, Sequence[str]],
    proposed_sources: Mapping[str, Mapping[str, str]],
) -> tuple[schedule_solver.PlacementIssue, ...]:
```

It returns placement issues for duplicates, missing people, disabled generated centers, min/max violations, qualification violations, and default violations.

Build `new_assignments` and `new_sources` in memory with the existing pure merge helpers, then apply the final gate:

```python
validation_issues = _validate_complete_rebuild(
    available_people=suggestion.available_people,
    protected_assignments=manual_locks,
    enabled_centers=enabled_centers,
    center_minimums=center_minimums,
    center_capacities=center_capacities,
    required_skills=required_skills,
    roster=roster,
    exact_defaults=exact_defaults,
    group_defaults=group_defaults,
    user_group_centers=user_group_centers,
    proposed_assignments=new_assignments,
    proposed_sources=new_sources,
)
if not suggestion.complete or suggestion.placement_issues or validation_issues:
    issues = tuple(suggestion.placement_issues) + tuple(validation_issues)
    return JSONResponse(
        {
            "ok": False,
            "error": "Auto could not safely assign everyone. Previous schedule kept.",
            "schedule_kept": True,
            "placement": {
                "available_people": list(suggestion.available_people),
                "placed_people": list(suggestion.placed_people),
                "unplaced_people": list(suggestion.unused_people),
                "issues": [issue.to_dict() for issue in issues],
            },
        },
        status_code=422,
    )
```

The pure in-memory merge helpers may run before validation. This failure branch must occur before cache invalidation and `save_schedule`; neither may run for an incomplete or invalid result.

- [ ] **Step 5: Return complete placement summary on success**

Add:

```python
"placement": {
    "available_people": list(suggestion.available_people),
    "placed_people": list(suggestion.placed_people),
    "unplaced_people": [],
    "defaults": dict(suggestion.default_assignments),
    "issues": [],
},
```

Keep the existing assignments, sources, reasons, warnings, coverage, and enabled-center fields.

- [ ] **Step 6: Keep Auto-center selection non-mutating to schedules**

`POST /api/rotations/auto-work-centers` continues to save the planner's requested enabled set even when today's roster is infeasible. It returns placement advisory details but never activates another center and never calls `save_schedule`. Add a regression asserting both behaviors.

- [ ] **Step 7: Run route tests**

Run:

```bash
ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_staffing_rotations.py tests/test_staffing_schedule_metadata.py tests/test_auto_schedule_capacity.py -q
```

Expected: PASS.

- [ ] **Step 8: Commit Task 5**

```bash
git add src/zira_dashboard/routes/staffing.py src/zira_dashboard/routes/rotations.py tests/test_staffing_rotations.py
git commit -m "fix: keep incomplete auto rebuilds transactional"
```

---

### Task 6: Move Reset to defaults onto the complete server rebuild

**Files:**
- Modify: `src/zira_dashboard/routes/rotations.py:276-360`
- Modify: `src/zira_dashboard/static/staffing.js:69-99,620-850`
- Modify: `src/zira_dashboard/templates/staffing.html:150-240`
- Modify: `tests/test_staffing_static.py`
- Modify: `tests/test_staffing_rotations.py`

**Interfaces:**
- Consumes: `/api/rotations/rebuild` with `reset_to_defaults: true`.
- Produces: server-authoritative reset that clears manual assignments only inside enabled Auto centers, preserves outside-Auto assignments, and follows all-people-or-no-save semantics.

- [ ] **Step 1: Write failing static reset/failure tests**

Replace the old DOM-reset contract in `tests/test_staffing_static.py` with:

```python
def test_reset_to_defaults_uses_complete_rebuild_endpoint():
    js = _script()
    rotation = js.split("// ---------- Rotation goal", 1)[1].split(
        "// Assignments to Do modal", 1
    )[0]
    reset = rotation.split("const resetScheduleBtn", 1)[1].split(
        "modeBtns.forEach", 1
    )[0]
    assert "await rebuild(currentMode(), { resetToDefaults: true })" in reset
    assert "window.SMART_DEFAULTS_BY_LOC" not in reset
    assert "kickAutosave()" not in reset


def test_failed_rebuild_keeps_grid_and_renders_person_issues():
    js = _script()
    rebuild = js.split("async function rebuild", 1)[1].split(
        "modeBtns.forEach", 1
    )[0]
    assert "if (!resp.ok || !data.ok)" in rebuild
    assert "renderPlacementFailure(data)" in rebuild
    failure_branch = rebuild.split("if (!resp.ok || !data.ok)", 1)[1].split("applyRebuild", 1)[0]
    assert "applyRebuild" not in failure_branch
    assert "kickAutosave" not in failure_branch
```

- [ ] **Step 2: Run static tests and verify RED**

Run:

```bash
ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_staffing_static.py::test_reset_to_defaults_uses_complete_rebuild_endpoint tests/test_staffing_static.py::test_failed_rebuild_keeps_grid_and_renders_person_issues -q
```

Expected: FAIL because reset remains client-only.

- [ ] **Step 3: Extend rebuild request parsing**

Parse a strict boolean:

```python
reset_to_defaults = body.get("reset_to_defaults", False)
if not isinstance(reset_to_defaults, bool):
    return _error("reset_to_defaults must be a boolean.")
```

When true, discard manual locks inside enabled Auto centers before building the suggestion. Continue preserving every assignment outside the enabled set. Exact/group defaults still apply through the normal input path.

- [ ] **Step 4: Refactor browser rebuild to accept options**

Use:

```javascript
async function rebuild(mode, options = {}) {
  const resp = await fetch('/api/rotations/rebuild', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      day: rotationControls.dataset.day,
      mode,
      reset_to_defaults: options.resetToDefaults === true,
    }),
  });
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok || !data.ok) {
    renderPlacementFailure(data);
    return false;
  }
  applyRebuild(data);
  return true;
}
```

`currentMode()` already exists inside the rotation-control IIFE. `renderPlacementFailure` must call the existing safe structured renderer with `data.placement.issues`, prepend `data.error`, and never mutate checkboxes, summaries, left rail, current mode, or autosave state. Remove `kickAutosave()` from `applyRebuild`; the rebuild API already persisted the authoritative schedule, so a second ordinary form save is unnecessary.

- [ ] **Step 5: Replace Reset's DOM mutation**

Delete the old top-level Reset handler and bind `#reset-schedule-btn` inside the rotation-control IIFE, where it can call `currentMode()` and `rebuild()`. Keep the posted/edit guard and confirmation, disable the button while waiting, and call:

```javascript
const succeeded = await rebuild(currentMode(), { resetToDefaults: true });
if (succeeded) syncLeftRailWithSchedule();
```

Update confirmation copy to say enabled Auto work centers will be rebuilt around saved exact/group defaults and the previous schedule will be kept if everyone cannot be placed.

- [ ] **Step 6: Add route regression for reset manual-lock scope**

Prove a manual assignment inside an enabled center is removed from locks before solving, while an assignment outside enabled Auto remains unchanged and counts as placed.

- [ ] **Step 7: Run browser-contract and route tests**

Run:

```bash
ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_staffing_static.py tests/test_staffing_rotations.py -q
```

Expected: PASS.

- [ ] **Step 8: Commit Task 6**

```bash
git add src/zira_dashboard/routes/rotations.py src/zira_dashboard/static/staffing.js src/zira_dashboard/templates/staffing.html tests/test_staffing_static.py tests/test_staffing_rotations.py
git commit -m "feat: rebuild defaults through complete scheduler"
```

---

### Task 7: Replay, document, and verify the complete scheduler

**Files:**
- Modify: `scripts/replay_schedule_solver.py`
- Modify: `CLAUDE.md`
- Modify: `CHANGELOG.md`
- Create: `tests/test_replay_schedule_solver.py`

**Interfaces:**
- Consumes: complete suggestion/result fields.
- Produces: read-only rollout diagnostics and permanent invariants.

- [ ] **Step 1: Write the failing replay serialization test**

Create `tests/test_replay_schedule_solver.py`:

```python
from types import SimpleNamespace

from scripts.replay_schedule_solver import serialize_replay


def test_serialize_replay_reports_complete_placement_and_defaults():
    issue = SimpleNamespace(to_dict=lambda: {
        "code": "person_no_enabled_qualified_center",
        "person": "Gerardo Garcia",
    })
    suggestion = SimpleNamespace(
        complete=False,
        available_people=("Gerardo Garcia", "Jose Ochoa"),
        placed_people=("Jose Ochoa",),
        unused_people=("Gerardo Garcia",),
        default_assignments={"Jose Ochoa": "group:Repair"},
        placement_issues=(issue,),
    )

    assert serialize_replay("2026-07-15", suggestion, 12.3456) == {
        "day": "2026-07-15",
        "complete": False,
        "available_people": ["Gerardo Garcia", "Jose Ochoa"],
        "placed_people": ["Jose Ochoa"],
        "unplaced_people": ["Gerardo Garcia"],
        "default_assignments": {"Jose Ochoa": "group:Repair"},
        "issues": [{
            "code": "person_no_enabled_qualified_center",
            "person": "Gerardo Garcia",
        }],
        "elapsed_ms": 12.346,
    }
```

- [ ] **Step 2: Run the replay test and verify RED**

Run:

```bash
ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_replay_schedule_solver.py -q
```

Expected: FAIL because `serialize_replay` does not exist.

- [ ] **Step 3: Extend replay output**

For each requested day, include:

```python
{
    "day": day.isoformat(),
    "complete": suggestion.complete,
    "available_people": list(suggestion.available_people),
    "placed_people": list(suggestion.placed_people),
    "unplaced_people": list(suggestion.unused_people),
    "default_assignments": dict(suggestion.default_assignments),
    "issues": [issue.to_dict() for issue in suggestion.placement_issues],
    "elapsed_ms": round(elapsed_ms, 3),
}
```

Extract the shown dictionary into `serialize_replay(day: str, suggestion, elapsed_ms: float) -> dict` and call it from the existing replay function. Retain the script's no-write guarantee.

- [ ] **Step 4: Run the plant-sized performance regression**

Run:

```bash
ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_schedule_solver.py tests/test_schedule_solver_properties.py tests/test_rotation_suggestions.py -q
```

Expected: PASS, with the existing plant-scale focused test under one second.

- [ ] **Step 5: Run the complete focused feature suite**

Run:

```bash
ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_group_default_people_store.py tests/test_settings_context.py tests/test_settings_group_defaults.py tests/test_schedule_solver.py tests/test_schedule_solver_properties.py tests/test_rotation_suggestions.py tests/test_staffing_rotations.py tests/test_staffing_static.py tests/test_staffing_schedule_metadata.py tests/test_auto_schedule_capacity.py tests/test_rotation_training.py tests/test_replay_schedule_solver.py -q
```

Expected: PASS; DB-only cases may skip without `DATABASE_URL`.

- [ ] **Step 6: Run the full repository suite**

Run:

```bash
ZIRA_API_KEY=test .venv/bin/python -m pytest -q
```

Expected: PASS with only documented environment-dependent skips. If Chromium is the sole sandbox failure, rerun that existing browser test with the required browser permission; do not weaken it.

- [ ] **Step 7: Run static hygiene checks**

Run:

```bash
git diff --check
.venv/bin/python -m compileall -q src/zira_dashboard
```

Expected: both commands exit 0.

- [ ] **Step 8: Document permanent invariants**

Add to `CLAUDE.md`:

- goal-button success means every available non-reserve person is placed;
- infeasible rebuilds never save partial schedules;
- disabled Auto centers are never populated automatically;
- each person has at most one exact/group default target;
- group defaults rotate only among qualified enabled user-group members;
- exact/group defaults and manual locks are hard constraints;
- Reset to defaults uses the same server-side complete solver;
- level 0 remains automatic only through validated training blocks.

Add a concise `CHANGELOG.md` entry describing complete Auto placement, group defaults, even group rotation, and unchanged-schedule failures.

- [ ] **Step 9: Commit Task 7**

```bash
git add scripts/replay_schedule_solver.py tests/test_replay_schedule_solver.py CLAUDE.md CHANGELOG.md
git commit -m "docs: explain complete automatic scheduling"
```

- [ ] **Step 10: Review the final diff against acceptance criteria**

Run:

```bash
git diff --stat origin/main...HEAD
git log --oneline origin/main..HEAD
```

Confirm every spec acceptance criterion has a focused passing test and no unrelated user-owned file was staged or committed.
