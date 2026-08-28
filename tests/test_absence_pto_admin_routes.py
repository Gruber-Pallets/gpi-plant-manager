from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from datetime import UTC, date, datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest
from starlette.requests import Request

from zira_dashboard import absence_pto_store as store
from zira_dashboard.routes import absence_pto_admin as routes


NOW = datetime(2026, 8, 28, 14, 30, tzinfo=UTC)


def _row(**changes) -> store.AbsencePtoRequest:
    row = store.AbsencePtoRequest(
        id=41,
        absence_day=date(2026, 8, 20),
        emp_id="44",
        person_odoo_id=44,
        person_name="Maria Example",
        holiday_status_id=7,
        leave_type_name="Paid Time Off",
        balance_at_submit=Decimal("4"),
        original_absence_leave_id=70,
        pto_leave_id=None,
        state="pending",
        conversion_step="not_started",
        employee_note="Please use PTO.",
        denial_reason=None,
        manual_resolution_note=None,
        sync_error=None,
        odoo_task_id=None,
        task_attempts=0,
        task_next_at=None,
        lease_owner=None,
        lease_until=None,
        requested_by_person_id=14,
        decided_by_upn=None,
        decided_by_name=None,
        requested_at=NOW,
        decided_at=None,
        resolved_at=None,
        created_at=NOW,
        updated_at=NOW,
    )
    return replace(row, **changes)


def _request(payload: object) -> Request:
    body = json.dumps(payload).encode()
    sent = False

    async def receive():
        nonlocal sent
        if sent:
            return {"type": "http.disconnect"}
        sent = True
        return {"type": "http.request", "body": body, "more_body": False}

    return Request({"type": "http", "method": "POST", "path": "/"}, receive)


def _body(response) -> dict:
    return json.loads(response.body.decode())


def _run(route, payload: object = None):
    return asyncio.run(route(41, _request({} if payload is None else payload)))


def test_approve_route_captures_actor_and_dispatches_one_sync_domain_call(monkeypatch):
    calls = []
    row = _row(state="approved", pto_leave_id=71)
    monkeypatch.setattr(
        routes.inbox_log,
        "actor_from",
        lambda request: ("dale@gruberpallets.com", "Dale Gruber"),
    )
    monkeypatch.setattr(
        routes.absence_pto_conversion,
        "approve",
        lambda *args: calls.append(args)
        or SimpleNamespace(status="approved", message="PTO approved.", request=row),
    )
    monkeypatch.setattr(routes, "_refresh_surfaces", lambda request: calls.append(("refresh", request.id)))

    response = _run(routes.approve_absence_pto, {"source": "page"})

    assert response.status_code == 200
    assert _body(response) == {
        "ok": True,
        "status": "approved",
        "message": "PTO approved.",
    }
    assert calls == [
        (41, "dale@gruberpallets.com", "Dale Gruber", "page"),
        ("refresh", 41),
    ]


@pytest.mark.parametrize(
    ("payload", "error"),
    [
        ([], "Request body must be a JSON object."),
        ({"source": []}, "Source must be text."),
        ({"source": {}}, "Source must be text."),
        ({"source": False}, "Source must be text."),
    ],
)
def test_approve_rejects_non_text_payload_values_before_actor_or_domain(
    monkeypatch, payload, error
):
    monkeypatch.setattr(
        routes.inbox_log,
        "actor_from",
        lambda request: pytest.fail("invalid JSON must fail before actor lookup"),
    )
    monkeypatch.setattr(
        routes.absence_pto_conversion,
        "approve",
        lambda *args: pytest.fail("invalid JSON must not reach conversion/Odoo"),
    )

    response = _run(routes.approve_absence_pto, payload)

    assert response.status_code == 400
    assert _body(response) == {"ok": False, "error": error}


