from __future__ import annotations

from datetime import UTC, date, datetime
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.testclient import TestClient


DAY = date(2026, 9, 2)


def _event(
    event_id: str,
    driver_id: str,
    driver_name: str,
    hour: int,
    minute: int,
):
    from zira_dashboard.forklift_ingest import ForkliftCompletionEvent

    return ForkliftCompletionEvent(
        event_id=event_id,
        driver_id=driver_id,
        driver_name=driver_name,
        created_at_utc=datetime(2026, 9, 2, hour, minute, tzinfo=UTC),
        workstation_name="Private workstation payload",
        on_time=True,
        late=False,
        response_ms=12_000,
        handling_ms=24_000,
    )


def test_identity_context_aggregates_unresolved_calls(monkeypatch):
    from zira_dashboard import (
        forklift_event_store,
        forklift_identity_store,
        forklift_store,
        staffing,
    )
    import zira_dashboard.forklift_identity_view as forklift_identity_view

    events = (
        _event("raw-call-1", "driver-8", "Alex", 12, 10),
        _event("raw-call-2", "driver-8", "A.", 12, 30),
        _event("raw-call-3", "driver-8", "Alex", 13, 0),
        _event("raw-call-4", "driver-8", "A.", 13, 20),
        _event("raw-call-5", "driver-8", "Alex", 13, 45),
    )
    roster = [
        staffing.Person("Sam Rivera", active=True, employee_id=708),
        staffing.Person("Inactive Person", active=False, employee_id=709),
        staffing.Person("No Odoo ID", active=True, employee_id=None),
        staffing.Person("Alex Chen", active=True, employee_id=707),
    ]
    mapping = SimpleNamespace(
        external_driver_id="driver-old",
        source_name="Sam",
        employee_odoo_id=708,
        employee_name="Sam Rivera",
        version=3,
        updated_at=datetime(2026, 9, 1, 15, 30, tzinfo=UTC),
        updated_by_upn="manager@example.com",
    )
    captured = {}
    monkeypatch.setattr(
        forklift_event_store,
        "completion_events_for_range",
        lambda start, end: captured.update(start=start, end=end) or events,
    )
    monkeypatch.setattr(staffing, "load_roster", lambda: roster)
    monkeypatch.setattr(forklift_identity_store, "list_mappings", lambda: (mapping,))

    def resolve(evidence, *, allowed_employee_ids):
        captured["evidence"] = evidence
        captured["allowed_employee_ids"] = allowed_employee_ids
        return {}

    monkeypatch.setattr(forklift_store, "resolve_forklift_driver_ids", resolve)

    context = forklift_identity_view.identity_context(DAY)

    assert context["day"] == "2026-09-02"
    assert context["unresolved"] == ({
        "external_driver_id": "driver-8",
        "source_names": ("Alex", "A."),
        "call_count": 5,
        "first_call": "7:10 AM",
        "last_call": "8:45 AM",
        "name_conflict": True,
        "version": None,
    },)
    assert context["mappings"][0]["employee_name"] == "Sam Rivera"
    assert [row["employee_name"] for row in context["employee_options"]] == [
        "Alex Chen", "Sam Rivera"
    ]
    assert captured["start"] == datetime(2026, 9, 2, 5, 0, tzinfo=UTC)
    assert captured["end"] == datetime(2026, 9, 3, 5, 0, tzinfo=UTC)
    assert captured["evidence"] == {"driver-8": {"Alex", "A."}}
    assert captured["allowed_employee_ids"] == {707, 708}
    assert "raw-call-1" not in repr(context)
    assert "Private workstation payload" not in repr(context)


def _identity_context():
    return {
        "day": DAY.isoformat(),
        "mappings": ({
            "external_driver_id": "driver-old",
            "source_name": "Sam",
            "employee_odoo_id": 708,
            "employee_name": "Sam Rivera",
            "version": 4,
            "updated_at": "Sep 1, 10:30 AM",
            "updated_by_upn": "manager@example.com",
        },),
        "unresolved": ({
            "external_driver_id": "driver-8",
            "source_names": ("Alex", "A."),
            "call_count": 5,
            "first_call": "7:10 AM",
            "last_call": "8:45 AM",
            "name_conflict": True,
            "version": None,
        },),
        "employee_options": (
            {"employee_odoo_id": 707, "employee_name": "Alex Chen"},
            {"employee_odoo_id": 708, "employee_name": "Sam Rivera"},
        ),
    }


