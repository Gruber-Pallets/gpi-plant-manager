from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from tests.people_performance_fixtures import DAY, busy_dashboard_model
from zira_dashboard.app import app
from zira_dashboard.auth import _is_bypass_path
from zira_dashboard.deps import templates
from zira_dashboard.people_performance_view import warning_groups
from zira_dashboard.people_performance_warnings import production_metric_warning
from zira_dashboard.routes import people_performance as route


NOW = datetime(2026, 8, 28, 19, 0, tzinfo=UTC)


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setitem(
        templates.env.globals,
        "nav_inbox_summary",
        lambda: {"total": 0, "urgent_total": 0, "source_errors": ()},
    )
    return TestClient(app)


@pytest.fixture
def dashboard_loader(monkeypatch):
    calls = []

    def _load(day, client, *, now_utc=None):
        calls.append((day, client, now_utc))
        return replace(
            busy_dashboard_model(),
            day=day,
            is_today=day == DAY,
            as_of_utc=now_utc,
        )

    monkeypatch.setattr(route, "load_dashboard", _load)
    monkeypatch.setattr(route, "_utc_now", lambda: NOW)
    return calls


def test_people_page_defaults_to_one_captured_plant_today(client, dashboard_loader, monkeypatch):
    plant_today_calls = []

    def _today(now):
        plant_today_calls.append(now)
        return DAY

    monkeypatch.setattr(route, "plant_today", _today)

    response = client.get("/people-performance")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "private, max-age=15"
    assert 'class="pp-page"' in response.text
    assert 'data-today="1"' in response.text
    assert plant_today_calls == [NOW]
    assert len(dashboard_loader) == 1
    selected_day, _client, supplied_now = dashboard_loader[0]
    assert selected_day == DAY
    assert supplied_now == NOW
    assert supplied_now.utcoffset() is not None


def test_people_page_uses_authenticated_performance_navigation(client, dashboard_loader):
    response = client.get(f"/people-performance?day={DAY.isoformat()}")

    assert response.status_code == 200
    assert '<a href="/recycling" class="active">Performance</a>' in response.text
    assert 'href="/people-performance"' in response.text
    assert 'class="subnav-item active"' in response.text
    assert _is_bypass_path("/people-performance") is False
    assert _is_bypass_path("/people-performance/rows") is False
    assert _is_bypass_path("/people-performance/warnings/" + "0" * 24) is False


def test_future_day_is_rejected(client, dashboard_loader):
    response = client.get("/people-performance?day=2026-08-29")

    assert response.status_code == 400
    assert dashboard_loader == []


@pytest.mark.parametrize(
    "path", ("/people-performance", "/people-performance/rows")
)
def test_status_and_attention_query_reaches_presenter(path, client, dashboard_loader):
    response = client.get(
        f"{path}?day={DAY.isoformat()}&status=working&attention=1"
    )

    assert response.status_code == 200
    assert 'data-status="working"' in response.text
    assert 'data-attention="1"' in response.text
    assert "Showing 4 of 5 working now who need attention." in response.text
    assert len(dashboard_loader) == 1


@pytest.mark.parametrize(
    "path", ("/people-performance", "/people-performance/rows")
)
def test_unknown_status_is_rejected_before_loading_dashboard(
    path, client, dashboard_loader
):
    response = client.get(f"{path}?day={DAY.isoformat()}&status=other")

    assert response.status_code == 400
    assert response.json() == {"detail": "Unknown People status filter"}
    assert dashboard_loader == []


def test_rows_partial_has_identity_contract_and_is_never_response_cached(client, dashboard_loader):
    url = f"/people-performance/rows?day={DAY.isoformat()}&attention=1"

    first = client.get(url)
    second = client.get(url)

    assert first.status_code == second.status_code == 200
    assert first.headers["cache-control"] == "no-store"
    assert first.headers["x-people-performance-response"] == "rows"
    assert 'id="people-performance-live"' in first.text
    assert 'data-response-kind="people-performance-rows"' in first.text
    assert 'data-is-today="1"' in first.text
    assert 'data-as-of="2:00 PM"' in first.text
    assert len(dashboard_loader) == 2


def test_historical_page_revalidates_for_source_corrections(client, dashboard_loader):
    historical = DAY - timedelta(days=1)

    response = client.get(f"/people-performance?day={historical.isoformat()}")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "private, no-cache"


def test_warning_detail_returns_marked_no_store_partial(client, dashboard_loader):
    warning = busy_dashboard_model().source_warnings[0]

    response = client.get(
        f"/people-performance/warnings/{warning.key}?day={DAY.isoformat()}"
    )

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-people-performance-response"] == "warning-detail"
    assert 'data-warning-state="open"' in response.text
    assert f'data-warning-key="{warning.key}"' in response.text
    assert warning.title in response.text
    assert response.text.count("data-pp-warning-action=") == len(warning.actions)
    assert 'data-pp-warning-action="check_again"' in response.text
    assert 'data-pp-warning-action="open_diagnostics"' in response.text
    assert 'data-pp-warning-action="review_identities"' not in response.text
    assert "source_unavailable" not in response.text


def test_grouped_production_warning_detail_lists_every_meter(client, monkeypatch):
    warnings = (
        production_metric_warning(
            station_name="Trim Saw 1",
            reason_code="calculation_failure",
            checked_at_utc=NOW,
            day=DAY,
        ),
        production_metric_warning(
            station_name="Hand Build #1",
            reason_code="missing_goal",
            checked_at_utc=NOW,
            day=DAY,
        ),
    )
    monkeypatch.setattr(
        route,
        "load_dashboard",
        lambda day, client, now_utc=None: replace(
            busy_dashboard_model(), source_warnings=warnings
        ),
    )
    monkeypatch.setattr(route, "_utc_now", lambda: NOW)
    group_key = warning_groups(warnings)[0].key

    response = client.get(
        f"/people-performance/warnings/{group_key}?day={DAY.isoformat()}"
    )

    assert response.status_code == 200
    assert 'data-warning-state="open"' in response.text
    assert 'id="pp-warning-member-1"' in response.text
    assert 'id="pp-warning-member-2"' in response.text
    assert "Hand Build #1" in response.text
    assert "Trim Saw 1" in response.text
    assert response.text.count('data-pp-warning-action="open_work_center"') == 2
    assert 'data-pp-warning-action="review_settings"' in response.text


def test_missing_warning_key_returns_cleared_partial(client, dashboard_loader):
    response = client.get(
        f"/people-performance/warnings/{'0' * 24}?day={DAY.isoformat()}"
    )

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-people-performance-response"] == "warning-detail"
    assert 'data-warning-state="cleared"' in response.text
    assert "Issue cleared" in response.text
    assert "data-warning-key=" not in response.text
    assert "data-pp-warning-action=" not in response.text
    assert len(dashboard_loader) == 1


def test_malformed_warning_key_is_rejected_before_loading_dashboard(
    client, dashboard_loader
):
    response = client.get(
        f"/people-performance/warnings/not-a-warning-key?day={DAY.isoformat()}"
    )

    assert response.status_code == 400
    assert response.json() == {"detail": "Invalid warning key"}
    assert dashboard_loader == []


def test_warning_detail_rejects_future_day_before_loading_dashboard(
    client, dashboard_loader
):
    warning = busy_dashboard_model().source_warnings[0]

    response = client.get(
        f"/people-performance/warnings/{warning.key}?day=2026-08-29"
    )

    assert response.status_code == 400
    assert response.json() == {"detail": "Choose today or an earlier day"}
    assert dashboard_loader == []
