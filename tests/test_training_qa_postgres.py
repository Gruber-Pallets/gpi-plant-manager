"""Real-Postgres regressions; opt in with a loopback-only TRAINING_QA_DATABASE_URL.

Each test owns a fresh temporary schema. No production services are called.
"""

from datetime import date
import os
from threading import Event, Thread
from uuid import uuid4

import psycopg2
from psycopg2 import sql
from psycopg2.extensions import parse_dsn, make_dsn
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from zira_dashboard import db, rotation_store, rotation_training, skill_levels, work_centers_store
from zira_dashboard.routes import rotations


@pytest.fixture
def training_db(monkeypatch):
    dsn = os.environ.get("TRAINING_QA_DATABASE_URL")
    if not dsn:
        pytest.skip("set TRAINING_QA_DATABASE_URL to a disposable local database")
    config = parse_dsn(dsn)
    assert config.get("host") in ("localhost", "127.0.0.1", "::1")
    assert config.get("dbname", "").endswith("_test")
    assert not any(config.get(key) for key in ("hostaddr", "service", "servicefile"))
    schema = "training_qa_" + uuid4().hex
    conn = psycopg2.connect(dsn)
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    db.shutdown_pool()
    monkeypatch.setenv("DATABASE_URL", make_dsn(dsn, options=f"-c search_path={schema}"))
    db.init_pool(2, 20)
    db.bootstrap_schema()
    work_centers_store._EFFECTIVE_CACHE.invalidate()
    monkeypatch.setattr(rotation_training.shift_config, "is_workday", lambda d: d.weekday() < 5)
    monkeypatch.setattr(rotation_training.scheduler_time_off, "full_day_off_names", lambda d: set())
    db.execute("INSERT INTO people (id,name,odoo_id) VALUES (1,'Learner',1),(2,'Trainer',2)")
    db.execute(
        "INSERT INTO skills (id,name,skill_type,odoo_id) VALUES (1,'Tablets','Production Skills',1),(2,'New Work Stations','Production Skills',2),(3,'Recycled Work Stations','Production Skills',3),(4,'Forklift Certified','Certifications',4)"
    )
    db.execute("INSERT INTO work_centers (id,name,category) VALUES (1,'Tablets','Supervisor')")
    db.execute(
        "INSERT INTO work_center_required_skills (wc_id,skill_id) VALUES (1,1),(1,2),(1,3),(1,4)"
    )
    db.execute(
        "INSERT INTO person_skills (person_id,skill_id,level) VALUES (1,1,1),(1,2,2),(2,1,3),(2,2,3),(2,3,3),(1,4,1),(2,4,1)"
    )
    monkeypatch.setattr(skill_levels.odoo_client, "set_employee_skill_level", lambda *a: None)
    try:
        yield
    finally:
        db.shutdown_pool()
        work_centers_store._EFFECTIVE_CACHE.invalidate()
        with conn.cursor() as cur:
            cur.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))
        conn.close()


def create(days=3):
    return rotation_store.create_block(
        trainee_id=1,
        trainer_id=2,
        work_center="Tablets",
        start_day=date(2026, 9, 21),
        planned_attended_days=days,
    )


def test_configured_skills_and_completion_roundtrip(training_db):
    block = create()
    assert block.skill_ids == (1, 2, 3)
    rotation_training.complete_block_now(block.id)
    assert rotation_store.get_block(block.id).status == "completed"
    levels = {
        r["skill_id"]: r["level"]
        for r in db.query("SELECT skill_id,level FROM person_skills WHERE person_id=1")
    }
    assert levels == {1: 1, 2: 2, 3: 1, 4: 1}


def test_pause_resume_records_pause_and_extends_schedule(training_db, monkeypatch):
    block = create()
    rotation_store.record_attended_day(block.id, date(2026, 9, 21))
    monkeypatch.setattr(rotation_store, "plant_today", lambda: date(2026, 9, 22))
    rotation_store.pause_block(block.id)
    assert rotation_store.get_block(block.id).status == "paused"
    assert rotation_store.get_block(block.id).paused_on == date(2026, 9, 22)
    monkeypatch.setattr(rotation_store, "plant_today", lambda: date(2026, 9, 25))
    rotation_store.resume_block(block.id)
    current = rotation_store.get_block(block.id)
    assert current.status == "active"
    assert current.day_statuses[date(2026, 9, 22)] == "paused"
    assert rotation_store.attended_day_count(block.id) == 1
    assert rotation_training.planned_block_days(current, {}) == [
        date(2026, 9, 21),
        date(2026, 9, 25),
        date(2026, 9, 28),
    ]


def test_end_and_stale_lifecycle_never_promote(training_db):
    block = create()
    rotation_store.end_block(block.id)
    assert rotation_store.get_block(block.id).status == "ended"
    with pytest.raises(rotation_store.InvalidTrainingBlock):
        rotation_store.resume_block(block.id)
    assert not db.query("SELECT level FROM person_skills WHERE person_id=1 AND skill_id=3")


