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


def _person(name: str, level: int, *, active: bool = True, reserve: bool = False):
    """A person holding the same level in all three Recycled rotation groups."""
    return staffing.Person(
        name=name,
        active=active,
        reserve=reserve,
        skills={"Dismantler": level, "Repair": level, "Trim Saw": level},
    )


@dataclass(frozen=True)
class _BlockEffect:
    """Duck-typed stand-in for Task 3's BlockEffect."""

    locked_people: dict = field(default_factory=dict)
    temporary_extra_people: dict = field(default_factory=dict)
    warnings: tuple = ()


def test_normal_mode_uses_primary_preference_before_regular():
    out = suggest_recycled_assignments(
        day=date(2026, 7, 14), mode="normal", roster=[_person("Primary", 3), _person("Regular", 3)],
        preferences={"Primary": {"Repair": "primary"}, "Regular": {"Repair": "regular"}},
        base_assignments={}, history=RecycledHistory(), locked_assignments={}, block_effects=[],
    )
    assert out.assignments["Repair 1"] == ["Primary"]


def test_training_mode_pairs_level_one_with_level_three():
    out = suggest_recycled_assignments(
        day=date(2026, 7, 14), mode="training", roster=[_person("Green", 3), _person("Learner", 1)],
        preferences={"Green": {"Repair": "regular"}, "Learner": {"Repair": "regular"}},
        base_assignments={}, group_locations={"Repair": ("Repair 1",)}, history=RecycledHistory(),
        locked_assignments={}, block_effects=(), training_cap=2,
    )
    assert {"Green", "Learner"} <= set(out.people_for_group("Repair"))


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


def test_never_preference_blocks_generated_placement():
    out = suggest_recycled_assignments(
        day=date(2026, 7, 14), mode="normal",
        roster=[_person("Refuser", 3), _person("Backup", 2)],
        preferences={"Refuser": {"Repair": "never"}},
        base_assignments={}, group_locations={"Repair": ("Repair 1",)},
        history=RecycledHistory(), locked_assignments={}, block_effects=(),
    )
    assert "Refuser" not in out.assigned_people
    assert out.assignments["Repair 1"] == ["Backup"]


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


def test_optimized_mode_prefers_level_three_over_preference():
    roster = [_person("Green Occasional", 3), _person("Two Primary", 2)]
    preferences = {
        "Green Occasional": {"Repair": "occasional"},
        "Two Primary": {"Repair": "primary"},
    }
    out = suggest_recycled_assignments(
        day=date(2026, 7, 14), mode="optimized", roster=roster, preferences=preferences,
        base_assignments={}, group_locations={"Repair": ("Repair 1",)},
        history=RecycledHistory(), locked_assignments={}, block_effects=(),
    )
    assert out.assignments["Repair 1"] == ["Green Occasional"]


def test_normal_mode_lets_strong_preference_outweigh_one_level():
    roster = [_person("Green Occasional", 3), _person("Two Primary", 2)]
    preferences = {
        "Green Occasional": {"Repair": "occasional"},
        "Two Primary": {"Repair": "primary"},
    }
    out = suggest_recycled_assignments(
        day=date(2026, 7, 14), mode="normal", roster=roster, preferences=preferences,
        base_assignments={}, group_locations={"Repair": ("Repair 1",)},
        history=RecycledHistory(), locked_assignments={}, block_effects=(),
    )
    assert out.assignments["Repair 1"] == ["Two Primary"]


def test_normal_mode_rests_person_with_heavy_recent_group_history():
    roster = [_person("Alicia", 3), _person("Beatriz", 3)]
    history = RecycledHistory(
        group_counts={("Alicia", "Repair"): 4},
        most_recent_group_names={"Repair": {"Alicia"}},
    )
    out = suggest_recycled_assignments(
        day=date(2026, 7, 14), mode="normal", roster=roster, preferences={},
        base_assignments={}, group_locations={"Repair": ("Repair 1",)},
        history=history, locked_assignments={}, block_effects=(),
    )
    assert out.assignments["Repair 1"] == ["Beatriz"]


def test_engine_rotates_centers_using_history():
    history = RecycledHistory(
        center_counts={("Jordan", "Repair 1"): 2, ("Jordan", "Repair 2"): 1},
        last_center_by_person_group={("Jordan", "Repair"): "Repair 2"},
    )
    out = suggest_recycled_assignments(
        day=date(2026, 7, 14), mode="normal", roster=[_person("Jordan", 3)], preferences={},
        base_assignments={}, group_locations={"Repair": ("Repair 1", "Repair 2", "Repair 3")},
        history=history, locked_assignments={}, block_effects=(),
    )
    assert out.assignments["Repair 3"] == ["Jordan"]


