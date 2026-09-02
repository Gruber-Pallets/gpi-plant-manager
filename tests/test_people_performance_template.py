from dataclasses import replace
from datetime import timedelta
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
    assert "Location unavailable" in rendered_html


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


def test_short_interval_has_a_separate_tablet_detail_target(rendered_html):
    assert 'class="pp-interval-shortcut"' in rendered_html
    assert 'data-interval-key="48:production:Repair 2:' in rendered_html
    assert "Repair 2 · 7:30 AM to 7:35 AM" in rendered_html


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


def test_only_production_triggers_carry_precise_hover_data_and_marker(rendered_html):
    assert 'data-production-hover="[[' in rendered_html
    assert 'data-hover-start-ms="' in rendered_html
    assert 'data-hover-end-ms="' in rendered_html
    assert 'class="pp-hover-marker" aria-hidden="true"' in rendered_html
    assert 'data-production-hover=' not in next(
        tag for tag in rendered_html.split('<button') if 'role-forklift' in tag
    ).split('</button>', 1)[0]


def test_page_uses_one_compact_live_manager_strip(rendered_html):
    assert 'class="pp-manager-strip"' in rendered_html
    assert 'class="pp-manager-primary"' in rendered_html
    assert 'class="pp-manager-actions"' in rendered_html
    assert rendered_html.index('class="pp-manager-primary"') < rendered_html.index(
        'class="pp-source-warnings"'
    )
    primary = rendered_html.split('<div class="pp-manager-primary">', 1)[1].split(
        '<aside class="pp-source-warnings"', 1
    )[0]
    assert primary.index('class="pp-counts"') < primary.index('class="pp-manager-actions"')
    actions = primary.split('<div class="pp-manager-actions">', 1)[1]
    assert actions.index('class="pp-updated"') < actions.index('class="pp-controls"')
    assert 'id="pp-live-status"' in rendered_html
    assert '<strong>5</strong> working now' in rendered_html
    assert '<strong>1</strong> worked earlier' in rendered_html
    assert '<strong>4</strong> need attention' in rendered_html
    assert "Forklift data unavailable" in rendered_html
    assert "DashboardWarning" not in rendered_html
    assert 'name="day"' in rendered_html
    assert 'name="attention"' not in rendered_html
    assert rendered_html.count('data-pp-count-filter=') == 3
    assert 'data-pp-count-filter="status" data-filter-value="working"' in rendered_html
    assert 'data-pp-count-filter="status" data-filter-value="earlier"' in rendered_html
    assert 'data-pp-count-filter="attention" data-filter-value="1"' in rendered_html
    assert 'data-pp-control-key="day"' in rendered_html
    assert 'data-pp-control-key="attention"' in rendered_html
    assert 'data-pp-auto-submit' in rendered_html
    assert '>Apply<' not in rendered_html
    assert '>Today<' not in rendered_html


def test_page_embeds_matching_control_styles_and_behavior(rendered_html):
    assert '<style data-pp-asset="people-performance.css">' in rendered_html
    assert ".pp-counts button[aria-pressed=\"true\"]" in rendered_html
    assert '<script data-pp-asset="people-performance.js">' in rendered_html
    assert "function activateCountFilter(control)" in rendered_html
    assert "function warningTriggerFor(node)" in rendered_html
    assert 'href="/static/people-performance.css' not in rendered_html
    assert 'src="/static/people-performance.js' not in rendered_html


def test_warning_strip_uses_safe_semantic_detail_triggers(rendered_html):
    warning = busy_dashboard_model().source_warnings[0]
    warning_strip = rendered_html.split(
        '<aside class="pp-source-warnings"', 1
    )[1].split("</aside>", 1)[0]

    assert 'role="status"' in warning_strip
    assert 'aria-live="off"' in warning_strip
    assert 'type="button" class="pp-warning-trigger"' in warning_strip
    assert f'data-warning-key="{warning.key}"' in warning_strip
    assert f'data-warning-kind="{warning.kind}"' in warning_strip
    assert f'data-warning-summary="{warning.summary}"' in warning_strip
    assert 'aria-expanded="false"' in warning_strip
    assert 'aria-controls="pp-warning-popover"' in warning_strip
    assert '<span aria-hidden="true">!</span>' in warning_strip
    assert "source_unavailable" not in warning_strip
    assert "data-warning-facts" not in warning_strip


def test_warning_panel_host_stays_outside_the_polled_rows_partial(
    rendered_html, client, monkeypatch
):
    monkeypatch.setattr(
        route,
        "load_dashboard",
        lambda day, client, now_utc=None: replace(
            busy_dashboard_model(), day=day, is_today=day == DAY
        ),
    )

    rows = client.get(f"/people-performance/rows?day={DAY.isoformat()}")
    host_tag = rendered_html.split('<div id="pp-warning-popover"', 1)[1].split(
        "></div>", 1
    )[0]

    assert rows.status_code == 200
    assert 'id="pp-warning-popover"' in rendered_html
    assert 'role="region"' in host_tag
    assert 'aria-label="Warning details"' in host_tag
    assert "hidden" in host_tag
    assert 'id="pp-action-status"' in rendered_html
    assert 'aria-live="polite" aria-atomic="true"' in rendered_html
    assert 'id="pp-warning-popover"' not in rows.text
    assert 'id="pp-action-status"' not in rows.text
    assert '<p class="pp-updated" id="pp-live-status">' in rendered_html
    assert 'id="pp-live-status" aria-live=' not in rendered_html


