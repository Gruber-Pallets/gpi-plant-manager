"""Pure-logic tests for flex detection mapping in odoo_client. Stubs execute."""
from __future__ import annotations

from unittest.mock import MagicMock

from zira_dashboard import odoo_client


def test_is_flexible_handles_bool_and_selection():
    assert odoo_client._is_flexible(True) is True
    assert odoo_client._is_flexible(False) is False
    assert odoo_client._is_flexible("flexible") is True
    assert odoo_client._is_flexible("fully_fixed") is False
    assert odoo_client._is_flexible(None) is False


def test_fetch_work_schedules_maps_is_flexible(monkeypatch):
    fake = MagicMock(return_value=[
        {"id": 1, "name": "Standard", odoo_client.SCHEDULE_TYPE_FIELD: False},
        {"id": 2, "name": "Flexible", odoo_client.SCHEDULE_TYPE_FIELD: True},
    ])
    monkeypatch.setattr(odoo_client, "execute", fake)

    out = odoo_client.fetch_work_schedules()

    # The schedule-type field is requested.
    _args, kwargs = fake.call_args
    assert odoo_client.SCHEDULE_TYPE_FIELD in kwargs["fields"]
    assert out == [
        {"id": 1, "name": "Standard", "is_flexible": False},
        {"id": 2, "name": "Flexible", "is_flexible": True},
    ]
