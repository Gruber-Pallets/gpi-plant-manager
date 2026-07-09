"""Per-day forklift performances on the player card.

The pure-function tests stub forklift_store so they need no DATABASE_URL; the
route-context test rides the existing `_capture` pattern (monkeypatch
TemplateResponse) to assert the context the template receives.
"""
from __future__ import annotations

from datetime import date

from fastapi.testclient import TestClient


def _rows():
    """Four driver-days for "Isidro": newest scoring day, a sub-gate day, an
    older scoring day, and a zero-activity day that must be dropped."""
    return [
        {"day": date(2026, 6, 27), "name": "Isidro", "driver_id": "Isidro",
         "calls": 30, "on_time": 30, "late": 0, "avg_ms": 35000,
         "max_ms": 90000, "utilization_pct": 26},
        {"day": date(2026, 6, 26), "name": "Isidro", "driver_id": "Isidro",
         "calls": 3, "on_time": 3, "late": 0, "avg_ms": 50000,
         "max_ms": 70000, "utilization_pct": 10},
        {"day": date(2026, 6, 25), "name": "Isidro", "driver_id": "Isidro",
         "calls": 20, "on_time": 19, "late": 1, "avg_ms": 40000,
         "max_ms": 88000, "utilization_pct": 30},
        {"day": date(2026, 6, 24), "name": "Isidro", "driver_id": "Isidro",
         "calls": 0, "on_time": 0, "late": 0, "avg_ms": 0,
         "max_ms": 0, "utilization_pct": 0},
    ]


def test_forklift_days_sorted_scored_and_zero_activity_dropped(monkeypatch):
    from zira_dashboard import forklift_score, forklift_store
    from zira_dashboard.routes import people

    monkeypatch.setattr(forklift_store, "name_map", lambda kind: {})
    monkeypatch.setattr(forklift_store, "driver_days_between", lambda s, e: _rows())

    out = people._forklift_days_for_person(
        "Isidro", date(2026, 6, 1), date(2026, 6, 27),
        forklift_score.DEFAULT_SCORE_CONFIG,
    )

    # Zero-activity day dropped; newest first.
    assert [d["date"] for d in out] == ["2026-06-27", "2026-06-26", "2026-06-25"]

    # On-time % and avg response pass through from the raw row.
    older = out[2]
    assert older["date"] == "2026-06-25"
    assert older["ontime_pct"] == 95.0  # 19 / (19 + 1)
    assert older["avg_ms"] == 40000

    # Scoring day carries a composite score + subscore components.
    newest = out[0]
    assert newest["score"] is not None
    assert newest["components"]["calls"]["sub"] == 100.0  # 30 calls vs target 25

    # Sub-gate day (3 calls < min 8) is listed but unscored.
    subgate = out[1]
    assert subgate["date"] == "2026-06-26"
    assert subgate["score"] is None
    assert subgate["components"] is None


def _stub_player_card(monkeypatch, *, driver_days):
    """Patch the player-card route's data sources so it renders with no DB.

    driver_days: rows returned by forklift_store.driver_days_between.
    """
    from zira_dashboard import (
        awards, forklift_awards, forklift_store, late_report,
        production_history, shift_config, staffing, work_centers_store,
    )

    monkeypatch.setattr(production_history, "attribution_range", lambda s, e: {})
    monkeypatch.setattr(production_history, "attribution_per_day", lambda s, e: [])
    monkeypatch.setattr(production_history, "daily_records", lambda s, e: [])
    monkeypatch.setattr(production_history, "normalized_daily_records", lambda s, e: [])
    monkeypatch.setattr(shift_config, "productive_minutes_per_day", lambda: 420)
    monkeypatch.setattr(work_centers_store, "registered_groups", lambda: [])
    monkeypatch.setattr(late_report, "absences_history_for_name", lambda *a, **k: [])
    monkeypatch.setattr(late_report, "late_arrivals_history_for_name", lambda *a, **k: [])
    monkeypatch.setattr(awards, "awards_earned_by", lambda *a, **k: [])

    class _FakePerson:
        def __init__(self, name):
            self.name, self.active, self.skills, self.reserve = name, True, {}, False

    monkeypatch.setattr(staffing, "load_roster", lambda: [_FakePerson("Isidro")])
    monkeypatch.setattr(forklift_store, "name_map", lambda kind: {})
    monkeypatch.setattr(forklift_store, "driver_days_between", lambda s, e: driver_days)
    monkeypatch.setattr(forklift_awards, "awards_earned_by_driver", lambda *a, **k: [])


