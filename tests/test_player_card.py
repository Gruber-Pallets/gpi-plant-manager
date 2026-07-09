from datetime import date
from unittest.mock import patch

from fastapi.testclient import TestClient

from zira_dashboard.app import app


def _attr(units: float, downtime: float = 0.0):
    return {"units": units, "downtime": downtime, "hours": 8.0, "days_worked": 1}


def test_player_card_renders_per_day_breakdown_table():
    """The player card surfaces a per-day-per-WC table below the per-WC
    summary, with each Date cell hyperlinked to the recycling dashboard
    for that day. Days are sorted newest-first."""
    fake = [
        (date(2026, 4, 27), {"Carlos": {"Repair-1": _attr(95)}}),
        (date(2026, 4, 28), {"Carlos": {"Repair-1": _attr(80), "Repair-2": _attr(70)}}),
        (date(2026, 4, 29), {"Other": {"Repair-1": _attr(50)}}),
    ]
    with patch("zira_dashboard.production_history.attribution_per_day", return_value=fake), \
         patch("zira_dashboard.production_history.daily_records", return_value=[]), \
         patch("zira_dashboard.production_history.attribution_range",
               return_value={"Carlos": {"Repair-1": {"units": 175.0, "downtime": 0.0,
                                                     "hours": 16.0, "days_worked": 2},
                                        "Repair-2": {"units": 70.0, "downtime": 0.0,
                                                     "hours": 8.0, "days_worked": 1}}}), \
         patch("zira_dashboard.staffing.load_roster", return_value=[]), \
         patch("zira_dashboard.shift_config.productive_minutes_per_day", return_value=420), \
         patch("zira_dashboard.work_centers_store.registered_groups", return_value=[]), \
         patch("zira_dashboard.awards.awards_earned_by", return_value=[]), \
         patch("zira_dashboard.late_report.absences_history_for_name", return_value=[]), \
         patch("zira_dashboard.late_report.late_arrivals_history_for_name", return_value=[]):
        client = TestClient(app)
        html = client.get("/staffing/people/Carlos?start=2026-04-27&end=2026-04-29").text

    # Per-day breakdown header is present.
    assert "Per-day breakdown" in html
    # Date hyperlinks point at the recycling dashboard for that day.
    assert 'href="/recycling?start=2026-04-28&end=2026-04-28"' in html
    assert 'href="/recycling?start=2026-04-27&end=2026-04-27"' in html
    # Newest first — anchor on the per-day-row href so we don't accidentally
    # match the date input fields at the top of the page (which carry the same
    # YYYY-MM-DD strings via value="..."  attributes).
    assert (
        html.index('href="/recycling?start=2026-04-28&end=2026-04-28"')
        < html.index('href="/recycling?start=2026-04-27&end=2026-04-27"')
    )
    # Carlos's entries appear, "Other" does not.
    assert "Repair-1" in html and "Repair-2" in html


def test_player_card_renders_attendance_section_with_reasons():
    """The player card shows an Attendance section with absent/late
    rows and reasons when history exists in the range."""
    from datetime import date
    from unittest.mock import patch
    from fastapi.testclient import TestClient
    from zira_dashboard.app import app

    abs_rows = [{"day": date(2026, 5, 6), "reason": "sick"}]
    late_rows = [{"day": date(2026, 5, 7), "reason": "car issues"}]

    with patch("zira_dashboard.production_history.attribution_per_day", return_value=[]), \
         patch("zira_dashboard.production_history.daily_records", return_value=[]), \
         patch("zira_dashboard.production_history.attribution_range", return_value={}), \
         patch("zira_dashboard.staffing.load_roster", return_value=[]), \
         patch("zira_dashboard.shift_config.productive_minutes_per_day", return_value=420), \
         patch("zira_dashboard.work_centers_store.registered_groups", return_value=[]), \
         patch("zira_dashboard.awards.awards_earned_by", return_value=[]), \
         patch("zira_dashboard.late_report.absences_history_for_name", return_value=abs_rows), \
         patch("zira_dashboard.late_report.late_arrivals_history_for_name", return_value=late_rows):
        client = TestClient(app)
        html = client.get("/staffing/people/Carlos?start=2026-05-01&end=2026-05-07").text

    assert "Attendance" in html
    assert "Days Absent" in html
    assert "Days Late" in html
    assert "sick" in html
    assert "car issues" in html
    # Date hyperlinks point to the recycling day-view.
    assert 'href="/recycling?start=2026-05-06&end=2026-05-06"' in html
    assert 'href="/recycling?start=2026-05-07&end=2026-05-07"' in html


