# Global Minimum-Coverage Solver Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the greedy Recycled scheduler and headcount-only Auto guard with one deterministic, skill-aware solver that makes cross-skill swaps, permits level-1+ `never` overrides only when they improve minimum coverage, reports when training is required, saves the safest partial schedule, and leaves unresolved Auto centers enabled for manual resolution.

**Architecture:** Add a pure `schedule_solver` module that accepts normalized candidates and complete crew options, solves one-person centers through minimum-cost maximum matching, and evaluates coupled crew choices without ever emitting a partial crew. Adapt `rotation_suggestions` to build the immutable problem, freeze protected commitments, apply the global minimum result, and only then run optional mode-specific filling. Route all page, Auto-toggle, and rebuild feasibility through that same suggestion result, and expose structured issues to a non-blocking planner alert with a “Why?” disclosure.

**Tech Stack:** Python 3.12, frozen dataclasses, pure-Python min-cost flow/search, FastAPI, Jinja2, vanilla JavaScript/CSS, pytest, Ruff.

## Global Constraints

- Follow the approved design in `docs/superpowers/specs/2026-07-13-global-minimum-coverage-solver-design.md`.
- Follow `docs/superpowers/specs/2026-07-13-hide-automated-scheduler-notes-design.md`: keep assignment reasons internal and never render per-person automated notes, badges, icons, tooltips, or hidden text.
- Do not move, delete, or invalidate manual/default assignments or active training-block commitments.
- A protected assignment counts toward safe minimum coverage only when the person is active, present, non-reserve, and level 1+ in every required skill, or the active training block explicitly authorizes their level-0 placement.
- Treat skill levels 1, 2, and 3 as qualified for automatic minimum coverage.
- Level 0 is never an ordinary automatic candidate. If only level-0 people could eventually cover the skill, emit `training_required`; do not choose a trainee, trainer, start date, or duration.
- A `never` preference may be overridden only when the maximum number of staffed centers would otherwise be lower. It remains excluded from optional filling.
- All enabled Auto centers have equal priority. Canonical center order and lowercase person name are deterministic tie-breakers only.
- Never emit a generated partial crew for an unresolved center.
- Persist the best safe partial result and keep unresolved Auto centers enabled.
- Keep the solver pure: no database, Odoo, clock, settings, or environment reads.
- Preserve unrelated worktree changes, especially the concurrent skills automation work and existing untracked plans.
- Run focused tests after each task and the full suite before completion.

## File Map

| File | Action | Responsibility |
|---|---|---|
| `src/zira_dashboard/schedule_solver.py` | Create | Immutable solver domain, deterministic min-cost maximum matching, coupled-crew search, structured issues |
| `tests/test_schedule_solver.py` | Create | Unit tests for swaps, `never`, locks, training-required issues, complete crews, partial results, determinism |
| `src/zira_dashboard/rotation_suggestions.py` | Modify | Normalize plant data into solver inputs, apply minimum result, preserve optional mode behavior, return structured metadata |
| `tests/test_rotation_suggestions.py` | Modify | End-to-end pure-engine cases including Jose Luis/backfill and existing Trim/training rules |
| `src/zira_dashboard/routes/staffing.py` | Modify | Remove headcount gating, use one suggestion result, serialize issues into page context |
| `src/zira_dashboard/routes/rotations.py` | Modify | Accept advisory Auto selections, return coverage issues, persist partial rebuilds |
| `src/zira_dashboard/auto_schedule_capacity.py` | Modify | Retain disabled-capacity expansion advisory; remove the obsolete headcount feasibility decision |
| `tests/test_auto_schedule_capacity.py` | Modify | Retain expansion tests and remove tests for the replaced headcount gate |
| `tests/test_staffing_rotations.py` | Modify | Route/API contracts for advisory Auto changes and partial rebuild persistence |
| `src/zira_dashboard/templates/staffing.html` | Modify | Structured unresolved-coverage alert and server-rendered “Why?” disclosure; remove turn-off dialog |
| `src/zira_dashboard/static/staffing.js` | Modify | Render structured issues after Auto/rebuild requests without blocking the planner |
| `src/zira_dashboard/static/staffing.css` | Modify | Coverage issue/disclosure styles; remove obsolete capacity-dialog styles |
| `tests/test_staffing_static.py` | Modify | DOM/JavaScript contract for issue rendering and removed capacity dialog |
| `tests/test_schedule_solver_properties.py` | Create | Small exhaustive oracle, determinism, invariants, and plant-sized performance guard |
| `scripts/replay_schedule_solver.py` | Create | Read-only comparison of saved assignments against new solver output for chosen days |
| `CHANGELOG.md` | Modify | Planner-facing description of the corrected scheduling behavior |
| `CLAUDE.md` | Modify | Durable solver invariants and focused verification commands |

---

### Task 1: Build the pure one-person global matching core

**Files:**

- Create: `src/zira_dashboard/schedule_solver.py`
- Create: `tests/test_schedule_solver.py`

- [ ] **Step 1: Write failing tests for the motivating swap, maximum coverage, `never`, and deterministic output**

Add these first tests to `tests/test_schedule_solver.py`:

```python
from zira_dashboard.schedule_solver import (
    CandidateEdge,
    CenterRequirement,
    solve_minimum_coverage,
)


def edge(person, center, *, level=1, never=False, rank=0):
    return CandidateEdge(
        person=person,
        center=center,
        level=level,
        preference="never" if never else "regular",
        rank_cost=rank,
    )


def test_cross_trained_person_moves_to_scarce_center_and_backfills_old_role():
    result = solve_minimum_coverage((
        CenterRequirement(
            center="Repair 2",
            group="Repair",
            remaining_slots=1,
            candidates=(
                edge("Jose Luis", "Repair 2", level=3),
                edge("Domingo Recinos", "Repair 2", level=1),
            ),
        ),
        CenterRequirement(
            center="Dismantler 1",
            group="Dismantler",
            remaining_slots=1,
            candidates=(edge("Jose Luis", "Dismantler 1", level=1),),
        ),
    ))

    assert {(d.center, d.person) for d in result.decisions} == {
        ("Repair 2", "Domingo Recinos"),
        ("Dismantler 1", "Jose Luis"),
    }
    assert result.unresolved_centers == ()


def test_never_is_used_only_when_it_increases_staffed_center_count():
    required = (
        CenterRequirement(
            center="Repair 1",
            group="Repair",
            remaining_slots=1,
            candidates=(
                edge("Regular", "Repair 1", level=1),
                edge("Never", "Repair 1", level=3, never=True),
            ),
        ),
        CenterRequirement(
            center="Dismantler 1",
            group="Dismantler",
            remaining_slots=1,
            candidates=(edge("Never", "Dismantler 1", level=3, never=True),),
        ),
    )

    result = solve_minimum_coverage(required)

    assert {(d.center, d.person) for d in result.decisions} == {
        ("Repair 1", "Regular"),
        ("Dismantler 1", "Never"),
    }
    override = next(d for d in result.decisions if d.person == "Never")
    assert override.reason_code == "preference_override"


def test_equal_results_are_stable_across_input_order():
    first = CenterRequirement(
        center="Repair 1",
        group="Repair",
        remaining_slots=1,
        candidates=(edge("B", "Repair 1"), edge("A", "Repair 1")),
    )
    second = CenterRequirement(
        center="Dismantler 1",
        group="Dismantler",
        remaining_slots=1,
        candidates=(edge("B", "Dismantler 1"), edge("A", "Dismantler 1")),
    )

    forward = solve_minimum_coverage((first, second))
    reverse = solve_minimum_coverage((second, first))

    assert forward.decisions == reverse.decisions
    assert len(forward.staffed_centers) == 2
```

- [ ] **Step 2: Run the tests and confirm the module does not exist yet**

Run:

```bash
ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_schedule_solver.py -q
```

Expected: collection fails with `ModuleNotFoundError: No module named 'zira_dashboard.schedule_solver'`.

- [ ] **Step 3: Add immutable public solver types and stable result payloads**

Create `src/zira_dashboard/schedule_solver.py` with these public types before the algorithm:

