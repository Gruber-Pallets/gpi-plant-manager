from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

import pytest

from zira_dashboard import staffing
from zira_dashboard.rotation_suggestions import (
    TRIM_SAW_SKILL,
    RecycledHistory,
    TrimSawHistory,
    _valid_trim_saw_pair,
    choose_center,
    suggest_recycled_assignments,
    suggest_trim_saw_pair,
)


TARGET_DAY = date(2026, 7, 6)


def person(name: str, level: int, *, active: bool = True, reserve: bool = False):
    return staffing.Person(
        name=name,
        active=active,
        reserve=reserve,
        skills={TRIM_SAW_SKILL: level},
    )


def empty_history():
    return TrimSawHistory(appearance_counts={}, most_recent_names=set())


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


def test_history_records_most_recent_user_group_center():
    from zira_dashboard.rotation_suggestions import _recycled_history_from_rows

    history = _recycled_history_from_rows(
        [
            {"assignments": {"Repair 2": ["Ana"]}},
            {"assignments": {"Repair 1": ["Ana"]}},
        ],
        {"Repair": ("Repair 1", "Repair 2")},
        {"Repair Line": ("Repair 1", "Repair 2")},
    )

    assert history.last_center_by_person_group[
        ("Ana", "User Group:Repair Line")
    ] == "Repair 2"


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


def test_complete_engine_excludes_reserves_and_uses_never_when_required():
    result = suggest_recycled_assignments(
        TARGET_DAY,
        "normal",
        roster=[
            staffing.Person("Only Available", True, False, {"Repair": 3}),
            staffing.Person("Reserve", True, True, {"Repair": 3}),
        ],
        preferences={"Only Available": {"Repair": "never"}},
        group_locations={"Repair": ("Repair 1",)},
        group_required_skills={"Repair": ("Repair",)},
        center_minimums={"Repair 1": 1},
        center_capacities={"Repair 1": 1},
        runnable_centers={"Repair 1"},
    )

    assert result.complete is True
    assert result.available_people == ("Only Available",)
    assert result.assignments["Repair 1"] == ["Only Available"]
    assert result.reason_codes["Repair 1"]["Only Available"] == "preference_override"


@pytest.mark.parametrize("mode", ("optimized", "normal", "training"))
def test_every_mode_places_the_same_complete_headcount(mode):
    result = suggest_recycled_assignments(
        TARGET_DAY,
        mode,
        roster=[
            staffing.Person("Green", True, False, {"Repair": 3}),
            staffing.Person("Learner", True, False, {"Repair": 1}),
        ],
        group_locations={"Repair": ("Repair 1",)},
        group_required_skills={"Repair": ("Repair",)},
        center_minimums={"Repair 1": 1},
        center_capacities={"Repair 1": 2},
        runnable_centers={"Repair 1"},
    )

    assert result.complete is True
    assert result.unused_people == ()
    assert len(result.assignments["Repair 1"]) == 2


def test_impossible_capacity_keeps_best_safe_partial_schedule():
    result = suggest_recycled_assignments(
        TARGET_DAY,
        "normal",
        roster=[
            staffing.Person("A", True, False, {"Repair": 3}),
            staffing.Person("B", True, False, {"Repair": 3}),
        ],
        group_locations={"Repair": ("Repair 1",)},
        group_required_skills={"Repair": ("Repair",)},
        center_minimums={"Repair 1": 1},
        center_capacities={"Repair 1": 1},
        runnable_centers={"Repair 1"},
    )

    assert result.complete is False
    assert result.assignments == {"Repair 1": ["A"]}
    assert result.unused_people == ("B",)
    assert any(
        issue.code == "person_unplaced"
        for issue in result.placement_issues
    )


def test_partial_rebuild_keeps_valid_existing_assignment_and_fills_open_capacity():
    result = suggest_recycled_assignments(
        TARGET_DAY,
        "normal",
        roster=[
            staffing.Person("Kept", True, False, {"Repair": 3}),
            staffing.Person("Placed", True, False, {"Repair": 2}),
            staffing.Person("Unqualified", True, False, {"Dismantle": 3}),
        ],
        base_assignments={"Repair 1": ["Kept"]},
        locked_assignments={"Repair 1": ["Kept"]},
        group_locations={"Repair": ("Repair 1",)},
        group_required_skills={"Repair": ("Repair",)},
        center_minimums={"Repair 1": 1},
        center_capacities={"Repair 1": 2},
        runnable_centers={"Repair 1"},
    )

    assert result.assignments["Repair 1"] == ["Kept", "Placed"]
    assert result.unused_people == ("Unqualified",)
    assert result.complete is False


def test_partial_rebuild_clears_unqualified_enabled_assignment_before_fill():
    result = suggest_recycled_assignments(
        TARGET_DAY,
        "normal",
        roster=[
            staffing.Person("Invalid", True, False, {"Dismantle": 3}),
            staffing.Person("Qualified", True, False, {"Repair": 3}),
        ],
        base_assignments={"Repair 1": ["Invalid"]},
        group_locations={"Repair": ("Repair 1",)},
        group_required_skills={"Repair": ("Repair",)},
        center_minimums={"Repair 1": 0},
        center_capacities={"Repair 1": 1},
        runnable_centers={"Repair 1"},
    )

    assert result.assignments["Repair 1"] == ["Qualified"]
    assert result.unused_people == ("Invalid",)


def test_valid_trim_saw_pair_rules():
    assert _valid_trim_saw_pair(3, 1) is True
    assert _valid_trim_saw_pair(3, 0) is True
    assert _valid_trim_saw_pair(2, 2) is True
    assert _valid_trim_saw_pair(2, 1) is False
    assert _valid_trim_saw_pair(1, 1) is False
    assert _valid_trim_saw_pair(0, 2) is False


def test_level_three_default_can_pair_with_level_one():
    roster = [person("Jesus Martinez", 3), person("Rosa", 1), person("Carlos", 2)]

    pair = suggest_trim_saw_pair(
        TARGET_DAY,
        roster,
        pinned_names=["Jesus Martinez"],
        unavailable_names=[],
        history=empty_history(),
    )

    assert pair == ["Jesus Martinez", "Carlos"]
    assert _valid_trim_saw_pair(3, 2)


def test_level_two_default_gets_level_two_or_three_partner():
    roster = [person("Jesus Martinez", 2), person("Luis", 1), person("Rosa", 2)]

    pair = suggest_trim_saw_pair(
        TARGET_DAY,
        roster,
        pinned_names=["Jesus Martinez"],
        unavailable_names=[],
        history=empty_history(),
    )

    assert pair == ["Jesus Martinez", "Rosa"]


def test_level_one_default_requires_level_three_partner():
    roster = [person("Jesus Martinez", 1), person("Luis", 2), person("Rosa", 3)]

    pair = suggest_trim_saw_pair(
        TARGET_DAY,
        roster,
        pinned_names=["Jesus Martinez"],
        unavailable_names=[],
        history=empty_history(),
    )

    assert pair == ["Jesus Martinez", "Rosa"]


