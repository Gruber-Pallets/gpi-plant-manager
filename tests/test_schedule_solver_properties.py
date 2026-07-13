from importlib.util import module_from_spec, spec_from_file_location
from itertools import combinations, product
from pathlib import Path
from time import perf_counter
from types import SimpleNamespace

import pytest

from zira_dashboard import staffing
from zira_dashboard.schedule_solver import (
    CandidateEdge,
    CenterRequirement,
    CrewOption,
    solve_minimum_coverage,
)


def _edge(person, center, level=1, *, rank_cost=None):
    return CandidateEdge(
        person,
        center,
        level,
        "regular",
        3 - level if rank_cost is None else rank_cost,
    )


def _oracle_count(requirements):
    """Independent brute-force maximum matching for single-slot centers."""
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


def _assert_solver_invariants(requirements, result):
    assert len(result.assigned_people) == len(result.decisions)
    for requirement in requirements:
        assigned = [item for item in result.decisions if item.center == requirement.center]
        assert len(assigned) in {0, requirement.remaining_slots}
    assert all(decision.level >= 1 for decision in result.decisions)


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


def _plant_requirements(*, reverse_roster=False, enabled=None, mode="normal"):
    """Build synthetic candidates against the plant's real center/minimum config."""
    people = tuple(f"Plant Person {index:02d}" for index in range(48))
    if reverse_roster:
        people = tuple(reversed(people))
    locations = tuple(
        loc for loc in staffing.LOCATIONS if enabled is None or loc.name in enabled
    )
    requirements = []
    mode_bias = {"optimized": 0, "normal": 10, "training": 20}[mode]
    for center_index, loc in enumerate(locations):
        if loc.name == "Trim Saw 1":
            qualified = {20, 21, 22, 23, 40}
        elif loc.name == "Hand Build #2":
            qualified = {28, 29, 30, 31, 40}
        elif loc.name == "Hand Build #1":
            qualified = {31, 32, 33, 34, 41}
        elif loc.name == "Big Build #1":
            qualified = {34, 35, 36, 37, 40}
        elif loc.skill == "Repair":
            qualified = set(range(12)) | {40}
        elif loc.skill == "Dismantler":
            qualified = set(range(10, 22)) | {41}
        else:
            start = (center_index * 3) % 14
            qualified = {(start + offset) % 22 for offset in range(8)}
        pool = tuple(
            person
            for person in people
            if int(person.rsplit(" ", 1)[1]) in qualified
        )
        edges = tuple(
            _edge(
                person,
                loc.name,
                1 + ((int(person.rsplit(" ", 1)[1]) + center_index) % 3),
                rank_cost=(
                    3 - (1 + ((int(person.rsplit(" ", 1)[1]) + center_index) % 3))
                )
                * 100
                + mode_bias,
            )
            for person in pool
        )
        if loc.min_ops == 1:
            requirements.append(CenterRequirement(
                center=loc.name,
                group=loc.skill,
                remaining_slots=1,
                candidates=edges,
            ))
            continue
        crew_options = []
        for left, right in combinations(edges, 2):
            if loc.name == "Trim Saw 1":
                low, high = sorted((left.level, right.level))
                if (low <= 1 and high < 3) or (low > 1 and high < 2):
                    continue
            crew_options.append(CrewOption(loc.name, (left, right)))
        requirements.append(CenterRequirement(
            center=loc.name,
            group=loc.skill,
            remaining_slots=loc.min_ops,
            crew_options=tuple(crew_options),
        ))
    return tuple(requirements)


def test_actual_plant_minimums_with_coupled_centers_are_under_one_second():
    requirements = _plant_requirements()
    minimums = {item.center: item.remaining_slots for item in requirements}
    assert minimums["Trim Saw 1"] == 2
    assert minimums["Hand Build #1"] == 2
    assert minimums["Hand Build #2"] == 2
    assert minimums["Big Build #1"] == 2

    started = perf_counter()
    first = solve_minimum_coverage(requirements)
    elapsed = perf_counter() - started
    second = solve_minimum_coverage(tuple(reversed(requirements)))

    assert elapsed < 1.0
    assert first == second
    _assert_solver_invariants(requirements, first)


