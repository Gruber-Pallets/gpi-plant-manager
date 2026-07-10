from datetime import date

from fastapi.testclient import TestClient

from zira_dashboard.app import app


def _fake_recycling_leaderboard_data():
    return {
        "ytd_start": date(2026, 1, 1),
        "ytd_end": date(2026, 7, 9),
        "l30_start": date(2026, 6, 10),
        "l30_end": date(2026, 7, 9),
        "roles": {
            "Repair": {
                "thresholds": {"ytd": 13, "l30": 2},
                "rows": [
                    {
                        "rank": 1,
                        "name": "Maria S.",
                        "ytd": {"eligible": True, "avg_units": 98.4, "days": 128, "label": None},
                        "l30": {"eligible": True, "avg_units": 102.2, "days": 16, "label": None},
                    },
                    {
                        "rank": 2,
                        "name": "Luis A.",
                        "ytd": {"eligible": False, "avg_units": None, "days": 8, "label": "not enough days"},
                        "l30": {"eligible": True, "avg_units": 88.2, "days": 3, "label": None},
                    },
                ],
            },
            "Dismantler": {"thresholds": {"ytd": 12, "l30": 2}, "rows": []},
        },
        "current_goats": [
            {
                "label": "Dismantler GOAT",
                "group": "Dismantlers",
                "name": "Daniel M.",
                "units": 168.0,
            },
            {
                "label": "Repair GOAT",
                "group": "Repairs",
                "name": "Maria S.",
                "units": 118.0,
            },
        ],
        "ribbons": [
            {
                "year": 2026,
                "month": 7,
                "month_label": "Jul",
                "repair": {"name": "Maria S.", "day": date(2026, 7, 2), "amount": 118.0},
                "dismantler": {"name": "Daniel M.", "day": date(2026, 7, 7), "amount": 168.0},
            }
        ],
    }


def test_tv_recycling_leaderboard_renders(monkeypatch):
    monkeypatch.setattr(
        "zira_dashboard.routes.recycling_leaderboard._leaderboard_payload",
        lambda today: _fake_recycling_leaderboard_data(),
    )
    r = TestClient(app).get("/tv/recycling-leaderboard")
    assert r.status_code == 200
    assert 'data-tv-theme="dark"' in r.text
    assert "Recycling-leaderboard" in r.text
    assert "Maria S." in r.text
    assert "not enough days" in r.text
    assert "q-days" not in r.text
    assert "actual times" not in r.text
    assert "CURRENT GOATS" in r.text
    assert "Dismantler GOAT" in r.text
    assert "Daniel M." in r.text
    assert "Repair GOAT" in r.text
    assert "tv-refresh.js" in r.text


def test_tv_recycling_range_moves_into_header_and_goat_group_has_one_icon(monkeypatch):
    monkeypatch.setattr(
        "zira_dashboard.routes.recycling_leaderboard._leaderboard_payload",
        lambda today: _fake_recycling_leaderboard_data(),
    )

    response = TestClient(app).get("/tv/recycling-leaderboard")

    assert response.status_code == 200
    assert response.text.count('class="tv-header-title-meta"') == 1
    assert "YTD: Jan 1-Jul 9" in response.text
    assert "L30: Jun 10-Jul 9" in response.text
    assert 'class="rlb-range"' not in response.text
    assert response.text.count('class="tv-header-right-icon"') == 1
    assert '<span class="tv-header-right-icon" aria-hidden="true">🐐</span>' in response.text


def test_desktop_recycling_range_and_header_stay_unchanged(monkeypatch):
    monkeypatch.setattr(
        "zira_dashboard.routes.recycling_leaderboard._leaderboard_payload",
        lambda today: _fake_recycling_leaderboard_data(),
    )

    response = TestClient(app).get("/recycling-leaderboard")

    assert response.status_code == 200
    assert 'class="rlb-range"' in response.text
    assert 'class="tv-header-title-meta"' not in response.text
    assert 'class="tv-header-right-icon"' not in response.text