def test_recent_history_reduces_candidate_rank():
    roster = [person("Alicia", 3), person("Beatriz", 3), person("Carlos", 2)]
    history = TrimSawHistory(
        appearance_counts={"Alicia": 4, "Beatriz": 0, "Carlos": 0},
        most_recent_names={"Alicia"},
    )

    pair = suggest_trim_saw_pair(
        TARGET_DAY,
        roster,
        pinned_names=[],
        unavailable_names=[],
        history=history,
    )

    assert pair == ["Beatriz", "Carlos"]


def test_level_three_still_outranks_level_two_when_similarly_due():
    roster = [person("Alicia", 3), person("Beatriz", 2), person("Carlos", 2)]

    pair = suggest_trim_saw_pair(
        TARGET_DAY,
        roster,
        pinned_names=[],
        unavailable_names=[],
        history=empty_history(),
    )

    assert pair[0] == "Alicia"
    assert set(pair) == {"Alicia", "Beatriz"}


def test_unavailable_and_reserve_people_are_excluded():
    roster = [
        person("Pinned Off", 3),
        person("Reserve Pro", 3, reserve=True),
        person("Available Pro", 3),
        person("Available Two", 2),
    ]

    pair = suggest_trim_saw_pair(
        TARGET_DAY,
        roster,
        pinned_names=["Pinned Off"],
        unavailable_names=["Pinned Off"],
        history=empty_history(),
    )

    assert pair == ["Available Pro", "Available Two"]


def test_no_safe_pair_returns_partial_assignment():
    roster = [person("Jesus Martinez", 1), person("Luis", 2)]

    pair = suggest_trim_saw_pair(
        TARGET_DAY,
        roster,
        pinned_names=["Jesus Martinez"],
        unavailable_names=[],
        history=empty_history(),
    )

    assert pair == ["Jesus Martinez"]


def test_invalid_two_pinned_defaults_retain_strongest_anchor_with_safe_partner():
    roster = [
        person("Pinned Level One", 1),
        person("Pinned Level Two", 2),
        person("Safe Level Three", 3),
    ]

    pair = suggest_trim_saw_pair(
        TARGET_DAY,
        roster,
        pinned_names=["Pinned Level One", "Pinned Level Two"],
        unavailable_names=[],
        history=empty_history(),
    )

    assert pair == ["Pinned Level Two", "Safe Level Three"]


def test_history_uses_published_snapshot_when_present():
    from zira_dashboard.rotation_suggestions import _history_from_schedule_rows

    rows = [
        {
            "day": date(2026, 7, 3),
            "assignments": {"Trim Saw 1": ["Draft Person"]},
            "published_snapshot": {"assignments": {"Trim Saw 1": ["Posted Person"]}},
        },
        {
            "day": date(2026, 7, 2),
            "assignments": {"Trim Saw 1": ["Posted Person", "Other"]},
            "published_snapshot": None,
        },
    ]

    history = _history_from_schedule_rows(rows)

    assert history.appearance_counts == {"Posted Person": 2, "Other": 1}
    assert history.most_recent_names == {"Posted Person"}


def test_load_trim_saw_history_queries_only_recent_non_testing_rows(monkeypatch):
    from zira_dashboard import db
    from zira_dashboard.rotation_suggestions import _load_trim_saw_history

    captured = {}

    def fake_query(sql, params=None):
        captured["sql"] = sql
        captured["params"] = params
        return [
            {
                "day": date(2026, 7, 3),
                "assignments": {"Trim Saw 1": ["Alicia"]},
                "published_snapshot": None,
            }
        ]

    monkeypatch.setattr(db, "query", fake_query)

    history = _load_trim_saw_history(date(2026, 7, 6))

    assert history.appearance_counts == {"Alicia": 1}
    assert history.most_recent_names == {"Alicia"}
    assert "LIMIT %s" in captured["sql"]
    assert captured["params"] == (date(2026, 7, 6), 20)


def test_load_trim_saw_history_filters_by_effective_schedule_testing_day(monkeypatch):
    from zira_dashboard import db
    from zira_dashboard.rotation_suggestions import _load_trim_saw_history

    captured = {}

    def fake_query(sql, params=None):
        captured["sql"] = sql
        captured["params"] = params
        return []

    monkeypatch.setattr(db, "query", fake_query)

    history = _load_trim_saw_history(date(2026, 7, 6))

    assert history.appearance_counts == {}
    assert history.most_recent_names == set()
    predicate = (
        "COALESCE((published_snapshot->>'testing_day')::boolean, "
        "testing_day, FALSE) = FALSE"
    )
    assert predicate in captured["sql"]
    assert captured["sql"].index(predicate) < captured["sql"].index("ORDER BY day DESC")
    assert captured["params"] == (date(2026, 7, 6), 20)


def test_smart_defaults_replaces_only_trim_saw_and_excludes_full_day_time_off(monkeypatch):
    from zira_dashboard import rotation_suggestions

    roster = [
        person("Jesus Martinez", 3),
        person("Off Person", 3),
        person("Rotation Two", 2),
        person("Repair Default", 2),
    ]
    base = {
        "Trim Saw 1": ["Jesus Martinez", "Off Person"],
        "Repair 1": ["Repair Default"],
    }

    monkeypatch.setattr(
        rotation_suggestions,
        "_load_trim_saw_history",
        lambda day: empty_history(),
    )

    smart = rotation_suggestions.smart_defaults_for_day(
        TARGET_DAY,
        roster,
        base,
        time_off_entries=[{"name": "Off Person", "hours": None}],
    )

    assert smart["Trim Saw 1"] == ["Jesus Martinez", "Rotation Two"]
    assert smart["Repair 1"] == ["Repair Default"]
    assert base["Trim Saw 1"] == ["Jesus Martinez", "Off Person"]


def test_smart_defaults_excludes_people_already_defaulted_elsewhere(monkeypatch):
    from zira_dashboard import rotation_suggestions

    roster = [
        person("Jesus Martinez", 3),
        person("Repair Default", 3),
        person("Rotation Two", 2),
    ]
    base = {
        "Trim Saw 1": ["Jesus Martinez"],
        "Repair 1": ["Repair Default"],
    }

    monkeypatch.setattr(
        rotation_suggestions,
        "_load_trim_saw_history",
        lambda day: empty_history(),
    )

    smart = rotation_suggestions.smart_defaults_for_day(TARGET_DAY, roster, base, [])

    assert smart["Trim Saw 1"] == ["Jesus Martinez", "Rotation Two"]


# ---------- Generic Recycled rotation engine ----------


def _person(
    name: str,
    level: int,
    group: str | None = None,
    *,
    active: bool = True,
    reserve: bool = False,
):
    """A person holding the same level in all three Recycled rotation groups."""
    return staffing.Person(
        name=name,
        active=active,
        reserve=reserve,
        skills={
            "Dismantle": level,
            "Repair": level,
            "Trim Saw": level,
            **({group: level} if group is not None else {}),
        },
    )


@dataclass(frozen=True)
class _BlockEffect:
    """Duck-typed stand-in for Task 3's BlockEffect."""

    locked_people: dict = field(default_factory=dict)
    temporary_extra_people: dict = field(default_factory=dict)
    locked_work_centers: dict = field(default_factory=dict)
    temporary_extra_work_centers: dict = field(default_factory=dict)
    warnings: tuple = ()


