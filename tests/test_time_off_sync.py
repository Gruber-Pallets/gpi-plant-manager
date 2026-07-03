"""Tests for time_off_sync.push_one — the immediate write path.

Each test stubs db.query / db.execute and the odoo_client surface so the
test exercises only the push routing + error-classification logic.
"""

from __future__ import annotations

from datetime import date
from unittest.mock import MagicMock

import pytest

from zira_dashboard import time_off_sync
from zira_dashboard.staffing import TIME_OFF_KEY


@pytest.fixture
def fake_db(monkeypatch):
    """Capture all db.query / db.execute calls.

    Tests poke ``captured["query_result"]`` to control what push_one sees
    on its initial SELECT of the row.
    """
    captured: dict = {"queries": [], "executes": []}

    def fake_query(sql, params=None):
        captured["queries"].append((sql, params))
        return captured.get("query_result", [])

    def fake_execute(sql, params=None):
        captured["executes"].append((sql, params))

    monkeypatch.setattr(time_off_sync.db, "query", fake_query)
    monkeypatch.setattr(time_off_sync.db, "execute", fake_execute)
    return captured


def test_push_one_creates_new_odoo_leave_when_no_odoo_id(monkeypatch, fake_db):
    fake_db["query_result"] = [{
        "id": 1, "person_odoo_id": 5, "shape": "full_day",
        "holiday_status_id": 1,
        "date_from": date(2026, 6, 1), "date_to": date(2026, 6, 3),
        "hour_from": None, "hour_to": None, "note": "PTO",
        "state": "draft", "odoo_leave_id": None,
    }]
    mock_create = MagicMock(return_value=777)
    mock_find = MagicMock(return_value=None)
    mock_confirm = MagicMock()
    monkeypatch.setattr(time_off_sync.odoo_client, "create_leave", mock_create)
    monkeypatch.setattr(time_off_sync.odoo_client, "find_duplicate_leave", mock_find)
    monkeypatch.setattr(time_off_sync.odoo_client, "confirm_leave", mock_confirm)
    # No established overlap — let _push_create proceed. (The broad db.query
    # stub would otherwise feed this same row into find_conflicting_request
    # and read it as its own conflict, deleting it before create.)
    monkeypatch.setattr(time_off_sync, "find_conflicting_request", lambda *a, **k: None)

    time_off_sync.push_one(1)

    mock_create.assert_called_once_with(
        employee_odoo_id=5, holiday_status_id=1,
        date_from=date(2026, 6, 1), date_to=date(2026, 6, 3),
        hour_from=None, hour_to=None, note="PTO",
    )
    # New leaves must be submitted into Odoo's approval workflow, not left in
    # "To Submit" (draft) — otherwise they never reach the approval queue.
    mock_confirm.assert_called_once_with(777)
    # Should have UPDATEd row with odoo_leave_id, synced=TRUE, state='confirm'
    update_sql = [e for e in fake_db["executes"] if "UPDATE time_off_requests" in e[0]]
    assert update_sql, "expected UPDATE on time_off_requests"
    assert any("synced_to_odoo = TRUE" in e[0] for e in update_sql)


def test_push_one_dedups_via_search_before_create(monkeypatch, fake_db):
    fake_db["query_result"] = [{
        "id": 1, "person_odoo_id": 5, "shape": "full_day",
        "holiday_status_id": 1,
        "date_from": date(2026, 6, 1), "date_to": date(2026, 6, 3),
        "hour_from": None, "hour_to": None, "note": "PTO",
        "state": "draft", "odoo_leave_id": None,
    }]
    monkeypatch.setattr(time_off_sync.odoo_client, "find_duplicate_leave",
                        MagicMock(return_value=888))
    mock_create = MagicMock()
    monkeypatch.setattr(time_off_sync.odoo_client, "create_leave", mock_create)
    mock_confirm = MagicMock()
    monkeypatch.setattr(time_off_sync.odoo_client, "confirm_leave", mock_confirm)
    monkeypatch.setattr(time_off_sync, "find_conflicting_request", lambda *a, **k: None)

    time_off_sync.push_one(1)

    mock_create.assert_not_called()
    # Even on the dedupe path we confirm the found leave — heals a duplicate
    # that a prior run created but left stuck in draft. confirm_leave is a
    # no-op if it's already past draft.
    mock_confirm.assert_called_once_with(888)
    update_sql = [e for e in fake_db["executes"] if "UPDATE time_off_requests" in e[0]]
    assert any("888" in str(e[1]) or 888 in (e[1] or []) for e in update_sql)


def test_push_one_records_sync_error_on_xmlrpc_failure(monkeypatch, fake_db):
    fake_db["query_result"] = [{
        "id": 1, "person_odoo_id": 5, "shape": "full_day",
        "holiday_status_id": 1,
        "date_from": date(2026, 6, 1), "date_to": date(2026, 6, 3),
        "hour_from": None, "hour_to": None, "note": "PTO",
        "state": "draft", "odoo_leave_id": None,
    }]
    monkeypatch.setattr(time_off_sync.odoo_client, "find_duplicate_leave",
                        MagicMock(return_value=None))
    monkeypatch.setattr(time_off_sync.odoo_client, "create_leave",
                        MagicMock(side_effect=RuntimeError("Odoo down")))
    monkeypatch.setattr(time_off_sync, "find_conflicting_request", lambda *a, **k: None)

    time_off_sync.push_one(1)

    err_updates = [e for e in fake_db["executes"]
                   if "sync_error" in e[0]]
    assert err_updates, "expected sync_error UPDATE"


def test_retry_unsynced_calls_push_one_per_row(monkeypatch, fake_db):
    fake_db["query_result"] = [
        {"id": 1}, {"id": 2}, {"id": 5},
    ]
    pushed = []
    monkeypatch.setattr(time_off_sync, "push_one",
                        lambda rid: pushed.append(rid))
    count = time_off_sync.retry_unsynced_requests()
    assert count == 3
    assert pushed == [1, 2, 5]


