import time
from datetime import date

import pytest
from unittest.mock import patch

from zira_dashboard import odoo_client


def _stub_execute(monkeypatch, responses):
    calls = []
    def fake(model, method, *args, **kwargs):
        calls.append((model, method, args, kwargs))
        key = (model, method)
        if key not in responses:
            raise AssertionError(f"unexpected call: {key}")
        return responses[key]
    monkeypatch.setattr(odoo_client, "execute", fake)
    return calls


def test_fetch_leave_types_returns_active_types(monkeypatch):
    odoo_client._leave_types_cache = None  # reset
    responses = {
        ("hr.leave.type", "search_read"): [
            {"id": 1, "name": "PTO", "request_unit": "day",
             "requires_allocation": "yes", "color": 1, "active": True},
            {"id": 2, "name": "Custom Hours", "request_unit": "hour",
             "requires_allocation": "no", "color": 4, "active": True},
        ],
    }
    _stub_execute(monkeypatch, responses)
    types = odoo_client.fetch_leave_types()
    assert len(types) == 2
    assert types[0]["name"] == "PTO"
    assert types[1]["request_unit"] == "hour"


def test_fetch_leave_types_uses_cache_within_ttl(monkeypatch):
    odoo_client._leave_types_cache = None
    responses = {
        ("hr.leave.type", "search_read"): [
            {"id": 1, "name": "PTO", "request_unit": "day",
             "requires_allocation": "yes", "color": 1, "active": True},
        ],
    }
    calls = _stub_execute(monkeypatch, responses)
    odoo_client.fetch_leave_types()
    odoo_client.fetch_leave_types()  # should not re-call
    assert len(calls) == 1


def test_fetch_leave_types_refreshes_after_ttl(monkeypatch):
    odoo_client._leave_types_cache = None
    responses = {
        ("hr.leave.type", "search_read"): [{"id": 1, "name": "PTO",
            "request_unit": "day", "requires_allocation": "yes",
            "color": 1, "active": True}],
    }
    calls = _stub_execute(monkeypatch, responses)
    odoo_client.fetch_leave_types()
    odoo_client._leave_types_cache = (
        odoo_client._leave_types_cache[0],
        time.time() - 1,  # force expiry
    )
    odoo_client.fetch_leave_types()
    assert len(calls) == 2


def test_fetch_leaves_for_range_passes_domain(monkeypatch):
    responses = {
        ("hr.leave", "search_read"): [
            {"id": 100, "employee_id": [5, "Bob"],
             "holiday_status_id": [1, "PTO"], "state": "validate",
             "date_from": "2026-06-01 06:00:00",
             "date_to": "2026-06-03 14:30:00",
             "request_date_from": "2026-06-01",
             "request_date_to": "2026-06-03",
             "request_hour_from": False, "request_hour_to": False,
             "request_unit_hours": False,
             "number_of_days": 3.0,
             "number_of_hours_display": 24.0,
             "name": "Vacation"},
        ],
    }
    calls = _stub_execute(monkeypatch, responses)
    leaves = odoo_client.fetch_leaves_for_range(date(2026, 5, 1), date(2026, 7, 1))
    assert len(leaves) == 1
    assert leaves[0]["id"] == 100
    # Verify domain spans the range
    domain = calls[0][2][0]
    assert any("date_from" in str(c) or "date_to" in str(c) for c in domain)


def test_fetch_leaves_for_range_extracts_id_from_many2one(monkeypatch):
    responses = {
        ("hr.leave", "search_read"): [
            {"id": 100, "employee_id": [5, "Bob"],
             "holiday_status_id": [1, "PTO"], "state": "confirm",
             "date_from": "2026-06-01 00:00:00",
             "date_to": "2026-06-01 23:59:59",
             "request_date_from": "2026-06-01",
             "request_date_to": "2026-06-01",
             "request_hour_from": False, "request_hour_to": False,
             "request_unit_hours": False,
             "number_of_days": 1.0,
             "number_of_hours_display": 8.0,
             "name": False},
        ],
    }
    _stub_execute(monkeypatch, responses)
    leaves = odoo_client.fetch_leaves_for_range(date(2026, 6, 1), date(2026, 6, 1))
    # Many2one fields come as [id, name] tuples from Odoo
    assert leaves[0]["employee_id"] == [5, "Bob"]
    assert leaves[0]["holiday_status_id"] == [1, "PTO"]


def test_fetch_resource_calendar_returns_shape(monkeypatch):
    responses = {
        ("hr.employee", "search_read"): [
            {"id": 5, "resource_calendar_id": [3, "Standard 40h"]},
        ],
        ("resource.calendar", "read"): [
            {"id": 3, "tz": "America/Chicago"},
        ],
        ("resource.calendar.attendance", "search_read"): [
            {"hour_from": 6.0, "hour_to": 14.5, "dayofweek": "0",
             "day_period": "morning"},
            {"hour_from": 6.0, "hour_to": 14.5, "dayofweek": "1",
             "day_period": "morning"},
        ],
    }
    _stub_execute(monkeypatch, responses)
    cal = odoo_client.fetch_resource_calendar(5)
    assert cal is not None
    assert cal["hour_from"] == 6.0
    assert cal["hour_to"] == 14.5
    assert cal["tz"] == "America/Chicago"


def test_fetch_resource_calendar_returns_none_when_unset(monkeypatch):
    responses = {
        ("hr.employee", "search_read"): [
            {"id": 5, "resource_calendar_id": False},
        ],
    }
    _stub_execute(monkeypatch, responses)
    assert odoo_client.fetch_resource_calendar(5) is None
