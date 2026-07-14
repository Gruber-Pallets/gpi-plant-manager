from __future__ import annotations

import os
from contextlib import contextmanager
from datetime import date

import pytest


def test_scheduling_preference_targets_group_sibling_centers():
    from zira_dashboard import staffing

    targets = {target.key: target for target in staffing.scheduling_preference_targets()}

    assert targets["Repair"].centers == (
        "Repair 1", "Repair 2", "Repair 3", "Repair 4", "Repair 5",
    )
    assert targets["Hand Build"].centers == (
        "Hand Build #2", "Hand Build #1", "Big Build #1",
    )
    assert targets["Trim Saw"].centers == ("Trim Saw 1",)
    assert targets["Trim Saw"].required_skills == ("Trim Saw",)
    assert targets["Woodpecker #1"].centers == ("Woodpecker #1",)
    assert targets["Woodpecker #1"].required_skills == ("Woodpecker",)


def test_eligible_targets_require_every_required_skill():
    from zira_dashboard import staffing

    person = staffing.Person(
        "Qualified", skills={"Repair": 1, "Loading": 1, "CPUs/VDOs": 1}
    )
    keys = {
        target.key for target in staffing.eligible_scheduling_preference_targets(person)
    }
    assert "Repair" in keys
    assert "Loading/Jockeying" not in keys

    person.skills["Trailer Jockeying"] = 1
    assert "Loading/Jockeying" in {
        target.key for target in staffing.eligible_scheduling_preference_targets(person)
    }


def test_tablets_skill_qualifies_for_tablets_scheduling_target():
    from zira_dashboard import staffing

    person = staffing.Person("Tablets Operator", skills={"Tablets": 1})

    targets = {
        target.key: target
        for target in staffing.eligible_scheduling_preference_targets(person)
    }

    assert targets["Tablets"].required_skills == ("Tablets",)


def test_dismantle_skill_qualifies_for_dismantler_scheduling_group():
    """Odoo's Dismantle skill backs the Dismantler scheduling group."""
    from zira_dashboard import staffing

    person = staffing.Person("Dismantle Green", skills={"Dismantle": 3})

    assert "Dismantler" in {
        target.key for target in staffing.eligible_scheduling_preference_targets(person)
    }


def test_save_preference_accepts_derived_scheduling_target(monkeypatch):
    from zira_dashboard import rotation_store

    monkeypatch.setattr(rotation_store.db, "execute", lambda *_args, **_kwargs: None)

    preference = rotation_store.save_preference(17, "Woodpecker #1", "primary")

    assert preference.rotation_group == "Woodpecker #1"


def test_missing_rotation_preference_is_regular(monkeypatch):
    from zira_dashboard import rotation_store

    monkeypatch.setattr(rotation_store.db, "query", lambda *_args, **_kwargs: [])
    assert rotation_store.preference_for({}, 17, "Repair") == "regular"


def test_training_block_rejects_non_green_trainer():
    from zira_dashboard import rotation_store

    with pytest.raises(rotation_store.InvalidTrainingBlock, match="level 3"):
        rotation_store.validate_block(level=0, trainer_level=2, workdays=5)


@pytest.mark.parametrize("target", ["Woodpecker", "Master Recycler"])
def test_training_block_rejects_non_recycled_target_before_persisting(monkeypatch, target):
    from zira_dashboard import rotation_store

    queries = []

    def fake_query(sql, params=None):
        queries.append((sql, params))
        if "FROM skills" in sql:
            return [{"name": target}]
        raise AssertionError("Training block insert must not run for an invalid target")

    monkeypatch.setattr(rotation_store.db, "query", fake_query)

    with pytest.raises(rotation_store.InvalidTrainingBlock, match="Recycled"):
        rotation_store.create_block(
            trainee_id=1,
            trainer_id=2,
            skill_id=3,
            start_day=date(2026, 7, 14),
            planned_attended_days=5,
        )

    assert len(queries) == 1


