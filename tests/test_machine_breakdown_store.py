"""machine_breakdowns / breakdown_snoozes store (Postgres). Mirrors
tests/test_inbox_open_items.py's fixture pattern."""
import os
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from threading import Event, Thread, current_thread

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


def test_dismissed_incident_wins_against_blocked_recovery(monkeypatch):
    """Recovery must re-check openness under the same incident row lock."""
    from zira_dashboard import wc_attributions

    now = datetime.now(timezone.utc)
    stop = now - timedelta(hours=1)
    resume = now - timedelta(minutes=10)
    incident_id = machine_breakdown.open_incident(
        WC, now.date(), stop, source="auto"
    )
    db.execute(
        "INSERT INTO wc_time_attributions "
        "(day, wc_name, person_name, employee_odoo_id, start_utc, end_utc, "
        "source, breakdown_id) VALUES (%s, %s, %s, %s, %s, NULL, %s, %s)",
        (
            now.date(),
            WC,
            "Alex",
            101,
            stop,
            wc_attributions.BREAKDOWN_SOURCE,
            incident_id,
        ),
    )
    incident = machine_breakdown.get_incident(incident_id)
    source = machine_breakdown.OperatorSourceSnapshot(
        (), (), False, True, False
    )
    monkeypatch.setattr(
        machine_breakdown, "_last_output_after", lambda *_args, **_kwargs: resume
    )

    original_cursor = db.cursor
    dismiss_uncommitted = Event()
    allow_dismiss_commit = Event()
    recovery_app = f"task12-recovery-{incident_id}"

    @contextmanager
    def coordinated_cursor():
        with original_cursor() as cur:
            role = current_thread().name
            if role == "task12-recovery":
                cur.execute("SET LOCAL application_name = %s", (recovery_app,))
            yield cur
            if role == "task12-dismiss":
                dismiss_uncommitted.set()
                if not allow_dismiss_commit.wait(timeout=5):
                    raise TimeoutError("recovery never reached the incident lock")

    monkeypatch.setattr(db, "cursor", coordinated_cursor)
    errors = []
    recovery_results = []

    def dismiss():
        try:
            machine_breakdown.dismiss_incident(incident_id)
        except Exception as exc:  # pragma: no cover - asserted below
            errors.append(exc)

    def recover():
        try:
            recovery_results.append(
                machine_breakdown._maybe_auto_resolve(
                    incident, now.date(), now, source
                )
            )
        except Exception as exc:  # pragma: no cover - asserted below
            errors.append(exc)

    dismiss_thread = Thread(target=dismiss, name="task12-dismiss")
    recovery_thread = Thread(target=recover, name="task12-recovery")
    dismiss_thread.start()
    assert dismiss_uncommitted.wait(timeout=5)
    recovery_thread.start()

    deadline = time.monotonic() + 5
    blocked = False
    try:
        while time.monotonic() < deadline:
            if db.query(
                "SELECT 1 FROM pg_stat_activity "
                "WHERE application_name = %s AND wait_event_type = 'Lock'",
                (recovery_app,),
            ):
                blocked = True
                break
            time.sleep(0.01)
    finally:
        allow_dismiss_commit.set()
        dismiss_thread.join(timeout=5)
        recovery_thread.join(timeout=5)

    assert blocked is True
    assert not dismiss_thread.is_alive()
    assert not recovery_thread.is_alive()
    assert errors == []
    assert recovery_results == [False]
    saved = machine_breakdown.get_incident(incident_id)
    assert saved["resolution"] == "dismissed"
    assert saved["resume_utc"] is None
    assert db.query(
        "SELECT id FROM wc_time_attributions WHERE breakdown_id = %s",
        (incident_id,),
    ) == []
