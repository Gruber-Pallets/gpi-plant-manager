"""Undo: load + mark-undone helpers and the reverse endpoint."""
import os

import pytest

from zira_dashboard import db, inbox_log

pytestmark = pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="needs Postgres")

KP = "test:undo:"


@pytest.fixture(autouse=True)
def _clean():
    db.bootstrap_schema()
    db.execute("DELETE FROM inbox_events WHERE item_key LIKE %s", (KP + "%",))
    yield
    db.execute("DELETE FROM inbox_events WHERE item_key LIKE %s", (KP + "%",))


def test_get_event_and_mark_undone():
    eid = inbox_log.record_event(
        item_kind="missing_wc", item_key=KP + "1", person_name="Maria",
        category_label="Missing WC", action="dismiss",
        actor_upn="dale@gruberpallets.com", actor_name="Dale", reversible=True)
    ev = inbox_log.get_event(eid)
    assert ev is not None
    assert ev["item_key"] == KP + "1"
    assert ev["action"] == "dismiss"
    assert ev["undone_at"] is None

    undo_id = inbox_log.record_event(
        item_kind="missing_wc", item_key=KP + "1", person_name="Maria",
        category_label="Missing WC", action="undo", actor_upn="dale@gruberpallets.com",
        actor_name="Dale")
    inbox_log.mark_undone(eid, undo_id)
    ev2 = inbox_log.get_event(eid)
    assert ev2["undone_at"] is not None
    assert ev2["undo_event_id"] == undo_id


def test_get_event_missing_returns_none():
    assert inbox_log.get_event(-1) is None


def test_missing_wc_unresolve_removes_suppression():
    from zira_dashboard import missing_wc
    ATT = 999700
    db.execute("DELETE FROM missing_wc_resolved WHERE attendance_id = %s", (ATT,))
    missing_wc.resolve(ATT, "dismissed", name="Maria")
    assert ATT in missing_wc.resolved_ids()
    missing_wc.unresolve(ATT)
    assert ATT not in missing_wc.resolved_ids()


def test_undo_late_arrival_deletes_row():
    from zira_dashboard import late_report
    from datetime import date
    DAY, EMP = date(2026, 6, 26), "999801"
    db.execute("DELETE FROM late_arrivals WHERE day = %s AND emp_id = %s", (DAY, EMP))
    late_report.save_late_arrival(DAY, EMP, "Test Person", reason="Sick")
    assert db.query("SELECT 1 FROM late_arrivals WHERE day=%s AND emp_id=%s", (DAY, EMP))
    late_report.undo_late_arrival(DAY, EMP)
    assert not db.query("SELECT 1 FROM late_arrivals WHERE day=%s AND emp_id=%s", (DAY, EMP))