@pytest.mark.parametrize("target", ["Dismantler", "Repair", "Trim Saw"])
def test_training_block_accepts_each_recycled_target(monkeypatch, target):
    from zira_dashboard import rotation_store

    calls = []

    def fake_query(sql, params=None):
        calls.append((sql, params))
        if "FROM skills" in sql:
            return [{"name": target}]
        if "trainee_level" in sql:
            return [{"trainee_level": 0, "trainer_level": 3}]
        if "INSERT INTO rotation_training_blocks" in sql:
            return [{
                "id": 9,
                "trainee_name": "Jordan",
                "trainer_name": "Taylor",
                "skill": target,
                "start_day": date(2026, 7, 14),
                "planned_attended_days": 5,
                "status": "active",
            }]
        raise AssertionError(f"Unexpected query: {sql}")

    monkeypatch.setattr(rotation_store.db, "query", fake_query)

    block = rotation_store.create_block(
        trainee_id=1,
        trainer_id=2,
        skill_id=3,
        start_day=date(2026, 7, 14),
        planned_attended_days=5,
    )

    assert block.skill == target
    assert len(calls) == 3


def test_schedule_metadata_round_trips(monkeypatch):
    from zira_dashboard import db, staffing

    schedule = staffing.Schedule(
        day=date(2026, 7, 14),
        assignments={"Repair 1": ["Jordan"]},
        rotation_mode="training",
        assignment_sources={"Repair 1": {"Jordan": "manual"}},
    )
    executed: list[tuple[str, tuple | None]] = []

    class Cursor:
        def execute(self, sql, params=None):
            executed.append((sql, params))

    @contextmanager
    def fake_cursor():
        yield Cursor()

    monkeypatch.setattr(db, "cursor", fake_cursor)
    staffing.save_schedule(schedule)

    insert_sql, insert_params = executed[0]
    assert "recycled_rotation_mode" in insert_sql
    assert "assignment_sources" in insert_sql
    assert insert_params is not None
    assert "training" in insert_params
    assert '{"Repair 1": {"Jordan": "manual"}}' in insert_params

    def fake_query(sql, params=None):
        if "FROM schedules" in sql:
            return [{
                "day": schedule.day,
                "published": False,
                "testing_day": False,
                "notes": "",
                "custom_hours": None,
                "published_snapshot": None,
                "recycled_rotation_mode": "training",
                "assignment_sources": {"Repair 1": {"Jordan": "manual"}},
            }]
        return []

    monkeypatch.setattr(db, "query", fake_query)
    hydrated = staffing._load_schedule_from_db(schedule.day)
    assert hydrated.rotation_mode == "training"
    assert hydrated.assignment_sources == {"Repair 1": {"Jordan": "manual"}}


@pytest.mark.parametrize(
    "sources",
    [
        [],
        {"Repair 1": []},
        {"Repair 1": {"Jordan": "automatic"}},
        {"Repair 1": {1: "manual"}},
    ],
)
def test_schedule_rejects_malformed_assignment_sources_before_persisting(monkeypatch, sources):
    from zira_dashboard import db, staffing

    called = False

    @contextmanager
    def fake_cursor():
        nonlocal called
        called = True
        yield object()

    monkeypatch.setattr(db, "cursor", fake_cursor)

    with pytest.raises(ValueError, match="assignment_sources"):
        staffing.save_schedule(staffing.Schedule(day=date(2026, 7, 14), assignment_sources=sources))

    assert called is False


def test_schedule_manual_and_generated_assignment_sources_round_trip(monkeypatch):
    from zira_dashboard import db, staffing

    sources = {"Repair 1": {"Jordan": "manual", "Taylor": "generated"}}
    schedule = staffing.Schedule(day=date(2026, 7, 14), assignment_sources=sources)
    executed: list[tuple[str, tuple | None]] = []

    class Cursor:
        def execute(self, sql, params=None):
            executed.append((sql, params))

    @contextmanager
    def fake_cursor():
        yield Cursor()

    monkeypatch.setattr(db, "cursor", fake_cursor)
    staffing.save_schedule(schedule)

    assert executed[0][1][-1] == '{"Repair 1": {"Jordan": "manual", "Taylor": "generated"}}'

    def fake_query(sql, params=None):
        if "FROM schedules" in sql:
            return [{
                "day": schedule.day,
                "published": False,
                "testing_day": False,
                "notes": "",
                "custom_hours": None,
                "published_snapshot": None,
                "recycled_rotation_mode": "normal",
                "assignment_sources": sources,
            }]
        return []

    monkeypatch.setattr(db, "query", fake_query)
    assert staffing._load_schedule_from_db(schedule.day).assignment_sources == sources