@pytest.mark.parametrize("mode", ("optimized", "normal", "training"))
@pytest.mark.parametrize(
    "enabled",
    (
        {"Repair 1", "Repair 2", "Dismantler 1"},
        {"Trim Saw 1", "Hand Build #1", "Repair 1"},
        {"Hand Build #1", "Hand Build #2", "Big Build #1"},
    ),
)
def test_solver_is_deterministic_and_preserves_invariants_across_modes_and_enabled_centers(
    mode, enabled
):
    forward_requirements = _plant_requirements(
        reverse_roster=False,
        enabled=enabled,
        mode=mode,
    )
    reversed_requirements = _plant_requirements(
        reverse_roster=True,
        enabled=enabled,
        mode=mode,
    )

    forward = solve_minimum_coverage(forward_requirements)
    reversed_result = solve_minimum_coverage(reversed_requirements)

    assert forward == reversed_result
    _assert_solver_invariants(forward_requirements, forward)
    _assert_solver_invariants(reversed_requirements, reversed_result)


def test_protected_level_zero_trainee_is_preserved_outside_generated_decisions():
    requirement = CenterRequirement(
        center="Hand Build #1",
        group="Hand Build",
        remaining_slots=1,
        protected_people=("Protected Trainee",),
        candidates=(_edge("Qualified Trainer", "Hand Build #1", level=3),),
        level_zero_people=("Protected Trainee",),
    )

    result = solve_minimum_coverage((requirement,))

    assert requirement.protected_people == ("Protected Trainee",)
    assert result.staffed_centers == ("Hand Build #1",)
    assert {item.person for item in result.decisions} == {"Qualified Trainer"}
    assert "Protected Trainee" not in result.assigned_people


def _load_replay_module():
    path = Path(__file__).parents[1] / "scripts" / "replay_schedule_solver.py"
    spec = spec_from_file_location("replay_schedule_solver", path)
    assert spec is not None and spec.loader is not None
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_replay_uses_only_read_paths_even_when_auto_centers_are_not_initialized(monkeypatch):
    replay_module = _load_replay_module()
    day = replay_module.date(2026, 7, 14)
    schedule = SimpleNamespace(
        assignments={"Repair 1": ["Saved"]},
        assignment_sources={"Repair 1": {"Saved": "manual"}},
        rotation_mode="normal",
    )
    suggestion = SimpleNamespace(
        assignments={"Repair 1": ["Suggested"]},
        staffed_centers=("Repair 1",),
        unresolved_centers=(),
        issues=(),
    )

    def forbidden(*_args, **_kwargs):
        raise AssertionError("replay attempted a write or reconciliation")

    monkeypatch.setattr(replay_module.staffing, "load_roster", lambda: ["Roster"])
    monkeypatch.setattr(replay_module.staffing, "load_schedule", lambda _day: schedule)
    monkeypatch.setattr(replay_module.staffing, "save_schedule", forbidden)
    monkeypatch.setattr(
        replay_module.scheduler_time_off,
        "time_off_entries_for_day",
        lambda _day: [],
    )
    route = replay_module.staffing_route
    monkeypatch.setattr(route, "_roster_minus_full_day_off", lambda roster, _off: roster)
    monkeypatch.setattr(route.app_settings, "get_setting", lambda _key: None)
    monkeypatch.setattr(route.app_settings, "set_setting", forbidden)
    monkeypatch.setattr(route, "_recently_used_work_centers", lambda _day: ["Repair 1"])
    monkeypatch.setattr(route, "_auto_group_maps", lambda _enabled: ({"Repair": ("Repair 1",)}, {}))
    monkeypatch.setattr(route, "_auto_history_group_locations", lambda: {})
    monkeypatch.setattr(route, "_block_effects_for_day", lambda *_args, **_kwargs: ())
    monkeypatch.setattr(route, "_protected_locks", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(route, "_gather_recycled_inputs", forbidden)
    monkeypatch.setattr(route, "_effective_minimum", lambda _loc: 1)
    monkeypatch.setattr(route, "_configured_center_capacities", lambda _enabled: {})
    monkeypatch.setattr(
        replay_module.staffing,
        "LOCATIONS",
        (SimpleNamespace(name="Repair 1"),),
    )
    monkeypatch.setattr(
        replay_module.rotation_store,
        "load_preferences_by_name",
        lambda: {},
    )
    monkeypatch.setattr(
        replay_module.rotation_suggestions,
        "_load_recycled_history",
        lambda *_args, **_kwargs: SimpleNamespace(),
    )
    monkeypatch.setattr(
        replay_module.rotation_suggestions,
        "suggest_recycled_assignments",
        lambda **_kwargs: suggestion,
    )

    assert replay_module.replay(day) == {
        "day": "2026-07-14",
        "saved_assignments": {"Repair 1": ["Saved"]},
        "suggested_assignments": {"Repair 1": ["Suggested"]},
        "staffed_centers": ("Repair 1",),
        "unresolved_centers": (),
        "issues": [],
    }
