from datetime import date

from fastapi.testclient import TestClient

from zira_dashboard.app import app


def test_tv_recycling_leaderboard_renders(monkeypatch):
    fake_data = {
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
    monkeypatch.setattr(
        "zira_dashboard.routes.recycling_leaderboard._leaderboard_payload",
        lambda today: fake_data,
    )
    r = TestClient(app).get("/tv/recycling-leaderboard")
    assert r.status_code == 200
    assert 'data-tv-theme="dark"' in r.text
    assert "Recycling-leaderboard" in r.text
    assert "Maria S." in r.text
    assert "not enough days" in r.text
    assert "q-days" not in r.text
    assert "actual times" not in r.text
    assert "tv-refresh.js" in r.text