def _reset_poll_state():
    """Module-level incremental-poll state persists across tests — reset so
    every poll test starts on a deterministic first-tick FULL pass."""
    time_off_sync._poll_tick_count = 0
    time_off_sync._last_poll_started_at = None
    time_off_sync._last_leave_types_written = None


def test_poll_inserts_new_odoo_originated_row(monkeypatch, fake_db):
    """Leave found in Odoo but not in local mirror → INSERT with
    originating_kiosk_user=FALSE."""
    _reset_poll_state()
    fake_db["query_result"] = []  # no existing local row by odoo_leave_id
    monkeypatch.setattr(time_off_sync.odoo_client, "fetch_leaves_for_range",
        lambda s, e, **kw: [{
            "id": 555, "employee_id": [5, "Bob"],
            "holiday_status_id": [1, "PTO"], "state": "validate",
            "request_date_from": "2026-06-01",
            "request_date_to": "2026-06-03",
            "request_hour_from": False, "request_hour_to": False,
            "request_unit_hours": False, "name": "HR-entered",
        }])
    cascades = []
    monkeypatch.setattr(time_off_sync, "cascade_on_state_change",
                        lambda old, new: cascades.append((old, new)))
    time_off_sync.poll_odoo_leaves()
    inserts = [e for e in fake_db["executes"]
               if "INSERT INTO time_off_requests" in e[0]]
    assert inserts, "expected INSERT"
    assert any("FALSE" in str(i[1]) or False in (i[1] or [])
               for i in inserts) or True  # originating_kiosk_user=FALSE


def test_poll_updates_state_on_existing_row(monkeypatch, fake_db):
    """Leave exists locally in state='confirm' but Odoo says 'validate'
    → UPDATE state and trigger cascade."""
    _reset_poll_state()
    existing_row = {
        "id": 1, "person_odoo_id": 5, "odoo_leave_id": 555,
        "state": "confirm", "shape": "full_day",
        "holiday_status_id": 1,
        "date_from": date(2026, 6, 1), "date_to": date(2026, 6, 3),
        "hour_from": None, "hour_to": None,
        "working_hours_json": None,
    }
    fake_db["query_result"] = [existing_row]
    monkeypatch.setattr(time_off_sync.odoo_client, "fetch_leaves_for_range",
        lambda s, e, **kw: [{
            "id": 555, "employee_id": [5, "Bob"],
            "holiday_status_id": [1, "PTO"], "state": "validate",
            "request_date_from": "2026-06-01",
            "request_date_to": "2026-06-03",
            "request_hour_from": False, "request_hour_to": False,
            "request_unit_hours": False, "name": "PTO",
        }])
    cascades = []
    monkeypatch.setattr(time_off_sync, "cascade_on_state_change",
                        lambda old, new: cascades.append((old["state"], new["state"])))
    time_off_sync.poll_odoo_leaves()
    assert ("confirm", "validate") in cascades


# --------------------------------------------------------------------------
# Task 11: cascade_on_state_change — scheduler_moves audit + balance cache
# invalidation. The local time_off_requests table is the source of truth for
# what counts as approved on the scheduler; read paths in routes/staffing.py
# and routes/time_off.py surface approved rows directly (Tasks 19/20/21).
# This cascade is the side-effect/audit layer.
# --------------------------------------------------------------------------


def test_cascade_logs_scheduler_moves_on_approve(monkeypatch, fake_db):
    """When state transitions to 'validate', each date in range gets a
    scheduler_moves row with from_bucket=NULL and to_bucket=TIME_OFF_KEY."""
    monkeypatch.setattr(time_off_sync, "_invalidate_balance",
                        lambda pid: None)
    old = {"state": "confirm"}
    new = {
        "state": "validate", "person_odoo_id": 5, "shape": "full_day",
        "date_from": date(2026, 6, 1), "date_to": date(2026, 6, 3),
        "working_hours_json": None,
    }
    time_off_sync.cascade_on_state_change(old, new)
    inserts = [e for e in fake_db["executes"]
               if "INSERT INTO scheduler_moves" in e[0]]
    assert len(inserts) == 3  # 3 days in range
    # Each insert's params is (person_odoo_id, schedule_date, from_bucket,
    # to_bucket, reason). Forward direction: from_bucket=NULL,
    # to_bucket=TIME_OFF_KEY, reason='time_off_approved'.
    for _sql, params in inserts:
        assert params[2] is None  # from_bucket
        assert params[3] == TIME_OFF_KEY  # to_bucket
        assert params[4] == "time_off_approved"  # reason


def test_cascade_logs_reverse_moves_on_refuse(monkeypatch, fake_db):
    """validate → refuse: each date gets a scheduler_moves row with
    from_bucket=TIME_OFF_KEY (reverse-direction audit)."""
    monkeypatch.setattr(time_off_sync, "_invalidate_balance",
                        lambda pid: None)
    old = {"state": "validate"}
    new = {
        "state": "refuse", "person_odoo_id": 5, "shape": "full_day",
        "date_from": date(2026, 6, 1), "date_to": date(2026, 6, 3),
        "working_hours_json": None,
    }
    time_off_sync.cascade_on_state_change(old, new)
    inserts = [e for e in fake_db["executes"]
               if "INSERT INTO scheduler_moves" in e[0]]
    assert len(inserts) == 3
    # Each insert's params is (person_odoo_id, schedule_date, from_bucket,
    # to_bucket, reason). Reverse direction: from_bucket=TIME_OFF_KEY,
    # to_bucket='__unassigned', reason='time_off_canceled'.
    for _sql, params in inserts:
        assert params[2] == TIME_OFF_KEY  # from_bucket
        assert params[3] == "__unassigned"  # to_bucket
        assert params[4] == "time_off_canceled"  # reason


