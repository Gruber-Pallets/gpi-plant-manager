import time
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
