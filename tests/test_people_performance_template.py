from dataclasses import replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tests.people_performance_fixtures import DAY, busy_dashboard_model
from zira_dashboard.app import app
from zira_dashboard.deps import templates
from zira_dashboard.routes import people_performance as route


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setitem(
        templates.env.globals,
        "nav_inbox_summary",
        lambda: {"total": 0, "urgent_total": 0, "source_errors": ()},
    )
    return TestClient(app)


@pytest.fixture
def rendered_html(client, monkeypatch):
    monkeypatch.setattr(
        route,
        "load_dashboard",
        lambda day, client, now_utc=None: replace(
            busy_dashboard_model(), day=day, is_today=day == DAY
        ),
    )
    response = client.get(f"/people-performance?day={DAY.isoformat()}")
    assert response.status_code == 200
    return response.text


def test_all_metric_states_are_named_and_keyboard_reachable(rendered_html):
    for text in (
        "Metered production",
        "Tablet forklift",
        "Other non-metered people",
        "Ahead",
        "Behind",
        "Planned break",
        "Transfer",
        "Unavailable",
        "location missing",
        "source stale",
    ):
        assert text.lower() in rendered_html.lower()
    assert 'type="button" class="pp-interval-trigger' in rendered_html
    assert 'aria-label="Transferred to' in rendered_html


def test_interval_buttons_carry_complete_text_and_decorative_charts_are_hidden(
    rendered_html,
):
    assert "Behind goal" in rendered_html
    assert "Uptime 97%" in rendered_html
    assert "Downtime 15 minutes" in rendered_html
    assert "2 forklift calls" in rendered_html
    assert "Latest rolling on-time 0%" in rendered_html
    assert "1 late call" in rendered_html
    assert '<svg aria-hidden="true" focusable="false"' in rendered_html
    assert 'aria-label="Rolling 30-minute' not in rendered_html
    assert 'class="pp-late-marker"' in rendered_html


def test_mixed_role_person_has_one_labelled_row(rendered_html):
    assert rendered_html.count('data-person-id="48"') == 1
    assert 'aria-labelledby="pp-person-48"' in rendered_html
    assert 'id="pp-person-48"' in rendered_html


def test_people_tab_is_in_both_performance_navigation_branches():
    staffing_base = (ROOT / "src/zira_dashboard/templates/_staffing_base.html").read_text(
        encoding="utf-8"
    )
    performance_subnav = (ROOT / "src/zira_dashboard/templates/_performance_subnav.html").read_text(
        encoding="utf-8"
    )

    assert staffing_base.count("'forklift', 'people'") == 2
    assert 'href="/people-performance"' in performance_subnav
    assert "active_dashboard_key == 'people'" in performance_subnav


def test_performance_subnav_contains_horizontal_overflow_on_small_tablets():
    css = (ROOT / "src/zira_dashboard/static/dashboards-subnav.css").read_text(encoding="utf-8")

    assert "overflow-x: auto" in css
    assert "overscroll-behavior-x: contain" in css
