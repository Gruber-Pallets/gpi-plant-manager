"""Exercise approval capture with real transactions, never a production DB."""
import os
from datetime import datetime, timedelta, UTC

import pytest

from zira_dashboard import absence_sync, db, plant_day, time_off_sync

pytestmark = pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="needs Postgres")


@pytest.fixture
def store():
    db.init_pool()
    db.bootstrap_schema()
    ids = []

    def insert(state="confirm", **changes):
        values = {
            "person_odoo_id": 990038, "shape": "full_day", "holiday_status_id": 1,
            "date_from": datetime.now(UTC).date() + timedelta(days=10),
            "date_to": datetime.now(UTC).date() + timedelta(days=11), "state": state,
        } | changes
        cols = ", ".join(values)
        placeholders = ", ".join(["%s"] * len(values))
        row = db.query(
            f"INSERT INTO time_off_requests ({cols}) VALUES ({placeholders}) RETURNING id",
            tuple(values.values()),
        )[0]
        ids.append(row["id"])
        return row["id"]

    yield insert
    db.execute("DELETE FROM time_off_approval_email WHERE request_id = ANY(%s)", (ids,))
    db.execute("DELETE FROM time_off_requests WHERE id = ANY(%s)", (ids,))


def emails(request_id):
    return db.query("SELECT * FROM time_off_approval_email WHERE request_id = %s", (request_id,))


def test_final_approval_is_captured_once_with_snapshot(store):
    request_id = store()
    db.execute("UPDATE time_off_requests SET state = 'validate1' WHERE id = %s", (request_id,))
    assert emails(request_id) == []
    db.execute("UPDATE time_off_requests SET state = 'validate' WHERE id = %s", (request_id,))
    row = emails(request_id)[0]
    assert row["state"] == "pending"
    assert row["person_odoo_id"] == 990038
    assert row["delivery_key"]
    db.execute("UPDATE time_off_requests SET state = 'validate' WHERE id = %s", (request_id,))
    db.execute("UPDATE time_off_requests SET state = 'confirm' WHERE id = %s", (request_id,))
    db.execute("UPDATE time_off_requests SET state = 'validate' WHERE id = %s", (request_id,))
    assert len(emails(request_id)) == 1


def test_approval_and_email_rollback_together(store):
    request_id = store()
    with pytest.raises(RuntimeError):
        with db.cursor() as cur:
            cur.execute("UPDATE time_off_requests SET state = 'validate' WHERE id = %s", (request_id,))
            cur.execute("SELECT * FROM time_off_approval_email WHERE request_id = %s", (request_id,))
            assert cur.fetchone()
            raise RuntimeError("rollback")
    assert emails(request_id) == []


@pytest.mark.parametrize("state", ["validate1", "refuse", "cancel", "draft_cancel"])
def test_nonapproval_does_not_email(store, state):
    request_id = store()
    db.execute("UPDATE time_off_requests SET state = %s WHERE id = %s", (state, request_id))
    assert emails(request_id) == []


def test_past_approval_does_not_email(store):
    yesterday = datetime.now(UTC).date() - timedelta(days=2)
    request_id = store(date_from=yesterday, date_to=yesterday)
    db.execute("UPDATE time_off_requests SET state = 'validate' WHERE id = %s", (request_id,))
    assert emails(request_id) == []


def test_imported_approval_requires_recent_odoo_timestamp(store):
    for timestamp in (None, datetime(2020, 1, 1, tzinfo=UTC)):
        request_id = store("validate", approval_source_updated_at=timestamp)
        assert emails(request_id) == []
    request_id = store("validate", approval_source_updated_at=datetime.now(UTC))
    assert len(emails(request_id)) == 1


def test_rebootstrap_does_not_backfill_or_reset_cutoff(store):
    request_id = store("validate")
    before = db.query("SELECT installed_at FROM time_off_email_installation")[0]
    db.bootstrap_schema()
    assert emails(request_id) == []
    assert db.query("SELECT installed_at FROM time_off_email_installation")[0] == before


