from __future__ import annotations

from contextlib import contextmanager
from datetime import date

import pytest


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