def test_completion_serializes_with_reconciliation(training_db, monkeypatch):
    block = create()
    entered, release = Event(), Event()
    writes = []

    def odoo_write(*args):
        entered.set()
        assert release.wait(5)
        writes.append(args)

    monkeypatch.setattr(skill_levels.odoo_client, "set_employee_skill_level", odoo_write)
    failures = []

    def complete():
        try:
            rotation_training.complete_block_now(block.id)
        except BaseException as exc:
            failures.append(exc)

    thread = Thread(target=complete)
    thread.start()
    try:
        assert entered.wait(5)
        assert rotation_training.reconcile_blocks(date(2026, 9, 22)) == []
        assert rotation_store.get_block(block.id).status == "completing"
    finally:
        release.set()
        thread.join(5)
    assert failures == []
    assert rotation_store.get_block(block.id).status == "completed"
    assert len(writes) == 1


def test_orphan_completion_repairs_missing_skills(training_db):
    block = create()
    assert rotation_store.claim_early_completion(block.id) == "active"
    assert rotation_training.reconcile_blocks(date(2026, 9, 22)) == [block.id]
    assert (
        db.query("SELECT level FROM person_skills WHERE person_id=1 AND skill_id=3")[0]["level"]
        == 1
    )


def test_odoo_failure_keeps_training_open_and_retryable(training_db, monkeypatch):
    block = create()

    def unavailable(*a):
        raise RuntimeError("simulated offline")

    monkeypatch.setattr(skill_levels.odoo_client, "set_employee_skill_level", unavailable)
    app = FastAPI()
    app.include_router(rotations.router)
    client = TestClient(app)
    response = client.post(f"/api/rotations/training-blocks/{block.id}/complete")
    assert response.status_code == 503
    assert "retry" in response.json()["error"].lower()
    assert rotation_store.get_block(block.id).status == "active"
    monkeypatch.setattr(skill_levels.odoo_client, "set_employee_skill_level", lambda *a: None)
    assert client.post(f"/api/rotations/training-blocks/{block.id}/complete").status_code == 200


def test_edit_and_invalid_input_roundtrip(training_db):
    block = create()
    updated = rotation_store.update_block(
        block.id,
        trainer_id=2,
        work_center="Tablets",
        start_day=date(2026, 9, 22),
        planned_attended_days=5,
    )
    assert updated.start_day == date(2026, 9, 22)
    assert updated.skill_ids == (1, 2, 3)
    app = FastAPI()
    app.include_router(rotations.router)
    client = TestClient(app)
    for days in (0, -1, 1.5, True, 40000):
        response = client.post(
            "/api/rotations/training-blocks",
            json={
                "trainee": "Learner",
                "trainer": "Trainer",
                "work_center": "Tablets",
                "start_day": "2026-09-21",
                "workdays": days,
            },
        )
        assert response.status_code == 422
    assert client.post("/api/rotations/training-blocks/999999/pause").status_code == 409


def test_live_validation_accepts_supervised_pair_in_one_person_center(training_db, monkeypatch):
    from zira_dashboard.routes import staffing as route

    create()
    db.execute("UPDATE work_centers SET max_ops=1 WHERE name='Tablets'")
    work_centers_store._EFFECTIVE_CACHE.invalidate()
    monkeypatch.setattr(route, "_absence_by_day_for_block", lambda *a: {})
    monkeypatch.setattr(
        rotations.staffing_route, "current_view_validation_for_day", lambda **kw: []
    )
    app = FastAPI()
    app.include_router(rotations.router)
    response = TestClient(app).post(
        "/api/rotations/validate-current",
        json={
            "day": "2026-09-21",
            "enabled_work_centers": ["Tablets"],
            "assignments": {"Tablets": ["Learner", "Trainer"]},
        },
    )
    assert response.status_code == 200, response.text


def test_training_does_not_overwrite_matrix_edit_after_initial_read(training_db, monkeypatch):
    block = create()
    original = rotation_training._promotion_skill_rows

    def rows_then_matrix_edit(block):
        rows = original(block)
        skill_levels.set_person_skill_level(1, 3, 3)
        return rows

    monkeypatch.setattr(rotation_training, "_promotion_skill_rows", rows_then_matrix_edit)
    rotation_training.complete_block_now(block.id)
    assert (
        db.query("SELECT level FROM person_skills WHERE person_id=1 AND skill_id=3")[0]["level"]
        == 3
    )


@pytest.mark.parametrize("person_id", [1, 2])
def test_required_certificates_are_prerequisites(training_db, person_id):
    db.execute("DELETE FROM person_skills WHERE person_id=%s AND skill_id=4", (person_id,))
    with pytest.raises(
        rotation_store.InvalidTrainingBlock, match="Both people need Forklift Certified"
    ):
        create()
    assert rotation_store.manageable_blocks() == []


def test_busy_skill_update_fails_quickly_without_an_external_write(training_db):
    with db.cursor() as cur:
        cur.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", ("person-skill:1:3",))
        with pytest.raises(skill_levels.SkillSyncError, match="being updated"):
            skill_levels.promote_untrained_person_skill(1, 3)
        with pytest.raises(skill_levels.SkillSyncError, match="being updated"):
            skill_levels.set_person_skill_level(1, 3, 3)
    assert not db.query("SELECT level FROM person_skills WHERE person_id=1 AND skill_id=3")