```python
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Sequence

Preference = Literal["primary", "regular", "occasional", "never"]


@dataclass(frozen=True)
class CandidateRejection:
    person: str
    code: str
    detail: str

    def to_dict(self) -> dict[str, str]:
        return {"person": self.person, "code": self.code, "detail": self.detail}


@dataclass(frozen=True)
class CandidateEdge:
    person: str
    center: str
    level: int
    preference: Preference
    rank_cost: int = 0

    @property
    def override_cost(self) -> int:
        return int(self.preference == "never")


@dataclass(frozen=True)
class CrewOption:
    center: str
    members: tuple[CandidateEdge, ...]

    @property
    def people(self) -> tuple[str, ...]:
        return tuple(member.person for member in self.members)


@dataclass(frozen=True)
class CenterRequirement:
    center: str
    group: str
    remaining_slots: int
    protected_people: tuple[str, ...] = ()
    candidates: tuple[CandidateEdge, ...] = ()
    crew_options: tuple[CrewOption, ...] = ()
    level_zero_people: tuple[str, ...] = ()
    rejections: tuple[CandidateRejection, ...] = ()
    unresolved_code: str = "insufficient_qualified_headcount"
    unresolved_message: str = ""


@dataclass(frozen=True)
class AssignmentDecision:
    center: str
    person: str
    level: int
    preference: Preference
    reason_code: str
    reason: str
    rank_cost: int


@dataclass(frozen=True)
class CoverageIssue:
    center: str
    group: str
    code: str
    message: str
    rejections: tuple[CandidateRejection, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "center": self.center,
            "group": self.group,
            "code": self.code,
            "message": self.message,
            "rejections": [item.to_dict() for item in self.rejections],
        }


@dataclass(frozen=True)
class CoverageResult:
    decisions: tuple[AssignmentDecision, ...]
    staffed_centers: tuple[str, ...]
    unresolved_centers: tuple[str, ...]
    issues: tuple[CoverageIssue, ...]

    @property
    def assigned_people(self) -> frozenset[str]:
        return frozenset(item.person for item in self.decisions)
```

- [ ] **Step 4: Implement deterministic min-cost maximum matching**

Below the public types, add a small residual graph and the one-person matcher. Maximum flow establishes the primary objective; the scalar cost only ranks solutions of the same cardinality. `never` receives a weight larger than every possible sum of ordinary rank costs, so it cannot win an equal-coverage tie.

```python
@dataclass
class _Arc:
    to: int
    reverse: int
    capacity: int
    cost: int


def _add_arc(graph: list[list[_Arc]], start: int, end: int, capacity: int, cost: int) -> int:
    forward_index = len(graph[start])
    graph[start].append(_Arc(end, len(graph[end]), capacity, cost))
    graph[end].append(_Arc(start, forward_index, 0, -cost))
    return forward_index


def _decision(edge: CandidateEdge) -> AssignmentDecision:
    override = edge.preference == "never"
    return AssignmentDecision(
        center=edge.center,
        person=edge.person,
        level=edge.level,
        preference=edge.preference,
        reason_code="preference_override" if override else "minimum_coverage",
        reason=(
            "Assigned despite Never to meet minimum coverage."
            if override
            else "Assigned to meet minimum coverage."
        ),
        rank_cost=edge.rank_cost,
    )


def _match_single_requirements(
    requirements: Sequence[CenterRequirement],
    unavailable_people: frozenset[str] = frozenset(),
) -> tuple[AssignmentDecision, ...]:
    centers = tuple(sorted(requirements, key=lambda item: item.center.lower()))
    edges = tuple(sorted(
        (
            edge
            for requirement in centers
            for edge in requirement.candidates
            if edge.person not in unavailable_people
        ),
        key=lambda edge: (edge.person.lower(), edge.center.lower(), edge.rank_cost),
    ))
    people = tuple(sorted({edge.person for edge in edges}, key=str.lower))
    source = 0
    person_node = {name: index + 1 for index, name in enumerate(people)}
    center_offset = 1 + len(people)
    center_node = {item.center: center_offset + index for index, item in enumerate(centers)}
    sink = center_offset + len(centers)
    graph: list[list[_Arc]] = [[] for _ in range(sink + 1)]

    for person in people:
        _add_arc(graph, source, person_node[person], 1, 0)
    for requirement in centers:
        _add_arc(graph, center_node[requirement.center], sink, 1, 0)

    ordinary_bound = sum(max(0, edge.rank_cost) for edge in edges) + len(edges) + 1
    chosen_arcs: list[tuple[int, int, CandidateEdge]] = []
    for edge in edges:
        cost = edge.override_cost * ordinary_bound + max(0, edge.rank_cost)
        start = person_node[edge.person]
        arc_index = _add_arc(graph, start, center_node[edge.center], 1, cost)
        chosen_arcs.append((start, arc_index, edge))

    node_count = len(graph)
    while True:
        distance = [10**18] * node_count
        previous: list[tuple[int, int] | None] = [None] * node_count
        distance[source] = 0
        for _ in range(node_count - 1):
            changed = False
            for node, arcs in enumerate(graph):
                if distance[node] == 10**18:
                    continue
                for arc_index, arc in enumerate(arcs):
                    candidate = distance[node] + arc.cost
                    if arc.capacity and candidate < distance[arc.to]:
                        distance[arc.to] = candidate
                        previous[arc.to] = (node, arc_index)
                        changed = True
            if not changed:
                break
        if previous[sink] is None:
            break
        node = sink
        while node != source:
            prior_node, arc_index = previous[node]  # type: ignore[misc]
            arc = graph[prior_node][arc_index]
            arc.capacity -= 1
            graph[node][arc.reverse].capacity += 1
            node = prior_node

    selected = [
        _decision(edge)
        for start, arc_index, edge in chosen_arcs
        if graph[start][arc_index].capacity == 0
    ]
    return tuple(sorted(selected, key=lambda item: (item.center.lower(), item.person.lower())))
```

- [ ] **Step 5: Add the single-center result builder and public entry point**

```python
def _issue(
    requirement: CenterRequirement,
    decisions: Sequence[AssignmentDecision],
) -> CoverageIssue:
    if requirement.level_zero_people and not requirement.candidates and not requirement.crew_options:
        code = "training_required"
        message = (
            f"{requirement.center} could not be staffed. "
            f"Training is required for {requirement.group}."
        )
    else:
        code = requirement.unresolved_code
        message = requirement.unresolved_message or (
            f"{requirement.center} could not be staffed to its minimum."
        )
    assigned_center = {item.person: item.center for item in decisions}
    candidate_people = {
        edge.person
        for edge in requirement.candidates
    } | {
        edge.person
        for option in requirement.crew_options
        for edge in option.members
    }
    rejections = list(requirement.rejections)
    known = {(item.person, item.code) for item in rejections}
    for person in sorted(candidate_people, key=str.lower):
        other_center = assigned_center.get(person)
        if other_center and other_center != requirement.center:
            item = CandidateRejection(
                person=person,
                code="needed_elsewhere",
                detail=f"Used at {other_center} by the maximum-coverage solution.",
            )
        else:
            item = CandidateRejection(
                person=person,
                code="global_tie_break",
                detail="Available, but assigning them here would not improve total coverage.",
            )
        if (item.person, item.code) not in known:
            rejections.append(item)
    return CoverageIssue(
        center=requirement.center,
        group=requirement.group,
        code=code,
        message=message,
        rejections=tuple(rejections),
    )


def _assemble_result(
    requirements: Sequence[CenterRequirement],
    decisions: Sequence[AssignmentDecision],
) -> CoverageResult:
    assigned_centers = {item.center for item in decisions}
    staffed = {
        requirement.center
        for requirement in requirements
        if requirement.remaining_slots == 0 or requirement.center in assigned_centers
    }
    unresolved = tuple(sorted(
        (item for item in requirements if item.center not in staffed),
        key=lambda item: item.center.lower(),
    ))
    return CoverageResult(
        decisions=tuple(sorted(
            decisions,
            key=lambda item: (item.center.lower(), item.person.lower()),
        )),
        staffed_centers=tuple(sorted(staffed, key=str.lower)),
        unresolved_centers=tuple(item.center for item in unresolved),
        issues=tuple(_issue(item, decisions) for item in unresolved),
    )


def solve_minimum_coverage(
    requirements: Sequence[CenterRequirement],
) -> CoverageResult:
    normalized = tuple(sorted(requirements, key=lambda item: item.center.lower()))
    singles = tuple(item for item in normalized if item.remaining_slots == 1)
    if any(item.remaining_slots > 1 for item in normalized):
        raise ValueError("multi-person requirements need complete crew options")
    return _assemble_result(normalized, _match_single_requirements(singles))
```