def test_exact_center_protocol_never_falls_back_to_sibling_center():
    effect = _BlockEffect(
        locked_work_centers={"Repair 2": ["Trainee"]},
        temporary_extra_work_centers={"Repair 2": ["Trainer"]},
    )

    out = suggest_recycled_assignments(
        day=date(2026, 7, 14),
        mode="normal",
        roster=[
            staffing.Person(name="Trainee", skills={"Repair": 0}),
            staffing.Person(name="Trainer", skills={"Repair": 3}),
        ],
        group_locations={"Repair": ("Repair 1", "Repair 2")},
        group_required_skills={"Repair": ("Repair",)},
        center_minimums={"Repair 1": 0, "Repair 2": 0},
        center_capacities={"Repair 1": 2, "Repair 2": 2},
        runnable_centers={"Repair 1", "Repair 2"},
        block_effects=[effect],
    )

    assert out.assignments["Repair 2"] == ["Trainee", "Trainer"]


def test_exact_center_protocol_does_not_move_trainer_when_trainee_is_manually_locked():
    effect = _BlockEffect(
        locked_work_centers={"Repair 2": ["Trainee"]},
        temporary_extra_work_centers={"Repair 2": ["Trainer"]},
    )

    out = suggest_recycled_assignments(
        day=date(2026, 7, 14),
        mode="normal",
        roster=[
            staffing.Person(name="Trainee", skills={"Repair": 1}),
            staffing.Person(name="Trainer", skills={"Repair": 3}),
        ],
        group_locations={"Repair": ("Repair 1", "Repair 2")},
        group_required_skills={"Repair": ("Repair",)},
        center_minimums={"Repair 1": 0, "Repair 2": 0},
        center_capacities={"Repair 1": 2, "Repair 2": 2},
        runnable_centers={"Repair 1", "Repair 2"},
        locked_assignments={"Repair 1": ["Trainee"]},
        block_effects=[effect],
    )

    assert out.assignments["Repair 1"] == ["Trainee"]
    assert "Trainer" not in out.assigned_people
    assert any("manual assignment owns" in warning for warning in out.warnings)


def test_engine_leaves_two_person_center_empty_when_only_one_qualified_person_exists():
    out = suggest_recycled_assignments(
        day=date(2026, 7, 14), mode="normal",
        roster=[staffing.Person(name="Only Builder", skills={"Hand Build": 3})],
        group_locations={"Hand Build": ("Hand Build #2",)},
        group_required_skills={"Hand Build": ("Hand Build",)},
        center_minimums={"Hand Build #2": 2},
        runnable_centers={"Hand Build #2"},
        history=RecycledHistory(), locked_assignments={}, block_effects=(),
    )

    assert out.assignments.get("Hand Build #2", []) == []
    assert out.complete is False
    assert out.placement_issues[0].code == "center_minimum_unmet"


def test_engine_never_exceeds_static_capacity_to_reach_minimum():
    out = suggest_recycled_assignments(
        day=date(2026, 7, 14), mode="normal",
        roster=[
            staffing.Person(name="A", skills={"Hand Build": 3}),
            staffing.Person(name="B", skills={"Hand Build": 3}),
            staffing.Person(name="C", skills={"Hand Build": 3}),
        ],
        group_locations={"Hand Build": ("Hand Build #1",)},
        group_required_skills={"Hand Build": ("Hand Build",)},
        center_minimums={"Hand Build #1": 3},
        runnable_centers={"Hand Build #1"},
        history=RecycledHistory(), locked_assignments={}, block_effects=(),
    )

    assert out.assignments == {}
    assert out.complete is False
    assert out.placement_issues[0].code == "invalid_center_configuration"


def test_engine_honors_configured_capacity_over_static_location_maximum():
    """A route-supplied maximum can enlarge a static one-person location."""
    roster = [
        staffing.Person(name="A", skills={"Repair": 3}),
        staffing.Person(name="B", skills={"Repair": 3}),
    ]
    common = dict(
        day=date(2026, 7, 14), mode="normal", roster=roster,
        group_locations={"Repair": ("Repair 2",)},
        group_required_skills={"Repair": ("Repair",)},
        center_minimums={"Repair 2": 1},
        runnable_centers={"Repair 2"}, history=RecycledHistory(),
        locked_assignments={}, block_effects=(),
    )

    fallback = suggest_recycled_assignments(**common)
    configured = suggest_recycled_assignments(
        **common, center_capacities={"Repair 2": 2},
    )

    assert fallback.complete is False
    assert fallback.assignments == {}
    assert configured.complete is True
    assert configured.assignments["Repair 2"] == ["A", "B"]


def test_engine_fills_each_minimum_before_optional_capacity():
    out = suggest_recycled_assignments(
        day=date(2026, 7, 14), mode="normal",
        roster=[
            staffing.Person(name="A", skills={"Hand Build": 3}),
            staffing.Person(name="B", skills={"Hand Build": 3}),
            staffing.Person(name="C", skills={"Hand Build": 3}),
        ],
        group_locations={"Hand Build": ("Hand Build #1", "Hand Build #2")},
        group_required_skills={"Hand Build": ("Hand Build",)},
        center_minimums={"Hand Build #1": 2, "Hand Build #2": 1},
        runnable_centers={"Hand Build #1", "Hand Build #2"},
        history=RecycledHistory(), locked_assignments={}, block_effects=(),
    )

    assert len(out.assignments["Hand Build #1"]) == 2
    assert len(out.assignments["Hand Build #2"]) == 1


def test_engine_generates_only_for_runnable_centers():
    out = suggest_recycled_assignments(
        day=date(2026, 7, 14), mode="normal",
        roster=[
            staffing.Person(name="A", skills={"Hand Build": 3}),
            staffing.Person(name="B", skills={"Hand Build": 3}),
        ],
        group_locations={"Hand Build": ("Hand Build #1", "Hand Build #2")},
        group_required_skills={"Hand Build": ("Hand Build",)},
        center_minimums={"Hand Build #1": 1, "Hand Build #2": 1},
        runnable_centers={"Hand Build #2"},
        history=RecycledHistory(), locked_assignments={}, block_effects=(),
    )

    assert out.assignments.get("Hand Build #1", []) == []
    assert out.assignments["Hand Build #2"] == ["A", "B"]


def test_training_block_trainee_requires_a_level_three_partner_to_run():
    effect = _BlockEffect(
        locked_people={"Hand Build": ["Trainee"]},
        temporary_extra_people={},
        warnings=(),
    )
    out = suggest_recycled_assignments(
        day=date(2026, 7, 14), mode="normal",
        roster=[
            staffing.Person(name="Trainee", skills={"Hand Build": 0}),
            staffing.Person(name="Green", skills={"Hand Build": 3}),
        ],
        group_locations={"Hand Build": ("Hand Build #1",)},
        group_required_skills={"Hand Build": ("Hand Build",)},
        center_minimums={"Hand Build #1": 2},
        runnable_centers={"Hand Build #1"},
        history=RecycledHistory(), locked_assignments={}, block_effects=(effect,),
    )

    assert set(out.assignments["Hand Build #1"]) == {"Trainee", "Green"}


