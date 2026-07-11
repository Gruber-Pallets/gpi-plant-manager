from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import pytest


@pytest.fixture(autouse=True)
def _default_work_week(monkeypatch):
    """Stub the global-schedule lookup so the pure day math never touches
    Postgres. Defaults to a Mon-Fri working week."""
    from zira_dashboard import rotation_training

    monkeypatch.setattr(
        rotation_training.schedule_store,
        "current",
        lambda: SimpleNamespace(work_weekdays=frozenset({0, 1, 2, 3, 4})),
    )


def _block(
    *,
    start_day: date = date(2026, 7, 14),
    planned_attended_days: int = 5,
    trainee_name: str = "Trainee",
    trainer_name: str = "Trainer",
    skill: str = "Repair",
    status: str = "active",
):
    from zira_dashboard import rotation_store

    return rotation_store.TrainingBlock(
        id=1,
        trainee_name=trainee_name,
        trainer_name=trainer_name,
        skill=skill,
        start_day=start_day,
        planned_attended_days=planned_attended_days,
        status=status,
    )


# ---------- planned_block_days (pure) ----------


def test_planned_block_days_are_consecutive_workdays():
    from zira_dashboard import rotation_training

    days = rotation_training.planned_block_days(
        _block(start_day=date(2026, 7, 14), planned_attended_days=3), {}
    )
    assert days == [date(2026, 7, 14), date(2026, 7, 15), date(2026, 7, 16)]


def test_planned_block_days_skip_weekends():
    from zira_dashboard import rotation_training

    days = rotation_training.planned_block_days(
        _block(start_day=date(2026, 7, 16), planned_attended_days=3), {}
    )
    # Thu 16, Fri 17, then skip Sat/Sun to Mon 20.
    assert days == [date(2026, 7, 16), date(2026, 7, 17), date(2026, 7, 20)]


def test_absence_does_not_consume_training_day():
    from zira_dashboard import rotation_training

    days = rotation_training.planned_block_days(
        _block(start_day=date(2026, 7, 14), planned_attended_days=3),
        {date(2026, 7, 15): {"Trainee"}},
    )
    assert days == [date(2026, 7, 14), date(2026, 7, 16), date(2026, 7, 17)]


def test_absence_of_a_different_person_still_consumes_the_day():
    from zira_dashboard import rotation_training

    days = rotation_training.planned_block_days(
        _block(start_day=date(2026, 7, 14), planned_attended_days=3),
        {date(2026, 7, 15): {"Someone Else"}},
    )
    assert days == [date(2026, 7, 14), date(2026, 7, 15), date(2026, 7, 16)]


def test_planned_block_days_honor_a_six_day_work_week(monkeypatch):
    from zira_dashboard import rotation_training

    monkeypatch.setattr(
        rotation_training.schedule_store,
        "current",
        lambda: SimpleNamespace(work_weekdays=frozenset({0, 1, 2, 3, 4, 5})),
    )
    days = rotation_training.planned_block_days(
        _block(start_day=date(2026, 7, 16), planned_attended_days=3), {}
    )
    # With Saturday working, Thu 16, Fri 17, Sat 18 all count.
    assert days == [date(2026, 7, 16), date(2026, 7, 17), date(2026, 7, 18)]


# ---------- effect_for_day (pure) ----------


def test_first_attended_day_pairs_trainee_and_green_trainer():
    from zira_dashboard import rotation_training

    effect = rotation_training.effect_for_day(
        _block(start_day=date(2026, 7, 14)), date(2026, 7, 14)
    )
    assert effect.locked_people == {"Repair": ["Trainee"]}
    assert effect.temporary_extra_people == {"Repair": ["Trainer"]}
    assert tuple(effect.warnings) == ()


def test_later_attended_day_reserves_trainee_only():
    from zira_dashboard import rotation_training

    effect = rotation_training.effect_for_day(
        _block(start_day=date(2026, 7, 14)), date(2026, 7, 16)
    )
    assert effect.locked_people == {"Repair": ["Trainee"]}
    assert effect.temporary_extra_people == {}
    assert tuple(effect.warnings) == ()