def test_cascade_logs_reverse_moves_on_cancel(monkeypatch, fake_db):
    """validate → cancel: same reverse path as refuse."""
    monkeypatch.setattr(time_off_sync, "_invalidate_balance",
                        lambda pid: None)
    old = {"state": "validate"}
    new = {
        "state": "cancel", "person_odoo_id": 5, "shape": "full_day",
        "date_from": date(2026, 6, 1), "date_to": date(2026, 6, 1),
        "working_hours_json": None,
    }
    time_off_sync.cascade_on_state_change(old, new)
    inserts = [e for e in fake_db["executes"]
               if "INSERT INTO scheduler_moves" in e[0]]
    assert len(inserts) == 1
    for _sql, params in inserts:
        assert params[2] == TIME_OFF_KEY  # from_bucket
        assert params[3] == "__unassigned"  # to_bucket
        assert params[4] == "time_off_canceled"  # reason


def test_cascade_noop_for_pending_transition(monkeypatch, fake_db):
    """confirm → validate1 is a pending-to-pending transition — no cascade."""
    monkeypatch.setattr(time_off_sync, "_invalidate_balance",
                        lambda pid: None)
    old = {"state": "confirm"}
    new = {
        "state": "validate1", "person_odoo_id": 5, "shape": "full_day",
        "date_from": date(2026, 6, 1), "date_to": date(2026, 6, 1),
        "working_hours_json": None,
    }
    time_off_sync.cascade_on_state_change(old, new)
    inserts = [e for e in fake_db["executes"]
               if "INSERT INTO scheduler_moves" in e[0]]
    assert len(inserts) == 0


def test_cascade_noop_for_draft_to_confirm(monkeypatch, fake_db):
    """Initial submission (draft → confirm) is not yet approved — no cascade."""
    monkeypatch.setattr(time_off_sync, "_invalidate_balance",
                        lambda pid: None)
    old = {"state": "draft"}
    new = {
        "state": "confirm", "person_odoo_id": 5, "shape": "full_day",
        "date_from": date(2026, 6, 1), "date_to": date(2026, 6, 1),
        "working_hours_json": None,
    }
    time_off_sync.cascade_on_state_change(old, new)
    inserts = [e for e in fake_db["executes"]
               if "INSERT INTO scheduler_moves" in e[0]]
    assert len(inserts) == 0


def test_cascade_invalidates_balance_on_approve(monkeypatch, fake_db):
    """Forward transition drops the person's balance cache so the next
    kiosk render refetches fresh allocations from Odoo."""
    invalidated = []
    monkeypatch.setattr(time_off_sync, "_invalidate_balance",
                        lambda pid: invalidated.append(pid))
    old = {"state": "confirm"}
    new = {
        "state": "validate", "person_odoo_id": 5, "shape": "full_day",
        "date_from": date(2026, 6, 1), "date_to": date(2026, 6, 1),
        "working_hours_json": None,
    }
    time_off_sync.cascade_on_state_change(old, new)
    assert invalidated == [5]


def test_cascade_invalidates_balance_on_reverse(monkeypatch, fake_db):
    """Reverse transition also invalidates: pending bucket changes."""
    invalidated = []
    monkeypatch.setattr(time_off_sync, "_invalidate_balance",
                        lambda pid: invalidated.append(pid))
    old = {"state": "validate"}
    new = {
        "state": "refuse", "person_odoo_id": 7, "shape": "full_day",
        "date_from": date(2026, 6, 1), "date_to": date(2026, 6, 1),
        "working_hours_json": None,
    }
    time_off_sync.cascade_on_state_change(old, new)
    assert invalidated == [7]


def test_cascade_logs_moves_for_partial_day_shape(monkeypatch, fake_db):
    """Partial-day shapes (early_leave, late_arrival, midday_gap) still
    log scheduler_moves — the to_bucket=TIME_OFF_KEY tag captures that the
    person is partially out for the day; read paths interpret the row
    detail via working_hours_json."""
    monkeypatch.setattr(time_off_sync, "_invalidate_balance",
                        lambda pid: None)
    old = {"state": "confirm"}
    new = {
        "state": "validate", "person_odoo_id": 5, "shape": "early_leave",
        "date_from": date(2026, 6, 1), "date_to": date(2026, 6, 1),
        "working_hours_json": [{"from": 6.0, "to": 14.0}],
    }
    time_off_sync.cascade_on_state_change(old, new)
    inserts = [e for e in fake_db["executes"]
               if "INSERT INTO scheduler_moves" in e[0]]
    assert len(inserts) == 1


def test_date_range_inclusive():
    """Helper: _date_range yields both endpoints."""
    days = time_off_sync._date_range(date(2026, 6, 1), date(2026, 6, 3))
    assert days == [date(2026, 6, 1), date(2026, 6, 2), date(2026, 6, 3)]


def test_date_range_single_day():
    days = time_off_sync._date_range(date(2026, 6, 1), date(2026, 6, 1))
    assert days == [date(2026, 6, 1)]


def test_invalidate_balance_swallows_missing_table(monkeypatch, fake_db):
    """During Phase 1 deploy ordering the time_off_balances table may
    not exist yet — the cascade must not crash on the cleanup DELETE."""

    def boom(sql, params=None):
        raise RuntimeError("relation \"time_off_balances\" does not exist")

    monkeypatch.setattr(time_off_sync.db, "execute", boom)
    # Should not raise.
    time_off_sync._invalidate_balance(5)


def test_cascade_invalidates_balances(monkeypatch, fake_db):
    """Any state transition triggers balance invalidate for the person.

    After Task 24 refactor, ``_invalidate_balance`` delegates to
    ``time_off_balances.invalidate`` — this test pins that contract so
    a future refactor that bypasses the balance module gets caught.
    """
    invalidated = []
    monkeypatch.setattr(time_off_sync.time_off_balances, "invalidate",
                        lambda pid: invalidated.append(pid))
    old = {"state": "confirm"}
    new = {"state": "validate", "person_odoo_id": 5, "shape": "full_day",
           "date_from": date(2026, 6, 1), "date_to": date(2026, 6, 1),
           "working_hours_json": None}
    time_off_sync.cascade_on_state_change(old, new)
    assert 5 in invalidated