def test_training_block_keeps_trainee_without_a_level_three_partner():
    effect = _BlockEffect(locked_people={"Repair": ["Trainee"]})
    out = suggest_recycled_assignments(
        day=date(2026, 7, 14), mode="normal",
        roster=[
            staffing.Person(name="Trainee", skills={"Repair": 0}),
            staffing.Person(name="Not Green", skills={"Repair": 2}),
        ],
        group_locations={"Repair": ("Repair 1",)},
        group_required_skills={"Repair": ("Repair",)},
        center_minimums={"Repair 1": 2},
        center_capacities={"Repair 1": 2},
        runnable_centers={"Repair 1"},
        history=RecycledHistory(), locked_assignments={}, block_effects=(effect,),
    )

    assert out.assignments["Repair 1"] == ["Trainee"]
    assert out.complete is False
    assert out.placement_issues[0].code == "no_safe_complete_crew"


def test_training_block_does_not_overfill_manual_lock_for_green_partner():
    effect = _BlockEffect(locked_people={"Repair": ["Trainee"]})
    out = suggest_recycled_assignments(
        day=date(2026, 7, 14), mode="normal",
        roster=[
            staffing.Person(name="Trainee", skills={"Repair": 0}),
            staffing.Person(name="Manual Level Two", skills={"Repair": 2}),
        ],
        group_locations={"Repair": ("Repair 1",)},
        group_required_skills={"Repair": ("Repair",)},
        center_minimums={"Repair 1": 2},
        runnable_centers={"Repair 1"},
        history=RecycledHistory(),
        locked_assignments={"Repair 1": ["Manual Level Two"]},
        block_effects=(effect,),
    )

    assert out.assignments["Repair 1"] == ["Manual Level Two"]
    assert "Trainee" not in out.assigned_people
    assert "Training block for Repair could not reserve an open work center." in out.warnings


def test_two_training_block_trainees_do_not_overfill_one_person_center():
    effects = (
        _BlockEffect(locked_people={"Repair": ["Trainee A"]}),
        _BlockEffect(locked_people={"Repair": ["Trainee B"]}),
    )
    out = suggest_recycled_assignments(
        day=date(2026, 7, 14), mode="normal",
        roster=[
            staffing.Person(name="Trainee A", skills={"Repair": 0}),
            staffing.Person(name="Trainee B", skills={"Repair": 0}),
        ],
        group_locations={"Repair": ("Repair 1",)},
        group_required_skills={"Repair": ("Repair",)},
        center_minimums={"Repair 1": 2},
        runnable_centers={"Repair 1"},
        history=RecycledHistory(), locked_assignments={}, block_effects=effects,
    )

    assert out.assignments["Repair 1"] == ["Trainee A"]
    assert "Trainee B" not in out.assigned_people
    assert "Training block for Repair could not reserve an open work center." in out.warnings


def test_training_block_reserves_green_before_ordinary_placement():
    effects = (_BlockEffect(locked_people={"Hand Build": ["Trainee"]}),)
    out = suggest_recycled_assignments(
        day=date(2026, 7, 14), mode="normal",
        roster=[
            staffing.Person(name="Trainee", skills={"Hand Build": 0}),
            staffing.Person(name="Green", skills={"Hand Build": 3, "Dismantle": 3}),
            staffing.Person(name="Dismantler Backup", skills={"Dismantle": 3}),
        ],
        group_locations={
            "Hand Build": ("Hand Build #1",),
            "Dismantler": ("Dismantler 1",),
        },
        group_required_skills={"Hand Build": ("Hand Build",), "Dismantler": ("Dismantle",)},
        center_minimums={"Hand Build #1": 2, "Dismantler 1": 1},
        runnable_centers={"Hand Build #1", "Dismantler 1"},
        history=RecycledHistory(), locked_assignments={}, block_effects=effects,
    )

    assert "Green" in out.assignments["Hand Build #1"]
    assert "Green" not in out.assignments.get("Dismantler 1", [])


def test_training_green_reservation_respects_center_capacity():
    effects = (
        _BlockEffect(locked_people={"Hand Build": ["Trainee A"]}),
        _BlockEffect(locked_people={"Hand Build": ["Trainee B"]}),
    )
    out = suggest_recycled_assignments(
        day=date(2026, 7, 14), mode="normal",
        roster=[
            staffing.Person(name="Trainee A", skills={"Hand Build": 0}),
            staffing.Person(name="Trainee B", skills={"Hand Build": 0}),
            staffing.Person(name="Green", skills={"Hand Build": 3}),
        ],
        group_locations={"Hand Build": ("Hand Build #1",)},
        group_required_skills={"Hand Build": ("Hand Build",)},
        center_minimums={"Hand Build #1": 2},
        runnable_centers={"Hand Build #1"},
        history=RecycledHistory(), locked_assignments={}, block_effects=effects,
    )

    assert len(out.assignments["Hand Build #1"]) <= 2
    assert set(out.assignments["Hand Build #1"]) == {"Trainee A", "Trainee B"}
    assert out.complete is False
    assert any(
        issue.person == "Green" and issue.code == "person_no_enabled_qualified_center"
        for issue in out.placement_issues
    )


def test_trim_saw_training_green_reservation_uses_occupant_levels():
    effect = _BlockEffect(locked_people={"Trim Saw": ["Trainee"]})
    out = suggest_recycled_assignments(
        day=date(2026, 7, 14), mode="normal",
        roster=[
            staffing.Person(name="Trainee", skills={"Trim Saw": 0}),
            staffing.Person(name="Green", skills={"Trim Saw": 3}),
        ],
        group_locations={"Trim Saw": ("Trim Saw 1",)},
        group_required_skills={"Trim Saw": ("Trim Saw",)},
        center_minimums={"Trim Saw 1": 2},
        runnable_centers={"Trim Saw 1"},
        history=RecycledHistory(), locked_assignments={}, block_effects=(effect,),
    )

    assert set(out.assignments["Trim Saw 1"]) == {"Trainee", "Green"}
    assert out.warnings == ()




def test_training_mode_pairs_level_one_with_level_three():
    out = suggest_recycled_assignments(
        day=date(2026, 7, 14), mode="training",
        roster=[_person("Green", 3, "Woodpecker"), _person("Learner", 1, "Woodpecker")],
        preferences={"Green": {"Woodpecker": "regular"}, "Learner": {"Woodpecker": "regular"}},
        base_assignments={}, group_locations={"Woodpecker": ("Woodpecker #1",)}, history=RecycledHistory(),
        locked_assignments={}, block_effects=(), training_cap=2,
    )
    assert {"Green", "Learner"} <= set(out.people_for_group("Woodpecker"))


def test_level_zero_is_ignored_without_training_block():
    out = suggest_recycled_assignments(
        day=date(2026, 7, 14), mode="normal", roster=[_person("Zero", 0), _person("Green", 3)],
        preferences={"Zero": {"Repair": "primary"}, "Green": {"Repair": "regular"}},
        base_assignments={}, group_locations={"Repair": ("Repair 1",)}, history=RecycledHistory(),
        locked_assignments={}, block_effects=(), training_cap=2,
    )
    assert "Zero" not in out.assigned_people