def test_non_planned_day_returns_empty_effect():
    from zira_dashboard import rotation_training

    # Saturday is not a working day, so it is never a planned block day.
    effect = rotation_training.effect_for_day(
        _block(start_day=date(2026, 7, 14)), date(2026, 7, 18)
    )
    assert effect.locked_people == {}
    assert effect.temporary_extra_people == {}
    assert tuple(effect.warnings) == ()


def test_day_after_the_block_window_returns_empty_effect():
    from zira_dashboard import rotation_training

    # planned days for a 5-day block starting Tue 7/14 are 14,15,16,17,20.
    effect = rotation_training.effect_for_day(
        _block(start_day=date(2026, 7, 14), planned_attended_days=5), date(2026, 7, 21)
    )
    assert effect.locked_people == {}
    assert effect.temporary_extra_people == {}


def test_absent_day_produces_no_lock():
    from zira_dashboard import rotation_training

    effect = rotation_training.effect_for_day(
        _block(start_day=date(2026, 7, 14)),
        date(2026, 7, 15),
        absence_by_day={date(2026, 7, 15): {"Trainee"}},
    )
    assert effect.locked_people == {}
    assert effect.temporary_extra_people == {}


def test_manual_trainee_conflict_warns_and_does_not_lock():
    from zira_dashboard import rotation_training

    effect = rotation_training.effect_for_day(
        _block(start_day=date(2026, 7, 14)),
        date(2026, 7, 14),
        manual_assignees={"Trainee"},
    )
    assert effect.locked_people == {}
    assert effect.temporary_extra_people == {}
    assert any("Trainee" in w for w in effect.warnings)


def test_manual_trainer_conflict_on_day_one_locks_trainee_only():
    from zira_dashboard import rotation_training

    effect = rotation_training.effect_for_day(
        _block(start_day=date(2026, 7, 14)),
        date(2026, 7, 14),
        manual_assignees={"Trainer"},
    )
    assert effect.locked_people == {"Repair": ["Trainee"]}
    assert effect.temporary_extra_people == {}
    assert any("Trainer" in w for w in effect.warnings)


def test_block_effect_shape_is_consumable_by_the_suggestion_engine():
    from zira_dashboard import rotation_suggestions, rotation_training, staffing

    effect = rotation_training.effect_for_day(
        _block(start_day=date(2026, 7, 14)), date(2026, 7, 14)
    )
    # The engine treats block effects duck-typed via these three attributes; a
    # round trip through the real engine proves the shape lines up.
    out = rotation_suggestions.suggest_recycled_assignments(
        day=date(2026, 7, 14),
        mode="normal",
        roster=[
            staffing.Person(name="Trainee", active=True, skills={"Repair": 0}),
            staffing.Person(name="Trainer", active=True, skills={"Repair": 3}),
        ],
        group_locations={"Repair": ("Repair 1", "Repair 2")},
        block_effects=[effect],
    )
    assert "Trainee" in out.assigned_people
    assert "Trainer" in out.assigned_people


# ---------- reconcile_blocks (DB only through rotation_store + skill_levels) ----------


def _attended(day: date = date(2026, 7, 14)):
    return SimpleNamespace(day=day, status="attended")


def _absent(day: date = date(2026, 7, 15)):
    return SimpleNamespace(day=day, status="absent")


def test_reconcile_promotes_once_and_returns_block_id(monkeypatch):
    from zira_dashboard import rotation_training

    calls: list[tuple] = []
    completed: list[int] = []
    block = SimpleNamespace(
        id=42, trainee_id=17, skill_id=9, planned_attended_days=5, status="active"
    )

    monkeypatch.setattr(rotation_training.rotation_store, "active_blocks", lambda: [block])
    monkeypatch.setattr(
        rotation_training.rotation_store, "resolved_days", lambda _bid: [_attended()] * 5
    )
    monkeypatch.setattr(
        rotation_training.rotation_store, "mark_completed", lambda bid: completed.append(bid)
    )
    monkeypatch.setattr(
        rotation_training.skill_levels,
        "set_person_skill_level",
        lambda *args: calls.append(args),
    )

    assert rotation_training.reconcile_blocks(date(2026, 7, 21)) == [42]
    assert calls == [(17, 9, 1)]
    assert completed == [42]