- [ ] **Step 6: Run focused tests and commit the matching core**

Run:

```bash
ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_schedule_solver.py -q
.venv/bin/python -m ruff check src/zira_dashboard/schedule_solver.py tests/test_schedule_solver.py
```

Expected: all new tests pass and Ruff reports no errors.

Commit:

```bash
git add src/zira_dashboard/schedule_solver.py tests/test_schedule_solver.py
git commit -m "feat: add global minimum coverage matcher"
```

---

### Task 2: Support protected commitments, complete crews, and safe partial results

**Files:**

- Modify: `src/zira_dashboard/schedule_solver.py`
- Modify: `tests/test_schedule_solver.py`

- [ ] **Step 1: Add failing tests for complete crews, protected coverage, partial results, and training-required issues**

Append:

```python
from zira_dashboard.schedule_solver import CandidateRejection, CrewOption


def test_multi_person_center_is_either_complete_or_empty():
    a = edge("A", "Hand Build #1", level=2)
    b = edge("B", "Hand Build #1", level=2)
    only_a_elsewhere = edge("A", "Repair 1", level=2)
    result = solve_minimum_coverage((
        CenterRequirement(
            center="Hand Build #1",
            group="Hand Build",
            remaining_slots=2,
            crew_options=(CrewOption("Hand Build #1", (a, b)),),
        ),
        CenterRequirement(
            center="Repair 1",
            group="Repair",
            remaining_slots=1,
            candidates=(only_a_elsewhere,),
        ),
    ))

    assert result.staffed_centers == ("Hand Build #1",)
    assert {(item.center, item.person) for item in result.decisions} == {
        ("Hand Build #1", "A"),
        ("Hand Build #1", "B"),
    }
    assert result.unresolved_centers == ("Repair 1",)


def test_protected_safe_minimum_needs_no_generated_assignment():
    result = solve_minimum_coverage((CenterRequirement(
        center="Repair 1",
        group="Repair",
        remaining_slots=0,
        protected_people=("Manual",),
    ),))

    assert result.staffed_centers == ("Repair 1",)
    assert result.decisions == ()


def test_level_zero_only_reports_training_required_without_assigning_anyone():
    result = solve_minimum_coverage((CenterRequirement(
        center="Dismantler 1",
        group="Dismantler",
        remaining_slots=1,
        level_zero_people=("Potential Trainee",),
        rejections=(CandidateRejection(
            person="Potential Trainee",
            code="level_zero",
            detail="Skill level is 0; an active training block is required.",
        ),),
    ),))

    assert result.decisions == ()
    assert result.issues[0].code == "training_required"
    assert result.issues[0].message == (
        "Dismantler 1 could not be staffed. Training is required for Dismantler."
    )


def test_best_safe_partial_leaves_unresolved_centers_enabled_in_result():
    result = solve_minimum_coverage((
        CenterRequirement(
            center="Repair 1",
            group="Repair",
            remaining_slots=1,
            candidates=(edge("Only Person", "Repair 1"),),
        ),
        CenterRequirement(
            center="Dismantler 1",
            group="Dismantler",
            remaining_slots=1,
            candidates=(edge("Only Person", "Dismantler 1"),),
        ),
    ))

    assert len(result.staffed_centers) == 1
    assert len(result.unresolved_centers) == 1
    assert len(result.decisions) == 1
    assert result.issues[0].code == "insufficient_qualified_headcount"
```

- [ ] **Step 2: Confirm the multi-person test fails at the current guard**

Run:

```bash
ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_schedule_solver.py -q
```

Expected: `test_multi_person_center_is_either_complete_or_empty` fails with the current `ValueError`.

- [ ] **Step 3: Implement coupled-crew search around the matching fast path**

Replace `solve_minimum_coverage` with the following search helpers and entry point:

```python
def _result_is_better(candidate: CoverageResult, current: CoverageResult | None) -> bool:
    if current is None:
        return True
    if len(candidate.staffed_centers) != len(current.staffed_centers):
        return len(candidate.staffed_centers) > len(current.staffed_centers)
    candidate_overrides = sum(item.preference == "never" for item in candidate.decisions)
    current_overrides = sum(item.preference == "never" for item in current.decisions)
    if candidate_overrides != current_overrides:
        return candidate_overrides < current_overrides
    candidate_cost = sum(item.rank_cost for item in candidate.decisions)
    current_cost = sum(item.rank_cost for item in current.decisions)
    if candidate_cost != current_cost:
        return candidate_cost < current_cost
    candidate_key = tuple((item.center.lower(), item.person.lower()) for item in candidate.decisions)
    current_key = tuple((item.center.lower(), item.person.lower()) for item in current.decisions)
    return candidate_key < current_key


def _crew_decisions(option: CrewOption) -> tuple[AssignmentDecision, ...]:
    return tuple(_decision(member) for member in option.members)


def solve_minimum_coverage(
    requirements: Sequence[CenterRequirement],
) -> CoverageResult:
    normalized = tuple(sorted(requirements, key=lambda item: item.center.lower()))
    singles = tuple(item for item in normalized if item.remaining_slots == 1)
    coupled = tuple(sorted(
        (item for item in normalized if item.remaining_slots > 1),
        key=lambda item: (len(item.crew_options), item.center.lower()),
    ))
    if any(
        len(option.members) != requirement.remaining_slots
        or option.center != requirement.center
        or len(set(option.people)) != len(option.people)
        for requirement in coupled
        for option in requirement.crew_options
    ):
        raise ValueError("crew options must be complete, unique, and center-scoped")

    best: CoverageResult | None = None
    seen_prefix: dict[
        tuple[int, frozenset[str]],
        tuple[int, int, tuple[tuple[str, str], ...]],
    ] = {}

    def visit(
        index: int,
        used_people: frozenset[str],
        decisions: tuple[AssignmentDecision, ...],
    ) -> None:
        nonlocal best
        prefix_score = (
            sum(item.preference == "never" for item in decisions),
            sum(item.rank_cost for item in decisions),
            tuple((item.center.lower(), item.person.lower()) for item in decisions),
        )
        state = (index, used_people)
        previous_prefix = seen_prefix.get(state)
        if previous_prefix is not None and previous_prefix <= prefix_score:
            return
        seen_prefix[state] = prefix_score
        staffed_coupled = len({item.center for item in decisions})
        optimistic = (
            sum(item.remaining_slots == 0 for item in normalized)
            + staffed_coupled
            + (len(coupled) - index)
            + len(singles)
        )
        if best is not None and optimistic < len(best.staffed_centers):
            return
        if index == len(coupled):
            matched = _match_single_requirements(singles, used_people)
            candidate = _assemble_result(normalized, decisions + matched)
            if _result_is_better(candidate, best):
                best = candidate
            return

        requirement = coupled[index]
        ordered_options = sorted(
            requirement.crew_options,
            key=lambda option: (
                sum(member.preference == "never" for member in option.members),
                sum(member.rank_cost for member in option.members),
                tuple(name.lower() for name in option.people),
            ),
        )
        for option in ordered_options:
            option_people = frozenset(option.people)
            if option_people & used_people:
                continue
            visit(
                index + 1,
                used_people | option_people,
                decisions + _crew_decisions(option),
            )
        visit(index + 1, used_people, decisions)

    visit(0, frozenset(), ())
    return best if best is not None else _assemble_result(normalized, ())
```

The engine adapter will bound crew options to valid, complete combinations. Do not add a “best partial crew” branch.

- [ ] **Step 4: Verify structured payloads and crew atomicity**

Run:

```bash
ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_schedule_solver.py -q
.venv/bin/python -m ruff check src/zira_dashboard/schedule_solver.py tests/test_schedule_solver.py
```

