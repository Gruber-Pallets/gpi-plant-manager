from datetime import UTC, date, datetime
from types import SimpleNamespace

from fastapi.responses import HTMLResponse

from zira_dashboard.routes import departments


DAY = date(2026, 7, 10)
NOW = datetime(2026, 7, 10, 15, 0, tzinfo=UTC)


def _empty_new_day():
    return {
        "total_units": 0,
        "total_downtime": 0,
        "elapsed": 0,
        "available": 0,
        "uptime_minutes": 0,
        "total_man_hours": 0.0,
        "total_recycling_people": 0,
        "per_wc_units": {},
        "per_wc_downtime": {},
        "per_wc_expected": {},
        "per_wc_who": {},
        "per_wc_state": {},
        "per_wc_category": {},
        "per_wc_station_obj": {},
        "active_wc_names": set(),
        "schedule_assignments": {},
        "group_buckets": {"New": []},
        "shift_start_label": "07:00",
    }


def _stub_new_renderer(monkeypatch):
    rendered = {}

    def render(_request, _name, context):
        rendered.update(context)
        return HTMLResponse("rendered")

    monkeypatch.setattr(departments.templates, "TemplateResponse", render)
    monkeypatch.setattr(departments.widget_customizer, "load_all", lambda page: {})
    monkeypatch.setattr(departments.layout_store, "layout_map", lambda page: {})
    return rendered


def test_new_legacy_day_becomes_single_day_range(monkeypatch):
    seen = []
    rendered = _stub_new_renderer(monkeypatch)
    monkeypatch.setattr(
        departments,
        "_new_day_data",
        lambda d, *args, **kwargs: seen.append(d) or _empty_new_day(),
    )
    from fastapi.testclient import TestClient
    from zira_dashboard.app import app

    response = TestClient(app).get("/new?day=2026-07-08")

    assert response.status_code == 200
    assert seen == [date(2026, 7, 8)]
    assert rendered["start"] == "2026-07-08"
    assert rendered["end"] == "2026-07-08"
    assert rendered["custom_range_active"] is True


def test_new_week_fans_out_inclusive_days(monkeypatch):
    seen = []
    rendered = _stub_new_renderer(monkeypatch)
    monkeypatch.setattr(
        departments,
        "_new_day_data",
        lambda d, *args, **kwargs: seen.append(d) or _empty_new_day(),
    )
    from fastapi.testclient import TestClient
    from zira_dashboard.app import app

    response = TestClient(app).get("/new?start=2026-07-06&end=2026-07-08")

    assert response.status_code == 200
    assert seen == [date(2026, 7, 6), date(2026, 7, 7), date(2026, 7, 8)]
    assert rendered["start"] == "2026-07-06"
    assert rendered["end"] == "2026-07-08"
    assert rendered["is_range"] is True


def test_downtime_rows_can_select_new_category():
    from zira_dashboard.recycling_data import build_downtime_rows

    rows = build_downtime_rows(
        agg_active_names={"Junior #2", "Repair 1"},
        agg_category={"Junior #2": "New", "Repair 1": "Repair"},
        agg_downtime={"Junior #2": 12, "Repair 1": 4},
        total_elapsed=60,
        agg_who_today={"Junior #2": "Lauro", "Repair 1": "Alice"},
        is_range=False,
        categories=("New",),
    )

    assert [row["name"] for row in rows] == ["Junior #2"]
    assert rows[0]["down"] == 12


def test_new_day_data_uses_one_new_group(monkeypatch):
    captured = {}

    def fake_compute(d, now, is_today_d, **kwargs):
        captured.update(kwargs)
        return {"group_buckets": {"New": []}}

    monkeypatch.setattr(departments, "_department_day_data", fake_compute)
    monkeypatch.setattr(
        departments.staffing,
        "LOCATIONS",
        (SimpleNamespace(
            name="Junior #2", skill="Junior", department="New",
            meter_id="42345",
        ),),
    )
    monkeypatch.setattr(
        departments.work_centers_store,
        "department",
        lambda loc: "New",
    )

    result = departments._new_day_data(DAY, NOW, True)

    assert captured["labor_department"] == "New"
    assert captured["group_categories"] == ("New",)
    assert [s.name for s in captured["stations"]] == ["Junior #2"]
    assert captured["stations"][0].category == "New"
    assert result["group_buckets"] == {"New": []}


def test_recycling_wrapper_preserves_two_groups(monkeypatch):
    captured = {}

    def fake_compute(d, now, is_today_d, **kwargs):
        captured.update(kwargs)
        return {"group_buckets": {"Dismantler": [], "Repair": []}}

    monkeypatch.setattr(departments, "_department_day_data", fake_compute)
    monkeypatch.setattr(departments, "recycling_stations", lambda: [])

    departments._recycling_day_data(DAY, NOW, True)

    assert captured["labor_department"] == "Recycled"
    assert captured["group_categories"] == ("Dismantler", "Repair")