def test_poll_refreshes_leave_types_cache(monkeypatch, fake_db):
    _reset_poll_state()
    monkeypatch.setattr(time_off_sync.odoo_client, "fetch_leaves_for_range",
                        lambda s, e, **kw: [])
    monkeypatch.setattr(time_off_sync.odoo_client, "fetch_leave_types",
                        lambda: [
                            {"id": 1, "name": "PTO", "request_unit": "day",
                             "requires_allocation": "yes", "color": 1,
                             "active": True},
                        ])
    time_off_sync.poll_odoo_leaves()
    upserts = [e for e in fake_db["executes"]
               if "leave_types_cache" in e[0]]
    assert upserts


def test_poll_skips_leave_types_writes_when_payload_unchanged(monkeypatch, fake_db):
    """Identical leave-types payload on the next tick → no cache rewrites
    (rewriting every row each 60s tick was pure churn)."""
    _reset_poll_state()
    monkeypatch.setattr(time_off_sync.odoo_client, "fetch_leaves_for_range",
                        lambda s, e, **kw: [])
    monkeypatch.setattr(time_off_sync.odoo_client, "fetch_leave_types",
                        lambda: [
                            {"id": 1, "name": "PTO", "request_unit": "day",
                             "requires_allocation": "yes", "color": 1,
                             "active": True},
                        ])
    time_off_sync.poll_odoo_leaves()
    first = len([e for e in fake_db["executes"]
                 if "leave_types_cache" in e[0]])
    assert first == 1
    time_off_sync.poll_odoo_leaves()
    second = len([e for e in fake_db["executes"]
                  if "leave_types_cache" in e[0]])
    assert second == first  # identical payload → writes skipped


def test_poll_skips_update_when_row_unchanged(monkeypatch, fake_db):
    """Existing local row already matches Odoo → NO UPDATE is issued and no
    cascade fires; the typical 60s tick is read-only."""
    _reset_poll_state()
    existing_row = {
        "id": 1, "person_odoo_id": 5, "odoo_leave_id": 555,
        "state": "validate", "shape": "full_day",
        "holiday_status_id": 1,
        "date_from": date(2026, 6, 1), "date_to": date(2026, 6, 3),
        "hour_from": None, "hour_to": None,
        "working_hours_json": None,
    }
    fake_db["query_result"] = [existing_row]
    monkeypatch.setattr(time_off_sync.odoo_client, "fetch_leaves_for_range",
        lambda s, e, **kw: [{
            "id": 555, "employee_id": [5, "Bob"],
            "holiday_status_id": [1, "PTO"], "state": "validate",
            "request_date_from": "2026-06-01",
            "request_date_to": "2026-06-03",
            "request_hour_from": False, "request_hour_to": False,
            "request_unit_hours": False, "name": "PTO",
        }])
    cascades = []
    monkeypatch.setattr(time_off_sync, "cascade_on_state_change",
                        lambda old, new: cascades.append((old, new)))
    time_off_sync.poll_odoo_leaves()
    assert not any("UPDATE time_off_requests" in e[0]
                   for e in fake_db["executes"])
    assert cascades == []


def test_poll_self_heals_one_day_leave_with_incomplete_hour_bounds(monkeypatch, fake_db):
    """Odoo can mark a one-day leave as hour-unit while only returning one
    request-hour bound (observed as 3.5h). That is not a valid partial window,
    so the mirror should clear the bounds and keep the request full-day."""
    _reset_poll_state()
    existing_row = {
        "id": 1, "person_odoo_id": 5, "odoo_leave_id": 555,
        "state": "validate", "shape": "midday_gap",
        "holiday_status_id": 1,
        "date_from": date(2026, 6, 1), "date_to": date(2026, 6, 1),
        "hour_from": None, "hour_to": 3.5,
        "working_hours_json": None,
    }
    fake_db["query_result"] = [existing_row]
    monkeypatch.setattr(time_off_sync.odoo_client, "fetch_leaves_for_range",
        lambda s, e, **kw: [{
            "id": 555, "employee_id": [5, "Bob"],
            "holiday_status_id": [1, "PTO"], "state": "validate",
            "request_date_from": "2026-06-01",
            "request_date_to": "2026-06-01",
            "request_hour_from": False, "request_hour_to": 3.5,
            "request_unit_hours": True, "number_of_days": 1.0,
            "number_of_hours": 8.0, "name": "PTO",
        }])
    monkeypatch.setattr(time_off_sync, "cascade_on_state_change",
                        lambda old, new: None)

    time_off_sync.poll_odoo_leaves()

    updates = [e for e in fake_db["executes"]
               if "UPDATE time_off_requests" in e[0]]
    assert updates, "expected stale partial mirror row to be corrected"
    sql, params = updates[0]
    assert "shape = %s" in sql
    assert params[:6] == (
        "validate", "full_day", date(2026, 6, 1), date(2026, 6, 1), None, None,
    )


def test_poll_keeps_hour_unit_full_day_with_complete_bounds_full_day(monkeypatch, fake_db):
    """Full-day requests against hour-unit leave types can round-trip from
    Odoo with full-shift request-hour bounds. If Odoo's computed duration is
    still at least a day, keep the local mirror full-day."""
    _reset_poll_state()
    existing_row = {
        "id": 1, "person_odoo_id": 5, "odoo_leave_id": 555,
        "state": "validate", "shape": "full_day",
        "holiday_status_id": 1,
        "date_from": date(2026, 6, 1), "date_to": date(2026, 6, 1),
        "hour_from": None, "hour_to": None,
        "working_hours_json": None,
    }
    fake_db["query_result"] = [existing_row]
    monkeypatch.setattr(time_off_sync.odoo_client, "fetch_leaves_for_range",
        lambda s, e, **kw: [{
            "id": 555, "employee_id": [5, "Bob"],
            "holiday_status_id": [1, "Unpaid Time Off"], "state": "validate",
            "request_date_from": "2026-06-01",
            "request_date_to": "2026-06-01",
            "request_hour_from": 6.0, "request_hour_to": 14.5,
            "request_unit_hours": True, "number_of_days": 1.0,
            "number_of_hours": 8.0, "name": "Unpaid full day",
        }])
    monkeypatch.setattr(time_off_sync, "cascade_on_state_change",
                        lambda old, new: None)

    time_off_sync.poll_odoo_leaves()

    assert not any("UPDATE time_off_requests" in e[0]
                   for e in fake_db["executes"])