Expected: all solver tests pass.

Commit:

```bash
git add src/zira_dashboard/schedule_solver.py tests/test_schedule_solver.py
git commit -m "feat: solve complete crews and partial coverage"
```

---

### Task 3: Make `rotation_suggestions` use the global solver

**Files:**

- Modify: `src/zira_dashboard/rotation_suggestions.py`
- Modify: `tests/test_rotation_suggestions.py`

- [ ] **Step 1: Add engine-level failing tests for Jose Luis, `never`, training-required, locks, and partial crews**

Use the existing `staffing.Person` model and append these tests:

```python
def test_global_minimum_moves_cross_trained_jose_and_backfills_repair():
    out = suggest_recycled_assignments(
        day=date(2026, 7, 14),
        mode="normal",
        roster=[
            staffing.Person(
                name="Jose Luis",
                skills={"Repair": 3, "Dismantle": 1},
            ),
            staffing.Person(
                name="Domingo Recinos",
                skills={"Repair": 1, "Dismantle": 0},
            ),
        ],
        group_locations={
            "Repair": ("Repair 2",),
            "Dismantler": ("Dismantler 1",),
        },
        group_required_skills={
            "Repair": ("Repair",),
            "Dismantler": ("Dismantle",),
        },
        center_minimums={"Repair 2": 1, "Dismantler 1": 1},
        center_capacities={"Repair 2": 1, "Dismantler 1": 1},
        runnable_centers={"Repair 2", "Dismantler 1"},
    )

    assert out.assignments["Dismantler 1"] == ["Jose Luis"]
    assert out.assignments["Repair 2"] == ["Domingo Recinos"]
    assert out.issues == ()


def test_level_zero_only_alerts_that_training_is_required():
    out = suggest_recycled_assignments(
        day=date(2026, 7, 14),
        mode="normal",
        roster=[staffing.Person(
            name="Potential Trainee",
            skills={"Dismantle": 0},
        )],
        group_locations={"Dismantler": ("Dismantler 1",)},
        group_required_skills={"Dismantler": ("Dismantle",)},
        center_minimums={"Dismantler 1": 1},
        center_capacities={"Dismantler 1": 1},
        runnable_centers={"Dismantler 1"},
    )

    assert out.assignments.get("Dismantler 1", []) == []
    assert out.issues[0].code == "training_required"
    assert any("Training is required for Dismantler" in warning for warning in out.warnings)


def test_unavoidable_never_override_has_structured_reason():
    out = suggest_recycled_assignments(
        day=date(2026, 7, 14),
        mode="normal",
        roster=[staffing.Person(name="Only Qualified", skills={"Repair": 1})],
        preferences={"Only Qualified": {"Repair": "never"}},
        group_locations={"Repair": ("Repair 1",)},
        center_minimums={"Repair 1": 1},
        center_capacities={"Repair 1": 1},
        runnable_centers={"Repair 1"},
    )

    assert out.assignments["Repair 1"] == ["Only Qualified"]
    assert out.reason_codes["Repair 1"]["Only Qualified"] == "preference_override"
    assert "despite Never" in out.reasons["Repair 1"]["Only Qualified"]


def test_unresolved_multi_person_center_has_no_generated_partial_crew():
    out = suggest_recycled_assignments(
        day=date(2026, 7, 14),
        mode="normal",
        roster=[staffing.Person(name="One Builder", skills={"Hand Build": 2})],
        group_locations={"Hand Build": ("Hand Build #1",)},
        group_required_skills={"Hand Build": ("Hand Build",)},
        center_minimums={"Hand Build #1": 2},
        center_capacities={"Hand Build #1": 2},
        runnable_centers={"Hand Build #1"},
    )

    assert out.assignments.get("Hand Build #1", []) == []
    assert out.unresolved_centers == ("Hand Build #1",)
```

- [ ] **Step 2: Extend `RecycledSuggestion` without breaking hand-built fixtures**

Import the solver module and add defaulted structured fields after `group_locations`:

```python
from . import schedule_solver, staffing


@dataclass(frozen=True)
class RecycledSuggestion:
    assignments: dict[str, list[str]]
    sources: dict[str, dict[str, str]]
    reasons: dict[str, dict[str, str]]
    warnings: Sequence[str]
    group_locations: dict[str, tuple[str, ...]] = field(default_factory=dict)
    reason_codes: dict[str, dict[str, str]] = field(default_factory=dict)
    staffed_centers: tuple[str, ...] = ()
    unresolved_centers: tuple[str, ...] = ()
    issues: tuple[schedule_solver.CoverageIssue, ...] = ()
    unused_people: tuple[str, ...] = ()
```

Keep `assigned_people` and `people_for_group` unchanged.

- [ ] **Step 3: Separate minimum eligibility from optional eligibility**

Retain the existing optional `_eligible(...)` behavior that rejects `never`. Add these helpers next to it:

```python
def _minimum_eligible(
    person: staffing.Person,
    group: str,
    preferences: dict[str, dict[str, str]],
    group_required_skills: Mapping[str, tuple[str, ...]],
) -> bool:
    return (
        person.active
        and not person.reserve
        and _group_level(person, group, group_required_skills) >= 1
    )


def _minimum_rank_cost(
    person: staffing.Person,
    group: str,
    center: str,
    mode: str,
    preferences: dict[str, dict[str, str]],
    history: RecycledHistory,
    group_required_skills: Mapping[str, tuple[str, ...]],
    center_order: Mapping[str, int],
) -> int:
    level = _group_level(person, group, group_required_skills)
    mode_key = _candidate_rank_key(
        mode,
        person,
        group,
        preferences,
        history,
        group_required_skills,
    )
    stable_center = center_order.get(center, 1_000_000)
    stable_name = sum(ord(char) for char in person.name.lower())
    mode_cost = 10_000 + int(mode_key[0])
    return (
        (3 - level) * 1_000_000_000_000
        + mode_cost * 1_000_000
        + stable_center * 10_000
        + stable_name
    )
```

The maximum-matching objective remains primary, and `CandidateEdge.override_cost` remains the next comparison. This rank is considered only among equally complete, equally low-override solutions.

- [ ] **Step 4: Build normalized center requirements after protected commitments**

Extract the Stage 2 construction into a new pure helper inside `rotation_suggestions.py`:

```python
def _coverage_requirements(
    *,
    mode: str,
    roster: Sequence[staffing.Person],
    groups: Mapping[str, Sequence[str]],
    required_skills: Mapping[str, tuple[str, ...]],
    preferences: dict[str, dict[str, str]],
    history: RecycledHistory,
    assignments: Mapping[str, Sequence[str]],
    sources: Mapping[str, Mapping[str, str]],
    assigned: Collection[str],
    allowed_centers: Collection[str],
    minimum_for,
    capacity_for,
    block_trainees_by_center: Mapping[str, Collection[str]],
) -> tuple[schedule_solver.CenterRequirement, ...]:
    by_name = {person.name: person for person in roster}
    center_order = {
        center: index
        for index, center in enumerate(center for values in groups.values() for center in values)
    }
    requirements = []
    for group, centers in groups.items():
        for center in centers:
            if center not in allowed_centers:
                continue
            existing = tuple(assignments.get(center, ()))
            trainees = set(block_trainees_by_center.get(center, ()))
            safe_existing = tuple(
                name
                for name in existing
                if name in trainees or (
                    (person := by_name.get(name)) is not None
                    and _minimum_eligible(person, group, preferences, required_skills)
                )
            )
            protected_crew_is_safe = _coverage_crew_is_safe(
                group=group,
                existing=safe_existing,
                new_people=(),
                by_name=by_name,
                required_skills=required_skills,
                trainees=trainees,
            )
            if group == TRIM_SAW_SKILL and len(existing) >= 2 and not protected_crew_is_safe:
                safe_existing = ()
            minimum = minimum_for(center)
            needs_green_partner = bool(trainees) and not any(
                name not in trainees
                and (person := by_name.get(name)) is not None
                and _group_level(person, group, required_skills) == 3
                for name in safe_existing
            )
            remaining = max(0, minimum - len(safe_existing), int(needs_green_partner))
            open_slots = max(0, capacity_for(center) - len(existing))
            available_people = [
                person
                for person in roster
                if person.name not in assigned
                and _minimum_eligible(person, group, preferences, required_skills)
            ]
            if needs_green_partner:
                available_people = [
                    person for person in available_people
                    if _group_level(person, group, required_skills) == 3
                ]
            edges = tuple(
                schedule_solver.CandidateEdge(
                    person=person.name,
                    center=center,
                    level=_group_level(person, group, required_skills),
                    preference=_preference_for(preferences, person.name, group),
                    rank_cost=_minimum_rank_cost(
                        person,
                        group,
                        center,
                        mode,
                        preferences,
                        history,
                        required_skills,
                        center_order,
                    ),
                )
                for person in sorted(available_people, key=lambda item: item.name.lower())
            )
            single_candidates = tuple(
                edge
                for edge in edges
                if _coverage_crew_is_safe(
                    group=group,
                    existing=existing,
                    new_people=(edge.person,),
                    by_name=by_name,
                    required_skills=required_skills,
                    trainees=trainees,
                )
            ) if remaining == 1 else ()
            crew_options = tuple(
                schedule_solver.CrewOption(center=center, members=tuple(crew))
                for crew in combinations(edges, remaining)
                if remaining > 1
                and remaining <= open_slots
                and _coverage_crew_is_safe(
                    group=group,
                    existing=existing,
                    new_people=tuple(member.person for member in crew),
                    by_name=by_name,
                    required_skills=required_skills,
                    trainees=trainees,
                )
            )
            level_zero_people = tuple(sorted(
                person.name
                for person in roster
                if person.active
                and not person.reserve
                and _group_level(person, group, required_skills) == 0
            ))
            rejections = _coverage_rejections(
                group=group,
                roster=roster,
                assigned=assigned,
                required_skills=required_skills,
            )
            requirements.append(schedule_solver.CenterRequirement(
                center=center,
                group=group,
                remaining_slots=remaining,
                protected_people=safe_existing,
                candidates=single_candidates if open_slots >= 1 else (),
                crew_options=crew_options,
                level_zero_people=level_zero_people,
                rejections=rejections,
                unresolved_code=(
                    "no_safe_pair"
                    if edges and not (single_candidates or crew_options)
                    and (group == TRIM_SAW_SKILL or needs_green_partner)
                    else "insufficient_qualified_headcount"
                ),
                unresolved_message=(
                    f"{center} could not be staffed to its minimum of {minimum} operators."
                ),
            ))
    return tuple(requirements)
```

Add the two helpers used above:

```python
def _coverage_crew_is_safe(
    *,
    group: str,
    existing: Sequence[str],
    new_people: Sequence[str],
    by_name: Mapping[str, staffing.Person],
    required_skills: Mapping[str, tuple[str, ...]],
    trainees: Collection[str],
) -> bool:
    final_people = tuple(existing) + tuple(new_people)
    if trainees and not any(
        name not in trainees
        and _group_level(by_name.get(name), group, dict(required_skills)) == 3
        for name in final_people
    ):
        return False
    if group != TRIM_SAW_SKILL:
        return True
    levels = [
        _group_level(by_name.get(name), group, dict(required_skills))
        for name in final_people
    ]
    return len(levels) == 2 and _valid_trim_saw_pair(levels[0], levels[1])


def _coverage_rejections(
    *,
    group: str,
    roster: Sequence[staffing.Person],
    assigned: Collection[str],
    required_skills: Mapping[str, tuple[str, ...]],
) -> tuple[schedule_solver.CandidateRejection, ...]:
    rejected = []
    for person in sorted(roster, key=lambda item: item.name.lower()):
        level = _group_level(person, group, dict(required_skills))
        if not person.active:
            code, detail = "inactive", "Person is inactive."
        elif person.reserve:
            code, detail = "reserve", "Person is in Reserves."
        elif person.name in assigned:
            code, detail = "already_assigned", "Person is already committed elsewhere."
        elif level == 0:
            code, detail = (
                "level_zero",
                "Skill level is 0; an active training block is required.",
            )
        else:
            continue
        rejected.append(schedule_solver.CandidateRejection(
            person=person.name,
            code=code,
            detail=detail,
        ))
    return tuple(rejected)
```

For multi-skill groups, `_group_level(...) == 0` covers any missing required skill. Set `code="missing_skill"` when a required skill key is absent from `person.skills`; use `level_zero` when all required keys exist and their effective minimum is zero. This distinction powers “Why?” but does not change eligibility.

- [ ] **Step 5: Replace the greedy minimum pass and retain optional mode filling**

Inside `suggest_recycled_assignments(...)`, keep pass-through assignments, locks, and validated training-block placement first. Delete the current Phase 3 greedy green reservation and Phases 4–5 greedy minimum pass. The training partner restriction must be part of the global candidate/crew options so a green is not consumed before other centers are considered. Replace those sections with:

```python
    resolved_preferences = preferences or {}
    block_trainees_by_center = {
        center: {
            name for name in assignments.get(center, ())
            if name in protected_block_people
        }
        for _group, center in block_centers
    }
    requirements = _coverage_requirements(
        mode=mode,
        roster=roster,
        groups=groups,
        required_skills=resolved_group_required_skills,
        preferences=resolved_preferences,
        history=resolved_history,
        assignments=assignments,
        sources=sources,
        assigned=assigned,
        allowed_centers=allowed_centers,
        minimum_for=_effective_minimum,
        capacity_for=_effective_capacity,
        block_trainees_by_center=block_trainees_by_center,
    )
    coverage = schedule_solver.solve_minimum_coverage(requirements)
    reason_codes: dict[str, dict[str, str]] = {}
    for decision in coverage.decisions:
        _place(decision.center, decision.person, GENERATED_SOURCE, decision.reason)
        reason_codes.setdefault(decision.center, {})[decision.person] = decision.reason_code

    warnings.extend(
        issue.message for issue in coverage.issues if issue.message not in warnings
    )
```

Then run the existing training-mode development fill plus optional capacity fill only for centers in `coverage.staffed_centers`, only after all coverage decisions are placed, and through the existing optional `_eligible(...)` so `never` is excluded. Delete the old post-hoc under-minimum generated-removal loop; the new solver never creates those partial crews.

Return:

```python
    generated_people = {
        name
        for center_sources in sources.values()
        for name, source in center_sources.items()
        if source == GENERATED_SOURCE
    }
    eligible_people = {
        person.name
        for person in roster
        if person.active and not person.reserve
    }
    return RecycledSuggestion(
        assignments=assignments,
        sources=sources,
        reasons=reasons,
        warnings=tuple(warnings),
        group_locations={group: tuple(centers) for group, centers in groups.items()},
        reason_codes=reason_codes,
        staffed_centers=coverage.staffed_centers,
        unresolved_centers=coverage.unresolved_centers,
        issues=coverage.issues,
        unused_people=tuple(sorted(eligible_people - generated_people - assigned, key=str.lower)),
    )
```

- [ ] **Step 6: Run the engine regression suite and fix only intentional expectation changes**

Run:

```bash
ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_schedule_solver.py tests/test_rotation_suggestions.py tests/test_rotation_training.py -q
.venv/bin/python -m ruff check src/zira_dashboard/schedule_solver.py src/zira_dashboard/rotation_suggestions.py tests/test_schedule_solver.py tests/test_rotation_suggestions.py
```

Expected: all tests pass. Update old tests that expected a greedy under-filled center only when the new maximum-coverage outcome is demonstrably safer; retain every existing Trim Saw and training-block safety assertion.

Commit:

```bash
git add src/zira_dashboard/schedule_solver.py src/zira_dashboard/rotation_suggestions.py tests/test_rotation_suggestions.py
git commit -m "fix: optimize scheduler minimum coverage globally"
```

---

### Task 4: Replace the Auto headcount gate with the shared skill-aware advisory

**Files:**

- Modify: `src/zira_dashboard/routes/staffing.py`
- Modify: `src/zira_dashboard/routes/rotations.py`
- Modify: `src/zira_dashboard/auto_schedule_capacity.py`
- Modify: `tests/test_auto_schedule_capacity.py`
- Modify: `tests/test_staffing_rotations.py`

