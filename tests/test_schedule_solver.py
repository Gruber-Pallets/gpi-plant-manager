import pytest

from zira_dashboard.schedule_solver import (
    CandidateEdge,
    CandidateRejection,
    CenterRequirement,
    CompleteCenter,
    CrewOption,
    solve_complete_schedule,
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
                    (
                        edge("Green", "Trim Saw 1", level=3),
                        edge("Learner", "Trim Saw 1", level=1),
                    ),
                ),),
            ),
            CompleteCenter("Repair 1", "Repair", minimum=1, capacity=1),
        ),
        candidates=(edge("Repair", "Repair 1"),),
    )

    assert result.complete is True
    assert {item.person for item in result.decisions} == {"Green", "Learner", "Repair"}


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


def test_equal_coverage_prefers_no_never_override_before_rank_or_canonical_name():
    result = solve_minimum_coverage((CenterRequirement(
        center="Repair 1",
        group="Repair",
        remaining_slots=1,
        candidates=(
            edge("A Never", "Repair 1", never=True, rank=0),
            edge("Z Regular", "Repair 1", rank=100),
        ),
    ),))

    assert tuple((item.center, item.person) for item in result.decisions) == (
        ("Repair 1", "Z Regular"),
    )
    assert result.decisions[0].preference == "regular"


def test_equal_coverage_and_overrides_prefers_rank_before_canonical_name():
    result = solve_minimum_coverage((CenterRequirement(
        center="Repair 1",
        group="Repair",
        remaining_slots=1,
        candidates=(
            edge("A Higher Cost", "Repair 1", rank=10),
            edge("Z Lower Cost", "Repair 1", rank=0),
        ),
    ),))

    assert tuple((item.center, item.person) for item in result.decisions) == (
        ("Repair 1", "Z Lower Cost"),
    )
    assert result.decisions[0].rank_cost == 0


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


def test_coupled_prefix_pruning_preserves_maximum_center_coverage():
    four_person = CrewOption(
        "A Four Person",
        tuple(edge(person, "A Four Person") for person in ("A", "B", "C", "D")),
    )
    first_pair = CrewOption(
        "B First Pair",
        tuple(edge(person, "B First Pair") for person in ("A", "B")),
    )
    second_pair = CrewOption(
        "C Second Pair",
        tuple(edge(person, "C Second Pair") for person in ("C", "D")),
    )

    result = solve_minimum_coverage((
        CenterRequirement(
            center="A Four Person",
            group="Four Person",
            remaining_slots=4,
            crew_options=(four_person,),
        ),
        CenterRequirement(
            center="B First Pair",
            group="Pair",
            remaining_slots=2,
            crew_options=(first_pair,),
        ),
        CenterRequirement(
            center="C Second Pair",
            group="Pair",
            remaining_slots=2,
            crew_options=(second_pair,),
        ),
    ))

    assert result.staffed_centers == ("B First Pair", "C Second Pair")
    assert {(item.center, item.person) for item in result.decisions} == {
        ("B First Pair", "A"),
        ("B First Pair", "B"),
        ("C Second Pair", "C"),
        ("C Second Pair", "D"),
    }


def test_protected_person_is_reserved_from_generated_assignments():
    result = solve_minimum_coverage((
        CenterRequirement(
            center="Manual Safe",
            group="Manual",
            remaining_slots=0,
            protected_people=("Protected",),
        ),
        CenterRequirement(
            center="Repair 1",
            group="Repair",
            remaining_slots=1,
            candidates=(edge("Protected", "Repair 1"),),
        ),
    ))

    assert result.decisions == ()
    assert result.staffed_centers == ("Manual Safe",)
    assert result.unresolved_centers == ("Repair 1",)


def test_crew_option_rejects_member_from_another_center():
    malformed = CrewOption(
        "Hand Build #1",
        (
            edge("A", "Hand Build #1"),
            edge("B", "Repair 1"),
        ),
    )

    with pytest.raises(ValueError, match="complete, unique, and center-scoped"):
        solve_minimum_coverage((CenterRequirement(
            center="Hand Build #1",
            group="Hand Build",
            remaining_slots=2,
            crew_options=(malformed,),
        ),))


