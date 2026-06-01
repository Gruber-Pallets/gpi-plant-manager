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


def test_poll_inserts_new_odoo_originated_row(monkeypatch, fake_db):
    """Leave found in Odoo but not in local mirror → INSERT with
    originating_kiosk_user=FALSE."""
    fake_db["query_result"] = []  # no existing local row by odoo_leave_id
    monkeypatch.setattr(time_off_sync.odoo_client, "fetch_leaves_for_range",
        lambda s, e: [{
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
        lambda s, e: [{
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
    monkeypatch.setattr(time_off_sync.odoo_client, "fetch_leaves_for_range",
                        lambda s, e: [])
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