@pytest.fixture
def identity_client(monkeypatch):
    from zira_dashboard.routes import forklift_identities

    test_app = FastAPI()

    @test_app.middleware("http")
    async def identity(request: Request, call_next):
        if request.headers.get("x-test-upn"):
            request.state.user_upn = request.headers["x-test-upn"]
        if request.headers.get("x-test-name"):
            request.state.user_name = request.headers["x-test-name"]
        return await call_next(request)

    test_app.include_router(forklift_identities.router)
    monkeypatch.setattr(forklift_identities, "plant_today", lambda: DAY)
    monkeypatch.setattr(
        forklift_identities.forklift_identity_view,
        "identity_context",
        lambda day: _identity_context(),
    )
    return TestClient(test_app, follow_redirects=False)


MANAGER_HEADERS = {
    "x-test-upn": "manager@example.com",
    "x-test-name": "Floor Manager",
}


def test_save_identity_redirects_to_selected_day_with_authenticated_actor(
    identity_client, monkeypatch
):
    from zira_dashboard.routes import forklift_identities

    saved_call = {}
    monkeypatch.setattr(
        forklift_identities.forklift_identity_store,
        "save_mapping",
        lambda *args, **kwargs: saved_call.update(args=args, **kwargs),
    )

    response = identity_client.post(
        "/settings/forklift-identities",
        data={
            "action": "save",
            "external_driver_id": "driver-8",
            "employee_odoo_id": "708",
            "expected_version": "",
            "day": "2026-09-02",
        },
        headers=MANAGER_HEADERS,
    )

    assert response.status_code == 303
    assert response.headers["location"] == (
        "/settings?section=forklift&identity_day=2026-09-02&identity_saved=1"
        "#forklift-identities"
    )
    assert saved_call == {
        "args": ("driver-8", "Alex / A.", 708),
        "expected_version": None,
        "actor_upn": "manager@example.com",
        "actor_name": "Floor Manager",
    }


def test_remove_identity_passes_posted_version_and_actor(identity_client, monkeypatch):
    from zira_dashboard.routes import forklift_identities

    removed_call = {}
    monkeypatch.setattr(
        forklift_identities.forklift_identity_store,
        "remove_mapping",
        lambda *args, **kwargs: removed_call.update(args=args, **kwargs),
    )

    response = identity_client.post(
        "/settings/forklift-identities",
        data={
            "action": "remove",
            "external_driver_id": "driver-old",
            "expected_version": "4",
            "day": DAY.isoformat(),
        },
        headers={**MANAGER_HEADERS, "accept": "application/json"},
    )

    assert response.status_code == 200
    assert response.json() == {"ok": True}
    assert removed_call == {
        "args": ("driver-old",),
        "expected_version": 4,
        "actor_upn": "manager@example.com",
        "actor_name": "Floor Manager",
    }


@pytest.mark.parametrize(
    "form",
    [
        {
            "action": "save",
            "external_driver_id": "driver-8",
            "employee_odoo_id": "not-an-id",
            "expected_version": "",
            "day": DAY.isoformat(),
        },
        {
            "action": "save",
            "external_driver_id": "driver-8",
            "employee_odoo_id": "708",
            "expected_version": "bad-version",
            "day": DAY.isoformat(),
        },
        {
            "action": "save",
            "external_driver_id": "driver-not-shown",
            "employee_odoo_id": "708",
            "expected_version": "",
            "day": DAY.isoformat(),
        },
        {
            "action": "remove",
            "external_driver_id": "driver-old",
            "expected_version": "",
            "day": DAY.isoformat(),
        },
    ],
)
def test_identity_route_rejects_malformed_or_out_of_scope_ids(
    identity_client, monkeypatch, form
):
    from zira_dashboard.routes import forklift_identities

    monkeypatch.setattr(
        forklift_identities.forklift_identity_store,
        "save_mapping",
        lambda *args, **kwargs: pytest.fail("invalid save reached the store"),
    )
    monkeypatch.setattr(
        forklift_identities.forklift_identity_store,
        "remove_mapping",
        lambda *args, **kwargs: pytest.fail("invalid remove reached the store"),
    )

    response = identity_client.post(
        "/settings/forklift-identities",
        data=form,
        headers={**MANAGER_HEADERS, "accept": "application/json"},
    )

    assert response.status_code == 422
    assert response.json() == {
        "ok": False,
        "error": "Choose a valid active employee.",
    }