@pytest.mark.parametrize(
    ("status", "message", "status_code", "message_key"),
    [
        ("busy", "This request is already being checked.", 409, "error"),
        ("pending", "The current PTO balance is below one day.", 409, "warning"),
        ("needs_review", "This needs payroll review.", 200, "warning"),
    ],
)
def test_approve_route_preserves_actionable_service_statuses(
    monkeypatch, status, message, status_code, message_key
):
    row = _row(state=status if status != "busy" else "pending")
    monkeypatch.setattr(routes.inbox_log, "actor_from", lambda request: ("manager@gruberpallets.com", "Manager"))
    monkeypatch.setattr(
        routes.absence_pto_conversion,
        "approve",
        lambda *args: SimpleNamespace(
            status=status,
            message=message,
            request=None if status == "busy" else row,
        ),
    )
    refreshed = []
    monkeypatch.setattr(routes, "_refresh_surfaces", lambda request: refreshed.append(request.id))

    response = _run(routes.approve_absence_pto, {"source": "page"})
    body = _body(response)

    assert response.status_code == status_code
    assert body["ok"] is (status == "needs_review")
    assert body["status"] == status
    assert body[message_key] == message
    assert refreshed == ([] if status == "busy" else [41])


def test_deny_route_requires_reason_before_calling_domain(monkeypatch):
    monkeypatch.setattr(
        routes.absence_pto,
        "deny",
        lambda *args: pytest.fail("blank reason must not reach the domain"),
    )

    response = _run(routes.deny_absence_pto, {"reason": "   ", "source": "page"})

    assert response.status_code == 400
    assert _body(response) == {"ok": False, "error": "A reason is required to deny."}


@pytest.mark.parametrize(
    ("payload", "error"),
    [
        ([], "Request body must be a JSON object."),
        ({"reason": []}, "Reason must be text."),
        ({"reason": {}}, "Reason must be text."),
        ({"reason": False}, "Reason must be text."),
        ({"reason": "No", "source": []}, "Source must be text."),
        ({"reason": "No", "source": {}}, "Source must be text."),
        ({"reason": "No", "source": True}, "Source must be text."),
    ],
)
def test_deny_rejects_non_text_payload_values_before_actor_or_domain(
    monkeypatch, payload, error
):
    monkeypatch.setattr(
        routes.inbox_log,
        "actor_from",
        lambda request: pytest.fail("invalid JSON must fail before actor lookup"),
    )
    monkeypatch.setattr(
        routes.absence_pto,
        "deny",
        lambda *args: pytest.fail("invalid JSON must not reach denial/Odoo"),
    )

    response = _run(routes.deny_absence_pto, payload)

    assert response.status_code == 400
    assert _body(response) == {"ok": False, "error": error}


def test_deny_route_captures_actor_refreshes_cache_and_returns_decision(monkeypatch):
    calls = []
    denied = _row(state="denied", denial_reason="Not enough PTO")
    monkeypatch.setattr(
        routes.inbox_log,
        "actor_from",
        lambda request: ("dale@gruberpallets.com", "Dale Gruber"),
    )
    monkeypatch.setattr(
        routes.absence_pto,
        "deny",
        lambda *args: calls.append(args) or denied,
    )
    monkeypatch.setattr(routes, "_refresh_surfaces", lambda request: calls.append(("refresh", request.id)))

    response = _run(
        routes.deny_absence_pto,
        {"reason": "  Not enough PTO  ", "source": "page"},
    )

    assert response.status_code == 200
    assert _body(response) == {
        "ok": True,
        "status": "denied",
        "message": "The past PTO request was denied.",
    }
    assert calls == [
        (41, "dale@gruberpallets.com", "Dale Gruber", "Not enough PTO", "page"),
        ("refresh", 41),
    ]


