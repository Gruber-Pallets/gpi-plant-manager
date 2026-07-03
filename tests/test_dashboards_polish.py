import os
from datetime import datetime, time, timezone
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from zira_dashboard import staffing
from zira_dashboard.app import app

# Dashboards-polish tests render full pages via TestClient, which
# transitively hits the work-centers store + schedule store DB lookups
# despite the monkeypatches at the staffing layer. Need a real DB.
pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set; dashboard render tests need Postgres",
)


def _freeze_route_clock_mid_shift(monkeypatch):
    """Pin the departments route's `now` to 13:00 plant time today.

    The recycling page resolves its who-worked-where labels from assignment
    windows capped at min(now, shift_end). When the suite runs before the
    07:00 America/Chicago shift start (CI pushes early in the morning),
    every schedule segment collapses to zero length and today's view renders
    "(no assignment)" in place of the operators' names. Freezing `now`
    mid-shift keeps the today-view name assertions independent of the
    wall-clock hour the tests happen to run at.
    """
    from zira_dashboard import shift_config
    from zira_dashboard.plant_day import today as plant_today
    from zira_dashboard.routes import departments

    frozen_utc = datetime.combine(
        plant_today(), time(13, 0), tzinfo=shift_config.SITE_TZ
    ).astimezone(timezone.utc)

    class _FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            if tz is None:
                return frozen_utc.replace(tzinfo=None)
            return frozen_utc.astimezone(tz)

    monkeypatch.setattr(departments, "datetime", _FrozenDatetime)


def test_recycling_headline_uses_per_person_rate(monkeypatch):
    # 100 units across 1.0 elapsed hour with 2 scheduled people = 50 / hr / person
    monkeypatch.setattr(staffing, "load_schedule", lambda d: staffing.Schedule(
        day=d, published=True,
        assignments={"Repair-1": ["Alice"], "Repair-2": ["Bob"]},
    ))
    with patch("zira_dashboard.routes.departments.leaderboard") as lb, \
         patch("zira_dashboard.routes.departments.shift_elapsed_minutes", return_value=60):
        from zira_dashboard.leaderboard import StationTotal
        from zira_dashboard.stations import Station
        s1 = Station(meter_id="m1", name="Repair-1", category="Repair", cell="Recycling")
        s2 = Station(meter_id="m2", name="Repair-2", category="Repair", cell="Recycling")
        lb.return_value = [
            StationTotal(s1, units=50, reading_count=1, truncated=False, downtime_minutes=0,
                         active_minutes=60, last_reading_at=None, last_status=None,
                         samples=(), active_intervals=()),
            StationTotal(s2, units=50, reading_count=1, truncated=False, downtime_minutes=0,
                         active_minutes=60, last_reading_at=None, last_status=None,
                         samples=(), active_intervals=()),
        ]
        client = TestClient(app)
        resp = client.get("/recycling")
    assert resp.status_code == 200
    html = resp.text
    # Headline label changed AND value reflects /person denominator
    assert "pallets/hr/person" in html
    assert ">50.0<" in html or ">50<" in html


def test_recycling_bar_row_renders_person_and_wc_stacked(monkeypatch):
    _freeze_route_clock_mid_shift(monkeypatch)
    monkeypatch.setattr(staffing, "load_schedule", lambda d: staffing.Schedule(
        day=d, published=True,
        assignments={"Repair-1": ["Alice"]},
    ))
    with patch("zira_dashboard.routes.departments.leaderboard") as lb:
        from zira_dashboard.leaderboard import StationTotal
        from zira_dashboard.stations import Station
        s1 = Station(meter_id="m1", name="Repair-1", category="Repair", cell="Recycling")
        lb.return_value = [
            StationTotal(s1, units=10, reading_count=1, truncated=False, downtime_minutes=0,
                         active_minutes=60, last_reading_at=None, last_status=None,
                         samples=(), active_intervals=()),
        ]
        client = TestClient(app)
        html = client.get("/recycling").text
    assert "name-primary" in html
    assert "name-secondary" in html
    assert "Alice" in html and "Repair-1" in html


def test_recycling_bar_row_no_assignment_fallback(monkeypatch):
    monkeypatch.setattr(staffing, "load_schedule", lambda d: staffing.Schedule(
        day=d, published=True, assignments={},
    ))
    with patch("zira_dashboard.routes.departments.leaderboard") as lb:
        from zira_dashboard.leaderboard import StationTotal
        from zira_dashboard.stations import Station
        s1 = Station(meter_id="m1", name="Repair-1", category="Repair", cell="Recycling")
        lb.return_value = [
            StationTotal(s1, units=20, reading_count=1, truncated=False, downtime_minutes=0,
                         active_minutes=60, last_reading_at=None, last_status=None,
                         samples=(), active_intervals=()),
        ]
        client = TestClient(app)
        html = client.get("/recycling").text
    assert "(no assignment)" in html
    assert "Repair-1" in html