def test_manual_lock_survives_rebuild_and_engine_fills_around_it():
    out = suggest_recycled_assignments(
        day=date(2026, 7, 14), mode="normal",
        roster=[_person("Manual Person", 2), _person("Stale Generated", 3), _person("Fresh", 3)],
        preferences={},
        base_assignments={"Repair 1": ["Manual Person"], "Repair 2": ["Stale Generated"]},
        group_locations={"Repair": ("Repair 1", "Repair 2")},
        history=RecycledHistory(),
        locked_assignments={"Repair 1": ["Manual Person"]},
        block_effects=(),
    )
    assert out.assignments["Repair 1"] == ["Manual Person"]
    assert out.sources["Repair 1"]["Manual Person"] == "manual"
    assert "Manual Person" not in out.reasons.get("Repair 1", {})
    # The stale generated pick is rebuilt; the best remaining candidate wins.
    assert out.assignments["Repair 2"] == ["Fresh"]
    assert out.sources["Repair 2"]["Fresh"] == "generated"
    all_names = [name for names in out.assignments.values() for name in names]
    assert all_names.count("Manual Person") == 1


def test_one_person_is_never_duplicated_across_centers():
    out = suggest_recycled_assignments(
        day=date(2026, 7, 14), mode="normal", roster=[_person("Multi", 3)], preferences={},
        base_assignments={}, history=RecycledHistory(), locked_assignments={}, block_effects=(),
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


def test_training_cap_limits_development_placements():
    roster = [
        _person("Green", 3),
        _person("Learner A", 1),
        _person("Learner B", 1),
        _person("Learner C", 1),
    ]
    out = suggest_recycled_assignments(
        day=date(2026, 7, 14), mode="training", roster=roster, preferences={},
        base_assignments={}, group_locations={"Repair": ("Repair 1",)},
        history=RecycledHistory(), locked_assignments={}, block_effects=(),
    )
    assert out.assignments["Repair 1"] == ["Green", "Learner A", "Learner B"]
    assert "Learner C" not in out.assigned_people
    assert out.reasons["Repair 1"]["Learner A"] == "training pair"

    capped = suggest_recycled_assignments(
        day=date(2026, 7, 14), mode="training", roster=roster, preferences={},
        base_assignments={}, group_locations={"Repair": ("Repair 1",)},
        history=RecycledHistory(), locked_assignments={}, block_effects=(), training_cap=1,
    )
    assert capped.assignments["Repair 1"] == ["Green", "Learner A"]


def test_training_development_requires_level_three_in_group():
    out = suggest_recycled_assignments(
        day=date(2026, 7, 14), mode="training",
        roster=[_person("Senior Two", 2), _person("Learner", 1)],
        preferences={}, base_assignments={}, group_locations={"Repair": ("Repair 1",)},
        history=RecycledHistory(), locked_assignments={}, block_effects=(),
    )
    assert out.assignments["Repair 1"] == ["Senior Two"]
    assert "Learner" not in out.assigned_people


def test_block_effect_reserves_trainee_and_pairs_trainer():
    effect = _BlockEffect(
        locked_people={"Repair": ["Trainee"]},
        temporary_extra_people={"Repair": ["Trainer"]},
        warnings=("Trainee was absent Monday; block extended.",),
    )
    out = suggest_recycled_assignments(
        day=date(2026, 7, 14), mode="normal",
        roster=[_person("Trainee", 0), _person("Trainer", 3), _person("Other", 3)],
        preferences={}, base_assignments={},
        group_locations={"Repair": ("Repair 1", "Repair 2")},
        history=RecycledHistory(), locked_assignments={}, block_effects=(effect,),
    )
    # The level-0 trainee is only eligible through the block, and the day-one
    # trainer pairs into the same center even though it exceeds normal staffing.
    assert out.assignments["Repair 1"] == ["Trainee", "Trainer"]
    assert out.sources["Repair 1"]["Trainee"] == "generated"
    assert out.sources["Repair 1"]["Trainer"] == "generated"
    assert out.reasons["Repair 1"]["Trainee"] == "training block"
    assert out.reasons["Repair 1"]["Trainer"] == "training pair"
    assert out.assignments["Repair 2"] == ["Other"]
    assert "Trainee was absent Monday; block extended." in out.warnings


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
    assert any("Trim Saw 1" in warning for warning in out.warnings)


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
        roster=[_person("Pinned One", 1), _person("Green Partner", 3), _person("Level Two", 2)],
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
    assert out.assignments["Trim Saw 1"] == ["Green", "One A"]
    assert "One B" not in out.assigned_people


def test_training_mode_never_creates_invalid_trim_saw_pair():
    out = suggest_recycled_assignments(
        day=date(2026, 7, 14), mode="training",
        roster=[_person("Green", 3), _person("Two", 2), _person("One", 1)],
        preferences={}, base_assignments={},
        group_locations={"Trim Saw": ("Trim Saw 1",)},
        history=RecycledHistory(), locked_assignments={}, block_effects=(),
    )
    # Adding the level-1 learner would create a forbidden (2, 1) co-presence.
    assert out.assignments["Trim Saw 1"] == ["Green", "Two"]
    assert "One" not in out.assigned_people
    levels = {"Green": 3, "Two": 2, "One": 1}
    seated = out.assignments["Trim Saw 1"]
    assert all(
        _valid_trim_saw_pair(levels[a], levels[b])
        for i, a in enumerate(seated)
        for b in seated[i + 1:]
    )


def test_optimized_covers_multiple_groups_with_multi_skill_green():
    roster = [
        staffing.Person(name="Alice", skills={"Repair": 3, "Dismantler": 3}),
        staffing.Person(name="Bob", skills={"Dismantler": 3}),
        staffing.Person(name="Carl", skills={"Repair": 2}),
    ]
    out = suggest_recycled_assignments(
        day=date(2026, 7, 14), mode="optimized", roster=roster, preferences={},
        base_assignments={},
        group_locations={"Repair": ("Repair 1",), "Dismantler": ("Dismantler 1",)},
        history=RecycledHistory(), locked_assignments={}, block_effects=(),
    )
    # Alice is the only green who can cover Repair, so optimized sends her
    # there and lets Bob cover Dismantler instead of leaving Repair to Carl.
    assert out.assignments["Repair 1"] == ["Alice"]
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


def test_block_effect_overfills_when_every_center_is_full():
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
    # The block reservation must be honored even when the group is full.
    assert out.assignments["Repair 1"] == ["Occupier", "Trainee"]
    assert out.sources["Repair 1"]["Trainee"] == "generated"
    assert out.reasons["Repair 1"]["Trainee"] == "training block"


def test_training_cap_zero_blocks_all_development_placements():
    out = suggest_recycled_assignments(
        day=date(2026, 7, 14), mode="training",
        roster=[_person("Green", 3), _person("Learner", 1)],
        preferences={}, base_assignments={}, group_locations={"Repair": ("Repair 1",)},
        history=RecycledHistory(), locked_assignments={}, block_effects=(), training_cap=0,
    )
    assert out.assignments["Repair 1"] == ["Green"]
    assert "Learner" not in out.assigned_people


def test_dismantler_group_schedules_end_to_end():
    roster = [
        staffing.Person(name="Dee", skills={"Dismantler": 3}),
        staffing.Person(name="Dan", skills={"Dismantler": 2}),
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
        history=history, locked_assignments={}, block_effects=(),
    )
    # Dee rotates onto a least-worked center that is not her most recent one.
    assert out.assignments["Dismantler 2"] == ["Dee"]
    assert out.assignments["Dismantler 1"] == ["Dan"]
    assert "Dee" not in out.reasons.get("Dismantler 2", {})
    assert out.reasons["Dismantler 1"]["Dan"] == "primary Dismantler operator"
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
    assert "Green" not in reasons
    assert reasons["Primary Two"] == "primary Repair operator"


def test_generic_group_locations_can_schedule_new_work_centers():
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

    assert out.assignments["Hand Build #1"] == ["Hand Builder"]
    assert out.assignments["Junior #1"] == ["Junior Pro"]


def test_normal_mode_rotates_one_green_through_every_repair_center_over_days():
    """End-to-end fairness (design goal 4): feeding each day's real suggestion
    back through the real history aggregator makes a single green cycle
    Repair 1 -> Repair 2 -> Repair 3 across days rather than parking on Repair 1.

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
            history=history, locked_assignments={}, block_effects=(),
        )
        assert out.people_for_group("Repair") == ["Jordan"]
        center = next(c for c, names in out.assignments.items() if "Jordan" in names)
        visited.append(center)
        rows.insert(0, {"assignments": {center: ["Jordan"]}})

    assert visited == ["Repair 1", "Repair 2", "Repair 3"]
