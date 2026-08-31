"""machine_breakdowns / breakdown_snoozes store (Postgres). Mirrors
tests/test_inbox_open_items.py's fixture pattern."""
import os
from datetime import datetime, timedelta, timezone

import pytest

from zira_dashboard import db, machine_breakdown

pytestmark = pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="needs Postgres")

WC = "Test Dismantler"


@pytest.fixture(autouse=True)
def _clean():
    db.bootstrap_schema()
    db.execute("DELETE FROM machine_breakdowns WHERE wc_name = %s", (WC,))
    yield
    db.execute("DELETE FROM machine_breakdowns WHERE wc_name = %s", (WC,))


def test_open_incident_and_get_open_incident():
    now = datetime.now(timezone.utc)
    incident_id = machine_breakdown.open_incident(WC, now.date(), now, source="auto")
    row = machine_breakdown.get_open_incident(WC, now.date())
    assert row["id"] == incident_id
    assert row["source"] == "auto"
    assert row["resolved_at"] is None


def test_get_open_incident_none_when_resolved():
    now = datetime.now(timezone.utc)
    incident_id = machine_breakdown.open_incident(WC, now.date(), now, source="auto")
    machine_breakdown.resolve_incident(incident_id, "recovered", resume_utc=now)
    assert machine_breakdown.get_open_incident(WC, now.date()) is None


def test_get_incident_by_id():
    now = datetime.now(timezone.utc)
    incident_id = machine_breakdown.open_incident(WC, now.date(), now, source="manual")
    row = machine_breakdown.get_incident(incident_id)
    assert row["wc_name"] == WC
    assert row["source"] == "manual"
    assert machine_breakdown.get_incident(-1) is None


def test_resolve_and_reopen_incident():
    now = datetime.now(timezone.utc)
    incident_id = machine_breakdown.open_incident(WC, now.date(), now, source="auto")
    machine_breakdown.resolve_incident(incident_id, "dismissed")
    row = machine_breakdown.get_incident(incident_id)
    assert row["resolution"] == "dismissed"
    assert row["resolved_at"] is not None

    machine_breakdown.reopen_incident(incident_id)
    row = machine_breakdown.get_incident(incident_id)
    assert row["resolution"] is None
    assert row["resolved_at"] is None


def test_all_open_incidents():
    now = datetime.now(timezone.utc)
    id1 = machine_breakdown.open_incident(WC, now.date(), now, source="auto")
    row_ids = {r["id"] for r in machine_breakdown.all_open_incidents(now.date())}
    assert id1 in row_ids
    machine_breakdown.resolve_incident(id1, "recovered")
    row_ids = {r["id"] for r in machine_breakdown.all_open_incidents(now.date())}
    assert id1 not in row_ids


def test_snooze_operator_and_active_snooze_until():
    now = datetime.now(timezone.utc)
    incident_id = machine_breakdown.open_incident(WC, now.date(), now, source="auto")
    assert machine_breakdown.active_snooze_until(incident_id, "Juan") is None
    machine_breakdown.snooze_operator(incident_id, "Juan")
    until = machine_breakdown.active_snooze_until(incident_id, "Juan")
    assert until is not None
    assert until > now


def test_active_snooze_until_none_after_expiry(monkeypatch):
    now = datetime.now(timezone.utc)
    incident_id = machine_breakdown.open_incident(WC, now.date(), now, source="auto")
    db.execute(
        "INSERT INTO breakdown_snoozes (breakdown_id, person_name, until_utc) VALUES (%s, %s, %s)",
        (incident_id, "Juan", now - timedelta(minutes=1)),
    )
    assert machine_breakdown.active_snooze_until(incident_id, "Juan") is None


def test_same_name_workers_have_independent_snoozes():
    now = datetime.now(timezone.utc)
    incident_id = machine_breakdown.open_incident(WC, now.date(), now, source="auto")

    machine_breakdown.snooze_operator(
        incident_id, "Alex", employee_odoo_id=101
    )

    assert machine_breakdown.active_snooze_until(
        incident_id, "Alex", employee_odoo_id=101
    ) is not None
    assert machine_breakdown.active_snooze_until(
        incident_id, "Alex", employee_odoo_id=202
    ) is None
