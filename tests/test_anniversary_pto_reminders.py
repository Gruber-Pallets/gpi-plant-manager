from __future__ import annotations

from datetime import date
from unittest.mock import MagicMock

import pytest

from zira_dashboard import anniversary_pto_reminders as reminders


@pytest.mark.parametrize(
    ("contract", "today", "expected"),
    [
        (date(2020, 10, 2), date(2026, 9, 2), date(2026, 10, 2)),
        (date(2020, 10, 3), date(2026, 9, 2), None),
        (date(2020, 9, 2), date(2026, 9, 2), date(2026, 9, 2)),
        (date(2020, 1, 5), date(2026, 12, 20), date(2027, 1, 5)),
        (date(2024, 2, 29), date(2027, 1, 29), date(2027, 2, 28)),
        (date(2026, 9, 2), date(2026, 9, 2), None),
    ],
)
def test_upcoming_anniversary(contract, today, expected):
    assert reminders.upcoming_anniversary(contract, today) == expected


def _install_sources(monkeypatch, people, leave_types=None):
    leave_types = leave_types if leave_types is not None else [{
        "holiday_status_id": 7,
        "name": "Paid Time Off",
        "request_unit": "day",
        "requires_allocation": "yes",
        "active": True,
    }]

    def query(sql, params=None):
        del params
        if "FROM people" in sql:
            return people
        if "FROM leave_types_cache" in sql:
            return leave_types
        raise AssertionError(sql)

    monkeypatch.setattr(reminders.db, "query", query)
    monkeypatch.setattr(reminders.employee_notifications, "notifications_enabled", lambda: True)


def test_run_reconciles_positive_fresh_paid_time_off(monkeypatch):
    _install_sources(monkeypatch, [
        {"odoo_id": 5, "first_contract_date": date(2020, 10, 2)},
        {"odoo_id": 9, "first_contract_date": date(2021, 9, 20)},
    ])
    fresh = {
        5: [{"holiday_status_id": 7, "available_practical": 2.5, "unit": "days"}],
        9: [{"holiday_status_id": 7, "available_practical": 6, "unit": "hours"}],
    }
    refresh = MagicMock(return_value=fresh)
    monkeypatch.setattr(reminders.time_off_balances, "refresh_for_employees", refresh)
    reconcile = MagicMock()
    monkeypatch.setattr(reminders.employee_notifications, "reconcile_anniversary_pto", reconcile)

    assert reminders.run(date(2026, 9, 2)) == 2

    refresh.assert_called_once_with([5, 9])
    notices = reconcile.call_args.args[0]
    assert [(n.person_odoo_id, n.balance_unit) for n in notices] == [(5, "days"), (9, "hours")]


def test_run_ignores_malformed_contracts_and_nonpositive_balances(monkeypatch):
    _install_sources(monkeypatch, [
        {"odoo_id": 5, "first_contract_date": "bad"},
        {"odoo_id": 9, "first_contract_date": date(2021, 9, 20)},
        {"odoo_id": 11, "first_contract_date": date(2021, 9, 21)},
    ])
    refresh = MagicMock(return_value={
        9: [{"holiday_status_id": 7, "available_practical": 0, "unit": "days"}],
        11: [{"holiday_status_id": 7, "available_practical": -1, "unit": "days"}],
    })
    monkeypatch.setattr(reminders.time_off_balances, "refresh_for_employees", refresh)
    reconcile = MagicMock()
    monkeypatch.setattr(reminders.employee_notifications, "reconcile_anniversary_pto", reconcile)

    assert reminders.run(date(2026, 9, 2)) == 0
    refresh.assert_called_once_with([9, 11])
    reconcile.assert_called_once_with(())


@pytest.mark.parametrize("leave_types", [[], [
    {"holiday_status_id": 7, "name": "Paid Time Off", "request_unit": "day", "requires_allocation": "yes", "active": True},
    {"holiday_status_id": 8, "name": "Paid Time Off", "request_unit": "hour", "requires_allocation": "yes", "active": True},
]])
def test_run_does_not_guess_missing_or_ambiguous_pto_type(monkeypatch, leave_types):
    _install_sources(
        monkeypatch,
        [{"odoo_id": 5, "first_contract_date": date(2020, 10, 2)}],
        leave_types,
    )
    refresh = MagicMock()
    reconcile = MagicMock()
    monkeypatch.setattr(reminders.time_off_balances, "refresh_for_employees", refresh)
    monkeypatch.setattr(reminders.employee_notifications, "reconcile_anniversary_pto", reconcile)

    assert reminders.run(date(2026, 9, 2)) == 0
    refresh.assert_not_called()
    reconcile.assert_not_called()


def test_run_keeps_existing_queue_when_fresh_balance_refresh_fails(monkeypatch):
    _install_sources(monkeypatch, [{"odoo_id": 5, "first_contract_date": date(2020, 10, 2)}])
    monkeypatch.setattr(reminders.time_off_balances, "refresh_for_employees", lambda ids: None)
    reconcile = MagicMock()
    monkeypatch.setattr(reminders.employee_notifications, "reconcile_anniversary_pto", reconcile)

    assert reminders.run(date(2026, 9, 2)) == 0
    reconcile.assert_not_called()


def test_run_is_inert_when_notifications_are_disabled(monkeypatch):
    query = MagicMock()
    monkeypatch.setattr(reminders.db, "query", query)
    monkeypatch.setattr(reminders.employee_notifications, "notifications_enabled", lambda: False)

    assert reminders.run(date(2026, 9, 2)) == 0
    query.assert_not_called()


def test_run_reads_only_active_nonexcluded_people(monkeypatch):
    sql_seen = []

    def query(sql, params=None):
        del params
        sql_seen.append(sql)
        if "FROM people" in sql:
            return []
        return [{
            "holiday_status_id": 7,
            "name": "Paid Time Off",
            "request_unit": "half_day",
            "requires_allocation": "yes",
            "active": True,
        }]

    monkeypatch.setattr(reminders.db, "query", query)
    monkeypatch.setattr(reminders.employee_notifications, "notifications_enabled", lambda: True)
    monkeypatch.setattr(reminders.time_off_balances, "refresh_for_employees", lambda ids: {})
    monkeypatch.setattr(reminders.employee_notifications, "reconcile_anniversary_pto", lambda rows: None)

    reminders.run(date(2026, 9, 2))

    people_sql = next(sql for sql in sql_seen if "FROM people" in sql)
    assert "active = TRUE" in people_sql
    assert "excluded = FALSE" in people_sql