def test_reconcile_marks_completed_before_it_would_re_promote(monkeypatch):
    """With a real store, once a block is marked completed it is no longer
    returned by ``active_blocks`` and so is never promoted twice."""
    from zira_dashboard import rotation_training

    calls: list[tuple] = []
    store_state = {
        "block": SimpleNamespace(
            id=42, trainee_id=17, skill_id=9, planned_attended_days=5, status="active"
        )
    }

    def fake_active_blocks():
        block = store_state["block"]
        return [block] if block.status == "active" else []

    def fake_mark_completed(bid):
        if store_state["block"].id == bid:
            store_state["block"].status = "completed"

    monkeypatch.setattr(rotation_training.rotation_store, "active_blocks", fake_active_blocks)
    monkeypatch.setattr(
        rotation_training.rotation_store, "resolved_days", lambda _bid: [_attended()] * 5
    )
    monkeypatch.setattr(rotation_training.rotation_store, "mark_completed", fake_mark_completed)
    monkeypatch.setattr(
        rotation_training.skill_levels,
        "set_person_skill_level",
        lambda *args: calls.append(args),
    )

    assert rotation_training.reconcile_blocks(date(2026, 7, 21)) == [42]
    assert rotation_training.reconcile_blocks(date(2026, 7, 21)) == []
    assert calls == [(17, 9, 1)]


def test_reconcile_does_not_promote_before_enough_attended_days(monkeypatch):
    from zira_dashboard import rotation_training

    calls: list[tuple] = []
    block = SimpleNamespace(
        id=42, trainee_id=17, skill_id=9, planned_attended_days=5, status="active"
    )
    # Four attended + several absent days: still short of the requested five.
    resolved = [_attended()] * 4 + [_absent()] * 3

    monkeypatch.setattr(rotation_training.rotation_store, "active_blocks", lambda: [block])
    monkeypatch.setattr(rotation_training.rotation_store, "resolved_days", lambda _bid: resolved)
    monkeypatch.setattr(
        rotation_training.rotation_store, "mark_completed", lambda bid: calls.append(("mark", bid))
    )
    monkeypatch.setattr(
        rotation_training.skill_levels,
        "set_person_skill_level",
        lambda *args: calls.append(args),
    )

    assert rotation_training.reconcile_blocks(date(2026, 7, 21)) == []
    assert calls == []


def test_record_then_reconcile_is_the_single_completion_owner(monkeypatch):
    """Full flow: the pure recorder logs attended days without completing the
    block; reconcile then promotes exactly once and marks it completed; a second
    reconcile is a no-op."""
    from zira_dashboard import rotation_store, rotation_training

    calls: list[tuple] = []
    state = {
        "block": SimpleNamespace(
            id=5, trainee_id=11, skill_id=9, planned_attended_days=3, status="active"
        ),
        "days": [],
    }

    def fake_record(block_id, day, status="attended"):
        # Mirror the real pure recorder: upsert the day, never touch status.
        state["days"] = [d for d in state["days"] if d.day != day]
        state["days"].append(SimpleNamespace(day=day, status=status))

    def fake_active_blocks():
        block = state["block"]
        return [block] if block.status == "active" else []

    def fake_mark_completed(bid):
        if state["block"].id == bid and state["block"].status == "active":
            state["block"].status = "completed"

    monkeypatch.setattr(rotation_training.rotation_store, "record_attended_day", fake_record)
    monkeypatch.setattr(rotation_training.rotation_store, "active_blocks", fake_active_blocks)
    monkeypatch.setattr(
        rotation_training.rotation_store, "resolved_days", lambda _bid: list(state["days"])
    )
    monkeypatch.setattr(rotation_training.rotation_store, "mark_completed", fake_mark_completed)
    monkeypatch.setattr(
        rotation_training.skill_levels, "set_person_skill_level", lambda *a: calls.append(a)
    )

    for day in (date(2026, 7, 14), date(2026, 7, 15), date(2026, 7, 16)):
        rotation_store.record_attended_day(5, day, "attended")

    # Recording the requested attended days must NOT complete the block.
    assert state["block"].status == "active"

    assert rotation_training.reconcile_blocks(date(2026, 7, 21)) == [5]
    assert calls == [(11, 9, 1)]
    assert state["block"].status == "completed"

    # Idempotent: the completed block is no longer active, so no re-promotion.
    assert rotation_training.reconcile_blocks(date(2026, 7, 21)) == []
    assert calls == [(11, 9, 1)]