def test_player_card_attendance_section_hidden_when_empty():
    from unittest.mock import patch
    from fastapi.testclient import TestClient
    from zira_dashboard.app import app

    with patch("zira_dashboard.production_history.attribution_per_day", return_value=[]), \
         patch("zira_dashboard.production_history.daily_records", return_value=[]), \
         patch("zira_dashboard.production_history.attribution_range", return_value={}), \
         patch("zira_dashboard.staffing.load_roster", return_value=[]), \
         patch("zira_dashboard.shift_config.productive_minutes_per_day", return_value=420), \
         patch("zira_dashboard.work_centers_store.registered_groups", return_value=[]), \
         patch("zira_dashboard.awards.awards_earned_by", return_value=[]), \
         patch("zira_dashboard.late_report.absences_history_for_name", return_value=[]), \
         patch("zira_dashboard.late_report.late_arrivals_history_for_name", return_value=[]):
        client = TestClient(app)
        html = client.get("/staffing/people/Carlos?start=2026-05-01&end=2026-05-07").text

    # No section header.
    assert ">Attendance<" not in html


# ---- Task 13: player-card forklift block --------------------------------

def _bare_card_patches():
    """The minimal set of patches that lets a player card render with no DB."""
    return [
        patch("zira_dashboard.production_history.attribution_per_day", return_value=[]),
        patch("zira_dashboard.production_history.daily_records", return_value=[]),
        patch("zira_dashboard.production_history.attribution_range", return_value={}),
        patch("zira_dashboard.staffing.load_roster", return_value=[]),
        patch("zira_dashboard.shift_config.productive_minutes_per_day", return_value=420),
        patch("zira_dashboard.work_centers_store.registered_groups", return_value=[]),
        patch("zira_dashboard.awards.awards_earned_by", return_value=[]),
        patch("zira_dashboard.late_report.absences_history_for_name", return_value=[]),
        patch("zira_dashboard.late_report.late_arrivals_history_for_name", return_value=[]),
    ]


def test_player_card_shows_forklift_block_when_mapped():
    import contextlib

    fk = {
        "calls": 513, "ontime_pct": 97.5, "avg_ms": 42000,
        "utilization_pct": 18.0, "best_score": 86.0,
        "trophies": [{"type": "forklift_goat", "score": 86.0}],
    }
    with contextlib.ExitStack() as stack:
        for p in _bare_card_patches():
            stack.enter_context(p)
        stack.enter_context(patch(
            "zira_dashboard.routes.people._forklift_for_person",
            return_value=fk))
        html = TestClient(app).get("/staffing/people/Trent").text

    assert "Forklift" in html and "513" in html


def test_player_card_no_forklift_block_when_unmapped():
    import contextlib

    with contextlib.ExitStack() as stack:
        for p in _bare_card_patches():
            stack.enter_context(p)
        stack.enter_context(patch(
            "zira_dashboard.routes.people._forklift_for_person",
            return_value=None))
        html = TestClient(app).get("/staffing/people/Nobody").text

    assert "Forklift stats" not in html


# ---- review fix 1: trophies must be looked up by the forklift display name --