def test_deny_route_refreshes_after_committed_denial_even_if_release_raises(
    monkeypatch,
):
    pending = _row()
    denied = _row(state="denied", denial_reason="Not enough PTO")
    refreshed = []
    monkeypatch.setattr(
        routes.inbox_log,
        "actor_from",
        lambda request: ("dale@gruberpallets.com", "Dale Gruber"),
    )
    monkeypatch.setattr(
        routes.absence_pto.absence_pto_store,
        "claim_request",
        lambda *args, **kwargs: pending,
    )
    monkeypatch.setattr(
        routes.absence_pto.absence_pto_store,
        "finalize_denied",
        lambda *args, **kwargs: denied,
    )
    monkeypatch.setattr(
        routes.absence_pto.absence_pto_store,
        "release_claim",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("release failed")),
    )
    monkeypatch.setattr(
        routes, "_refresh_surfaces", lambda request: refreshed.append(request)
    )

    response = _run(
        routes.deny_absence_pto,
        {"reason": "Not enough PTO", "source": "page"},
    )

    assert response.status_code == 200
    assert _body(response)["status"] == "denied"
    assert refreshed == [denied]


def test_handled_route_requires_note_before_calling_domain(monkeypatch):
    monkeypatch.setattr(
        routes.absence_pto_review,
        "resolve_manually",
        lambda *args: pytest.fail("blank note must not reach the domain"),
    )

    response = _run(routes.handle_absence_pto, {"note": ""})

    assert response.status_code == 400
    assert _body(response) == {"ok": False, "error": "A note is required to mark this handled."}


@pytest.mark.parametrize(
    ("payload", "error"),
    [
        ([], "Request body must be a JSON object."),
        ({"note": []}, "Note must be text."),
        ({"note": {}}, "Note must be text."),
        ({"note": False}, "Note must be text."),
        ({"note": "Handled", "source": []}, "Source must be text."),
        ({"note": "Handled", "source": {}}, "Source must be text."),
        ({"note": "Handled", "source": True}, "Source must be text."),
    ],
)
def test_handled_rejects_non_text_payload_values_before_actor_or_domain(
    monkeypatch, payload, error
):
    monkeypatch.setattr(
        routes.inbox_log,
        "actor_from",
        lambda request: pytest.fail("invalid JSON must fail before actor lookup"),
    )
    monkeypatch.setattr(
        routes.absence_pto_review,
        "resolve_manually",
        lambda *args: pytest.fail("invalid JSON must not reach manual resolution/Odoo"),
    )

    response = _run(routes.handle_absence_pto, payload)

    assert response.status_code == 400
    assert _body(response) == {"ok": False, "error": error}


def test_handled_route_captures_actor_refreshes_cache_and_returns_result(monkeypatch):
    calls = []
    handled = _row(state="resolved_manually", manual_resolution_note="Paid another way")
    monkeypatch.setattr(
        routes.inbox_log,
        "actor_from",
        lambda request: ("wendy@gruberpallets.com", "Wendy"),
    )
    monkeypatch.setattr(
        routes.absence_pto_review,
        "resolve_manually",
        lambda *args: calls.append(args)
        or SimpleNamespace(
            status="resolved_manually",
            task_id=501,
            request=handled,
            message="The review was marked handled.",
        ),
    )
    monkeypatch.setattr(routes, "_refresh_surfaces", lambda request: calls.append(("refresh", request.id)))

    response = _run(routes.handle_absence_pto, {"note": " Paid another way "})

    assert response.status_code == 200
    assert _body(response) == {
        "ok": True,
        "status": "resolved_manually",
        "message": "The review was marked handled.",
    }
    assert calls == [
        (41, "wendy@gruberpallets.com", "Wendy", "Paid another way"),
        ("refresh", 41),
    ]


def test_cache_refresh_still_clears_page_cache_when_staffing_cache_fails(monkeypatch):
    events = []

    def fail_staffing(day):
        events.append(("staffing", day))
        raise RuntimeError("staffing cache unavailable")

    monkeypatch.setattr(routes.staffing, "invalidate_schedule_cache", fail_staffing)
    monkeypatch.setattr(
        routes._http_cache,
        "invalidate_all_cache",
        lambda: events.append(("page", None)),
    )

    routes._refresh_surfaces(_row())

    assert events == [
        ("staffing", date(2026, 8, 20)),
        ("page", None),
    ]