@pytest.mark.parametrize(
    ("raw_day", "expected_error"),
    [
        ("not-a-day", "Choose a valid day."),
        ("2026-09-03", "Choose today or an earlier day."),
    ],
)
def test_identity_route_rejects_invalid_day_before_store(
    identity_client, monkeypatch, raw_day, expected_error
):
    from zira_dashboard.routes import forklift_identities

    monkeypatch.setattr(
        forklift_identities.forklift_identity_store,
        "save_mapping",
        lambda *args, **kwargs: pytest.fail("invalid date reached the store"),
    )

    response = identity_client.post(
        "/settings/forklift-identities",
        data={
            "action": "save",
            "external_driver_id": "driver-8",
            "employee_odoo_id": "708",
            "expected_version": "",
            "day": raw_day,
        },
        headers={**MANAGER_HEADERS, "accept": "application/json"},
    )

    assert response.status_code == 422
    assert response.json() == {"ok": False, "error": expected_error}


def test_identity_route_requires_authenticated_actor(identity_client, monkeypatch):
    from zira_dashboard.routes import forklift_identities

    monkeypatch.setenv("AUTH_DISABLED", "0")
    monkeypatch.setattr(
        forklift_identities.forklift_identity_store,
        "save_mapping",
        lambda *args, **kwargs: pytest.fail("signed-out save reached the store"),
    )

    response = identity_client.post(
        "/settings/forklift-identities",
        data={
            "action": "save",
            "external_driver_id": "driver-8",
            "employee_odoo_id": "708",
            "expected_version": "",
            "day": DAY.isoformat(),
        },
        headers={"accept": "application/json"},
    )

    assert response.status_code == 401
    assert response.json() == {
        "ok": False,
        "error": "Sign in again before changing identities.",
    }


def test_identity_route_returns_safe_mapping_conflict(identity_client, monkeypatch):
    from zira_dashboard.routes import forklift_identities

    def stale(*args, **kwargs):
        raise forklift_identities.forklift_identity_store.MappingConflict(
            "This forklift identity changed. Reload and try again."
        )

    monkeypatch.setattr(
        forklift_identities.forklift_identity_store, "save_mapping", stale
    )

    response = identity_client.post(
        "/settings/forklift-identities",
        data={
            "action": "save",
            "external_driver_id": "driver-8",
            "employee_odoo_id": "708",
            "expected_version": "",
            "day": DAY.isoformat(),
        },
        headers={**MANAGER_HEADERS, "accept": "application/json"},
    )

    assert response.status_code == 409
    assert response.json() == {
        "ok": False,
        "error": "This forklift identity changed. Reload and try again.",
    }


def test_identity_route_redacts_unexpected_store_error(identity_client, monkeypatch):
    from zira_dashboard.routes import forklift_identities

    def unavailable(*args, **kwargs):
        raise RuntimeError("secret database details")

    monkeypatch.setattr(
        forklift_identities.forklift_identity_store, "save_mapping", unavailable
    )

    response = identity_client.post(
        "/settings/forklift-identities",
        data={
            "action": "save",
            "external_driver_id": "driver-8",
            "employee_odoo_id": "708",
            "expected_version": "",
            "day": DAY.isoformat(),
        },
        headers={**MANAGER_HEADERS, "accept": "application/json"},
    )

    assert response.status_code == 503
    assert response.json() == {
        "ok": False,
        "error": "Forklift identities are unavailable right now. No change was made.",
    }
    assert "secret database details" not in response.text


def test_main_app_registers_forklift_identity_route():
    from zira_dashboard.app import app
    from zira_dashboard.page_views import _leaf_routes

    assert any(
        getattr(route, "path", None) == "/settings/forklift-identities"
        and "POST" in (getattr(route, "methods", None) or set())
        for route in _leaf_routes(app.routes)
    )