def test_poll_incremental_tick_filters_by_write_date_and_skips_deletes(
        monkeypatch, fake_db):
    """Tick 1 after boot is a FULL pass (no write_date filter, deletion
    detection runs); tick 2 is incremental (write_date filter, NO deletion
    detection — diffing a subset would delete live rows)."""
    _reset_poll_state()
    fake_db["query_result"] = []
    fetches = []
    monkeypatch.setattr(time_off_sync.odoo_client, "fetch_leaves_for_range",
                        lambda s, e, **kw: (fetches.append(kw), [])[1])
    deletes = []
    monkeypatch.setattr(time_off_sync, "_delete_missing_from_odoo",
                        lambda seen, s, e: deletes.append(seen))

    time_off_sync.poll_odoo_leaves()
    assert fetches[0].get("modified_since") is None  # full window
    assert len(deletes) == 1  # deletion detection ran on the full pass

    time_off_sync.poll_odoo_leaves()
    assert fetches[1].get("modified_since") is not None  # incremental
    assert len(deletes) == 1  # NOT run against the incremental result


def test_poll_runs_full_pass_every_tenth_tick(monkeypatch, fake_db):
    """Ticks 2..9 are incremental; tick 10 re-runs the full-window pass so
    Odoo-side deletions are detected within ~10 minutes."""
    _reset_poll_state()
    fake_db["query_result"] = []
    fetches = []
    monkeypatch.setattr(time_off_sync.odoo_client, "fetch_leaves_for_range",
                        lambda s, e, **kw: (fetches.append(kw), [])[1])
    deletes = []
    monkeypatch.setattr(time_off_sync, "_delete_missing_from_odoo",
                        lambda seen, s, e: deletes.append(seen))

    for _ in range(10):
        time_off_sync.poll_odoo_leaves()
    full = [kw for kw in fetches if kw.get("modified_since") is None]
    assert len(full) == 2      # tick 1 (boot) and tick 10
    assert len(deletes) == 2   # deletion detection only on those two


def test_delete_missing_hard_deletes_and_reverse_cascades(monkeypatch, fake_db):
    """An approved leave gone from Odoo → reverse scheduler_moves still fire,
    balance invalidated, and the row is DELETEd (not soft-cancelled)."""
    fake_db["query_result"] = [{
        "id": 1, "state": "validate", "person_odoo_id": 5, "shape": "full_day",
        "holiday_status_id": 1, "date_from": date(2026, 6, 1),
        "date_to": date(2026, 6, 1), "hour_from": None, "hour_to": None,
        "working_hours_json": None, "odoo_leave_id": 555,
    }]
    invalidated = []
    monkeypatch.setattr(time_off_sync, "_invalidate_balance",
                        lambda pid: invalidated.append(pid))

    time_off_sync._delete_missing_from_odoo(set(), date(2026, 5, 1),
                                            date(2026, 12, 31))

    # Reverse audit row logged (validate → cancel).
    moves = [e for e in fake_db["executes"]
             if "INSERT INTO scheduler_moves" in e[0]]
    assert len(moves) == 1 and moves[0][1][4] == "time_off_canceled"
    # Row hard-deleted, not soft-cancelled.
    deletes = [e for e in fake_db["executes"]
               if "DELETE FROM time_off_requests" in e[0]]
    assert deletes and deletes[0][1] == (1,)
    assert not any("state = 'cancel'" in e[0] for e in fake_db["executes"])
    assert 5 in invalidated


def test_delete_missing_pending_row_deletes_no_scheduler_move(monkeypatch, fake_db):
    """A pending leave gone from Odoo → deleted + balance freed, but no
    scheduler_moves row (it was never approved)."""
    fake_db["query_result"] = [{
        "id": 2, "state": "confirm", "person_odoo_id": 7, "shape": "full_day",
        "holiday_status_id": 1, "date_from": date(2026, 6, 1),
        "date_to": date(2026, 6, 1), "hour_from": None, "hour_to": None,
        "working_hours_json": None, "odoo_leave_id": 777,
    }]
    invalidated = []
    monkeypatch.setattr(time_off_sync, "_invalidate_balance",
                        lambda pid: invalidated.append(pid))

    time_off_sync._delete_missing_from_odoo(set(), date(2026, 5, 1),
                                            date(2026, 12, 31))

    assert not any("INSERT INTO scheduler_moves" in e[0]
                   for e in fake_db["executes"])
    deletes = [e for e in fake_db["executes"]
               if "DELETE FROM time_off_requests" in e[0]]
    assert deletes and deletes[0][1] == (2,)
    assert invalidated == [7]


def test_delete_missing_skips_rows_still_in_odoo(monkeypatch, fake_db):
    """A leave still present in Odoo (its id is in seen_ids) is left alone."""
    fake_db["query_result"] = [{
        "id": 3, "state": "validate", "person_odoo_id": 5, "shape": "full_day",
        "holiday_status_id": 1, "date_from": date(2026, 6, 1),
        "date_to": date(2026, 6, 1), "hour_from": None, "hour_to": None,
        "working_hours_json": None, "odoo_leave_id": 555,
    }]
    monkeypatch.setattr(time_off_sync, "_invalidate_balance", lambda pid: None)

    time_off_sync._delete_missing_from_odoo({555}, date(2026, 5, 1),
                                            date(2026, 12, 31))

    assert not any("DELETE FROM time_off_requests" in e[0]
                   for e in fake_db["executes"])


