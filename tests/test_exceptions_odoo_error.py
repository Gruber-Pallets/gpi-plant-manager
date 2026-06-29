"""Odoo faults surfaced by inbox time-off actions must read as clean,
actionable English — never the raw ``<Fault N: '...\\n...'>`` repr.

Regression for the inbox card that showed
``<Fault 2: 'The following employees are not supposed to work during that
period:\\n Gerardo Vergara Quintero'>`` after an Approve click.
"""
import json
import xmlrpc.client
from datetime import date

from zira_dashboard.routes import exceptions as exceptions_route

_WORK_SCHEDULE_FAULT = xmlrpc.client.Fault(
    2,
    "The following employees are not supposed to work during that period:\n"
    " Gerardo Vergara Quintero",
)


def _body(resp):
    return json.loads(resp.body.decode())


def test_friendly_error_strips_fault_repr_and_collapses_newlines():
    msg = exceptions_route._friendly_odoo_error(_WORK_SCHEDULE_FAULT)
    assert "<Fault" not in msg
    assert "\n" not in msg
    # The specific employee context Odoo gave is preserved.
    assert "Gerardo Vergara Quintero" in msg


def test_friendly_error_adds_working_schedule_hint_for_schedule_conflict():
    msg = exceptions_route._friendly_odoo_error(_WORK_SCHEDULE_FAULT)
    assert "Working Schedule" in msg


def test_friendly_error_cleans_generic_fault():
    fault = xmlrpc.client.Fault(3, "Some other Odoo problem")
    msg = exceptions_route._friendly_odoo_error(fault)
    assert "<Fault" not in msg
    assert "Some other Odoo problem" in msg


def test_friendly_error_passes_through_plain_exception():
    msg = exceptions_route._friendly_odoo_error(ValueError("plain boom"))
    assert msg == "plain boom"


def test_approve_surfaces_clean_message_when_odoo_rejects(monkeypatch):
    from zira_dashboard import odoo_client

    row = {
        "id": 71, "person_odoo_id": 7, "person_name": "Gerardo Vergara",
        "leave_type": "Absence", "date_from": date(2026, 6, 27),
        "date_to": date(2026, 6, 27), "state": "confirm", "odoo_leave_id": 88,
    }
    monkeypatch.setattr(exceptions_route, "_load_time_off_request", lambda rid: row)

    def _raise(_leave_id):
        raise _WORK_SCHEDULE_FAULT

    monkeypatch.setattr(odoo_client, "approve_leave", _raise)

    resp = exceptions_route._approve_time_off_sync(71, source="inbox")

    assert resp.status_code == 500
    err = _body(resp)["error"]
    assert "<Fault" not in err
    assert "\n" not in err
    assert "Working Schedule" in err


def test_refuse_surfaces_clean_message_when_odoo_rejects(monkeypatch):
    from zira_dashboard import odoo_client

    row = {
        "id": 72, "person_odoo_id": 8, "person_name": "Carlos Ortega",
        "leave_type": "Unpaid", "date_from": date(2026, 6, 27),
        "date_to": date(2026, 6, 27), "state": "confirm", "odoo_leave_id": 89,
    }
    monkeypatch.setattr(exceptions_route, "_load_time_off_request", lambda rid: row)

    def _raise(_leave_id):
        raise xmlrpc.client.Fault(3, "Some other Odoo problem")

    monkeypatch.setattr(odoo_client, "refuse_leave", _raise)

    resp = exceptions_route._refuse_time_off_sync(72, reason="No coverage", source="inbox")

    assert resp.status_code == 500
    err = _body(resp)["error"]
    assert "<Fault" not in err
    assert "Some other Odoo problem" in err