def test_local_fallback_approval_is_captured(store):
    request_id = store(approval_source_updated_at=datetime(2020, 1, 1, tzinfo=UTC))
    db.execute(
        "UPDATE time_off_requests SET state = 'validate', local_record = TRUE WHERE id = %s",
        (request_id,),
    )
    assert len(emails(request_id)) == 1


@pytest.fixture
def mirror(monkeypatch):
    """Keep the real mirror SQL and trigger; isolate unrelated side effects."""
    monkeypatch.setattr(time_off_sync, "_company_shift_bounds", lambda: (7.0, 15.5))
    monkeypatch.setattr(time_off_sync, "cascade_on_state_change", lambda *_: None)
    monkeypatch.setattr(
        time_off_sync.employee_notifications, "maybe_notify_resolution", lambda *_: None,
    )

    def apply(request_id, **changes):
        existing = db.query("SELECT * FROM time_off_requests WHERE id = %s", (request_id,))[0]
        leave = {
            "id": existing["odoo_leave_id"],
            "employee_id": [existing["person_odoo_id"], "Test employee"],
            "holiday_status_id": [existing["holiday_status_id"], "Test leave"],
            "state": "validate", "request_unit_hours": False, "number_of_days": 1,
            "request_date_from": existing["date_from"].isoformat(),
            "request_date_to": existing["date_to"].isoformat(),
            "write_date": datetime.now(UTC).isoformat(),
        } | changes
        time_off_sync._upsert_one(leave, existing)

    return apply


@pytest.mark.parametrize("timestamp", [None, "invalid", "2020-01-01 00:00:00"])
def test_existing_import_requires_recent_odoo_timestamp(store, mirror, timestamp):
    request_id = store(odoo_leave_id=99003801)
    mirror(request_id, write_date=timestamp)
    assert db.query("SELECT state FROM time_off_requests WHERE id = %s", (request_id,))[0][
        "state"
    ] == "validate"
    assert emails(request_id) == []


def test_existing_fresh_import_captures_odoo_timestamp(store, mirror):
    request_id = store(odoo_leave_id=99003801)
    updated_at = datetime.now(UTC)
    mirror(request_id, write_date=updated_at.isoformat())
    assert len(emails(request_id)) == 1
    assert db.query(
        "SELECT approval_source_updated_at FROM time_off_requests WHERE id = %s", (request_id,),
    )[0]["approval_source_updated_at"] == updated_at


@pytest.mark.parametrize("state", ["confirm", "validate"])
def test_employee_reassignment_updates_mirror_and_approval_snapshot(store, mirror, state):
    request_id = store(
        state, odoo_leave_id=99003801, approval_source_updated_at=datetime.now(UTC),
    )
    mirror(request_id, employee_id=[990039, "Replacement employee"])
    assert db.query(
        "SELECT person_odoo_id FROM time_off_requests WHERE id = %s", (request_id,),
    )[0]["person_odoo_id"] == 990039
    # A pending request captures the employee attached at final approval. A previously
    # captured approval retains its original snapshot so delivery can skip it.
    assert emails(request_id)[0]["person_odoo_id"] == (990039 if state == "confirm" else 990038)


@pytest.mark.parametrize("existing", [False, True])
def test_fresh_manager_absence_is_captured(store, mirror, existing):
    today = plant_day.today()
    request_id = None
    if existing:
        request_id = store(
            date_from=today, date_to=today, odoo_leave_id=99003802,
            approval_source_updated_at=datetime(2020, 1, 1, tzinfo=UTC),
        )
    try:
        absence_sync.mirror_approved_absence(
            employee_odoo_id=990038, holiday_status_id=1, leave_id=99003802,
            day=today, employee_name="Test employee", reason="",
        )
        request_id = db.query(
            "SELECT id FROM time_off_requests WHERE odoo_leave_id = %s", (99003802,),
        )[0]["id"]
        assert len(emails(request_id)) == 1
    finally:
        if not existing and request_id is not None:
            db.execute("DELETE FROM time_off_approval_email WHERE request_id = %s", (request_id,))
            db.execute("DELETE FROM time_off_requests WHERE id = %s", (request_id,))