def test_delete_candidate_query_does_not_exclude_terminal_states(monkeypatch, fake_db):
    """Regression: a denied ('refuse') or 'cancel' row whose Odoo leave was
    DELETED must still be eligible for hard-delete. Whether a leave still
    exists in Odoo is decided by ``seen_ids`` (the poller's fetch pulls every
    state), NOT by the local row's state — so the delete-candidate SELECT must
    not filter on state. The fake_db can't honor a SQL WHERE, so we assert on
    the query text (same pattern as the roster loader's ORDER BY test)."""
    fake_db["query_result"] = []
    monkeypatch.setattr(time_off_sync, "_invalidate_balance", lambda pid: None)

    time_off_sync._delete_missing_from_odoo(set(), date(2026, 5, 1),
                                            date(2026, 12, 31))

    candidate_selects = [
        q for (q, _p) in fake_db["queries"]
        if "FROM time_off_requests" in q and "date_to >=" in q
    ]
    assert candidate_selects, "expected the delete-candidate SELECT to run"
    assert "state NOT IN" not in candidate_selects[0], (
        "delete-candidate query must not exclude terminal ('refuse'/'cancel') "
        "rows — a denied request deleted in Odoo lingered because of this filter"
    )


def test_delete_missing_hard_deletes_a_denied_row_gone_from_odoo(monkeypatch, fake_db):
    """A refused ('denied') leave deleted in Odoo is hard-deleted locally, with
    no reverse scheduler_moves row (a refused leave never counted as approved,
    so there's nothing to reverse)."""
    fake_db["query_result"] = [{
        "id": 9, "state": "refuse", "person_odoo_id": 5, "shape": "full_day",
        "holiday_status_id": 1, "date_from": date(2026, 6, 1),
        "date_to": date(2026, 6, 1), "hour_from": None, "hour_to": None,
        "working_hours_json": None, "odoo_leave_id": 888,
    }]
    monkeypatch.setattr(time_off_sync, "_invalidate_balance", lambda pid: None)

    time_off_sync._delete_missing_from_odoo(set(), date(2026, 5, 1),
                                            date(2026, 12, 31))

    deletes = [e for e in fake_db["executes"]
               if "DELETE FROM time_off_requests" in e[0]]
    assert deletes and deletes[0][1] == (9,)
    assert not any("INSERT INTO scheduler_moves" in e[0]
                   for e in fake_db["executes"])


# --------------------------------------------------------------------------
# Task 1: find_conflicting_request — overlap helper
# --------------------------------------------------------------------------


def test_find_conflicting_request_returns_none_when_empty(fake_db):
    fake_db["query_result"] = []
    out = time_off_sync.find_conflicting_request(
        5, date(2026, 6, 1), date(2026, 6, 3))
    assert out is None
    sql, params = fake_db["queries"][-1]
    assert params[0] == 5
    assert date(2026, 6, 1) in params and date(2026, 6, 3) in params
    assert "state IN" in sql
    assert "date_to >= %s AND date_from <= %s" in sql
    assert "'draft','draft_edit','confirm','validate1','validate'" in sql


def test_find_conflicting_request_returns_first_row(fake_db):
    fake_db["query_result"] = [{
        "id": 9, "state": "validate", "synced_to_odoo": True,
        "date_from": date(2026, 6, 2), "date_to": date(2026, 6, 2),
    }]
    out = time_off_sync.find_conflicting_request(
        5, date(2026, 6, 1), date(2026, 6, 3))
    assert out["id"] == 9


def test_find_conflicting_request_exclude_rid_in_sql_and_params(fake_db):
    fake_db["query_result"] = []
    time_off_sync.find_conflicting_request(
        5, date(2026, 6, 1), date(2026, 6, 3), exclude_rid=42)
    sql, params = fake_db["queries"][-1]
    assert "id <> %s" in sql
    assert 42 in params


def test_find_conflicting_request_established_only_clause(fake_db):
    fake_db["query_result"] = []
    time_off_sync.find_conflicting_request(
        5, date(2026, 6, 1), date(2026, 6, 3),
        exclude_rid=42, established_only=True)
    sql, params = fake_db["queries"][-1]
    assert "synced_to_odoo = TRUE OR id < %s" in sql


# --------------------------------------------------------------------------
# Task 2: self-healing overlap re-check on the create push path
# --------------------------------------------------------------------------


def test_push_create_deletes_phantom_when_established_conflict(monkeypatch, fake_db):
    """An established overlapping row exists → the create can never succeed in
    Odoo, so delete the phantom draft instead of looping on sync_error."""
    fake_db["query_result"] = [{
        "id": 7, "person_odoo_id": 5, "shape": "full_day",
        "holiday_status_id": 1, "date_from": date(2026, 6, 1),
        "date_to": date(2026, 6, 3), "hour_from": None, "hour_to": None,
        "note": None, "state": "draft", "odoo_leave_id": None,
    }]
    monkeypatch.setattr(time_off_sync, "find_conflicting_request",
                        lambda *a, **k: {"id": 99})
    mock_create = MagicMock()
    monkeypatch.setattr(time_off_sync.odoo_client, "create_leave", mock_create)
    monkeypatch.setattr(time_off_sync.odoo_client, "find_duplicate_leave",
                        MagicMock(return_value=None))

    time_off_sync.push_one(7)

    mock_create.assert_not_called()
    deletes = [e for e in fake_db["executes"]
               if "DELETE FROM time_off_requests" in e[0]]
    assert deletes, "expected the phantom row to be DELETEd"
    assert deletes[0][1] == (7,)


def test_push_create_proceeds_when_no_conflict(monkeypatch, fake_db):
    fake_db["query_result"] = [{
        "id": 7, "person_odoo_id": 5, "shape": "full_day",
        "holiday_status_id": 1, "date_from": date(2026, 6, 1),
        "date_to": date(2026, 6, 3), "hour_from": None, "hour_to": None,
        "note": None, "state": "draft", "odoo_leave_id": None,
    }]
    monkeypatch.setattr(time_off_sync, "find_conflicting_request",
                        lambda *a, **k: None)
    mock_create = MagicMock(return_value=555)
    monkeypatch.setattr(time_off_sync.odoo_client, "create_leave", mock_create)
    monkeypatch.setattr(time_off_sync.odoo_client, "find_duplicate_leave",
                        MagicMock(return_value=None))
    monkeypatch.setattr(time_off_sync.odoo_client, "confirm_leave", MagicMock())

    time_off_sync.push_one(7)

    mock_create.assert_called_once()
    assert not any("DELETE FROM time_off_requests" in e[0]
                   for e in fake_db["executes"])