- [ ] **Step 1: Add failing API tests for accepting an infeasible Auto selection and returning structured issues**

Replace the tests that expect HTTP 409/headcount replacement choices with:

```python
def test_auto_center_selection_is_saved_and_reports_unresolved_coverage(monkeypatch, client):
    saved = []
    issue = schedule_solver.CoverageIssue(
        center="Dismantler 1",
        group="Dismantler",
        code="training_required",
        message="Dismantler 1 could not be staffed. Training is required for Dismantler.",
    )
    monkeypatch.setattr(
        staffing_route,
        "_recycled_suggestion_for_day",
        lambda *args, **kwargs: rotation_suggestions.RecycledSuggestion(
            assignments={"Repair 1": ["Qualified"]},
            sources={"Repair 1": {"Qualified": "generated"}},
            reasons={},
            warnings=(issue.message,),
            issues=(issue,),
            staffed_centers=("Repair 1",),
            unresolved_centers=("Dismantler 1",),
        ),
    )
    monkeypatch.setattr(
        staffing_route,
        "_save_enabled_auto_work_centers",
        lambda centers: saved.append(tuple(centers)) or list(centers),
    )

    response = client.post("/api/rotations/auto-work-centers", json={
        "day": "2026-07-14",
        "work_centers": ["Repair 1", "Dismantler 1"],
        "turn_off": [],
    })

    assert response.status_code == 200
    assert saved[-1] == ("Repair 1", "Dismantler 1")
    assert response.json()["coverage"] == {
        "staffed_centers": ["Repair 1"],
        "unresolved_centers": ["Dismantler 1"],
        "issues": [issue.to_dict()],
    }
```

Also add a rebuild test proving that assignments from a partial suggestion are persisted and the endpoint returns 200 plus issues instead of aborting.

- [ ] **Step 2: Remove the obsolete headcount decision while retaining expansion advice**

In `src/zira_dashboard/auto_schedule_capacity.py`, delete `AutoCapacity` and `analyze_auto_capacity`. Keep `AutoExpansion` and `analyze_auto_expansion` unchanged. In `tests/test_auto_schedule_capacity.py`, remove the three headcount-capacity tests and retain the expansion tests.

In `routes/staffing.py`:

- remove imports of `AutoCapacity` and `analyze_auto_capacity`;
- delete `_auto_capacity_for_day(...)` and `_capacity_inputs_with_block_effects(...)`;
- stop deriving `runnable_centers` from a headcount result;
- pass the full enabled set to `suggest_recycled_assignments(..., runnable_centers=enabled)`;
- remove “Turn off at least N work centers” warning generation.

The central call becomes:

```python
        suggestion = rotation_suggestions.suggest_recycled_assignments(
            day=d,
            mode=mode,
            roster=available,
            preferences=preferences,
            base_assignments=base_assignments,
            group_locations=group_locations,
            group_required_skills=group_required_skills,
            history=history,
            locked_assignments=scoped_locks,
            block_effects=block_effects,
            training_cap=_RECYCLED_TRAINING_CAP,
            center_minimums=center_minimums,
            center_capacities=center_capacities,
            runnable_centers=enabled,
        )
```

- [ ] **Step 3: Make disabled-capacity expansion advice defer to real coverage issues**

Change `_append_auto_expansion_warning(...)` to remove its `capacity` parameter and return immediately whenever minimum coverage is unresolved:

```python
def _append_auto_expansion_warning(
    *, suggestion, day, mode, available_roster, preferences, history,
    block_effects, enabled_work_centers, base_assignments, assignment_sources,
):
    if suggestion.issues:
        return suggestion
    # Retain the existing disabled-center open-slot calculation and
    # counterfactual solver proof below this guard.
```

The counterfactual proof must call the same global engine with all proof centers enabled. Only emit “Turn on N more Auto work centers” if every otherwise-unused qualified person becomes safely assigned and the proof has no `issues`.

- [ ] **Step 4: Serialize the same structured coverage result on both page and API paths**

Add one shared serializer in `routes/rotations.py`:

```python
def _coverage_payload(suggestion) -> dict[str, object]:
    return {
        "staffed_centers": list(suggestion.staffed_centers),
        "unresolved_centers": list(suggestion.unresolved_centers),
        "issues": [issue.to_dict() for issue in suggestion.issues],
    }
```

Use it in Auto-center and rebuild responses:

```python
return JSONResponse({
    "ok": True,
    "enabled_work_centers": enabled,
    "warnings": list(suggestion.warnings),
    "coverage": _coverage_payload(suggestion),
})
```

Replace the Auto endpoint's `_work()` body with the shared-engine flow:

```python
    def _work():
        proposed = staffing_route._ordered_work_center_names(names)
        turn_off_names = set(staffing_route._ordered_work_center_names(turn_off))
        enabled = [name for name in proposed if name not in turn_off_names]
        roster = staffing.load_roster()
        sched = staffing.load_schedule(d)
        try:
            time_off = scheduler_time_off.time_off_entries_for_day(d)
            locks = staffing_route._protected_locks(
                sched.assignment_sources,
                sched.assignments,
                allowed_centers=enabled,
                strict_default_reads=True,
            )
        except Exception:
            return _error("Could not verify daily staffing coverage.", 503)
        suggestion = staffing_route._recycled_suggestion_for_day(
            d,
            roster,
            sched.rotation_mode or "normal",
            base_assignments={wc: list(values) for wc, values in sched.assignments.items()},
            locked_assignments=locks,
            time_off_entries=time_off,
            enabled_work_centers=enabled,
            assignment_sources=sched.assignment_sources,
        )
        if suggestion is None:
            return _error("Could not verify daily staffing coverage.", 503)
        enabled = staffing_route._save_enabled_auto_work_centers(enabled)
        _http_cache.invalidate_today_cache()
        _http_cache.invalidate_stable_cache()
        return JSONResponse({
            "ok": True,
            "enabled_work_centers": enabled,
            "warnings": list(suggestion.warnings),
            "coverage": _coverage_payload(suggestion),
        })
```

Do not reject an Auto selection solely because `unresolved_centers` is non-empty. The endpoint computes the advisory, saves the exact requested selection after required reads succeed, and returns the issue for manual resolution. Preserve existing 400/503 behavior for malformed input or failed required data reads.

In `_recycled_context_for_day(...)`, initialize and populate:

```python
    ctx = {
        "recycled_rotation_mode": mode or "normal",
        "rotation_reasons": {},
        "rotation_reason_codes": {},
        "rotation_warnings": [],
        "rotation_issues": [],
        "active_training_blocks": [],
    }
    # after the suggestion
    ctx["rotation_reason_codes"] = {
        wc: dict(values) for wc, values in suggestion.reason_codes.items()
    }
    ctx["rotation_issues"] = [issue.to_dict() for issue in suggestion.issues]
```

- [ ] **Step 5: Ensure partial rebuild persistence is unconditional on coverage completeness**

In the rebuild handler, keep failure handling for a missing suggestion, but remove any condition that treats `suggestion.issues` or `suggestion.unresolved_centers` as a failed rebuild. Merge and save `suggestion.assignments` through the existing preservation path, then return `coverage` and `warnings`.

- [ ] **Step 6: Run focused route tests**

Run:

```bash
ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_auto_schedule_capacity.py tests/test_staffing_rotations.py tests/test_staffing_trim_saw_defaults.py -q
.venv/bin/python -m ruff check src/zira_dashboard/auto_schedule_capacity.py src/zira_dashboard/routes/staffing.py src/zira_dashboard/routes/rotations.py tests/test_auto_schedule_capacity.py tests/test_staffing_rotations.py
```

Expected: Auto selections persist with HTTP 200 even when coverage is partial; rebuild persists the partial safe result; data-source failures still fail safely.

Commit:

```bash
git add src/zira_dashboard/auto_schedule_capacity.py src/zira_dashboard/routes/staffing.py src/zira_dashboard/routes/rotations.py tests/test_auto_schedule_capacity.py tests/test_staffing_rotations.py
git commit -m "fix: make auto coverage advisory and skill aware"
```