def test_reconcile_continues_past_a_failed_promotion(monkeypatch):
    """One block's Odoo failure must not abort the pass: later blocks still
    promote, and the failed block stays active (not completed) to retry."""
    from zira_dashboard import rotation_training, skill_levels

    completed: list[int] = []
    block_a = SimpleNamespace(
        id=1, trainee_id=10, skill_id=9, planned_attended_days=2, status="active"
    )
    block_b = SimpleNamespace(
        id=2, trainee_id=20, skill_id=9, planned_attended_days=2, status="active"
    )

    def failing_writer(person_id, skill_id, level):
        if person_id == 10:
            raise skill_levels.SkillSyncError("odoo down")

    monkeypatch.setattr(
        rotation_training.rotation_store, "active_blocks", lambda: [block_a, block_b]
    )
    monkeypatch.setattr(
        rotation_training.rotation_store, "resolved_days", lambda _bid: [_attended()] * 2
    )
    monkeypatch.setattr(
        rotation_training.rotation_store, "mark_completed", lambda bid: completed.append(bid)
    )
    monkeypatch.setattr(
        rotation_training.skill_levels, "set_person_skill_level", failing_writer
    )

    assert rotation_training.reconcile_blocks(date(2026, 7, 21)) == [2]
    assert completed == [2]  # block_a not completed -> retried next pass


def test_reconcile_counts_only_attended_days(monkeypatch):
    from zira_dashboard import rotation_training

    calls: list[tuple] = []
    block = SimpleNamespace(
        id=7, trainee_id=3, skill_id=4, planned_attended_days=2, status="active"
    )
    # Mapping-style records (dict) must also count, proving robust access.
    resolved = [{"status": "attended"}, {"status": "absent"}, {"status": "attended"}]

    monkeypatch.setattr(rotation_training.rotation_store, "active_blocks", lambda: [block])
    monkeypatch.setattr(rotation_training.rotation_store, "resolved_days", lambda _bid: resolved)
    monkeypatch.setattr(rotation_training.rotation_store, "mark_completed", lambda bid: None)
    monkeypatch.setattr(
        rotation_training.skill_levels,
        "set_person_skill_level",
        lambda *args: calls.append(args),
    )

    assert rotation_training.reconcile_blocks(date(2026, 7, 21)) == [7]
    assert calls == [(3, 4, 1)]


# ---------- end-to-end: completion promotes once and never repeats ----------


def _completed_candidate_block():
    """A real, completion-ready block: 5 planned attended days, still active."""
    from zira_dashboard import rotation_store

    return rotation_store.TrainingBlock(
        id=42,
        trainee_name="Learner",
        trainer_name="Green",
        skill="Repair",
        start_day=date(2026, 7, 14),
        planned_attended_days=5,
        status="active",
        trainee_id=17,
        skill_id=9,
    )


def _attended_day():
    from zira_dashboard import rotation_store

    return rotation_store.TrainingBlockDay(day=date(2026, 7, 14), status="attended")


def test_completed_training_block_promotes_to_one_and_never_repeats(monkeypatch):
    """End-to-end promotion: a block whose attended days meet its plan promotes
    the trainee to level 1 exactly once, then is never promoted again.

    The fakes are STATEFUL to mirror the real store contract: ``mark_completed``
    removes the block from the active set (that is how idempotency is achieved,
    not via any internal already-promoted flag), so the second reconcile sees an
    empty active set and does nothing.
    """
    from zira_dashboard import rotation_training

    calls: list[tuple] = []
    state = {"active": [_completed_candidate_block()]}

    monkeypatch.setattr(
        rotation_training.skill_levels,
        "set_person_skill_level",
        lambda *a: calls.append(a),
    )
    monkeypatch.setattr(
        rotation_training.rotation_store, "active_blocks", lambda: list(state["active"])
    )
    monkeypatch.setattr(
        rotation_training.rotation_store,
        "resolved_days",
        lambda _bid: [_attended_day()] * 5,
    )
    monkeypatch.setattr(
        rotation_training.rotation_store,
        "mark_completed",
        lambda bid: state.__setitem__(
            "active", [b for b in state["active"] if b.id != bid]
        ),
    )

    assert rotation_training.reconcile_blocks(date(2026, 7, 21)) == [42]
    assert rotation_training.reconcile_blocks(date(2026, 7, 21)) == []
    assert calls == [(17, 9, 1)]