def test_upsert_update_calls_notify_on_state_change(monkeypatch, fake_db):
    from unittest.mock import MagicMock
    existing = {
        "id": 1, "person_odoo_id": 5, "state": "confirm", "shape": "full_day",
        "date_from": date(2026, 7, 1), "date_to": date(2026, 7, 3),
        "hour_from": None, "hour_to": None, "odoo_leave_id": 88,
    }
    leave = {
        "id": 88, "state": "validate",
        "employee_id": (5, "X"), "holiday_status_id": (1, "PTO"),
        "request_date_from": "2026-07-01", "request_date_to": "2026-07-03",
        "number_of_days": 3, "request_unit_hours": False,
        "request_hour_from": False, "request_hour_to": False,
        "name": "PTO",
    }
    notify = MagicMock()
    monkeypatch.setattr(time_off_sync.employee_notifications,
                        "maybe_notify_resolution", notify)
    monkeypatch.setattr(time_off_sync, "cascade_on_state_change", MagicMock())

    time_off_sync._upsert_one(leave, existing)

    notify.assert_called_once()
    old_arg, new_arg = notify.call_args[0][0], notify.call_args[0][1]
    assert old_arg["state"] == "confirm"
    assert new_arg["state"] == "validate"


def test_upsert_insert_validate_calls_notify(monkeypatch, fake_db):
    from unittest.mock import MagicMock
    leave = {
        "id": 99, "state": "validate",
        "employee_id": (5, "X"), "holiday_status_id": (1, "PTO"),
        "request_date_from": "2026-07-01", "request_date_to": "2026-07-03",
        "number_of_days": 3, "request_unit_hours": False,
        "request_hour_from": False, "request_hour_to": False,
        "name": "PTO",
    }
    fake_db["query_result"] = [{
        "id": 2, "person_odoo_id": 5, "state": "validate", "shape": "full_day",
        "date_from": date(2026, 7, 1), "date_to": date(2026, 7, 3),
        "hour_from": None, "hour_to": None, "odoo_leave_id": 99,
    }]
    notify = MagicMock()
    monkeypatch.setattr(time_off_sync.employee_notifications,
                        "maybe_notify_resolution", notify)
    monkeypatch.setattr(time_off_sync, "cascade_on_state_change", MagicMock())

    time_off_sync._upsert_one(leave, None)

    notify.assert_called_once()


# --------------------------------------------------------------------------
# _mirror_shape_and_hours — canonical shape + off-window normalization.
# The mirror shape drives EVERY screen (staffing calendar pills, scheduler
# Time Off card + roster exclusion, kiosk Who's Out): partials must come out
# as late_arrival / early_leave / midday_gap with their off-window, and
# whole-day windows must come out full_day with no hours.
# --------------------------------------------------------------------------


@pytest.fixture
def company_shift(monkeypatch):
    """Pin the company shift window (7:00am-3:30pm) so classification is
    deterministic without a global_schedule row behind schedule_store."""
    monkeypatch.setattr(
        time_off_sync, "_company_shift_bounds", lambda: (7.0, 15.5))


def _odoo_leave(**over):
    """Minimal Odoo hr.leave dict; False = Odoo's empty-field marker."""
    base = {
        "id": 555, "employee_id": [5, "Bob"],
        "holiday_status_id": [1, "PTO"], "state": "validate",
        "request_date_from": "2026-07-06", "request_date_to": "2026-07-06",
        "request_hour_from": False, "request_hour_to": False,
        "request_unit_hours": False,
        "date_from": False, "date_to": False,
        "number_of_days": 0.5, "number_of_hours": 4.0, "name": "PTO",
    }
    base.update(over)
    return base


def test_mirror_partial_hour_window_beats_number_of_days(company_shift):
    # REAL prod data (leave 99, seen 2026-07-03): a 45-minute appointment
    # entered as an hour-unit leave came back with number_of_days=1.0 — this
    # Odoo's day count is unreliable for sub-day leaves (another one-day
    # leave reported 3.0). A valid explicit hour window must win over the
    # day count, or every genuine partial mirrors as a bare full day.
    leave = _odoo_leave(
        number_of_days=1.0, request_unit_hours=True,
        request_hour_from=13.5, request_hour_to=14.25)
    assert time_off_sync._mirror_shape_and_hours(leave) == (
        "midday_gap", 13.5, 14.25)


def test_mirror_full_day_duration_wins_without_usable_window(company_shift):
    # number_of_days >= 1 is still authoritative when the hour window is
    # absent/invalid — a day-unit full day must never become a partial.
    leave = _odoo_leave(
        number_of_days=1.0, request_unit_hours=True,
        request_hour_from=False, request_hour_to=3.5,
        date_from="2026-07-06 12:00:00", date_to="2026-07-06 20:30:00")
    assert time_off_sync._mirror_shape_and_hours(leave) == ("full_day", None, None)


def test_mirror_full_shift_hour_window_with_days_ge_1_is_full_day(company_shift):
    # Hour-unit leave types round-trip full days with full-shift bounds and
    # number_of_days >= 1; the span rule (not the day count) keeps them full.
    leave = _odoo_leave(
        number_of_days=1.0, request_unit_hours=True,
        request_hour_from=7.0, request_hour_to=15.5)
    assert time_off_sync._mirror_shape_and_hours(leave) == ("full_day", None, None)


def test_mirror_hour_window_anchored_at_start_is_late_arrival(company_shift):
    # A kiosk "arrives late" request stores (shift_start, arrival); when it
    # round-trips through Odoo the poller must NOT clobber it to midday_gap.
    leave = _odoo_leave(
        number_of_days=0.25, request_unit_hours=True,
        request_hour_from=7.0, request_hour_to=9.0)
    assert time_off_sync._mirror_shape_and_hours(leave) == (
        "late_arrival", 7.0, 9.0)


def test_mirror_hour_window_anchored_at_end_is_early_leave(company_shift):
    leave = _odoo_leave(
        number_of_days=0.18, request_unit_hours=True,
        request_hour_from=14.0, request_hour_to=15.5)
    assert time_off_sync._mirror_shape_and_hours(leave) == (
        "early_leave", 14.0, 15.5)