def test_forklift_for_person_passes_forklift_name_to_awards(monkeypatch):
    """When a driver's forklift display name differs from the plant name,
    _forklift_for_person must resolve the forklift name and pass *that* to
    awards_earned_by_driver — otherwise trophies silently never match."""
    from zira_dashboard import forklift_awards, forklift_store
    from zira_dashboard.routes import people

    # Forklift name "Trenton" maps to plant name "Trent" (they differ).
    monkeypatch.setattr(forklift_store, "name_map",
                        lambda kind: {"Trenton": "Trent"})
    # Daily rows are carried under the forklift display name.
    monkeypatch.setattr(forklift_store, "driver_days_between",
                        lambda start, end: [
                            {"name": "Trenton", "driver_id": "d1",
                             "day": date(2026, 6, 1), "calls": 30,
                             "on_time": 29, "late": 1, "avg_ms": 42000,
                             "utilization_pct": 18.0},
                        ])

    captured = {}

    def _fake_awards(name, today, cfg):
        captured["name"] = name
        return []

    monkeypatch.setattr(forklift_awards, "awards_earned_by_driver", _fake_awards)

    from zira_dashboard import forklift_score
    out = people._forklift_for_person(
        "Trent", date(2026, 6, 29), forklift_score.DEFAULT_SCORE_CONFIG)

    assert out is not None
    # Must be the forklift display name, not the plant name.
    assert captured["name"] == "Trenton"


# ---- review fix 2: best-day breakdown returned + rendered on the card -------

def test_forklift_for_person_returns_best_day_components(monkeypatch):
    """_forklift_for_person returns the winning day's component breakdown so
    the player card can show a compact breakdown beside the best-day score."""
    from zira_dashboard import forklift_awards, forklift_score, forklift_store
    from zira_dashboard.routes import people

    monkeypatch.setattr(forklift_store, "name_map", lambda kind: {})
    monkeypatch.setattr(forklift_store, "driver_days_between",
                        lambda start, end: [
                            {"name": "Trent", "driver_id": "d1",
                             "day": date(2026, 6, 1), "calls": 30,
                             "on_time": 29, "late": 1, "avg_ms": 42000,
                             "utilization_pct": 18.0},
                        ])
    monkeypatch.setattr(forklift_awards, "awards_earned_by_driver",
                        lambda *a, **k: [])

    out = people._forklift_for_person(
        "Trent", date(2026, 6, 29), forklift_score.DEFAULT_SCORE_CONFIG)

    assert out is not None
    assert out["best_score"] is not None
    # Breakdown carries the four component sub-scores.
    assert out["best_components"] is not None
    assert set(out["best_components"]) == {"calls", "ontime", "speed", "util"}


def test_forklift_for_person_no_breakdown_when_no_eligible_day(monkeypatch):
    """Below the min-calls gate there is no best day, so no breakdown."""
    from zira_dashboard import forklift_awards, forklift_score, forklift_store
    from zira_dashboard.routes import people

    monkeypatch.setattr(forklift_store, "name_map", lambda kind: {})
    monkeypatch.setattr(forklift_store, "driver_days_between",
                        lambda start, end: [
                            {"name": "Trent", "driver_id": "d1",
                             "day": date(2026, 6, 1), "calls": 1,  # below gate
                             "on_time": 1, "late": 0, "avg_ms": 42000,
                             "utilization_pct": 18.0},
                        ])
    monkeypatch.setattr(forklift_awards, "awards_earned_by_driver",
                        lambda *a, **k: [])

    out = people._forklift_for_person(
        "Trent", date(2026, 6, 29), forklift_score.DEFAULT_SCORE_CONFIG)

    assert out is not None
    assert out["best_score"] is None
    assert out["best_components"] is None


def test_player_card_renders_best_day_component_line():
    """The forklift block renders a compact component line beside the
    best-day score when a breakdown is present."""
    import contextlib

    fk = {
        "calls": 513, "ontime_pct": 97.5, "avg_ms": 42000,
        "utilization_pct": 18.0, "best_score": 86.0,
        "best_components": {
            "calls": {"sub": 100.0, "points": 40.0},
            "ontime": {"sub": 87.5, "points": 26.0},
            "speed": {"sub": 92.0, "points": 18.4},
            "util": {"sub": 18.0, "points": 1.8},
        },
        "trophies": [],
    }
    with contextlib.ExitStack() as stack:
        for p in _bare_card_patches():
            stack.enter_context(p)
        stack.enter_context(patch(
            "zira_dashboard.routes.people._forklift_for_person",
            return_value=fk))
        html = TestClient(app).get("/staffing/people/Trent").text

    # Mirrors the trophy-case GOAT component line.
    assert "calls 100" in html
    assert "on-time 88" in html
    assert "speed 92" in html
    assert "util 18" in html
