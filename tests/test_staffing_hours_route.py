"""HTTP contracts for the read-only Staffing Hours report."""

from __future__ import annotations

from datetime import UTC, date, datetime
from html import unescape
from importlib import import_module
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import pytest
from starlette.testclient import TestClient

from zira_dashboard.app import app


TODAY = date(2026, 8, 27)


def _staffing_hours_route_is_registered() -> bool:
    return any(
        any(
            getattr(candidate, "path", None) == "/staffing/hours"
            for candidate in getattr(getattr(route, "original_router", None), "routes", ())
        )
        for route in app.routes
    )


def _stub_hours_dependencies(
    monkeypatch,
    *,
    attendance_error=None,
    payroll_error=None,
    departments_error=None,
    batch_error=None,
):
    """Keep route tests focused on URL-backed rendering, not Odoo or Postgres."""
    from zira_dashboard import odoo_client, staffing, staffing_hours

    route = import_module("zira_dashboard.routes.staffing_hours")
    calls = {"attendance": [], "payroll": [], "departments": []}
    roster = [
        SimpleNamespace(name="Ana", employee_id=7, active=True, reserve=False),
        SimpleNamespace(name="Reserve", employee_id=8, active=True, reserve=True),
        SimpleNamespace(name="Former", employee_id=9, active=False, reserve=False),
    ]

    monkeypatch.setattr(staffing, "load_roster", lambda: roster)
    monkeypatch.setattr(staffing_hours.app_settings, "get_setting", lambda _key: None)
    monkeypatch.setattr(route, "plant_today", lambda: TODAY)
    monkeypatch.setattr(route, "plant_now", lambda: datetime(2026, 8, 28, 12, tzinfo=UTC))

    def fetch_batches(_start, _end):
        if batch_error:
            raise batch_error
        return []

    def fetch_attendance(employee_ids, start, end):
        calls["attendance"].append((employee_ids, start, end))
        if attendance_error:
            raise attendance_error
        return [
            {
                "employee_odoo_id": 7,
                "check_in": "2026-08-25T12:00:00+00:00",
                "check_out": "2026-08-25T22:00:00+00:00",
            },
            {
                "employee_odoo_id": 7,
                "check_in": "2026-08-26T12:00:00+00:00",
                "check_out": "2026-08-27T20:00:00+00:00",
            },
        ]

    def fetch_payroll(employee_ids, start, end):
        calls["payroll"].append((employee_ids, start, end))
        if payroll_error:
            raise payroll_error
        return [
            {"employee_id": 7, "date": start, "type_code": "WORK100", "duration": 38, "active": True},
            {"employee_id": 7, "date": end, "type_code": "OVERTIME", "duration": 3, "active": True},
        ]

    def fetch_departments(employee_ids):
        calls["departments"].append(employee_ids)
        if departments_error:
            raise departments_error
        return {7: "Recycled"}

    monkeypatch.setattr(odoo_client, "fetch_payroll_batches", fetch_batches)
    monkeypatch.setattr(odoo_client, "fetch_attendance_intervals_for_range", fetch_attendance)
    monkeypatch.setattr(odoo_client, "fetch_payroll_work_entries", fetch_payroll)
    monkeypatch.setattr(odoo_client, "fetch_employee_departments", fetch_departments)
    return calls


def _staffing_hours_links(response, class_name):
    """Return the parsed query state from links in one Hours control group."""
    import re

    pattern = rf'<a href="([^"]+)" class="[^"]*{class_name}[^"]*"'
    return [
        parse_qs(urlparse(unescape(href)).query, keep_blank_values=True)
        for href in re.findall(pattern, response.text)
    ]