def test_current_recycling_goats_uses_awards_with_overrides(monkeypatch):
    from zira_dashboard.routes import recycling_leaderboard

    def fake_goat(group_name):
        return {
            "Dismantlers": {"name": "Daniel M.", "units": 168.0},
            "Repairs": {"name": "Maria S.", "units": 118.0},
        }[group_name]

    def fake_apply(slot, *, scope, group_name, overrides=None):
        assert scope == "award_goat"
        return {**slot, "name": f"{slot['name']} Final", "group_name": group_name}

    monkeypatch.setattr(recycling_leaderboard.awards, "goat", fake_goat)
    monkeypatch.setattr(
        recycling_leaderboard.awards,
        "apply_overrides_single",
        fake_apply,
    )

    goats = recycling_leaderboard._current_recycling_goats()

    assert [g["label"] for g in goats] == ["Dismantler GOAT", "Repair GOAT"]
    assert [g["group"] for g in goats] == ["Dismantlers", "Repairs"]
    assert [g["name"] for g in goats] == ["Daniel M. Final", "Maria S. Final"]
    assert [g["units"] for g in goats] == [168.0, 118.0]


def test_tv_recycling_leaderboard_adds_current_goats_from_route_helper(monkeypatch):
    data = _fake_recycling_leaderboard_data()
    data.pop("current_goats")
    monkeypatch.setattr(
        "zira_dashboard.routes.recycling_leaderboard._leaderboard_payload",
        lambda today: data,
    )
    monkeypatch.setattr(
        "zira_dashboard.routes.recycling_leaderboard._current_recycling_goats",
        lambda: [
            {
                "label": "Dismantler GOAT",
                "group": "Dismantlers",
                "name": "Daniel M.",
                "units": 168.0,
            },
            {
                "label": "Repair GOAT",
                "group": "Repairs",
                "name": "Maria S.",
                "units": 118.0,
            },
        ],
        raising=False,
    )

    r = TestClient(app).get("/tv/recycling-leaderboard")

    assert r.status_code == 200
    assert "CURRENT GOATS" in r.text
    assert "Daniel M." in r.text
    assert "Maria S." in r.text


def test_dashboard_recycling_leaderboard_renders_as_dashboards_tab(monkeypatch):
    monkeypatch.setattr(
        "zira_dashboard.routes.recycling_leaderboard._leaderboard_payload",
        lambda today: _fake_recycling_leaderboard_data(),
    )

    r = TestClient(app).get("/recycling-leaderboard")

    assert r.status_code == 200
    assert 'data-tv-theme="' not in r.text
    assert "tv-refresh.js" not in r.text
    assert ">Dashboards<" in r.text
    assert 'href="/recycling-leaderboard"' in r.text
    assert "Recycling-leaderboard" in r.text
    assert "Maria S." in r.text
    assert "Gold Ribbons" in r.text


def test_direct_tv_recycling_leaderboard_uses_saved_theme(monkeypatch):
    from zira_dashboard import tv_displays_store
    from zira_dashboard.routes import recycling_leaderboard

    monkeypatch.setattr(
        tv_displays_store,
        "by_slug",
        lambda slug: {
            "id": 1,
            "name": "Recycling-leaderboard",
            "slug": slug,
            "kind": "vs_recycling_leaderboard",
            "wc_name": None,
            "theme": "light",
            "sort_order": 2,
        },
    )

    def _fake_render(request, *, tv_theme="dark"):
        from fastapi.responses import HTMLResponse
        return HTMLResponse(f'<html data-tv-theme="{tv_theme}">ok</html>')

    monkeypatch.setattr(recycling_leaderboard, "render_recycling_leaderboard_tv", _fake_render)
    r = TestClient(app).get("/tv/recycling-leaderboard")
    assert r.status_code == 200
    assert 'data-tv-theme="light"' in r.text
