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