def _capture_ctx(monkeypatch):
    captured = {}

    def _capture(request, template, ctx):
        captured["ctx"] = ctx
        from fastapi.responses import HTMLResponse
        return HTMLResponse("ok")

    from zira_dashboard.deps import templates
    monkeypatch.setattr(templates, "TemplateResponse", _capture)
    return captured


def test_route_passes_forklift_days_for_mapped_driver(monkeypatch):
    _stub_player_card(monkeypatch, driver_days=_rows())
    captured = _capture_ctx(monkeypatch)

    r = TestClient(_app()).get("/staffing/people/Isidro?start=2026-06-01&end=2026-06-27")
    assert r.status_code == 200
    ctx = captured["ctx"]
    assert ctx["forklift"] is not None
    assert [d["date"] for d in ctx["forklift_days"]] == [
        "2026-06-27", "2026-06-26", "2026-06-25",
    ]


def test_route_no_forklift_days_for_non_driver(monkeypatch):
    _stub_player_card(monkeypatch, driver_days=[])  # no rows -> not a driver
    captured = _capture_ctx(monkeypatch)

    r = TestClient(_app()).get("/staffing/people/Isidro?start=2026-06-01&end=2026-06-27")
    assert r.status_code == 200
    ctx = captured["ctx"]
    assert ctx["forklift"] is None
    assert ctx["forklift_days"] == []


def _app():
    from zira_dashboard.app import app
    return app


def _extract_forklift_days_block() -> str:
    """Pull the per-day forklift `<section>` out of player_card.html so we
    render exactly the shipped markup in isolation (no DB, no full page)."""
    import re
    from pathlib import Path
    html = Path("src/zira_dashboard/templates/player_card.html").read_text()
    m = re.search(
        r'<section class="pc-forklift-days">.*?</section>', html, re.DOTALL
    )
    assert m, "forklift per-day section missing from player_card.html"
    return m.group(0)


def _scored_day():
    return {
        "date": "2026-06-27", "calls": 30, "on_time": 30, "late": 0,
        "ontime_pct": 100.0, "avg_ms": 35000, "max_ms": 90000,
        "utilization_pct": 26.0, "score": 91.9,
        "components": {"calls": {"sub": 100.0}, "ontime": {"sub": 100.0},
                       "speed": {"sub": 96.7}, "util": {"sub": 26.0}},
    }


def test_forklift_days_block_renders_rows_and_detail():
    from zira_dashboard.deps import templates
    rendered = templates.env.from_string(_extract_forklift_days_block()).render(
        forklift_days=[_scored_day()], forklift_min_calls=8)
    assert "Forklift — per-day performances" in rendered
    assert "2026-06-27" in rendered
    assert "fk-day-detail" in rendered          # expandable detail drawer present
    assert "35.0" in rendered                   # avg response shown in seconds
    assert "speed 97" in rendered               # subscore breakdown in the drawer


def test_forklift_days_block_subgate_note():
    from zira_dashboard.deps import templates
    subgate = {**_scored_day(), "date": "2026-06-26", "calls": 3,
               "score": None, "components": None}
    rendered = templates.env.from_string(_extract_forklift_days_block()).render(
        forklift_days=[subgate], forklift_min_calls=8)
    assert "Below scoring threshold (min 8 calls)" in rendered


def test_forklift_days_block_empty_range_message():
    from zira_dashboard.deps import templates
    rendered = templates.env.from_string(_extract_forklift_days_block()).render(
        forklift_days=[], forklift_min_calls=8)
    assert "No forklift days in this range" in rendered