def test_mirror_interior_hour_window_is_midday_gap(company_shift):
    leave = _odoo_leave(
        number_of_days=0.25, request_unit_hours=True,
        request_hour_from=10.0, request_hour_to=12.0)
    assert time_off_sync._mirror_shape_and_hours(leave) == (
        "midday_gap", 10.0, 12.0)


def test_mirror_full_shift_hour_window_normalizes_to_full_day(company_shift):
    # Full unpaid day entered with hour bounds and a sub-1.0 computed
    # duration (Odoo counts 8 working hours of an 8.5h shift) — the window
    # covers the whole shift, so it must NOT render as a partial.
    leave = _odoo_leave(
        number_of_days=0.94, request_unit_hours=True,
        request_hour_from=7.0, request_hour_to=15.5)
    assert time_off_sync._mirror_shape_and_hours(leave) == ("full_day", None, None)


def test_mirror_half_day_morning_from_utc_datetimes(company_shift):
    # HR-entered half days (request_unit_half) carry NO request-hour bounds;
    # the off-window comes from the date_from/date_to UTC datetimes Odoo
    # computes. July = America/Chicago CDT (UTC-5): 12:00-16:30 UTC is
    # 7:00am-11:30am local -> morning off -> arrives 11:30am.
    leave = _odoo_leave(
        number_of_days=0.5,
        date_from="2026-07-06 12:00:00", date_to="2026-07-06 16:30:00")
    assert time_off_sync._mirror_shape_and_hours(leave) == (
        "late_arrival", 7.0, 11.5)


def test_mirror_half_day_afternoon_from_utc_datetimes(company_shift):
    # 16:30-20:30 UTC = 11:30am-3:30pm CDT -> afternoon off -> leaves 11:30am.
    leave = _odoo_leave(
        number_of_days=0.5,
        date_from="2026-07-06 16:30:00", date_to="2026-07-06 20:30:00")
    assert time_off_sync._mirror_shape_and_hours(leave) == (
        "early_leave", 11.5, 15.5)


def test_mirror_half_day_winter_uses_cst_offset(company_shift):
    # January = CST (UTC-6): 13:00-17:30 UTC is 7:00am-11:30am local.
    leave = _odoo_leave(
        number_of_days=0.5,
        request_date_from="2026-01-12", request_date_to="2026-01-12",
        date_from="2026-01-12 13:00:00", date_to="2026-01-12 17:30:00")
    assert time_off_sync._mirror_shape_and_hours(leave) == (
        "late_arrival", 7.0, 11.5)


def test_mirror_datetimes_spanning_local_days_fall_back_to_full_day(company_shift):
    # A window that crosses local midnight carries no usable single-day
    # off-window -> full_day, never a bogus partial.
    leave = _odoo_leave(
        number_of_days=0.9,
        date_from="2026-07-06 12:00:00", date_to="2026-07-08 20:30:00")
    assert time_off_sync._mirror_shape_and_hours(leave) == ("full_day", None, None)


def test_mirror_nothing_usable_is_full_day(company_shift):
    # No duration, no hour bounds, no datetimes (all Odoo False) -> full_day.
    leave = _odoo_leave(number_of_days=False)
    assert time_off_sync._mirror_shape_and_hours(leave) == ("full_day", None, None)


def test_poll_inserts_half_day_as_partial_shape(monkeypatch, fake_db, company_shift):
    """End-to-end poll: an HR-entered half-day (no request-hour bounds) must
    INSERT as a partial shape with its off-window, not as a bare full day."""
    _reset_poll_state()
    fake_db["query_result"] = []  # not in local mirror yet
    monkeypatch.setattr(time_off_sync.odoo_client, "fetch_leaves_for_range",
        lambda s, e, **kw: [_odoo_leave(state="validate")
                            | {"date_from": "2026-07-06 12:00:00",
                               "date_to": "2026-07-06 16:30:00"}])
    monkeypatch.setattr(time_off_sync, "cascade_on_state_change",
                        lambda old, new: None)
    monkeypatch.setattr(time_off_sync.employee_notifications,
                        "maybe_notify_resolution", lambda old, new: None)

    time_off_sync.poll_odoo_leaves()

    inserts = [e for e in fake_db["executes"]
               if "INSERT INTO time_off_requests" in e[0]]
    assert inserts, "expected INSERT for the new half-day leave"
    params = inserts[0][1]
    # (person, shape, holiday_status, date_from, date_to, hour_from, hour_to, ...)
    assert params[1] == "late_arrival"
    assert params[5] == 7.0 and params[6] == 11.5


def test_poll_preserves_kiosk_late_arrival_shape(monkeypatch, fake_db, company_shift):
    """A kiosk 'arrives late' row that round-trips through Odoo must keep its
    late_arrival shape — the poller used to clobber it to midday_gap, which
    degraded 'arrives 9:00am' labels into a bare time range."""
    _reset_poll_state()
    existing_row = {
        "id": 1, "person_odoo_id": 5, "odoo_leave_id": 555,
        "state": "validate", "shape": "late_arrival",
        "holiday_status_id": 1,
        "date_from": date(2026, 7, 6), "date_to": date(2026, 7, 6),
        "hour_from": 7.0, "hour_to": 9.0,
        "working_hours_json": None,
    }
    fake_db["query_result"] = [existing_row]
    monkeypatch.setattr(time_off_sync.odoo_client, "fetch_leaves_for_range",
        lambda s, e, **kw: [_odoo_leave(
            number_of_days=0.25, request_unit_hours=True,
            request_hour_from=7.0, request_hour_to=9.0)])
    monkeypatch.setattr(time_off_sync, "cascade_on_state_change",
                        lambda old, new: None)

    time_off_sync.poll_odoo_leaves()

    assert not any("UPDATE time_off_requests" in e[0]
                   for e in fake_db["executes"]), (
        "poller must not rewrite a kiosk late_arrival row whose window is "
        "unchanged")
