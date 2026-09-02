"""Tests for time_off_balances — per-employee leave balance cache.

Each test stubs ``db.execute`` / ``db.query`` and the ``odoo_client``
surface so the test exercises only the cache upsert + error-swallow
logic. No real Odoo or Postgres call ever happens.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from zira_dashboard import time_off_balances


def _balance(holiday_status_id, unit, available_practical):
    return {
        "holiday_status_id": holiday_status_id,
        "unit": unit,
        "allocated_total": available_practical,
        "taken": 0,
        "pending": 0,
        "available": available_practical,
        "available_practical": available_practical,
    }


@pytest.fixture
def fake_db(monkeypatch):
    """Capture all DB writes and stub ``db.query`` to empty.

    Tests assert against ``captured`` to verify which SQL path ran
    and with what params.
    """
    captured: dict = {"executes": [], "execute_values": []}

    class FakeCursor:
        pass

    class FakeCursorContext:
        def __enter__(self):
            return FakeCursor()

        def __exit__(self, exc_type, exc, tb):
            return False

    monkeypatch.setattr(
        time_off_balances.db, "execute",
        lambda sql, params=None: captured["executes"].append((sql, params)),
    )
    monkeypatch.setattr(time_off_balances.db, "cursor", lambda: FakeCursorContext())
    monkeypatch.setattr(
        time_off_balances.db,
        "execute_values",
        lambda cur, sql, rows, template=None: captured["execute_values"].append(
            (sql, list(rows), template)
        ),
    )
    monkeypatch.setattr(
        time_off_balances.db, "query",
        lambda sql, params=None: [],
    )
    return captured


def test_refresh_for_employee_upserts_each_balance(monkeypatch, fake_db):
    """One bulk INSERT … ON CONFLICT DO UPDATE for balance rows from Odoo."""
    monkeypatch.setattr(
        time_off_balances.odoo_client, "fetch_balances_for",
        MagicMock(return_value=[
            {"holiday_status_id": 1, "unit": "days",
             "allocated_total": 15.0, "taken": 3.0, "pending": 2.0,
             "available": 12.0, "available_practical": 10.0},
            {"holiday_status_id": 2, "unit": "hours",
             "allocated_total": 0.0, "taken": 0.0, "pending": 0.0,
             "available": 0.0, "available_practical": 0.0},
        ]),
    )
    time_off_balances.refresh_for_employee(5)
    assert len(fake_db["execute_values"]) == 1
    sql, rows, template = fake_db["execute_values"][0]
    assert "INSERT INTO time_off_balances" in sql
    assert "ON CONFLICT (person_odoo_id, holiday_status_id)" in sql
    assert template.endswith("now())")
    assert rows == [
        (5, 1, "days", 15.0, 3.0, 2.0, 12.0, 10.0),
        (5, 2, "hours", 0.0, 0.0, 0.0, 0.0, 0.0),
    ]


def test_refresh_for_employee_swallows_odoo_errors(monkeypatch, fake_db):
    """Odoo raising must not propagate — caller still renders the wizard."""
    monkeypatch.setattr(
        time_off_balances.odoo_client, "fetch_balances_for",
        MagicMock(side_effect=RuntimeError("Odoo down")),
    )
    # Should not raise
    time_off_balances.refresh_for_employee(5)


def test_refresh_for_employees_returns_fresh_rows_after_one_batch(monkeypatch, fake_db):
    fresh = {5: [_balance(1, "days", 2.5)], 9: [_balance(1, "hours", 6)]}
    fetch = MagicMock(return_value=fresh)
    monkeypatch.setattr(time_off_balances.odoo_client, "fetch_balances_for_many", fetch)

    assert time_off_balances.refresh_for_employees([5, 9, 5]) == fresh

    fetch.assert_called_once_with([5, 9])
    assert len(fake_db["execute_values"]) == 1


def test_refresh_for_employees_distinguishes_failure(monkeypatch, fake_db):
    monkeypatch.setattr(
        time_off_balances.odoo_client,
        "fetch_balances_for_many",
        MagicMock(side_effect=RuntimeError("unavailable")),
    )

    assert time_off_balances.refresh_for_employees([5]) is None
    assert fake_db["execute_values"] == []


def test_refresh_for_employees_empty_input_does_not_call_odoo(monkeypatch, fake_db):
    fetch = MagicMock()
    monkeypatch.setattr(time_off_balances.odoo_client, "fetch_balances_for_many", fetch)

    assert time_off_balances.refresh_for_employees([]) == {}

    fetch.assert_not_called()


def test_invalidate_one(monkeypatch, fake_db):
    """invalidate() issues a DELETE scoped to one person."""
    time_off_balances.invalidate(5)
    deletes = [
        e for e in fake_db["executes"]
        if "DELETE FROM time_off_balances" in e[0]
    ]
    assert deletes


def test_refresh_stale_batches_all_employees_into_one_fetch(monkeypatch, fake_db):
    """The 10-min sweep fetches ALL stale employees via one
    fetch_balances_for_many call (2 XML-RPC round-trips total) and writes
    all returned balance rows in one bulk upsert."""
    monkeypatch.setattr(
        time_off_balances.db, "query",
        lambda sql, params=None: [{"person_odoo_id": 5},
                                  {"person_odoo_id": 9}],
    )
    fetch_calls = []

    def fake_many(ids):
        fetch_calls.append(ids)
        return {
            5: [{"holiday_status_id": 1, "unit": "days",
                 "allocated_total": 15.0, "taken": 3.0, "pending": 0.0,
                 "available": 12.0, "available_practical": 12.0}],
            9: [{"holiday_status_id": 1, "unit": "days",
                 "allocated_total": 0.0, "taken": 0.0, "pending": 0.0,
                 "available": 0.0, "available_practical": 0.0}],
        }

    monkeypatch.setattr(
        time_off_balances.odoo_client, "fetch_balances_for_many", fake_many)
    refreshed = time_off_balances.refresh_stale(600)
    assert fetch_calls == [[5, 9]]  # ONE batched fetch for both employees
    assert refreshed == 2
    assert len(fake_db["execute_values"]) == 1
    _sql, rows, _template = fake_db["execute_values"][0]
    assert rows == [
        (5, 1, "days", 15.0, 3.0, 0.0, 12.0, 12.0),
        (9, 1, "days", 0.0, 0.0, 0.0, 0.0, 0.0),
    ]


def test_refresh_stale_swallows_odoo_errors(monkeypatch, fake_db):
    """Odoo raising during the batched fetch must not propagate — the sweep
    just retries in 10 minutes."""
    monkeypatch.setattr(
        time_off_balances.db, "query",
        lambda sql, params=None: [{"person_odoo_id": 5}],
    )
    monkeypatch.setattr(
        time_off_balances.odoo_client, "fetch_balances_for_many",
        MagicMock(side_effect=RuntimeError("Odoo down")),
    )
    assert time_off_balances.refresh_stale(600) == 0