def test_count_controls_expose_filter_state_and_empty_descriptions(rendered_html):
    counts = rendered_html.split('<div class="pp-counts"', 1)[1].split("</div>", 1)[0]

    assert 'data-status=""' in rendered_html
    assert 'data-attention="0"' in rendered_html
    assert counts.count('aria-pressed="false"') == 3
    assert 'id="pp-working-empty"' in rendered_html
    assert 'id="pp-earlier-empty"' in rendered_html
    assert 'id="pp-attention-empty"' in rendered_html
    assert "There are no working people to show." in rendered_html
    assert "There are no people who worked earlier to show." in rendered_html
    assert "There are no people who need attention to show." in rendered_html


def test_active_filters_are_selected_and_preserved_by_the_date_form(client, monkeypatch):
    monkeypatch.setattr(
        route,
        "load_dashboard",
        lambda day, client, now_utc=None: replace(
            busy_dashboard_model(), day=day, is_today=day == DAY
        ),
    )

    response = client.get(
        f"/people-performance?day={DAY.isoformat()}&status=working&attention=1"
    )

    assert response.status_code == 200
    assert 'data-status="working"' in response.text
    assert 'data-attention="1"' in response.text
    assert '<input type="hidden" name="status" value="working">' in response.text
    assert '<input type="hidden" name="attention" value="1">' in response.text
    assert 'type="checkbox"' not in response.text
    counts = response.text.split('<div class="pp-counts"', 1)[1].split("</div>", 1)[0]
    assert counts.count('aria-pressed="true"') == 2
    assert response.text.count('class="pp-filter-selected"') == 2
    assert "Showing 4 of 5 working now who need attention." in response.text


def test_filtered_empty_state_replaces_generic_section_messages(client, monkeypatch):
    monkeypatch.setattr(
        route,
        "load_dashboard",
        lambda day, client, now_utc=None: replace(
            busy_dashboard_model(), day=day, is_today=day == DAY
        ),
    )

    response = client.get(
        f"/people-performance?day={DAY.isoformat()}&status=earlier&attention=1"
    )

    assert response.status_code == 200
    assert '<p class="pp-filter-summary" role="status">Showing 0 of 1 worked earlier who need attention.</p>' in response.text
    assert '<div class="pp-filter-empty">' in response.text
    assert "No people match these filters." in response.text
    assert f'href="/people-performance?day={DAY.isoformat()}"' in response.text
    assert "No people in this group." not in response.text


def test_count_button_styles_cover_touch_focus_selected_and_disabled_states():
    css = (ROOT / "src/zira_dashboard/static/people-performance.css").read_text(
        encoding="utf-8"
    )

    assert ".pp-counts button {" in css
    assert "min-height: 44px" in css
    assert "cursor: pointer" in css
    assert ".pp-counts button[aria-pressed=\"true\"]" in css
    assert ".pp-counts button:disabled" in css
    assert "cursor: not-allowed" in css
    assert ".pp-counts button:focus-visible" in css
    assert "outline-offset: 2px" in css


def test_manager_strip_omits_warning_region_when_sources_are_healthy(client, monkeypatch):
    monkeypatch.setattr(
        route,
        "load_dashboard",
        lambda day, client, now_utc=None: replace(
            busy_dashboard_model(), day=day, source_warnings=()
        ),
    )

    response = client.get(f"/people-performance?day={DAY.isoformat()}")

    assert response.status_code == 200
    assert 'class="pp-manager-primary"' in response.text
    assert 'class="pp-manager-actions"' in response.text
    assert 'class="pp-source-warnings"' not in response.text


def test_historical_manager_strip_offers_today_shortcut(client, monkeypatch):
    historical = DAY - timedelta(days=1)
    monkeypatch.setattr(
        route,
        "load_dashboard",
        lambda day, client, now_utc=None: replace(
            busy_dashboard_model(), day=day, is_today=False
        ),
    )

    response = client.get(f"/people-performance?day={historical.isoformat()}")

    assert response.status_code == 200
    assert '<a data-pp-control-key="today" href="/people-performance">Today</a>' in response.text


def test_live_partial_sets_one_shared_schedule_track_width(rendered_html):
    assert 'style="--pp-track-width:' in rendered_html
    assert rendered_html.count("--pp-track-width:") == 1


def test_page_does_not_repeat_tab_identity_or_render_hourly_axis(rendered_html):
    assert 'class="pp-toolbar"' not in rendered_html
    assert 'class="pp-eyebrow"' not in rendered_html
    assert '<h1>Today</h1>' not in rendered_html
    assert 'class="pp-axis"' not in rendered_html


def test_each_green_section_header_contains_schedule_ticks_and_summary(rendered_html):
    assert rendered_html.count('class="pp-section-header"') == 3
    assert rendered_html.count('class="pp-schedule-tick ') == 9
    assert rendered_html.count('class="pp-schedule-time-group ') == 9
    assert rendered_html.count('class="pp-section-summary"') == 3
    assert "Shift starts at 6:00 AM" in rendered_html
    assert "Planned break starts at 10:30 AM" in rendered_html
    assert "Shift ends at 2:00 PM" in rendered_html
