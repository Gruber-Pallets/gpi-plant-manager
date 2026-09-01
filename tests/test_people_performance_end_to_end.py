from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from tests.people_performance_fixtures import DAY, START, event, score, span
from tests.test_people_performance_data import install_sources
from zira_dashboard import people_performance_data
from zira_dashboard.app import app
from zira_dashboard.deps import templates
from zira_dashboard.forklift_event_store import ForkliftCompletionCoverage
from zira_dashboard.leaderboard import StationTotal
from zira_dashboard.routes import people_performance as route
from zira_dashboard.stations import Station


NOW = START + timedelta(minutes=300)
CALENDAR_END = datetime(2026, 8, 29, tzinfo=UTC)


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setitem(
        templates.env.globals,
        "nav_inbox_summary",
        lambda: {"total": 0, "urgent_total": 0, "source_errors": ()},
    )
    monkeypatch.setattr(route, "_utc_now", lambda: NOW)
    monkeypatch.setattr(route, "plant_today", lambda now: DAY)
    return TestClient(app)


@pytest.fixture
def installed_sources(monkeypatch):
    spans = (
        span(44, "Alex Worker", 0, 60, "Repair 1", is_open=False),
        span(44, "Alex Worker", 60, 300, "Tablets", is_open=True),
        span(45, "Robin Producer", 0, 300, "Repair 1", is_open=True),
    )
    call = event("Alex", 90, late=True)
    driver_row = {
        "day": DAY,
        "driver_id": call.driver_id,
        "name": "Alex",
        "calls": 1,
        "on_time": 0,
        "late": 1,
        "on_call_ms": 120000,
        "avg_ms": 60000,
        "max_ms": 60000,
        "available_ms": 3_600_000,
        "utilization_pct": 40,
    }
    station = Station("m1", "Repair 1", "Repair", "Recycling")
    total = StationTotal(
        station=station,
        units=10,
        reading_count=1,
        truncated=False,
        downtime_minutes=0,
        active_minutes=60,
        last_reading_at=START,
        last_status="Working",
        samples=((START + timedelta(minutes=30), 10),),
        active_intervals=((START, START + timedelta(minutes=60)),),
        downtime_intervals=(),
    )
    install_sources(
        monkeypatch,
        spans=spans,
        events=(call,),
        driver_rows=(driver_row,),
        coverage=ForkliftCompletionCoverage(
            day=DAY,
            covered_through_utc=CALENDAR_END,
            raw_event_count=1,
            successful_at=CALENDAR_END,
        ),
        calls_row={"day": DAY, "total_calls": 1},
        resolved={call.driver_id: 44},
        totals=(total,),
        catalog=(station,),
        scores=(
            score(44, "Alex Worker", "Repair 1", 0, 60, 10, 20),
            score(45, "Robin Producer", "Repair 1", 0, 300, 50, 60),
        ),
    )
    monkeypatch.setattr(
        people_performance_data.settings_store,
        "station_target",
        lambda station: 20.0,
    )


def test_people_dashboard_cross_source_day(client, installed_sources):
    response = client.get(f"/people-performance?day={DAY.isoformat()}")

    assert response.status_code == 200
    html = response.text
    assert html.count('data-person-id="44"') == 1
    assert "Repair 1" in html and "Tablets" in html
    assert "Transferred to Tablets" in html
    assert "Behind" in html
    assert "late call" in html.lower()
    assert "goal" in html.lower() and "uptime" in html.lower()


def test_each_source_degrades_without_false_zero(client, installed_sources, monkeypatch):
    monkeypatch.setattr(
        people_performance_data.production_history,
        "metered_station_totals",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("offline")),
    )

    response = client.get(f"/people-performance?day={DAY.isoformat()}")

    assert response.status_code == 200
    assert "Production data unavailable" in response.text
    production_html = response.text.split('data-section="production"', 1)[1].split("</section>", 1)[
        0
    ]
    assert ">N/A<" in production_html
    assert ">0%<" not in production_html


def test_attendance_failure_never_calls_schedule(client, installed_sources, monkeypatch):
    called = {"schedule": False}
    monkeypatch.setattr(
        people_performance_data.attendance_timeline,
        "snapshot_for_range",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("offline")),
    )
    monkeypatch.setattr(
        "zira_dashboard.staffing.load_schedule",
        lambda *args, **kwargs: called.update(schedule=True),
    )

    response = client.get(f"/people-performance?day={DAY.isoformat()}")

    assert response.status_code == 200
    assert "Attendance data unavailable" in response.text
    assert called["schedule"] is False


def test_forklift_failure_keeps_production(client, installed_sources, monkeypatch):
    monkeypatch.setattr(
        people_performance_data.forklift_event_store,
        "completion_events_for_range",
        lambda *args: (_ for _ in ()).throw(RuntimeError("offline")),
    )

    response = client.get(f"/people-performance?day={DAY.isoformat()}")

    assert response.status_code == 200
    assert "Forklift data unavailable" in response.text
    assert "Repair 1" in response.text and "Behind" in response.text


def test_route_uses_real_loader_instead_of_a_dashboard_stub():
    assert route.load_dashboard is people_performance_data.load_dashboard