---

### Task 5: Alert the planner and support manual resolution with “Why?” details

**Files:**

- Modify: `src/zira_dashboard/templates/staffing.html`
- Modify: `src/zira_dashboard/static/staffing.js`
- Modify: `src/zira_dashboard/static/staffing.css`
- Modify: `tests/test_staffing_static.py`
- Modify: `tests/test_staffing_rotations.py`

- [ ] **Step 1: Add failing static contract tests**

Replace capacity-dialog assertions with:

```python
def test_rotation_warning_supports_structured_coverage_issues():
    html = TEMPLATE.read_text()
    js = STAFFING_JS.read_text()
    css = STAFFING_CSS.read_text()

    assert 'id="rotation-warnings"' in html
    assert 'class="coverage-why"' in html
    assert "rotation_issues" in html
    assert "renderCoverageIssues" in js
    assert "ROTATION_ISSUES" in js
    assert "rotation-reason" not in html
    assert "rotation-reason" not in js
    assert "rotation-reason" not in css
    assert "ROTATION_REASONS" not in html
    assert "ROTATION_REASONS" not in js


def test_auto_capacity_turn_off_dialog_is_removed():
    html = TEMPLATE.read_text()
    js = STAFFING_JS.read_text()

    assert 'id="auto-capacity-dialog"' not in html
    assert "showAutoCapacityDialog" not in js
```

Add a route context test asserting `rotation_issues` contains `training_required`, its exact message, and stable rejection details.

- [ ] **Step 2: Render initial issues on the server**

Replace the current warning-only list in `staffing.html` with:

```html
<div class="rotation-warning" id="rotation-warnings" role="alert"
     {% if not rotation_warnings and not rotation_issues %}hidden{% endif %}>
  <ul id="rotation-warning-list">
    {% for issue in rotation_issues %}
      <li class="coverage-issue" data-issue-code="{{ issue.code }}">
        <span>{{ issue.message }}</span>
        {% if issue.rejections %}
          <details class="coverage-why">
            <summary>Why?</summary>
            <ul>
              {% for rejection in issue.rejections %}
                <li><strong>{{ rejection.person }}</strong>: {{ rejection.detail }}</li>
              {% endfor %}
            </ul>
          </details>
        {% endif %}
      </li>
    {% endfor %}
    {% set issue_messages = rotation_issues | map(attribute='message') | list %}
    {% for warning in rotation_warnings %}
      {% if warning not in issue_messages %}
        <li>{{ warning }}</li>
      {% endif %}
    {% endfor %}
  </ul>
</div>
```

Seed client state beside the existing warning bootstrap:

```html
<script>
  window.ROTATION_WARNINGS = {{ rotation_warnings | tojson }};
  window.ROTATION_ISSUES = {{ rotation_issues | tojson }};
</script>
```

Delete the `auto-capacity-dialog` block completely. There is no replacement modal: the selection succeeds, the alert appears, and the planner remains on the schedule to solve it manually.

- [ ] **Step 3: Render structured issues safely in JavaScript**

Replace `renderWarnings(warnings)` with DOM-only rendering:

```javascript
function renderCoverageIssues(warnings, issues) {
  window.ROTATION_WARNINGS = Array.isArray(warnings) ? warnings : [];
  window.ROTATION_ISSUES = Array.isArray(issues) ? issues : [];
  const list = document.getElementById('rotation-warning-list');
  if (!warnBox || !list) return;

  list.replaceChildren();
  const issueMessages = new Set();
  window.ROTATION_ISSUES.forEach(issue => {
    issueMessages.add(issue.message);
    const item = document.createElement('li');
    item.className = 'coverage-issue';
    item.dataset.issueCode = issue.code || '';
    const message = document.createElement('span');
    message.textContent = issue.message || 'A work center needs manual attention.';
    item.appendChild(message);

    if (Array.isArray(issue.rejections) && issue.rejections.length) {
      const details = document.createElement('details');
      details.className = 'coverage-why';
      const summary = document.createElement('summary');
      summary.textContent = 'Why?';
      const reasons = document.createElement('ul');
      issue.rejections.forEach(rejection => {
        const reason = document.createElement('li');
        reason.textContent = `${rejection.person}: ${rejection.detail}`;
        reasons.appendChild(reason);
      });
      details.append(summary, reasons);
      item.appendChild(details);
    }
    list.appendChild(item);
  });

  window.ROTATION_WARNINGS.forEach(warning => {
    if (issueMessages.has(warning)) return;
    const item = document.createElement('li');
    item.textContent = warning;
    list.appendChild(item);
  });
  warnBox.hidden = list.childElementCount === 0;
}
```

Update Auto-toggle and rebuild success paths to call:

```javascript
renderCoverageIssues(data.warnings, data.coverage?.issues || []);
```

Delete capacity-dialog state, listeners, 409 handling, and replacement-checkbox code. Continue sending `turn_off: []` for backward-compatible request parsing until the endpoint field is removed in a later API cleanup.

- [ ] **Step 4: Add concise issue and disclosure styling**

Remove `#auto-capacity-dialog` and `.auto-capacity-*` rules. Add:

```css
.coverage-issue > span { font-weight: 650; }
.coverage-why { margin-top: 0.3rem; color: var(--muted); }
.coverage-why summary {
  width: max-content;
  cursor: pointer;
  color: var(--accent);
  font-weight: 650;
}
.coverage-why ul { margin: 0.35rem 0 0.1rem 1.1rem; }
.coverage-why li { margin: 0.15rem 0; }
```

- [ ] **Step 5: Run static and route rendering tests**

Run:

```bash
ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_staffing_static.py tests/test_staffing_rotations.py -q
.venv/bin/python -m ruff check tests/test_staffing_static.py tests/test_staffing_rotations.py
```

Expected: the initial page and asynchronous responses expose the same issue details; no capacity modal remains.

Commit:

```bash
git add src/zira_dashboard/templates/staffing.html src/zira_dashboard/static/staffing.js src/zira_dashboard/static/staffing.css tests/test_staffing_static.py tests/test_staffing_rotations.py
git commit -m "feat: explain unresolved scheduler coverage"
```

---

### Task 6: Prove optimality on small cases and guard plant-scale performance

**Files:**

- Create: `tests/test_schedule_solver_properties.py`
- Create: `scripts/replay_schedule_solver.py`

- [ ] **Step 1: Add a small exhaustive oracle and invariants**

Create `tests/test_schedule_solver_properties.py`:

```python
from itertools import product
from time import perf_counter

from zira_dashboard.schedule_solver import CandidateEdge, CenterRequirement, solve_minimum_coverage


def _edge(person, center, level=1):
    return CandidateEdge(person, center, level, "regular", 3 - level)


def _oracle_count(requirements):
    choices = [
        (None,) + tuple(edge.person for edge in requirement.candidates)
        for requirement in requirements
    ]
    best = 0
    for selection in product(*choices):
        used = [person for person in selection if person is not None]
        if len(used) != len(set(used)):
            continue
        best = max(best, len(used))
    return best


def test_matching_agrees_with_exhaustive_oracle_for_all_small_graphs():
    people = ("A", "B", "C")
    centers = ("One", "Two", "Three")
    possible = tuple((person, center) for person in people for center in centers)
    for mask in range(1 << len(possible)):
        requirements = tuple(
            CenterRequirement(
                center=center,
                group=center,
                remaining_slots=1,
                candidates=tuple(
                    _edge(person, center)
                    for index, (person, edge_center) in enumerate(possible)
                    if edge_center == center and mask & (1 << index)
                ),
            )
            for center in centers
        )
        result = solve_minimum_coverage(requirements)
        assert len(result.staffed_centers) == _oracle_count(requirements)
        assert len(result.assigned_people) == len(result.decisions)


def test_plant_sized_matching_is_deterministic_and_under_one_second():
    people = tuple(f"Person {index:02d}" for index in range(48))
    centers = tuple(f"Center {index:02d}" for index in range(22))
    requirements = tuple(
        CenterRequirement(
            center=center,
            group=f"Group {center_index % 7}",
            remaining_slots=1,
            candidates=tuple(
                _edge(person, center, 1 + ((person_index + center_index) % 3))
                for person_index, person in enumerate(people)
                if (person_index * 5 + center_index * 3) % 7 < 3
            ),
        )
        for center_index, center in enumerate(centers)
    )

    started = perf_counter()
    first = solve_minimum_coverage(requirements)
    elapsed = perf_counter() - started
    second = solve_minimum_coverage(tuple(reversed(requirements)))

    assert elapsed < 1.0
    assert first == second
    assert len(first.assigned_people) == len(first.decisions)
```