def test_repair_center_rotation_uses_least_recent_then_least_frequent():
    history = RecycledHistory(center_counts={("Jordan", "Repair 1"): 1, ("Jordan", "Repair 2"): 1, ("Jordan", "Repair 3"): 1}, last_center_by_person_group={("Jordan", "Repair"): "Repair 3"})
    assert choose_center("Jordan", "Repair", ("Repair 1", "Repair 2", "Repair 3"), history) == "Repair 1"


def test_choose_center_prefers_least_frequent_center_first():
    history = RecycledHistory(
        center_counts={("Jordan", "Repair 1"): 2, ("Jordan", "Repair 2"): 1},
        last_center_by_person_group={("Jordan", "Repair"): "Repair 1"},
    )
    assert choose_center("Jordan", "Repair", ("Repair 1", "Repair 2", "Repair 3"), history) == "Repair 3"


def test_unknown_mode_is_rejected():
    with pytest.raises(ValueError, match="mode"):
        suggest_recycled_assignments(day=date(2026, 7, 14), mode="chaotic", roster=[])




def test_never_preference_keeps_manual_lock_in_place():
    out = suggest_recycled_assignments(
        day=date(2026, 7, 14), mode="normal",
        roster=[_person("Refuser", 3), _person("Backup", 2)],
        preferences={"Refuser": {"Repair": "never"}},
        base_assignments={"Repair 1": ["Refuser"]},
        group_locations={"Repair": ("Repair 1",)},
        history=RecycledHistory(),
        locked_assignments={"Repair 1": ["Refuser"]},
        block_effects=(),
    )
    assert out.assignments["Repair 1"] == ["Refuser"]
    assert out.sources["Repair 1"]["Refuser"] == "manual"










def test_minimum_coverage_rotates_people_across_fully_staffed_equal_centers():
    history = RecycledHistory(
        center_counts={
            ("Alicia", "Repair 1"): 1,
            ("Beatriz", "Repair 2"): 1,
        },
        last_center_by_person_group={
            ("Alicia", "Repair"): "Repair 1",
            ("Beatriz", "Repair"): "Repair 2",
        },
    )
    out = suggest_recycled_assignments(
        day=date(2026, 7, 14), mode="normal",
        roster=[_person("Alicia", 3), _person("Beatriz", 3)],
        group_locations={"Repair": ("Repair 1", "Repair 2")},
        center_minimums={"Repair 1": 1, "Repair 2": 1},
        center_capacities={"Repair 1": 1, "Repair 2": 1},
        runnable_centers={"Repair 1", "Repair 2"},
        history=history,
    )

    assert out.assignments["Repair 1"] == ["Beatriz"]
    assert out.assignments["Repair 2"] == ["Alicia"]


def test_manual_lock_survives_rebuild_and_engine_fills_around_it():
    out = suggest_recycled_assignments(
        day=date(2026, 7, 14), mode="normal",
        roster=[_person("Manual Person", 2), _person("Stale Generated", 3), _person("Fresh", 3)],
        preferences={},
        base_assignments={"Repair 1": ["Manual Person"], "Repair 2": ["Stale Generated"]},
        group_locations={"Repair": ("Repair 1", "Repair 2")},
        center_capacities={"Repair 1": 2, "Repair 2": 1},
        runnable_centers={"Repair 1", "Repair 2"},
        history=RecycledHistory(),
        locked_assignments={"Repair 1": ["Manual Person"]},
        block_effects=(),
    )
    assert out.complete is True
    assert out.assignments["Repair 1"][0] == "Manual Person"
    assert out.sources["Repair 1"]["Manual Person"] == "manual"
    assert "Manual Person" not in out.reasons.get("Repair 1", {})
    # Stale generated inputs are rebuilt, but every available person is placed.
    assert {"Fresh", "Stale Generated"} <= out.assigned_people
    all_names = [name for names in out.assignments.values() for name in names]
    assert all_names.count("Manual Person") == 1


def _duplicate_protected_lock_suggestion(locked_assignments):
    return suggest_recycled_assignments(
        day=date(2026, 7, 14), mode="normal",
        roster=[_person("Duplicated Lock", 3), _person("Backfill A", 3), _person("Backfill B", 3)],
        group_locations={"Repair": ("Repair 1", "Repair 2")},
        center_minimums={"Repair 1": 1, "Repair 2": 1},
        center_capacities={"Repair 1": 2, "Repair 2": 2},
        runnable_centers={"Repair 1", "Repair 2"},
        locked_assignments=locked_assignments,
    )


def _assert_duplicate_protected_lock_blocks_rebuild(out):
    assert out.assignments["Repair 1"][0] == "Duplicated Lock"
    assert out.assignments["Repair 2"][0] == "Duplicated Lock"
    assert out.complete is False
    assert out.reason_codes == {}
    assert out.placement_issues[0].code == "protected_assignment_conflict"
    assert out.placement_issues[0].person == "Duplicated Lock"


def test_duplicate_manual_lock_is_preserved_but_never_counts_twice():
    out = _duplicate_protected_lock_suggestion({
        "Repair 1": ["Duplicated Lock"],
        "Repair 2": ["Duplicated Lock"],
    })
    _assert_duplicate_protected_lock_blocks_rebuild(out)


def test_duplicate_default_lock_is_preserved_but_never_counts_twice(monkeypatch):
    from zira_dashboard.routes import staffing as staffing_route

    monkeypatch.setattr(
        staffing_route.work_centers_store,
        "default_people",
        lambda loc: ["Duplicated Lock"] if loc.name in {"Repair 1", "Repair 2"} else [],
    )
    default_locks = staffing_route._protected_locks(
        {}, {}, allowed_centers={"Repair 1", "Repair 2"}, strict_default_reads=True,
    )
    out = _duplicate_protected_lock_suggestion(default_locks)
    _assert_duplicate_protected_lock_blocks_rebuild(out)


def test_enabled_lock_conflicts_with_preserved_pass_through_assignment():
    out = suggest_recycled_assignments(
        day=date(2026, 7, 14), mode="normal",
        roster=[_person("Duplicated Lock", 3), _person("Backfill", 3)],
        base_assignments={"Disabled Bench": ["Duplicated Lock"]},
        group_locations={"Repair": ("Repair 1",)},
        center_minimums={"Repair 1": 1},
        center_capacities={"Repair 1": 2},
        runnable_centers={"Repair 1"},
        locked_assignments={"Repair 1": ["Duplicated Lock"]},
    )

    assert out.assignments["Disabled Bench"] == ["Duplicated Lock"]
    assert out.assignments["Repair 1"] == ["Duplicated Lock"]
    assert out.complete is False
    assert out.placement_issues[0].code == "protected_assignment_conflict"


def test_one_person_is_never_duplicated_across_centers():
    out = suggest_recycled_assignments(
        day=date(2026, 7, 14), mode="normal", roster=[_person("Multi", 3)], preferences={},
        base_assignments={}, history=RecycledHistory(), locked_assignments={}, block_effects=(),
        group_locations={"Repair": ("Repair 1",)},
        center_minimums={"Repair 1": 1},
        center_capacities={"Repair 1": 1},
        runnable_centers={"Repair 1"},
    )
    all_names = [name for names in out.assignments.values() for name in names]
    assert all_names.count("Multi") == 1


