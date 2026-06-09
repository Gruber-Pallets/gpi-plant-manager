"""Schema + DB layer for the missed-punch-out alert. Postgres-backed."""

import os
from datetime import datetime, timezone

import pytest

from zira_dashboard import db
from zira_dashboard.shift_config import SITE_TZ

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="needs Postgres",
)

ATT = 999500


@pytest.fixture(autouse=True)
def _clean():
    db.bootstrap_schema()
    db.execute("DELETE FROM missed_punch_out WHERE attendance_id = %s", (ATT,))
    yield
    db.execute("DELETE FROM missed_punch_out WHERE attendance_id = %s", (ATT,))


def test_table_round_trips():
    ci = datetime(2026, 6, 8, 14, 0, tzinfo=timezone.utc)
    midnight = datetime(2026, 6, 9, 0, 0, tzinfo=SITE_TZ)
    db.execute(
        "INSERT INTO missed_punch_out "
        "(attendance_id, employee_odoo_id, name, check_in, auto_closed_at) "
        "VALUES (%s, %s, %s, %s, %s)",
        (ATT, 42, "Jesus Moreno", ci, midnight),
    )
    rows = db.query(
        "SELECT employee_odoo_id, name, resolved_at FROM missed_punch_out "
        "WHERE attendance_id = %s", (ATT,))
    assert rows and rows[0]["employee_odoo_id"] == 42
    assert rows[0]["name"] == "Jesus Moreno"
    assert rows[0]["resolved_at"] is None