def test_schedule_default_assignment_source_round_trips_through_persistence_validation(monkeypatch):
    """Defaults-only resets must persist their distinct assignment source."""
    from zira_dashboard import db, staffing

    sources = {"Repair 1": {"Jordan": "default"}}
    schedule = staffing.Schedule(day=date(2026, 7, 14), assignment_sources=sources)
    executed: list[tuple[str, tuple | None]] = []

    class Cursor:
        def execute(self, sql, params=None):
            executed.append((sql, params))

    @contextmanager
    def fake_cursor():
        yield Cursor()

    monkeypatch.setattr(db, "cursor", fake_cursor)
    staffing.save_schedule(schedule)

    assert executed[0][1][-1] == '{"Repair 1": {"Jordan": "default"}}'

    def fake_query(sql, params=None):
        if "FROM schedules" in sql:
            return [{
                "day": schedule.day,
                "published": False,
                "testing_day": False,
                "notes": "",
                "custom_hours": None,
                "published_snapshot": None,
                "recycled_rotation_mode": "normal",
                "assignment_sources": sources,
            }]
        return []

    monkeypatch.setattr(db, "query", fake_query)
    assert staffing._load_schedule_from_db(schedule.day).assignment_sources == sources


@pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"), reason="needs Postgres"
)
def test_block_accessors_round_trip_against_real_schema():
    """DB-backed guard for the new store SQL. The monkeypatched unit tests
    above can't catch a column/join typo in active_blocks / resolved_days /
    mark_completed or the added b.trainee_id / b.skill_id, so exercise them
    against the real schema. Skips cleanly when Postgres isn't configured."""
    from zira_dashboard import db, rotation_store

    db.bootstrap_schema()

    trainee_name = "ZZ Rotation Trainee"
    trainer_name = "ZZ Rotation Trainer"
    skill_name = "ZZ Rotation Skill"

    def _cleanup():
        db.execute(
            "DELETE FROM rotation_training_blocks WHERE trainee_id IN "
            "(SELECT id FROM people WHERE name IN (%s, %s))",
            (trainee_name, trainer_name),
        )
        db.execute("DELETE FROM people WHERE name IN (%s, %s)", (trainee_name, trainer_name))
        db.execute("DELETE FROM skills WHERE name = %s", (skill_name,))

    _cleanup()
    try:
        trainee_id = db.query(
            "INSERT INTO people (name, active) VALUES (%s, TRUE) RETURNING id",
            (trainee_name,),
        )[0]["id"]
        trainer_id = db.query(
            "INSERT INTO people (name, active) VALUES (%s, TRUE) RETURNING id",
            (trainer_name,),
        )[0]["id"]
        skill_id = db.query(
            "INSERT INTO skills (name, skill_type) VALUES (%s, 'Production Skills') RETURNING id",
            (skill_name,),
        )[0]["id"]
        block_id = db.query(
            "INSERT INTO rotation_training_blocks "
            "(trainee_id, trainer_id, skill_id, start_day, planned_attended_days, status) "
            "VALUES (%s, %s, %s, %s, %s, 'active') RETURNING id",
            (trainee_id, trainer_id, skill_id, date(2026, 7, 14), 2),
        )[0]["id"]

        # active_blocks(): joins resolve and the added id columns populate.
        matches = [b for b in rotation_store.active_blocks() if b.id == block_id]
        assert len(matches) == 1
        block = matches[0]
        assert block.trainee_id == trainee_id
        assert block.skill_id == skill_id
        assert block.trainee_name == trainee_name
        assert block.trainer_name == trainer_name
        assert block.skill == skill_name

        # resolved_days(): records round-trip with day + status.
        rotation_store.record_attended_day(block_id, date(2026, 7, 14), "attended")
        rotation_store.record_attended_day(block_id, date(2026, 7, 15), "absent")
        assert [(d.day, d.status) for d in rotation_store.resolved_days(block_id)] == [
            (date(2026, 7, 14), "attended"),
            (date(2026, 7, 15), "absent"),
        ]

        # record_attended_day is a pure recorder: reaching the planned attended
        # count must NOT auto-complete the block (reconcile owns completion).
        rotation_store.record_attended_day(block_id, date(2026, 7, 16), "attended")
        assert any(b.id == block_id for b in rotation_store.active_blocks())

        # mark_completed(): flips status; the block drops out of active_blocks().
        rotation_store.mark_completed(block_id)
        assert not any(b.id == block_id for b in rotation_store.active_blocks())
    finally:
        _cleanup()
