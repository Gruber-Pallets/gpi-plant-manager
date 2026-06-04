"""Schema for the missing-work-center alert. Postgres-backed."""

import os

import pytest

from zira_dashboard import db

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="needs Postgres",
)


def test_cache_table_round_trips():
    db.bootstrap_schema()
    db.execute(
        "INSERT INTO missing_wc_cache (id, snapshot, refreshed_at) "
        "VALUES (1, %s::jsonb, now()) "
        "ON CONFLICT (id) DO UPDATE SET snapshot = EXCLUDED.snapshot",
        ('[{"att_id": 1}]',),
    )
    rows = db.query("SELECT snapshot FROM missing_wc_cache WHERE id = 1")
    assert rows and rows[0]["snapshot"] == [{"att_id": 1}]


def test_resolved_table_upserts():
    db.bootstrap_schema()
    db.execute("DELETE FROM missing_wc_resolved WHERE attendance_id = %s", (999001,))
    db.execute(
        "INSERT INTO missing_wc_resolved (attendance_id, action, name) "
        "VALUES (%s, 'dismissed', 'X') "
        "ON CONFLICT (attendance_id) DO UPDATE SET action = EXCLUDED.action",
        (999001,),
    )
    rows = db.query("SELECT action FROM missing_wc_resolved WHERE attendance_id = %s", (999001,))
    assert rows and rows[0]["action"] == "dismissed"
    db.execute("DELETE FROM missing_wc_resolved WHERE attendance_id = %s", (999001,))
