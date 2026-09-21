"""Regression coverage from the 2026-09-21 training process QA."""

from contextlib import nullcontext
from datetime import date
from types import SimpleNamespace

import pytest
from zira_dashboard import rotation_store, rotation_training, work_centers_store


@pytest.fixture(autouse=True)
def _no_database_lock(monkeypatch):
    monkeypatch.setattr(
        rotation_training.skill_levels,
        "promote_untrained_person_skill",
        lambda pid, sid: rotation_training.skill_levels.set_person_skill_level(pid, sid, 1),
    )
    monkeypatch.setattr(rotation_store, "completion_guard", lambda _: nullcontext(True))


def test_partial_skills_use_configured_requirements_and_exclude_certifications(monkeypatch):
    monkeypatch.setattr(
        work_centers_store,
        "required_skills",
        lambda loc: ["Tablets", "Recycled Work Stations", "Forklift Certified"],
    )
    saved = []

    def query(sql, params=None):
        if "FROM skills" in sql:
            return [
                {"id": 1, "name": "Tablets", "skill_type": "Production Skills"},
                {"id": 2, "name": "Recycled Work Stations", "skill_type": "Production Skills"},
                {"id": 3, "name": "Forklift Certified", "skill_type": "Certifications"},
            ]
        if "trainee_level" in sql:
            return [{"trainee_level": 1 if params[1] in (1, 3) else 0, "trainer_level": 3}]
        if "INSERT INTO rotation_training_blocks" in sql:
            saved.append(params)
            return [
                {
                    "id": 1,
                    "trainee_name": "Learner",
                    "trainer_name": "Trainer",
                    "skill": "Tablets",
                    "start_day": date(2026, 9, 21),
                    "planned_attended_days": 5,
                    "status": "active",
                    "skill_ids": params[4],
                }
            ]
        return [{"name": "Learner"}]

    monkeypatch.setattr(rotation_store.db, "query", query)
    block = rotation_store.create_block(
        trainee_id=10,
        trainer_id=20,
        work_center="Tablets",
        start_day=date(2026, 9, 21),
        planned_attended_days=5,
    )
    assert block.skill_ids == (1, 2)
    assert len(saved) == 1


def test_certification_only_center_cannot_start_training(monkeypatch):
    monkeypatch.setattr(work_centers_store, "required_skills", lambda loc: ["CDL"])
    monkeypatch.setattr(
        rotation_store.db,
        "query",
        lambda *a: [{"id": 3, "name": "CDL", "skill_type": "Certifications"}],
    )
    with pytest.raises(rotation_store.InvalidTrainingBlock, match="no trainable skills"):
        rotation_store.create_block(
            trainee_id=10,
            trainer_id=20,
            work_center="Truck Driver",
            start_day=date(2026, 9, 21),
            planned_attended_days=5,
        )


def test_completion_preserves_existing_levels_and_certifications(monkeypatch):
    block = SimpleNamespace(
        id=1, trainee_id=10, skill_id=1, skill_ids=(1, 2, 3, 4), status="active"
    )
    monkeypatch.setattr(rotation_store, "completion_guard", lambda _: nullcontext(True))
    monkeypatch.setattr(rotation_store, "get_block", lambda _: block)
    monkeypatch.setattr(rotation_store, "claim_early_completion", lambda _: "active")
    monkeypatch.setattr(rotation_store, "mark_completed", lambda _: None)
    monkeypatch.setattr(
        rotation_store.db,
        "query",
        lambda *a: [
            {"id": 1, "level": 0, "skill_type": "Production Skills"},
            {"id": 2, "level": 2, "skill_type": "Production Skills"},
            {"id": 3, "level": 3, "skill_type": "Production Skills"},
            {"id": 4, "level": 0, "skill_type": "Certifications"},
        ],
    )
    writes = []
    monkeypatch.setattr(
        rotation_training.skill_levels, "set_person_skill_level", lambda *args: writes.append(args)
    )
    rotation_training.complete_block_now(1)
    assert writes == [(10, 1, 1)]


def test_daily_reconciliation_counts_each_day_once_and_only_pairs_day_one(monkeypatch):
    block = rotation_store.TrainingBlock(
        id=1,
        trainee_name="Learner",
        trainer_name="Trainer",
        trainee_id=10,
        skill_id=1,
        skill="Repair",
        start_day=date(2026, 9, 21),
        planned_attended_days=5,
        status="active",
        work_center="Repair 1",
    )
    rows = []
    monkeypatch.setattr(rotation_training.shift_config, "is_workday", lambda d: d.weekday() < 5)
    monkeypatch.setattr(rotation_store, "resolved_days", lambda _: list(rows))
    monkeypatch.setattr(
        rotation_store,
        "record_attended_day",
        lambda _, d, status: rows.append(rotation_store.TrainingBlockDay(d, status)),
    )
    monkeypatch.setattr(rotation_training.scheduler_time_off, "full_day_off_names", lambda _: set())

    def schedule(d):
        names = ["Learner", "Trainer"] if d == block.start_day else ["Learner"]
        return SimpleNamespace(
            assignments={"Repair 1": names},
            assignment_sources={"Repair 1": {n: "generated" for n in names}},
        )

    monkeypatch.setattr(rotation_training.staffing, "load_schedule", schedule)
    for day in (22, 23, 24, 25, 26):
        rotation_training._record_elapsed_day_outcomes(block, date(2026, 9, day))
    assert len(rows) == 5
    assert all(r.status == "attended" for r in rows)


