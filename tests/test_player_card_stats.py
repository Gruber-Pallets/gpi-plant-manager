"""Unit tests for player card stats redesign — full-day avg + group_avgs.

These tests stub the route's data sources (production_history.attribution_range,
work_centers_store) so they don't need DATABASE_URL.
"""
from __future__ import annotations


from fastapi.testclient import TestClient


def _make_client():
    from zira_dashboard.app import app
    return TestClient(app)


def _stub_route_dependencies(monkeypatch, *, person_data, registered, members_map, roster_names=None):
    """Patch the things the player-card route reaches for.

    person_data: {wc_name: {"units": float, "downtime": float, "hours": float, "days_worked": int}}
    registered:  list[str] — registered group names
    members_map: {group_name: [wc_name, ...]}
    """
    from zira_dashboard import staffing
    from zira_dashboard import shift_config
    from zira_dashboard import production_history, work_centers_store, late_report, awards

    class _FakeLoc:
        def __init__(self, name): self.name = name

    daily_records = []
    for wc_name, totals in person_data.items():
        daily_records.append({
            "day": __import__("datetime").date(2026, 7, 1),
            "person": "Test Person",
            "wc": wc_name,
            "units": totals["units"],
            "downtime": totals["downtime"],
            "hours": totals["hours"],
        })
    monkeypatch.setattr(
        production_history,
        "attribution_range",
        lambda s, e: {"Test Person": person_data},
    )
    monkeypatch.setattr(
        production_history,
        "normalized_daily_records",
        lambda s, e: list(daily_records),
    )
    monkeypatch.setattr(
        production_history,
        "attribution_per_day",
        lambda s, e: [],
    )
    monkeypatch.setattr(
        work_centers_store, "registered_groups", lambda: list(registered),
    )
    monkeypatch.setattr(
        work_centers_store,
        "members",
        lambda kind, name: [_FakeLoc(n) for n in members_map.get(name, [])],
    )

    class _FakePerson:
        def __init__(self, name):
            self.name = name
            self.active = True
            self.skills = {}
            self.reserve = False

    monkeypatch.setattr(
        staffing,
        "load_roster",
        lambda: [_FakePerson("Test Person")],
    )
    monkeypatch.setattr(late_report, "absences_history_for_name", lambda *a, **k: [])
    monkeypatch.setattr(late_report, "late_arrivals_history_for_name", lambda *a, **k: [])
    monkeypatch.setattr(awards, "awards_earned_by", lambda *a, **k: [])
    monkeypatch.setattr(shift_config, "productive_minutes_per_day", lambda: 420)


def test_full_day_avg_per_wc_added_to_rows(monkeypatch):
    _stub_route_dependencies(
        monkeypatch,
        person_data={
            "Repair 1": {"units": 80.0, "downtime": 5.0, "hours": 4.0, "days_worked": 1},
        },
        registered=[],
        members_map={},
    )
    captured = {}

    def _capture(request, template, ctx):
        captured["ctx"] = ctx
        from fastapi.responses import HTMLResponse
        return HTMLResponse("ok")

    from zira_dashboard.deps import templates
    monkeypatch.setattr(templates, "TemplateResponse", _capture)

    r = _make_client().get("/staffing/people/Test Person?start=2026-07-01&end=2026-07-01")
    assert r.status_code == 200
    rows = captured["ctx"]["rows"]
    assert rows[0]["wc"] == "Repair 1"
    assert rows[0]["full_day_avg"] == 140.0
    assert rows[0]["days_worked"] == 1