def test_hours_route_preserves_filters_and_renders_clocked_total(monkeypatch):
    assert _staffing_hours_route_is_registered(), "GET /staffing/hours must be registered"
    calls = _stub_hours_dependencies(monkeypatch)

    response = TestClient(app).get(
        "/staffing/hours?source=clocked&range=custom&start=2026-08-16&end=2026-08-29"
        "&q=Ana&department=Recycled&attention=over_40"
    )

    assert response.status_code == 200
    assert "Ana" in response.text and "42.0" in response.text
    assert "Clocked time" in response.text
    assert calls["attendance"] == [((7,), date(2026, 8, 16), date(2026, 8, 29))]
    assert calls["payroll"] == []
    assert calls["departments"] == [(7,)]

    common_state = {
        "start": ["2026-08-16"],
        "end": ["2026-08-29"],
        "q": ["Ana"],
        "department": ["Recycled"],
    }
    source_links = _staffing_hours_links(response, "hours-source")
    range_links = _staffing_hours_links(response, "hours-range-chip")
    summary_links = _staffing_hours_links(response, "hours-summary-chip")
    assert len(source_links) == 2
    assert len(range_links) == 6
    assert len(summary_links) == 3
    for link in source_links + range_links + summary_links:
        assert {key: link[key] for key in common_state} == common_state
    assert all(link["range"] == ["custom"] for link in source_links + summary_links)
    assert {tuple(link["range"]) for link in range_links} == {
        ("this_week",), ("last_week",), ("this_pay_period",),
        ("last_pay_period",), ("this_month",), ("last_month",),
    }
    assert {tuple(link["source"]) for link in source_links} == {("clocked",), ("payroll",)}
    assert all(link["source"] == ["clocked"] for link in range_links + summary_links)
    assert all(link["attention"] == ["over_40"] for link in source_links + range_links)
    assert {tuple(link["attention"]) for link in summary_links} == {
        ("approaching_40",), ("over_40",), ("attention",)
    }


def test_hours_route_defaults_to_clocked_this_week_and_supports_payroll(monkeypatch):
    assert _staffing_hours_route_is_registered(), "GET /staffing/hours must be registered"
    calls = _stub_hours_dependencies(monkeypatch)
    client = TestClient(app)

    default_response = client.get("/staffing/hours")
    payroll_response = client.get("/staffing/hours?source=payroll&range=last_month")

    assert default_response.status_code == payroll_response.status_code == 200
    assert "Clocked time" in default_response.text
    assert "Payroll hours" in payroll_response.text
    assert "Regular" in payroll_response.text and "Overtime" in payroll_response.text
    assert len(calls["attendance"]) == 1
    assert len(calls["payroll"]) == 1


def test_hours_route_renders_an_error_for_invalid_filters_without_rows(monkeypatch):
    assert _staffing_hours_route_is_registered(), "GET /staffing/hours must be registered"
    _stub_hours_dependencies(monkeypatch)

    response = TestClient(app).get("/staffing/hours?source=scheduled&range=unknown")

    assert response.status_code == 200
    assert "Choose a valid date range." in response.text
    assert "Ana" not in response.text


@pytest.mark.parametrize(
    ("source", "failure"),
    [
        ("clocked", "attendance_error"),
        ("payroll", "payroll_error"),
        ("clocked", "departments_error"),
    ],
)
def test_hours_route_never_renders_partial_source_data(monkeypatch, source, failure):
    assert _staffing_hours_route_is_registered(), "GET /staffing/hours must be registered"
    _stub_hours_dependencies(monkeypatch, **{failure: RuntimeError("Odoo down")})

    response = TestClient(app).get(f"/staffing/hours?source={source}")

    assert response.status_code == 200
    assert "Hours could not be refreshed. Try again soon." in response.text
    assert "Ana" not in response.text


def test_hours_route_keeps_the_anchor_fallback_notice_for_batch_failures(monkeypatch):
    assert _staffing_hours_route_is_registered(), "GET /staffing/hours must be registered"
    _stub_hours_dependencies(monkeypatch, batch_error=RuntimeError("Odoo unavailable"))

    response = TestClient(app).get("/staffing/hours?range=this_pay_period")

    assert response.status_code == 200
    assert "Odoo could not verify this pay period." in response.text