def test_non_recycled_base_assignments_pass_through_untouched():
    base = {"Woodpecker #1": ["New Guy"], "Repair 1": ["Old Generated"]}
    out = suggest_recycled_assignments(
        day=date(2026, 7, 14), mode="normal",
        roster=[_person("New Guy", 3), _person("Fresh", 3)],
        preferences={}, base_assignments=base,
        group_locations={"Repair": ("Repair 1",)},
        history=RecycledHistory(), locked_assignments={}, block_effects=(),
    )
    assert out.assignments["Woodpecker #1"] == ["New Guy"]
    # New Guy already works elsewhere, so the open Repair slot goes to Fresh.
    assert out.assignments["Repair 1"] == ["Fresh"]
    all_names = [name for names in out.assignments.values() for name in names]
    assert all_names.count("New Guy") == 1
    # Inputs are never mutated.
    assert base == {"Woodpecker #1": ["New Guy"], "Repair 1": ["Old Generated"]}


def test_inactive_and_reserve_people_are_not_auto_scheduled():
    out = suggest_recycled_assignments(
        day=date(2026, 7, 14), mode="normal",
        roster=[
            _person("Gone", 3, active=False),
            _person("Reserve", 3, reserve=True),
            _person("Here", 2),
        ],
        preferences={}, base_assignments={}, group_locations={"Repair": ("Repair 1",)},
        history=RecycledHistory(), locked_assignments={}, block_effects=(),
    )
    assert out.assignments["Repair 1"] == ["Here"]
    assert out.assigned_people == {"Here"}


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




def test_empty_assignment_keys_follow_canonical_group_center_order():
    out = suggest_recycled_assignments(
        day=date(2026, 7, 14),
        mode="normal",
        roster=[],
        group_locations={
            "First Group": ("Center Z", "Center A"),
            "Second Group": ("Center M",),
        },
        group_required_skills={
            "First Group": ("First Skill",),
            "Second Group": ("Second Skill",),
        },
        runnable_centers={center for center in ("Center M", "Center A", "Center Z")},
    )

    assert out.complete is False
    assert out.assignments == {}
    assert {issue.code for issue in out.placement_issues} == {"center_minimum_unmet"}


def test_staffed_center_reports_invalid_protected_assignment():
    out = suggest_recycled_assignments(
        day=date(2026, 7, 14),
        mode="normal",
        roster=[
            staffing.Person(name="Protected Zero", skills={"Repair": 0}),
            staffing.Person(name="Qualified", skills={"Repair": 3}),
        ],
        group_locations={"Repair": ("Repair 1",)},
        group_required_skills={"Repair": ("Repair",)},
        locked_assignments={"Repair 1": ("Protected Zero",)},
        center_minimums={"Repair 1": 1},
        center_capacities={"Repair 1": 2},
        runnable_centers={"Repair 1"},
    )

    assert out.assignments["Repair 1"] == ["Protected Zero", "Qualified"]
    assert out.staffed_centers == ("Repair 1",)
    issue = next(
        item for item in out.issues
        if item.code == "protected_assignment_unqualified"
    )
    assert issue.center == "Repair 1"
    assert issue.rejections[0].person == "Protected Zero"
    assert "does not safely count toward minimum coverage" in issue.rejections[0].detail
    assert issue.message in out.warnings


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
    assert out.complete is False
    assert out.placement_issues[0].code == "person_no_enabled_qualified_center"


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








def test_trim_saw_generated_pair_must_be_valid():
    out = suggest_recycled_assignments(
        day=date(2026, 7, 14), mode="normal",
        roster=[_person("Level Two", 2), _person("Level One", 1)],
        preferences={}, base_assignments={},
        group_locations={"Trim Saw": ("Trim Saw 1",)},
        history=RecycledHistory(), locked_assignments={}, block_effects=(),
    )
    # A 2/1 pair is unsafe, so nothing is generated and a warning explains it.
    assert out.assignments.get("Trim Saw 1", []) == []
    assert out.complete is False
    assert out.placement_issues[0].code == "no_safe_complete_crew"


def test_trim_saw_generated_pair_is_placed_when_valid():
    out = suggest_recycled_assignments(
        day=date(2026, 7, 14), mode="normal",
        roster=[_person("Green", 3), _person("Level One", 1)],
        preferences={}, base_assignments={},
        group_locations={"Trim Saw": ("Trim Saw 1",)},
        history=RecycledHistory(), locked_assignments={}, block_effects=(),
    )
    assert out.assignments["Trim Saw 1"] == ["Green", "Level One"]
    assert out.warnings == ()


def test_trim_saw_locked_operator_gets_only_safe_partner():
    out = suggest_recycled_assignments(
        day=date(2026, 7, 14), mode="normal",
        roster=[
            _person("Pinned One", 1),
            _person("Green Partner", 3),
            _person("Level Two", 2, reserve=True),
        ],
        preferences={}, base_assignments={},
        group_locations={"Trim Saw": ("Trim Saw 1",)},
        history=RecycledHistory(),
        locked_assignments={"Trim Saw 1": ["Pinned One"]},
        block_effects=(),
    )
    assert out.assignments["Trim Saw 1"] == ["Pinned One", "Green Partner"]
    assert out.sources["Trim Saw 1"]["Pinned One"] == "manual"
    assert out.sources["Trim Saw 1"]["Green Partner"] == "generated"


def test_trim_saw_locked_single_without_safe_partner_warns():
    out = suggest_recycled_assignments(
        day=date(2026, 7, 14), mode="normal",
        roster=[_person("Pinned One", 1), _person("Level Two", 2)],
        preferences={}, base_assignments={},
        group_locations={"Trim Saw": ("Trim Saw 1",)},
        history=RecycledHistory(),
        locked_assignments={"Trim Saw 1": ["Pinned One"]},
        block_effects=(),
    )
    assert out.assignments["Trim Saw 1"] == ["Pinned One"]
    assert any("Trim Saw 1" in warning for warning in out.warnings)


def test_training_mode_never_seats_third_person_on_trim_saw():
    out = suggest_recycled_assignments(
        day=date(2026, 7, 14), mode="training",
        roster=[_person("Green", 3), _person("One A", 1), _person("One B", 1)],
        preferences={}, base_assignments={},
        group_locations={"Trim Saw": ("Trim Saw 1",)},
        history=RecycledHistory(), locked_assignments={}, block_effects=(),
    )
    # Trim Saw 1 is a hard two-operator center: development placements must
    # not overfill it the way they may overfill single-operator centers.
    assert out.complete is False
    assert out.assignments == {}
    assert set(out.unused_people) == {"Green", "One A", "One B"}


def test_training_mode_never_creates_invalid_trim_saw_pair():
    out = suggest_recycled_assignments(
        day=date(2026, 7, 14), mode="training",
        roster=[_person("Green", 3), _person("Two", 2), _person("One", 1)],
        preferences={}, base_assignments={},
        group_locations={"Trim Saw": ("Trim Saw 1",)},
        history=RecycledHistory(), locked_assignments={}, block_effects=(),
    )
    # Adding the level-1 learner would create a forbidden (2, 1) co-presence.
    assert out.complete is False
    assert out.assignments == {}
    assert set(out.unused_people) == {"Green", "Two", "One"}