def test_recycling_past_day_view_shows_assigned_names(monkeypatch):
    """Regression: viewing a past single day (e.g. Saturday from Monday) was
    dropping the assigned-name labels because the aggregation loop only
    captured `agg_who_today` when `d == today`. Past-day views fell through
    to "(no assignment)" even when the schedule had names published for
    that day."""
    monkeypatch.setattr(staffing, "load_schedule", lambda d: staffing.Schedule(
        day=d, published=True,
        assignments={"Repair-1": ["Alice"]},
    ))
    with patch("zira_dashboard.routes.departments.leaderboard") as lb:
        from zira_dashboard.leaderboard import StationTotal
        from zira_dashboard.stations import Station
        s1 = Station(meter_id="m1", name="Repair-1", category="Repair", cell="Recycling")
        lb.return_value = [
            StationTotal(s1, units=20, reading_count=1, truncated=False, downtime_minutes=0,
                         active_minutes=60, last_reading_at=None, last_status=None,
                         samples=(), active_intervals=()),
        ]
        client = TestClient(app)
        # Pick a past date (clearly before today's date in any reasonable test env).
        html = client.get("/recycling?start=2024-01-06&end=2024-01-06").text
    assert "Alice" in html
    assert "(no assignment)" not in html


def test_recycling_downtime_row_renders_person_and_wc_stacked(monkeypatch):
    _freeze_route_clock_mid_shift(monkeypatch)
    monkeypatch.setattr(staffing, "load_schedule", lambda d: staffing.Schedule(
        day=d, published=True,
        assignments={"Repair-1": ["Alice"]},
    ))
    with patch("zira_dashboard.routes.departments.leaderboard") as lb:
        from zira_dashboard.leaderboard import StationTotal
        from zira_dashboard.stations import Station
        s1 = Station(meter_id="m1", name="Repair-1", category="Repair", cell="Recycling")
        lb.return_value = [
            StationTotal(s1, units=5, reading_count=1, truncated=False, downtime_minutes=12,
                         active_minutes=48, last_reading_at=None, last_status=None,
                         samples=(), active_intervals=()),
        ]
        client = TestClient(app)
        html = client.get("/recycling").text
    # Both Alice and Repair-1 appear in the rendered HTML; with 12 downtime minutes
    # the WC qualifies as active so we expect at least 2 occurrences (bar widget + downtime row).
    assert html.count("Alice") >= 2
    assert html.count("Repair-1") >= 2


def test_top_nav_renamed_and_work_centers_dropped():
    client = TestClient(app)
    html = client.get("/recycling").text
    assert ">Dashboards<" in html
    # The top-nav "Work Centers" link is gone (subnav still has it)
    # We can assert by counting: there should be exactly one "Work Centers" string
    # (in the subnav).
    assert html.count("Work Centers") == 1
    assert ">Recycling VS<" in html
    assert ">New VS<" in html


def test_work_centers_subnav_active_on_work_centers_page():
    client = TestClient(app)
    html = client.get("/work-centers").text
    # subnav appears on the work-centers page
    assert ">Recycling VS<" in html
    assert ">New VS<" in html
    assert ">Work Centers<" in html
    # "Work Centers" tab is active
    import re
    m = re.search(r'class="[^"]*active[^"]*"[^>]*>\s*Work Centers', html)
    assert m, "Work Centers tab should be active on /work-centers page"


def test_root_redirects_to_recycling():
    client = TestClient(app)
    resp = client.get("/", follow_redirects=False)
    assert resp.status_code in (307, 308)
    assert resp.headers["location"] == "/recycling"


def test_all_three_dashboard_pages_render_200():
    client = TestClient(app)
    for path in ("/work-centers", "/recycling", "/new"):
        resp = client.get(path)
        assert resp.status_code == 200, f"{path} returned {resp.status_code}"
        # subnav is present on all three
        assert ">Recycling<" in resp.text
        assert ">New<" in resp.text
        assert ">Work Centers<" in resp.text
        # top nav rename
        assert ">Dashboards<" in resp.text


def test_recycling_renders_edit_controls_after_partial_extraction():
    """After moving edit_controls to a shared partial, /recycling
    still renders the per-widget edit button on at least the KPI tiles."""
    c = TestClient(app)
    r = c.get("/recycling")
    assert r.status_code == 200
    assert 'data-widget="kpi-pallets"' in r.text
    assert 'data-widget="kpi-uptime"' in r.text
    assert 'class="widget-edit-btn"' in r.text
