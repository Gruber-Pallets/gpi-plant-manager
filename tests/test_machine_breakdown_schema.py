"""machine_breakdowns / breakdown_snoozes tables + production_daily.excluded_minutes
(Postgres). Mirrors tests/test_inbox_open_items.py's fixture pattern."""
import os
from datetime import datetime, timezone

import pytest

from zira_dashboard import db

pytestmark = pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="needs Postgres")


@pytest.fixture(autouse=True)
def _clean():
    db.bootstrap_schema()
    db.execute("DELETE FROM breakdown_snoozes WHERE person_name = 'Test Person'")
    db.execute("DELETE FROM machine_breakdowns WHERE wc_name = 'Test WC'")
    db.execute("DELETE FROM production_daily WHERE wc_name = 'Test WC'")
    yield
    db.execute("DELETE FROM breakdown_snoozes WHERE person_name = 'Test Person'")
    db.execute("DELETE FROM machine_breakdowns WHERE wc_name = 'Test WC'")
    db.execute("DELETE FROM production_daily WHERE wc_name = 'Test WC'")


def test_machine_breakdowns_round_trips():
    now = datetime.now(timezone.utc)
    rows = db.query(
        "INSERT INTO machine_breakdowns (wc_name, day, detected_stop_utc, source) "
        "VALUES (%s, %s, %s, %s) RETURNING id",
        ("Test WC", now.date(), now, "auto"),
    )
    incident_id = rows[0]["id"]
    fetched = db.query(
        "SELECT wc_name, source, resolved_at, resolution, resume_utc "
        "FROM machine_breakdowns WHERE id = %s",
        (incident_id,),
    )
    assert fetched[0]["wc_name"] == "Test WC"
    assert fetched[0]["source"] == "auto"
    assert fetched[0]["resolved_at"] is None
    assert fetched[0]["resolution"] is None


def test_breakdown_snoozes_round_trips():
    now = datetime.now(timezone.utc)
    rows = db.query(
        "INSERT INTO machine_breakdowns (wc_name, day, detected_stop_utc, source) "
        "VALUES (%s, %s, %s, %s) RETURNING id",
        ("Test WC", now.date(), now, "auto"),
    )
    incident_id = rows[0]["id"]
    db.execute(
        "INSERT INTO breakdown_snoozes (breakdown_id, person_name, snooze_until) "
        "VALUES (%s, %s, %s)",
        (incident_id, "Test Person", now),
    )
    fetched = db.query(
        "SELECT person_name FROM breakdown_snoozes WHERE breakdown_id = %s",
        (incident_id,),
    )
    assert fetched[0]["person_name"] == "Test Person"


def test_production_daily_has_excluded_minutes_column():
    db.execute(
        "INSERT INTO production_daily (day, emp_id, name, wc_name, units, downtime, "
        "hours, days_worked, excluded_minutes) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) "
        "ON CONFLICT (day, emp_id, wc_name) DO UPDATE SET excluded_minutes = EXCLUDED.excluded_minutes",
        (datetime.now(timezone.utc).date(), "test-emp", "Test Person", "Test WC",
         10.0, 0.0, 7.0, 1.0, 42.5),
    )
    fetched = db.query(
        "SELECT excluded_minutes FROM production_daily WHERE wc_name = 'Test WC'"
    )
    assert float(fetched[0]["excluded_minutes"]) == 42.5


def test_wc_time_attributions_has_breakdown_id_column():
    db.execute(
        "INSERT INTO wc_time_attributions (day, wc_name, person_name, start_utc, "
        "source, breakdown_id) VALUES (%s, %s, %s, %s, %s, %s)",
        (datetime.now(timezone.utc).date(), "Test WC", "Test Person",
         datetime.now(timezone.utc), "breakdown", 999),
    )
    fetched = db.query(
        "SELECT breakdown_id FROM wc_time_attributions WHERE wc_name = 'Test WC' "
        "AND person_name = 'Test Person'"
    )
    assert fetched[0]["breakdown_id"] == 999
    db.execute("DELETE FROM wc_time_attributions WHERE wc_name = 'Test WC'")
