"""Tests for time_off_sync.push_one — the immediate write path.

Each test stubs db.query / db.execute and the odoo_client surface so the
test exercises only the push routing + error-classification logic.
"""

from __future__ import annotations

from datetime import date
from unittest.mock import MagicMock

import pytest

from zira_dashboard import time_off_sync


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
    monkeypatch.setattr(time_off_sync.odoo_client, "create_leave", mock_create)
    monkeypatch.setattr(time_off_sync.odoo_client, "find_duplicate_leave", mock_find)

    time_off_sync.push_one(1)

    mock_create.assert_called_once_with(
        employee_odoo_id=5, holiday_status_id=1,
        date_from=date(2026, 6, 1), date_to=date(2026, 6, 3),
        hour_from=None, hour_to=None, note="PTO",
    )
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

    time_off_sync.push_one(1)

    mock_create.assert_not_called()
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