def test_optimized_covers_multiple_groups_with_multi_skill_green():
    roster = [
        staffing.Person(name="Alice", skills={"Repair": 3, "Dismantle": 3}),
        staffing.Person(name="Bob", skills={"Dismantle": 3}),
        staffing.Person(name="Carl", skills={"Repair": 2}),
    ]
    out = suggest_recycled_assignments(
        day=date(2026, 7, 14), mode="optimized", roster=roster, preferences={},
        base_assignments={},
        group_locations={"Repair": ("Repair 1",), "Dismantler": ("Dismantler 1",)},
        center_capacities={"Repair 1": 2, "Dismantler 1": 1},
        runnable_centers={"Repair 1", "Dismantler 1"},
        history=RecycledHistory(), locked_assignments={}, block_effects=(),
    )
    # Alice is the only green who can cover Repair, so optimized sends her
    # there and lets Bob cover Dismantler instead of leaving Repair to Carl.
    assert set(out.assignments["Repair 1"]) == {"Alice", "Carl"}
    assert out.assignments["Dismantler 1"] == ["Bob"]


def test_block_effect_with_unknown_group_warns_once():
    effect = _BlockEffect(
        locked_people={"Ghost Group": ["Trainee"]},
        temporary_extra_people={"Ghost Group": ["Trainer"]},
    )
    out = suggest_recycled_assignments(
        day=date(2026, 7, 14), mode="normal",
        roster=[_person("Trainee", 0), _person("Trainer", 3)],
        preferences={}, base_assignments={}, group_locations={"Repair": ("Repair 1",)},
        history=RecycledHistory(), locked_assignments={}, block_effects=(effect,),
    )
    block_warnings = [w for w in out.warnings if "Ghost Group" in w]
    assert len(block_warnings) == 1


def test_training_block_trainee_is_unassigned_when_every_center_is_full():
    effect = _BlockEffect(locked_people={"Repair": ["Trainee"]})
    out = suggest_recycled_assignments(
        day=date(2026, 7, 14), mode="normal",
        roster=[_person("Trainee", 0), _person("Occupier", 3)],
        preferences={}, base_assignments={"Repair 1": ["Occupier"]},
        group_locations={"Repair": ("Repair 1",)},
        history=RecycledHistory(),
        locked_assignments={"Repair 1": ["Occupier"]},
        block_effects=(effect,),
    )
    assert out.assignments["Repair 1"] == ["Occupier"]
    assert "Trainee" not in out.assigned_people
    assert "Training block for Repair could not reserve an open work center." in out.warnings


def test_temporary_training_partner_never_exceeds_center_capacity():
    effect = _BlockEffect(
        locked_people={"Repair": ["Trainee"]},
        temporary_extra_people={"Repair": ["Trainer"]},
    )
    out = suggest_recycled_assignments(
        day=date(2026, 7, 14), mode="normal",
        roster=[_person("Trainee", 0), _person("Trainer", 3)],
        preferences={}, base_assignments={}, group_locations={"Repair": ("Repair 1",)},
        history=RecycledHistory(), locked_assignments={}, block_effects=(effect,),
    )

    assert out.assignments["Repair 1"] == ["Trainee"]
    assert "Trainer" not in out.assigned_people










def test_training_cap_does_not_block_level_three_optional_fill():
    out = suggest_recycled_assignments(
        day=date(2026, 7, 14), mode="training",
        roster=[_person("Green A", 3), _person("Green B", 3)],
        group_locations={"Repair": ("Repair 1",)},
        center_minimums={"Repair 1": 1},
        center_capacities={"Repair 1": 2},
        runnable_centers={"Repair 1"},
        training_cap=0,
    )

    assert set(out.assignments["Repair 1"]) == {"Green A", "Green B"}






def test_training_cap_zero_allows_empty_trim_saw_level_three_pair():
    out = suggest_recycled_assignments(
        day=date(2026, 7, 14), mode="training",
        roster=[
            staffing.Person("Trim Green A", skills={"Trim Saw": 3}),
            staffing.Person("Trim Green B", skills={"Trim Saw": 3}),
        ],
        group_locations={"Trim Saw": ("Trim Saw 1",)},
        group_required_skills={"Trim Saw": ("Trim Saw",)},
        center_minimums={"Trim Saw 1": 0},
        center_capacities={"Trim Saw 1": 2},
        runnable_centers={"Trim Saw 1"},
        training_cap=0,
    )

    assert set(out.assignments["Trim Saw 1"]) == {"Trim Green A", "Trim Green B"}


def test_dismantler_group_schedules_end_to_end():
    roster = [
        staffing.Person(name="Dee", skills={"Dismantle": 3}),
        staffing.Person(name="Dan", skills={"Dismantle": 2}),
    ]
    history = RecycledHistory(
        center_counts={
            ("Dee", "Dismantler 1"): 2,
            ("Dee", "Dismantler 2"): 1,
            ("Dee", "Dismantler 3"): 1,
            ("Dee", "Dismantler 4"): 1,
        },
        last_center_by_person_group={("Dee", "Dismantler"): "Dismantler 4"},
    )
    out = suggest_recycled_assignments(
        day=date(2026, 7, 14), mode="normal", roster=roster,
        preferences={"Dan": {"Dismantler": "primary"}},
        base_assignments={},
        group_locations={
            "Dismantler": ("Dismantler 4", "Dismantler 3", "Dismantler 2", "Dismantler 1"),
        },
        center_minimums={
            "Dismantler 1": 0,
            "Dismantler 2": 0,
            "Dismantler 3": 0,
            "Dismantler 4": 0,
        },
        center_capacities={
            "Dismantler 1": 1,
            "Dismantler 2": 1,
            "Dismantler 3": 1,
            "Dismantler 4": 1,
        },
        runnable_centers={
            "Dismantler 1", "Dismantler 2", "Dismantler 3", "Dismantler 4"
        },
        history=history, locked_assignments={}, block_effects=(),
    )
    assert out.complete is True
    assert set(out.people_for_group("Dismantler")) == {"Dee", "Dan"}


def test_people_for_group_uses_the_engine_group_locations():
    out = suggest_recycled_assignments(
        day=date(2026, 7, 14), mode="normal", roster=[_person("Solo", 3)], preferences={},
        base_assignments={}, group_locations={"Repair": ("Custom Bench",)},
        history=RecycledHistory(), locked_assignments={}, block_effects=(),
    )
    # The suggestion reports groups from the map it was built with, even for
    # center names that do not exist in staffing.LOCATIONS.
    assert out.people_for_group("Repair") == ["Solo"]


def test_generated_assignments_carry_reasons():
    out = suggest_recycled_assignments(
        day=date(2026, 7, 14), mode="normal",
        roster=[_person("Green", 3), _person("Primary Two", 2)],
        preferences={"Primary Two": {"Repair": "primary"}},
        base_assignments={}, group_locations={"Repair": ("Repair 1", "Repair 2")},
        history=RecycledHistory(), locked_assignments={}, block_effects=(),
    )
    reasons = {
        name: reason
        for center_reasons in out.reasons.values()
        for name, reason in center_reasons.items()
    }
    assert reasons == {
        "Green": "Assigned to meet minimum coverage.",
        "Primary Two": "Assigned to meet minimum coverage.",
    }