def test_conflict_and_pause_extend_the_scheduling_window(monkeypatch):
    block = SimpleNamespace(
        id=1,
        trainee_name="Learner",
        trainer_name="Trainer",
        skill="Repair",
        work_center="Repair 1",
        start_day=date(2026, 9, 21),
        planned_attended_days=2,
        status="active",
        day_statuses={
            date(2026, 9, 21): "conflict",
            date(2026, 9, 22): "paused",
            date(2026, 9, 23): "attended",
        },
    )
    monkeypatch.setattr(rotation_training.shift_config, "is_workday", lambda d: d.weekday() < 5)
    assert rotation_training.planned_block_days(block, {}) == [date(2026, 9, 23), date(2026, 9, 24)]
    effect = rotation_training.effect_for_day(block, date(2026, 9, 24))
    assert effect.locked_work_centers == {"Repair 1": ["Learner"]}
    assert effect.temporary_extra_work_centers == {}


def test_edit_cannot_transfer_earned_days_to_another_work_center(monkeypatch):
    block = SimpleNamespace(
        id=1,
        status="active",
        trainee_id=10,
        work_center="Repair 1",
        start_day=date(2026, 9, 21),
        skill_ids=(1,),
    )
    monkeypatch.setattr(rotation_store, "get_block", lambda _: block)
    monkeypatch.setattr(rotation_store, "attended_day_count", lambda _: 4)
    monkeypatch.setattr(
        rotation_store,
        "resolved_days",
        lambda _: [rotation_store.TrainingBlockDay(date(2026, 9, 21), "attended")],
    )
    monkeypatch.setattr(rotation_store, "_validated_protocol_skills", lambda **kw: (2,))
    monkeypatch.setattr(
        rotation_store.db,
        "query",
        lambda *a: [
            dict(
                id=1,
                trainee_name="Learner",
                trainer_name="Trainer",
                skill="Tablets",
                start_day=block.start_day,
                planned_attended_days=5,
                status="active",
                skill_ids=[2],
            )
        ],
    )
    with pytest.raises(rotation_store.InvalidTrainingBlock, match="new training"):
        rotation_store.update_block(
            1,
            trainer_id=20,
            work_center="Tablets",
            start_day=block.start_day,
            planned_attended_days=5,
        )


@pytest.mark.parametrize("acquired", [False, True])
def test_completion_claim_is_not_proof_of_finished_skill_write(monkeypatch, acquired):
    block = SimpleNamespace(id=1, status="completing", trainee_id=10, skill_id=1, skill_ids=(1,))
    monkeypatch.setattr(
        rotation_store, "completion_guard", lambda _: nullcontext(acquired), raising=False
    )
    monkeypatch.setattr(rotation_store, "get_block", lambda _: block)
    monkeypatch.setattr(rotation_store, "completing_blocks", lambda: [block])
    monkeypatch.setattr(rotation_store, "active_blocks", lambda: [])
    monkeypatch.setattr(
        rotation_training,
        "_promotion_skill_rows",
        lambda _: [dict(id=1, level=0, skill_type="Production Skills")],
    )
    events = []
    monkeypatch.setattr(
        rotation_training.skill_levels,
        "set_person_skill_level",
        lambda *a: events.append("promote"),
    )
    monkeypatch.setattr(rotation_store, "mark_completed", lambda _: events.append("complete"))
    rotation_training.reconcile_blocks(date(2026, 9, 21))
    assert events == (["promote", "complete"] if acquired else [])


@pytest.mark.parametrize("needs_partner", [True, False])
def test_validation_requires_trainer_only_on_first_attended_day(needs_partner):
    from zira_dashboard import current_schedule_validation, staffing

    issues = current_schedule_validation.validate_current_assignments(
        roster=[staffing.Person("Learner", skills={"Repair": 0})],
        assignments={"Repair 1": ["Learner"]},
        enabled_centers={"Repair 1"},
        locations=[staffing.location_by_name("Repair 1")],
        minimums={"Repair 1": 1},
        capacities={"Repair 1": 1},
        required_skills={"Repair 1": ("Repair",)},
        full_day_off_names=set(),
        trim_saw_centers=set(),
        training_trainees_by_center={"Repair 1": {"Learner"}},
        training_requires_partner_by_center={"Repair 1": {"Learner"}} if needs_partner else {},
        exact_defaults={},
        group_defaults={},
        user_group_centers={},
    )
    assert ("training_partner_missing" in {issue.code for issue in issues}) is needs_partner
    if not needs_partner:
        assert issues == ()


def test_training_does_not_bypass_a_missing_certificate():
    from zira_dashboard import current_schedule_validation, staffing

    issues = current_schedule_validation.validate_current_assignments(
        roster=[staffing.Person("Learner", skills={"Repair": 0})],
        assignments={"Repair 1": ["Learner"]},
        enabled_centers={"Repair 1"},
        locations=[staffing.location_by_name("Repair 1")],
        minimums={"Repair 1": 1},
        capacities={"Repair 1": 1},
        required_skills={"Repair 1": ("Repair", "Forklift Certified")},
        full_day_off_names=set(),
        trim_saw_centers=set(),
        training_trainees_by_center={"Repair 1": {"Learner"}},
        training_requires_partner_by_center={},
        certification_skills={"Forklift Certified"},
        exact_defaults={},
        group_defaults={},
        user_group_centers={},
    )
    assert "assignment_unqualified" in {issue.code for issue in issues}
    assert "center_minimum_unmet" in {issue.code for issue in issues}
