from datetime import date

from zira_dashboard import goat_categories, leaderboard, production_history, staffing
from zira_dashboard._schema import SCHEMA_DDL
from zira_dashboard.routes import departments
from zira_dashboard.stations import STATIONS, recycling_stations


def _hand_build_location():
    return next(
        location for location in staffing.LOCATIONS if location.name == "Hand Build #1"
    )


def _hand_build_station():
    return next(station for station in STATIONS if station.name == "Hand Build #1")


def test_hand_build_1_uses_its_zira_meter_in_both_registries():
    location = _hand_build_location()
    station = _hand_build_station()

    assert location.meter_id == "44484"
    assert station.meter_id == location.meter_id
    assert station.category == "Hand Build"
    assert station.cell == "New"
    assert station not in recycling_stations()


def test_hand_build_1_meter_backfill_only_fills_a_blank_mapping():
    assert "SET meter_id = '44484'" in SCHEMA_DDL
    assert "WHERE name = 'Hand Build #1'" in SCHEMA_DDL
    assert "COALESCE(meter_id, '') = ''" in SCHEMA_DDL


def test_hand_build_1_goal_is_not_reapplied_during_schema_bootstrap():
    assert "SET goal_per_day_override = 400" not in SCHEMA_DDL
    assert "COALESCE(goal_per_day_override, 0) = 0" not in SCHEMA_DDL


def test_hand_build_1_auto_activates_existing_new_paths(monkeypatch):
    monkeypatch.setattr(
        departments.work_centers_store,
        "department",
        lambda location: location.department,
    )
    new_stations = departments._new_stations()
    assert next(
        station for station in new_stations if station.name == "Hand Build #1"
    ).meter_id == "44484"

    captured = {}

    def fake_leaderboard(client, stations, day):
        captured["stations"] = stations
        return []

    monkeypatch.setattr(leaderboard, "cached_leaderboard", fake_leaderboard)
    production_history._metered_leaderboard(object(), date(2026, 8, 18))
    assert next(
        station for station in captured["stations"] if station.name == "Hand Build #1"
    ).meter_id == "44484"

    category = goat_categories.category_for_key("hand_build")
    assert goat_categories.has_metered_source(category) is True
    assert category.minimum_data_days == 30