def test_generic_group_locations_keep_an_under_minimum_hand_build_crew_empty():
    roster = [
        staffing.Person(name="Hand Builder", skills={"Hand Build": 3}),
        staffing.Person(name="Junior Pro", skills={"Junior": 3}),
    ]

    out = suggest_recycled_assignments(
        day=date(2026, 7, 14), mode="normal", roster=roster, preferences={},
        base_assignments={},
        group_locations={
            "Hand Build": ("Hand Build #1",),
            "Junior": ("Junior #1",),
        },
        history=RecycledHistory(), locked_assignments={}, block_effects=(),
    )

    assert out.complete is False
    assert out.assignments == {}
    assert out.unused_people == ("Hand Builder", "Junior Pro")


def test_minimum_skill_precedes_standalone_preference_for_optional_fill():
    roster = [
        staffing.Person("Primary", skills={"Woodpecker": 2}),
        staffing.Person("Regular", skills={"Woodpecker": 3}),
    ]
    out = suggest_recycled_assignments(
        day=TARGET_DAY, mode="normal", roster=roster,
        preferences={"Primary": {"Woodpecker #1": "primary"}},
        base_assignments={},
        group_locations={"Woodpecker #1": ("Woodpecker #1",)},
        group_required_skills={"Woodpecker #1": ("Woodpecker",)},
        history=RecycledHistory(), locked_assignments={}, block_effects=(),
    )
    assert out.assignments["Woodpecker #1"] == ["Regular", "Primary"]


def test_minimum_coverage_center_tie_rotates_across_days():
    """Feeding each day's real suggestion back through the real history
    aggregator rotates minimum placements before the final canonical tie.

    This exercises ``suggest_recycled_assignments`` and
    ``_recycled_history_from_rows`` together across a simulated multi-day run,
    so it is a genuine integration regression, not a restatement of either
    single-shot helper test.
    """
    from zira_dashboard import rotation_suggestions as rs

    group_locations = {"Repair": ("Repair 1", "Repair 2", "Repair 3")}
    roster = [_person("Jordan", 3)]
    rows: list[dict] = []  # accumulated schedule history, newest first
    visited: list[str] = []

    for _ in range(3):
        history = rs._recycled_history_from_rows(rows, group_locations)
        out = suggest_recycled_assignments(
            day=date(2026, 7, 14), mode="normal", roster=roster, preferences={},
            base_assignments={}, group_locations=group_locations,
            center_minimums={"Repair 1": 0, "Repair 2": 0, "Repair 3": 0},
            center_capacities={"Repair 1": 1, "Repair 2": 1, "Repair 3": 1},
            runnable_centers={"Repair 1", "Repair 2", "Repair 3"},
            history=history, locked_assignments={}, block_effects=(),
        )
        assert out.people_for_group("Repair") == ["Jordan"]
        center = next(c for c, names in out.assignments.items() if "Jordan" in names)
        visited.append(center)
        rows.insert(0, {"assignments": {center: ["Jordan"]}})

    assert visited == ["Repair 1", "Repair 2", "Repair 3"]


def test_invalid_minimum_above_capacity_is_configuration_issue_even_with_headcount():
    out = suggest_recycled_assignments(
        day=date(2026, 7, 14), mode="normal",
        roster=[_person("Green A", 3), _person("Green B", 3)],
        group_locations={"Repair": ("Repair 1",)},
        center_minimums={"Repair 1": 2},
        center_capacities={"Repair 1": 1},
        runnable_centers={"Repair 1"},
    )

    assert out.assignments == {}
    assert out.unresolved_centers == ("Repair 1",)
    assert out.placement_issues[0].code == "invalid_center_configuration"


def test_invalid_minimum_above_capacity_preserves_protected_assignment():
    out = suggest_recycled_assignments(
        day=date(2026, 7, 14), mode="normal",
        roster=[_person("Protected", 3), _person("Green", 3)],
        group_locations={"Repair": ("Repair 1",)},
        center_minimums={"Repair 1": 2},
        center_capacities={"Repair 1": 1},
        runnable_centers={"Repair 1"},
        locked_assignments={"Repair 1": ["Protected"]},
    )

    assert out.assignments["Repair 1"] == ["Protected"]
    assert out.complete is False
    assert out.placement_issues[0].code == "invalid_center_configuration"


def test_every_generated_assignment_path_has_a_stable_reason_code_and_text():
    block = _BlockEffect(
        locked_people={"Hand Build": ["Trainee"]},
        temporary_extra_people={"Hand Build": ["Trainer"]},
    )
    blocked = suggest_recycled_assignments(
        day=date(2026, 7, 14), mode="normal",
        roster=[
            staffing.Person("Trainee", skills={"Hand Build": 0}),
            staffing.Person("Trainer", skills={"Hand Build": 3}),
        ],
        group_locations={"Hand Build": ("Hand Build #1",)},
        group_required_skills={"Hand Build": ("Hand Build",)},
        center_minimums={"Hand Build #1": 2},
        center_capacities={"Hand Build #1": 2},
        runnable_centers={"Hand Build #1"},
        block_effects=(block,),
    )
    assert blocked.reason_codes["Hand Build #1"] == {
        "Trainee": "training_block",
        "Trainer": "training_block",
    }

    strongest = suggest_recycled_assignments(
        day=date(2026, 7, 14), mode="optimized",
        roster=[_person("A Green", 3), _person("B Green", 3)],
        group_locations={"Repair": ("Repair 1",)},
        center_minimums={"Repair 1": 1}, center_capacities={"Repair 1": 2},
        runnable_centers={"Repair 1"},
    )
    assert strongest.reason_codes["Repair 1"]["B Green"] == "strongest_coverage"

    primary = suggest_recycled_assignments(
        day=date(2026, 7, 14), mode="normal",
        roster=[_person("A Green", 3), _person("Primary", 2)],
        preferences={"Primary": {"Repair": "primary"}},
        group_locations={"Repair": ("Repair 1",)},
        center_minimums={"Repair 1": 1}, center_capacities={"Repair 1": 2},
        runnable_centers={"Repair 1"},
    )
    assert primary.reason_codes["Repair 1"]["Primary"] == "primary_preference"

    rotated = suggest_recycled_assignments(
        day=date(2026, 7, 14), mode="normal",
        roster=[_person("A Green", 3), _person("Rotator", 2)],
        group_locations={"Repair": ("Repair 1",)},
        center_minimums={"Repair 1": 1}, center_capacities={"Repair 1": 2},
        runnable_centers={"Repair 1"},
    )
    assert rotated.reason_codes["Repair 1"]["Rotator"] == "rotation_fairness"

    developed = suggest_recycled_assignments(
        day=date(2026, 7, 14), mode="training",
        roster=[_person("A Green", 3), _person("Learner", 1)],
        group_locations={"Repair": ("Repair 1",)},
        center_minimums={"Repair 1": 1}, center_capacities={"Repair 1": 2},
        runnable_centers={"Repair 1"}, training_cap=1,
    )
    assert developed.reason_codes["Repair 1"]["Learner"] == "training_development"

    for suggestion in (blocked, strongest, primary, rotated, developed):
        for center, sources in suggestion.sources.items():
            for name, source in sources.items():
                if source == "generated":
                    assert suggestion.reason_codes[center][name]
                    assert suggestion.reasons[center][name]
