"""Flags page smoke tests. skipif Postgres (rows come from the flags table)."""
import os
from datetime import date

import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"), reason="needs Postgres")

from zira_dashboard import db
from zira_dashboard.app import app

PID = 990890


@pytest.fixture(autouse=True)
def _setup():
    db.bootstrap_schema()
    db.execute("DELETE FROM auto_salaried_flags WHERE person_odoo_id = %s", (PID,))
    yield
    db.execute("DELETE FROM auto_salaried_flags WHERE person_odoo_id = %s", (PID,))


def test_flags_page_lists_unresolved():
    db.execute(
        "INSERT INTO auto_salaried_flags (person_odoo_id, day, reason, details) "
        "VALUES (%s, %s, 'leave_conflict', 'test detail xyz')",
        (PID, date(2026, 9, 1)))
    with TestClient(app) as client:
        r = client.get("/auto-salaried/flags")
    assert r.status_code == 200
    assert "leave_conflict" in r.text
    assert "test detail xyz" in r.text


def test_resolve_flag():
    db.execute(
        "INSERT INTO auto_salaried_flags (person_odoo_id, day, reason) "
        "VALUES (%s, %s, 'incomplete_day')", (PID, date(2026, 9, 1)))
    flag_id = db.query(
        "SELECT id FROM auto_salaried_flags WHERE person_odoo_id = %s", (PID,))[0]["id"]
    with TestClient(app) as client:
        r = client.post(f"/auto-salaried/flags/{flag_id}/resolve", follow_redirects=False)
        assert r.status_code == 303
        r2 = client.get("/auto-salaried/flags")
    assert "incomplete_day" not in r2.text