def test_full_day_avg_zero_hours_returns_none(monkeypatch):
    """Defensive: sub-cutoff hours produce no normalized average."""
    _stub_route_dependencies(
        monkeypatch,
        person_data={
            "Repair 1": {"units": 50.0, "downtime": 0.0, "hours": 0.0, "days_worked": 1},
        },
        registered=[],
        members_map={},
    )
    captured = {}

    def _capture(request, template, ctx):
        captured["ctx"] = ctx
        from fastapi.responses import HTMLResponse
        return HTMLResponse("ok")

    from zira_dashboard.deps import templates
    monkeypatch.setattr(templates, "TemplateResponse", _capture)

    _make_client().get("/staffing/people/Test Person")
    assert captured["ctx"]["rows"][0]["full_day_avg"] is None
    assert captured["ctx"]["rows"][0]["days_worked"] == 0


def test_group_avgs_hides_groups_with_no_hours(monkeypatch):
    """A registered group with no overlap with the person's WCs is omitted."""
    _stub_route_dependencies(
        monkeypatch,
        person_data={
            "Repair 1": {"units": 100.0, "downtime": 0.0, "hours": 10.0, "days_worked": 2},
        },
        registered=["Repairs", "Dismantlers"],
        members_map={
            "Repairs": ["Repair 1", "Repair 2"],
            "Dismantlers": ["Dismantle 1"],
        },
    )
    captured = {}

    def _capture(request, template, ctx):
        captured["ctx"] = ctx
        from fastapi.responses import HTMLResponse
        return HTMLResponse("ok")

    from zira_dashboard.deps import templates
    monkeypatch.setattr(templates, "TemplateResponse", _capture)

    _make_client().get("/staffing/people/Test Person")
    group_avgs = captured["ctx"]["group_avgs"]
    names = [g["name"] for g in group_avgs]
    assert names == ["Repairs"]
    assert group_avgs[0]["avg_units"] == 70.0
    assert group_avgs[0]["days"] == 1


def test_group_avgs_hours_weighted_across_wcs(monkeypatch):
    """Full-day avg sums same-day WC scope before normalizing."""
    _stub_route_dependencies(
        monkeypatch,
        person_data={
            "Repair 1": {"units": 50.0, "downtime": 0.0, "hours": 5.0, "days_worked": 1},
            "Repair 2": {"units": 100.0, "downtime": 0.0, "hours": 10.0, "days_worked": 2},
        },
        registered=["Repairs"],
        members_map={"Repairs": ["Repair 1", "Repair 2"]},
    )
    captured = {}

    def _capture(request, template, ctx):
        captured["ctx"] = ctx
        from fastapi.responses import HTMLResponse
        return HTMLResponse("ok")

    from zira_dashboard.deps import templates
    monkeypatch.setattr(templates, "TemplateResponse", _capture)

    _make_client().get("/staffing/people/Test Person")
    group_avgs = captured["ctx"]["group_avgs"]
    assert len(group_avgs) == 1
    assert group_avgs[0]["avg_units"] == 70.0
    assert group_avgs[0]["days"] == 1


def test_group_avgs_preserves_registry_order(monkeypatch):
    """`group_avgs` follows the order returned by `registered_groups()`."""
    _stub_route_dependencies(
        monkeypatch,
        person_data={
            "A": {"units": 10.0, "downtime": 0.0, "hours": 4.0, "days_worked": 1},
            "B": {"units": 20.0, "downtime": 0.0, "hours": 4.0, "days_worked": 1},
            "C": {"units": 30.0, "downtime": 0.0, "hours": 4.0, "days_worked": 1},
        },
        registered=["Juniors", "Repairs", "Dismantlers"],
        members_map={
            "Juniors": ["A"],
            "Repairs": ["B"],
            "Dismantlers": ["C"],
        },
    )
    captured = {}

    def _capture(request, template, ctx):
        captured["ctx"] = ctx
        from fastapi.responses import HTMLResponse
        return HTMLResponse("ok")

    from zira_dashboard.deps import templates
    monkeypatch.setattr(templates, "TemplateResponse", _capture)

    _make_client().get("/staffing/people/Test Person")
    names = [g["name"] for g in captured["ctx"]["group_avgs"]]
    assert names == ["Juniors", "Repairs", "Dismantlers"]
