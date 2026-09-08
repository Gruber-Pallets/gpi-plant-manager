"""Real transaction checks for historical completion repair (disposable Postgres)."""
import os
from datetime import UTC, datetime

import pytest

from zira_dashboard import db, feedback_store

pytestmark = pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="needs local Postgres")
NOW = datetime(2026, 8, 21, tzinfo=UTC)


@pytest.fixture
def imported_feedback():
    db.init_pool()
    db.bootstrap_schema()
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO feedback(message,status,lifecycle_origin,odoo_task_id,projection_version,"
            "legacy_lifecycle_migrated_at,updated_at) "
            "VALUES ('repair transaction fixture','requested','legacy_project_task',3078,2,%s,%s) RETURNING id",
            (NOW, NOW),
        )
        feedback_id = cur.fetchone()["id"]
    yield feedback_id
    with db.cursor() as cur:
        cur.execute("DELETE FROM feedback_odoo_sync WHERE feedback_id=%s", (feedback_id,))
        cur.execute("DELETE FROM feedback WHERE id=%s", (feedback_id,))


def repair(feedback_id):
    feedback_store.repair_legacy_completion(
        feedback_id=feedback_id, expected_odoo_task_id=3078, expected_projection_version=2,
        expected_status="requested", expected_migrated_at=NOW, now=NOW,
    )


def test_missing_sync_rolls_back_feedback_change(imported_feedback):
    with pytest.raises(feedback_store.InvalidTransition):
        repair(imported_feedback)
    row = db.query("SELECT status,projection_version FROM feedback WHERE id=%s", (imported_feedback,))[0]
    assert row == {"status": "requested", "projection_version": 2}


def test_repair_enqueues_once_and_rejects_stale_retry(imported_feedback):
    with db.cursor() as cur:
        cur.execute("INSERT INTO feedback_odoo_sync(feedback_id,desired_version,last_synced_version,state) "
                    "VALUES (%s,2,2,'idle')", (imported_feedback,))
    repair(imported_feedback)
    row = db.query("SELECT f.status,f.finished_at,f.finished_by,f.projection_version,s.desired_version,"
                   "s.last_synced_version FROM feedback f JOIN feedback_odoo_sync s ON s.feedback_id=f.id "
                   "WHERE f.id=%s", (imported_feedback,))[0]
    assert row == {"status": "completed", "finished_at": None, "finished_by": None,
                   "projection_version": 3, "desired_version": 3, "last_synced_version": 2}
    with pytest.raises(feedback_store.InvalidTransition):
        repair(imported_feedback)