Add a second plant fixture with the actual configured center minimums, including Trim Saw and the multi-person Hand Build centers. If coupled search exceeds one second, optimize through safe dominance pruning or memoization keyed by `(coupled_index, used_people)`; do not truncate crew options or weaken optimality.

- [ ] **Step 2: Add invariant tests for no duplicate people and no partial generated crews**

For a matrix of modes, roster orderings, and enabled-center subsets, assert:

```python
assert len(result.assigned_people) == len(result.decisions)
for requirement in requirements:
    assigned = [d for d in result.decisions if d.center == requirement.center]
    assert len(assigned) in {0, requirement.remaining_slots}
assert all(decision.level >= 1 for decision in result.decisions)
```

For an explicit training-block fixture, separately assert the protected level-0 trainee is preserved but never appears in `CoverageResult.decisions`.

- [ ] **Step 3: Create a read-only replay command**

Create `scripts/replay_schedule_solver.py` with no save/update calls:

```python
#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from datetime import date

from zira_dashboard import rotation_store, rotation_suggestions, scheduler_time_off, staffing
from zira_dashboard.routes import staffing as staffing_route


def replay(day: date) -> dict[str, object]:
    roster = staffing.load_roster()
    schedule = staffing.load_schedule(day)
    time_off = scheduler_time_off.time_off_entries_for_day(day)
    available = staffing_route._roster_minus_full_day_off(roster, time_off)
    enabled = set(staffing_route._enabled_auto_work_centers(day))
    groups, skills = staffing_route._auto_group_maps(enabled)
    preferences = rotation_store.load_preferences_by_name()
    history = rotation_suggestions._load_recycled_history(
        day,
        group_locations=staffing_route._auto_history_group_locations(),
    )
    blocks = staffing_route._block_effects_for_day(
        day,
        time_off,
        assignments=schedule.assignments,
        assignment_sources=schedule.assignment_sources,
    )
    locks = staffing_route._protected_locks(
        schedule.assignment_sources,
        schedule.assignments,
        allowed_centers=enabled,
        strict_default_reads=True,
    )
    result = rotation_suggestions.suggest_recycled_assignments(
        day=day,
        mode=schedule.rotation_mode,
        roster=available,
        preferences=preferences,
        base_assignments=schedule.assignments,
        group_locations=groups,
        group_required_skills=skills,
        history=history,
        locked_assignments=locks,
        block_effects=blocks,
        center_minimums={
            loc.name: staffing_route._effective_minimum(loc)
            for loc in staffing.LOCATIONS if loc.name in enabled
        },
        center_capacities=staffing_route._configured_center_capacities(enabled),
        runnable_centers=enabled,
    )
    return {
        "day": day.isoformat(),
        "saved_assignments": schedule.assignments,
        "suggested_assignments": result.assignments,
        "staffed_centers": result.staffed_centers,
        "unresolved_centers": result.unresolved_centers,
        "issues": [issue.to_dict() for issue in result.issues],
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Read-only comparison of saved schedules with the global solver."
    )
    parser.add_argument("day", nargs="+", type=date.fromisoformat)
    args = parser.parse_args()
    print(json.dumps([replay(day) for day in args.day], indent=2, default=list))


if __name__ == "__main__":
    main()
```

Before committing, confirm every imported helper is read-only. In particular, do not call `_gather_recycled_inputs(...)` because it reconciles training blocks. Keep the replay wired to the exact read-only loaders shown above rather than adding a convenience call that writes.

- [ ] **Step 4: Run correctness and performance guards**

Run:

```bash
ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_schedule_solver.py tests/test_schedule_solver_properties.py -q
.venv/bin/python -m ruff check tests/test_schedule_solver_properties.py scripts/replay_schedule_solver.py
```

Expected: exhaustive oracle agrees, invariants pass, and both plant-scale fixtures complete under one second locally.

Run a representative read-only replay only in an environment with configured database/Odoo credentials:

```bash
.venv/bin/python scripts/replay_schedule_solver.py 2026-07-14
```

Expected: JSON is printed; no schedule, training-block, or Auto-center writes occur.

Commit:

```bash
git add tests/test_schedule_solver_properties.py scripts/replay_schedule_solver.py
git commit -m "test: verify scheduler optimality and performance"
```

---

### Task 7: Document invariants and complete repository verification

**Files:**

- Modify: `CHANGELOG.md`
- Modify: `CLAUDE.md`

- [ ] **Step 1: Add the planner-facing changelog entry**

Under the current date’s Fixes section, add:

```markdown
- **Auto scheduling now makes cross-skill swaps before declaring a work center uncovered.** The scheduler globally maximizes minimum work-center coverage, so a cross-trained person can move to a scarce role while another qualified unscheduled person backfills their old role. Skill levels 1–3 may cover a minimum even when the person’s preference is Never, but Never is overridden only when it staffs more centers. When full coverage is impossible, the safest partial schedule is saved, unresolved Auto centers stay enabled, and the page explains what needs manual attention. If only level-0 people remain, the alert says training is required without choosing the trainee or trainer.
```

- [ ] **Step 2: Record the durable engineering contract in `CLAUDE.md`**

Add a concise scheduler invariant section near the existing Plant Scheduler notes:

```markdown
### Global Auto scheduling

- `schedule_solver.solve_minimum_coverage` is the pure authority for enabled Auto-center minimum feasibility.
- Coverage cardinality is primary; `never` overrides, mode rank, and stable ordering are tie-breakers in that order.
- Generated multi-person crews are atomic: complete or absent.
- Level 0 is automatic only through a validated training block; otherwise surface `training_required`.
- Page context, Auto selection, and rebuild responses must serialize the same structured coverage issues.
- Focused checks: `ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_schedule_solver.py tests/test_schedule_solver_properties.py tests/test_rotation_suggestions.py tests/test_staffing_rotations.py -q`.
```

- [ ] **Step 3: Run all focused scheduler tests**

Run:

```bash
ZIRA_API_KEY=test .venv/bin/python -m pytest tests/test_schedule_solver.py tests/test_schedule_solver_properties.py tests/test_rotation_suggestions.py tests/test_rotation_training.py tests/test_auto_schedule_capacity.py tests/test_staffing_rotations.py tests/test_staffing_static.py tests/test_staffing_trim_saw_defaults.py -q
```

Expected: all focused tests pass.

- [ ] **Step 4: Run the full suite and Ruff**

Run:

```bash
ZIRA_API_KEY=test .venv/bin/python -m pytest -q
.venv/bin/python -m ruff check src tests scripts
```

Expected: full suite passes and Ruff reports no errors. If unrelated concurrent files fail Ruff, report them separately and still run Ruff on every file changed by this plan.

- [ ] **Step 5: Review the final diff against the approved policy**

Run:

```bash
git diff --check
git diff --stat
git status --short
```

Verify manually:

- Jose Luis moves to Dismantler and a qualified unscheduled operator backfills Repair.
- A level-1+ `never` person is used only when coverage cardinality increases.
- Level 0 outside a training block is never generated and produces “Training is required.”
- Insufficient coverage saves a safe partial schedule, leaves unresolved centers enabled, and exposes manual-resolution details.
- Manual/default locks and active training blocks are preserved.
- Trim Saw and training partner rules remain safe.
- Auto toggle, initial page, and rebuild return the same structured issue semantics.
- No generated unresolved center contains a partial crew.
- Solver output is deterministic and plant-sized fixtures remain below one second.

- [ ] **Step 6: Commit documentation and verification record**

Commit:

```bash
git add CHANGELOG.md CLAUDE.md
git commit -m "docs: explain global scheduler coverage behavior"
```

Do not stage or commit unrelated modified/untracked files.
