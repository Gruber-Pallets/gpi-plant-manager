"""Schema + DB layer for the missed-punch-out alert. Postgres-backed."""

import os
from datetime import datetime, timezone

import pytest

from zira_dashboard import db, missed_punch_out as mpo
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


def test_record_close_is_idempotent():
    ci = datetime(2026, 6, 8, 14, 0, tzinfo=timezone.utc)
    midnight = datetime(2026, 6, 9, 0, 0, tzinfo=SITE_TZ)
    mpo.record_close(ATT, 42, ci.isoformat(), midnight)
    mpo.record_close(ATT, 42, ci.isoformat(), midnight)  # ON CONFLICT DO NOTHING
    rows = db.query(
        "SELECT count(*) AS n FROM missed_punch_out WHERE attendance_id = %s", (ATT,))
    assert rows[0]["n"] == 1


def test_current_rows_shapes_unresolved_only():
    ci = datetime(2026, 6, 8, 18, 0, tzinfo=timezone.utc)  # 13:00 local
    midnight = datetime(2026, 6, 9, 0, 0, tzinfo=SITE_TZ)
    mpo.record_close(ATT, 42, ci.isoformat(), midnight)
    rows = [r for r in mpo.current_rows() if r["attendance_id"] == ATT]
    assert len(rows) == 1
    assert rows[0]["check_in_label"] == "1:00 PM Mon Jun 8"
    assert rows[0]["check_in_date"] == "2026-06-08"
    # After correction it drops out.
    mpo.correct(ATT, datetime(2026, 6, 8, 16, 30, tzinfo=SITE_TZ))
    assert not [r for r in mpo.current_rows() if r["attendance_id"] == ATT]


def test_get_unresolved_then_correct():
    ci = datetime(2026, 6, 8, 14, 0, tzinfo=timezone.utc)
    midnight = datetime(2026, 6, 9, 0, 0, tzinfo=SITE_TZ)
    mpo.record_close(ATT, 42, ci.isoformat(), midnight)
    row = mpo.get_unresolved(ATT)
    assert row and row["employee_odoo_id"] == 42
    mpo.correct(ATT, datetime(2026, 6, 8, 16, 30, tzinfo=SITE_TZ))
    assert mpo.get_unresolved(ATT) is None  # resolved -> not returned


def test_record_close_falls_back_to_id_name_when_unknown():
    ci = datetime(2026, 6, 8, 14, 0, tzinfo=timezone.utc)
    midnight = datetime(2026, 6, 9, 0, 0, tzinfo=SITE_TZ)
    mpo.record_close(ATT, 987654, ci.isoformat(), midnight)  # not in people
    rows = db.query("SELECT name FROM missed_punch_out WHERE attendance_id = %s", (ATT,))
    assert rows[0]["name"] == "#987654"