def _stub_settings_page(monkeypatch):
    from zira_dashboard import (
        auto_lunch_settings,
        db,
        forklift_advisor,
        forklift_settings,
        odoo_sync,
        rounding_system_store,
        saturday_schedule_store,
        schedule_store,
        shift_config,
        staffing,
        staffing_hours,
        work_centers_store,
        work_schedule_store,
    )
    from zira_dashboard.routes import settings

    captured = {}
    monkeypatch.setattr(odoo_sync, "sync", lambda force=False: None)
    monkeypatch.setattr(settings, "_settings_default_auto_work_centers", lambda: [])
    monkeypatch.setattr(shift_config, "productive_minutes_per_day", lambda: 480)
    monkeypatch.setattr(staffing, "load_roster", lambda: [])
    monkeypatch.setattr(
        work_centers_store,
        "effective",
        lambda loc: {
            "goal_per_day": 0,
            "min_ops": loc.min_ops,
            "max_ops": loc.max_ops,
            "required_skills": [],
            "note": "",
            "groups": [],
            "department": "",
            "default_people": [],
        },
    )
    monkeypatch.setattr(work_centers_store, "all_group_names", lambda kind: [])
    monkeypatch.setattr(work_centers_store, "synced_departments", lambda: [])
    monkeypatch.setattr(work_centers_store, "registered_groups", lambda: [])
    monkeypatch.setattr(schedule_store, "current", lambda: schedule_store.DEFAULT_SCHEDULE)
    monkeypatch.setattr(
        saturday_schedule_store, "current", lambda: saturday_schedule_store.DEFAULT
    )
    monkeypatch.setattr(work_schedule_store, "all_overrides", lambda: [])
    monkeypatch.setattr(rounding_system_store, "all_systems", lambda: [])
    monkeypatch.setattr(rounding_system_store, "department_map", lambda: {})
    monkeypatch.setattr(db, "query", lambda sql, params=None: [])
    monkeypatch.setattr(auto_lunch_settings, "current", lambda: auto_lunch_settings.DEFAULT)
    monkeypatch.setattr(auto_lunch_settings, "recent_events", lambda limit: [])
    monkeypatch.setattr(
        forklift_settings,
        "current",
        lambda: SimpleNamespace(
            enabled=True,
            include_loading_jockeying=False,
            coldstart_calls_per_day=0,
        ),
    )
    monkeypatch.setattr(
        forklift_advisor,
        "demand_summary",
        lambda day: {"recommended": 4, "advisor_marker": "still-loaded"},
    )
    monkeypatch.setattr(settings, "_forklift_score_ctx", lambda current: {})
    monkeypatch.setattr(
        staffing_hours,
        "current_pay_period_config",
        lambda: SimpleNamespace(anchor=DAY, cycle_days=14),
    )
    monkeypatch.setattr(settings, "plant_today", lambda: DAY)

    def template_response(request, name, context):
        captured.update(name=name, context=context)
        return HTMLResponse("rendered")

    monkeypatch.setattr(settings.templates, "TemplateResponse", template_response)
    test_app = FastAPI()
    test_app.include_router(settings.router)
    return TestClient(test_app), captured


def test_settings_loads_identity_context_only_for_forklift_section(monkeypatch):
    from zira_dashboard import forklift_identity_view

    client, captured = _stub_settings_page(monkeypatch)
    loaded_days = []
    monkeypatch.setattr(
        forklift_identity_view,
        "identity_context",
        lambda day: loaded_days.append(day) or _identity_context(),
    )

    response = client.get(
        "/settings?section=forklift&identity_day=2026-09-01"
    )

    assert response.status_code == 200
    assert loaded_days == [date(2026, 9, 1)]
    assert captured["context"]["forklift_identities"] == _identity_context()
    assert captured["context"]["identity_saved"] is False
    assert captured["context"]["identity_error"] == ""
    assert captured["context"]["today"] == "2026-09-02"

    loaded_days.clear()
    assert client.get("/settings?section=integrations").status_code == 200
    assert loaded_days == []


def test_settings_rejects_future_identity_day(monkeypatch):
    client, _captured = _stub_settings_page(monkeypatch)

    response = client.get(
        "/settings?section=forklift&identity_day=2026-09-03"
    )

    assert response.status_code == 400
    assert response.json() == {"detail": "Choose today or an earlier day"}


def test_settings_identity_failure_isolated_from_demand_advisor(monkeypatch):
    from zira_dashboard import forklift_identity_view

    client, captured = _stub_settings_page(monkeypatch)

    def unavailable(day):
        raise RuntimeError("private source failure")

    monkeypatch.setattr(forklift_identity_view, "identity_context", unavailable)

    response = client.get("/settings?section=forklift")

    assert response.status_code == 200
    context = captured["context"]
    assert context["forklift"]["advisor_marker"] == "still-loaded"
    assert context["forklift_identities"] == {
        "day": DAY.isoformat(),
        "mappings": (),
        "unresolved": (),
        "employee_options": (),
        "unavailable": "Forklift identities are unavailable right now. Try again later.",
    }
    assert "private source failure" not in response.text