def test_level_zero_single_edge_is_rejected_at_solver_boundary():
    with pytest.raises(ValueError, match="levels must be 1, 2, or 3"):
        solve_minimum_coverage((CenterRequirement(
            center="Repair 1",
            group="Repair",
            remaining_slots=1,
            candidates=(edge("Trainee", "Repair 1", level=0),),
            level_zero_people=("Trainee",),
        ),))


def test_level_zero_crew_edge_is_rejected_at_solver_boundary():
    with pytest.raises(ValueError, match="levels must be 1, 2, or 3"):
        solve_minimum_coverage((CenterRequirement(
            center="Hand Build #1",
            group="Hand Build",
            remaining_slots=2,
            crew_options=(CrewOption(
                "Hand Build #1",
                (
                    edge("Qualified", "Hand Build #1", level=2),
                    edge("Trainee", "Hand Build #1", level=0),
                ),
            ),),
            level_zero_people=("Trainee",),
        ),))


def test_equal_cost_single_matching_uses_canonical_center_person_mapping():
    result = solve_minimum_coverage((
        CenterRequirement(
            center="C1",
            group="G1",
            remaining_slots=1,
            candidates=(edge("A", "C1"), edge("C", "C1")),
        ),
        CenterRequirement(
            center="C2",
            group="G2",
            remaining_slots=1,
            candidates=(edge("A", "C2"), edge("B", "C2")),
        ),
        CenterRequirement(
            center="C3",
            group="G3",
            remaining_slots=1,
            candidates=(edge("A", "C3"), edge("B", "C3")),
        ),
    ))

    assert tuple((item.center, item.person) for item in result.decisions) == (
        ("C1", "C"),
        ("C2", "A"),
        ("C3", "B"),
    )


def test_large_canonical_tie_costs_remain_reachable():
    requirements = tuple(
        CenterRequirement(
            center=f"C{index:02}",
            group="Group",
            remaining_slots=1,
            candidates=(CandidateEdge(
                person=f"P{index:02}",
                center=f"C{index:02}",
                level=1,
                preference="regular",
                rank_cost=1,
            ),),
        )
        for index in range(20)
    )

    result = solve_minimum_coverage(requirements)

    assert len(result.staffed_centers) == 20
    assert result.unresolved_centers == ()


def test_coupled_prefix_tie_uses_canonical_center_person_mapping():
    result = solve_minimum_coverage((
        CenterRequirement(
            center="A Center",
            group="A",
            remaining_slots=2,
            crew_options=tuple(
                CrewOption(
                    "A Center",
                    tuple(edge(person, "A Center") for person in people),
                )
                for people in (("A", "B"), ("C", "D"), ("E", "F"))
            ),
        ),
        CenterRequirement(
            center="Z Center",
            group="Z",
            remaining_slots=2,
            crew_options=tuple(
                CrewOption(
                    "Z Center",
                    tuple(edge(person, "Z Center") for person in people),
                )
                for people in (("A", "B"), ("C", "D"))
            ),
        ),
    ))

    assert tuple((item.center, item.person) for item in result.decisions) == (
        ("A Center", "A"),
        ("A Center", "B"),
        ("Z Center", "C"),
        ("Z Center", "D"),
    )


@pytest.mark.parametrize(
    "requirement",
    (
        CenterRequirement(
            center="Repair 1",
            group="Repair",
            remaining_slots=1,
            candidates=(edge("Expert", "Repair 1", level=4),),
        ),
        CenterRequirement(
            center="Hand Build #1",
            group="Hand Build",
            remaining_slots=2,
            crew_options=(CrewOption(
                "Hand Build #1",
                (
                    edge("Qualified", "Hand Build #1", level=2),
                    edge("Expert", "Hand Build #1", level=4),
                ),
            ),),
        ),
    ),
    ids=("single", "crew"),
)
def test_level_four_edge_is_rejected_at_solver_boundary(requirement):
    with pytest.raises(ValueError, match="levels must be 1, 2, or 3"):
        solve_minimum_coverage((requirement,))
