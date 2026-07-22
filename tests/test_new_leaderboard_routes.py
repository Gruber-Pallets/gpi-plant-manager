from datetime import date

import pytest
from fastapi.testclient import TestClient

from zira_dashboard import _http_cache
from zira_dashboard.app import app


@pytest.fixture(autouse=True)
def _clear_response_cache(monkeypatch):
    _http_cache.invalidate_today_cache()
    monkeypatch.setattr(
        "zira_dashboard.routes.new_leaderboard.shift_config.productive_minutes_per_day",
        lambda: 420,
    )
    yield
    _http_cache.invalidate_today_cache()


def fake_payload():
    return {
        "ytd_start": date(2026, 1, 1),
        "ytd_end": date(2026, 7, 10),
        "l30_start": date(2026, 6, 11),
        "l30_end": date(2026, 7, 10),
        "active_families": ["Juniors"],
        "families": {
            "Juniors": {
                "thresholds": {"ytd": 1, "l30": 1},
                "rows": [{
                    "rank": 1,
                    "name": "Junior Operator",
                    "ytd": {"eligible": True, "avg_units": 640.0, "days": 10, "label": None},
                    "l30": {"eligible": True, "avg_units": 660.0, "days": 4, "label": None},
                }],
            },
            "Woodpecker": {"thresholds": {"ytd": 0, "l30": 0}, "rows": []},
            "Hand Build": {"thresholds": {"ytd": 0, "l30": 0}, "rows": []},
        },
        "ribbons": [{
            "year": 2026,
            "month": 7,
            "month_label": "Jul",
            "winners": {
                "Juniors": {
                    "name": "Junior Operator",
                    "day": date(2026, 7, 2),
                    "amount": 700.0,
                    "days": 1,
                },
            },
        }],
        "current_goats": [{
            "label": "Junior GOAT",
            "group": "Juniors",
            "name": "Junior Operator",
            "units": 700.0,
            "day": date(2026, 7, 2),
        }],
        "error_message": None,
    }


def test_family_wc_names_include_big_build_with_hand_build():
    from zira_dashboard.routes import new_leaderboard
    from zira_dashboard.staffing import Location

    locations = [
        Location("Junior #2", "Junior", "Bay 17", "New", "42345"),
        Location("Woodpecker #1", "Woodpecker", "Bay 16", "New", None),
        Location("Hand Build #1", "Hand Build", "Bay 6", "New", None),
        Location("Big Build #1", "Hand Build", "Bay 14", "New", None),
        Location("Repair 1", "Repair", "Bay 1", "Recycled", "40721"),
    ]
    assert new_leaderboard._family_wc_names(locations) == {
        "Juniors": {"Junior #2"},
        "Woodpecker": {"Woodpecker #1"},
        "Hand Build": {"Hand Build #1", "Big Build #1"},
    }


def test_dashboard_new_leaderboard_renders_junior_only(monkeypatch):
    monkeypatch.setattr(
        "zira_dashboard.routes.new_leaderboard._leaderboard_payload",
        lambda today: fake_payload(),
    )
    response = TestClient(app).get("/new-leaderboard")
    assert response.status_code == 200
    assert "New-Leaderboard" in response.text
    assert "Junior Operator" in response.text
    assert "Woodpecker #1" not in response.text
    assert 'href="/new-leaderboard"' in response.text
    assert "tv-refresh.js" not in response.text


@pytest.mark.parametrize(
    ("active", "count"),
    [
        (["Juniors", "Woodpecker"], 2),
        (["Juniors", "Woodpecker", "Hand Build"], 3),
    ],
)
def test_new_leaderboard_renders_active_family_count(monkeypatch, active, count):
    payload = fake_payload()
    payload["active_families"] = active
    junior_block = payload["families"]["Juniors"]
    junior_winner = payload["ribbons"][0]["winners"]["Juniors"]
    for family in active[1:]:
        payload["families"][family] = junior_block
        payload["ribbons"][0]["winners"][family] = junior_winner
    monkeypatch.setattr(
        "zira_dashboard.routes.new_leaderboard._leaderboard_payload",
        lambda today: payload,
    )
    response = TestClient(app).get("/new-leaderboard")
    assert response.status_code == 200
    assert f"nlb-family-count-{count}" in response.text
    for family in active:
        assert family in response.text


def test_tv_new_leaderboard_renders_dark_and_refreshes(monkeypatch):
    monkeypatch.setattr(
        "zira_dashboard.routes.new_leaderboard._leaderboard_payload",
        lambda today: fake_payload(),
    )
    response = TestClient(app).get("/tv/new-leaderboard?theme=dark")
    assert response.status_code == 200
    assert 'data-tv-theme="dark"' in response.text
    assert "CURRENT GOATS" in response.text
    assert "tv-refresh.js" in response.text


def test_direct_tv_new_leaderboard_uses_saved_theme(monkeypatch):
    from zira_dashboard import tv_displays_store
    from zira_dashboard.routes import new_leaderboard

    monkeypatch.setattr(
        tv_displays_store,
        "by_slug",
        lambda slug: {"slug": slug, "theme": "light"},
    )

    def fake_render(request, *, tv_theme="dark"):
        from fastapi.responses import HTMLResponse
        return HTMLResponse(f'<html data-tv-theme="{tv_theme}">ok</html>')

    monkeypatch.setattr(
        new_leaderboard,
        "render_new_leaderboard_tv",
        fake_render,
    )
    response = TestClient(app).get("/tv/new-leaderboard")
    assert response.status_code == 200
    assert 'data-tv-theme="light"' in response.text


def test_new_leaderboard_no_data_state(monkeypatch):
    payload = fake_payload()
    payload["active_families"] = []
    payload["current_goats"] = []
    payload["ribbons"] = []
    for block in payload["families"].values():
        block["rows"] = []
    monkeypatch.setattr(
        "zira_dashboard.routes.new_leaderboard._leaderboard_payload",
        lambda today: payload,
    )
    response = TestClient(app).get("/new-leaderboard")
    assert response.status_code == 200
    assert "Waiting for qualifying Zira production." in response.text


def test_new_leaderboard_data_error_keeps_shell_and_refresh(monkeypatch):
    from zira_dashboard.routes import new_leaderboard

    def fail(today):
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(
        "zira_dashboard.routes.new_leaderboard._leaderboard_payload",
        fail,
    )
    monkeypatch.setattr(
        new_leaderboard.shift_config,
        "productive_minutes_per_day",
        lambda: (_ for _ in ()).throw(RuntimeError("schedule unavailable")),
    )
    response = TestClient(app).get("/tv/new-leaderboard")
    assert response.status_code == 200
    assert "New-Leaderboard" in response.text
    assert "Production data is temporarily unavailable." in response.text
    assert "tv-refresh.js" in response.text


def test_payload_goat_failure_omits_chip_but_keeps_family_data(monkeypatch):
    from zira_dashboard.routes import new_leaderboard

    payload = fake_payload()
    payload.pop("current_goats")
    payload.pop("error_message")
    monkeypatch.setattr(
        new_leaderboard.production_history,
        "normalized_daily_records",
        lambda start, end: [],
    )
    monkeypatch.setattr(
        new_leaderboard.production_metrics,
        "build_family_leaderboard",
        lambda records, **kwargs: payload,
    )
    monkeypatch.setattr(new_leaderboard.awards, "load_overrides", lambda: [])
    monkeypatch.setattr(
        new_leaderboard.awards,
        "goat_for_wc_names",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("goat failed")),
    )
    data = new_leaderboard._leaderboard_payload(date(2026, 7, 10))
    assert data["active_families"] == ["Juniors"]
    assert data["current_goats"] == []


def test_new_leaderboard_response_cache_avoids_duplicate_payload(monkeypatch):
    calls = []

    def build(today):
        calls.append(today)
        return fake_payload()

    monkeypatch.setattr(
        "zira_dashboard.routes.new_leaderboard._leaderboard_payload",
        build,
    )
    client = TestClient(app)
    assert client.get("/new-leaderboard").status_code == 200
    assert client.get("/new-leaderboard").status_code == 200
    assert len(calls) == 1


def test_tv_new_leaderboard_has_no_desktop_chrome(monkeypatch):
    """Chrome-consolidation guard: TV variant renders no topnav/footer."""
    monkeypatch.setattr(
        "zira_dashboard.routes.new_leaderboard._leaderboard_payload",
        lambda today: fake_payload(),
    )
    r = TestClient(app).get("/tv/new-leaderboard")
    assert r.status_code == 200
    assert "<title>New-Leaderboard</title>" in r.text  # suffix-less TV title
    assert 'class="brand-row"' not in r.text
    assert "changelog-modal" not in r.text
    assert r.text.lower().count("<!doctype") == 1


def test_desktop_new_leaderboard_has_single_chrome(monkeypatch):
    monkeypatch.setattr(
        "zira_dashboard.routes.new_leaderboard._leaderboard_payload",
        lambda today: fake_payload(),
    )
    r = TestClient(app).get("/new-leaderboard")
    assert r.status_code == 200
    assert r.text.count('class="brand-row"') == 1
    assert "data-tv-theme" not in r.text
