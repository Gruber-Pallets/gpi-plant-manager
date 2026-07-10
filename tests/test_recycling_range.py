from datetime import date

import pytest

from zira_dashboard.recycling_range import aggregate_range


def _day(units, who, *, station_obj=None):
    return {
        "total_units": units,
        "total_downtime": 60,
        "elapsed": 120,
        "available": 100,
        "uptime_minutes": 90,
        "total_man_hours": 2.0,
        "active_wc_names": {"Dismantler 1"},
        "per_wc_units": {"Dismantler 1": units},
        "per_wc_downtime": {"Dismantler 1": 60},
        "per_wc_expected": {"Dismantler 1": 80.0},
        "per_wc_who": {"Dismantler 1": who},
        "per_wc_category": {"Dismantler 1": "Dismantler"},
        "per_wc_station_obj": {"Dismantler 1": station_obj or object()},
        "schedule_assignments": {"Dismantler 1": [who]},
    }


def test_single_day_keeps_who_and_assignments():
    item = _day(100, "Ana")

    result = aggregate_range([item], [date(2026, 7, 9)], is_range=False)

    assert result.total_units == 100
    assert result.agg_who_today is item["per_wc_who"]
    assert result.schedule_today_assignments is item["schedule_assignments"]


def test_multi_day_sums_work_center_metrics_without_single_day_labels():
    result = aggregate_range(
        [_day(100, "Ana"), _day(50, "Luis")],
        [date(2026, 7, 8), date(2026, 7, 9)],
        is_range=True,
    )

    assert result.total_units == 150
    assert result.total_downtime == 120
    assert result.total_elapsed == 240
    assert result.total_available == 200
    assert result.total_uptime_minutes == 180
    assert result.total_man_hours == 4.0
    assert result.agg_units == {"Dismantler 1": 150}
    assert result.agg_downtime == {"Dismantler 1": 120}
    assert result.agg_expected == {"Dismantler 1": 160.0}
    assert result.agg_who_today == {}
    assert result.schedule_today_assignments == {}


def test_aggregation_preserves_day_order_and_later_dict_overwrites():
    first_station = object()
    replacement_station = object()
    added_station = object()
    first = _day(1, "Ana", station_obj=first_station)
    first.update(
        elapsed=1e16,
        per_wc_units={"Dismantler 1": 1, "Shared": 2},
        per_wc_expected={"Dismantler 1": 1e16, "Shared": 2.0},
        per_wc_category={"Dismantler 1": "Dismantler", "Shared": "Original"},
        per_wc_station_obj={"Dismantler 1": first_station, "Shared": object()},
    )
    second = _day(2, "Luis", station_obj=replacement_station)
    second.update(
        elapsed=-1e16,
        active_wc_names={"Dismantler 1", "Repair 1"},
        per_wc_units={"Dismantler 1": 2, "Repair 1": 3},
        per_wc_expected={"Dismantler 1": -1e16, "Repair 1": 4.0},
        per_wc_category={"Dismantler 1": "Replacement", "Repair 1": "Repair"},
        per_wc_station_obj={
            "Dismantler 1": replacement_station,
            "Repair 1": added_station,
        },
    )
    third = _day(3, "Kai")
    third.update(
        elapsed=1.0,
        per_wc_expected={"Dismantler 1": 1.0},
        per_wc_category={},
        per_wc_station_obj={},
    )

    result = aggregate_range(
        [first, second, third],
        [date(2026, 7, 7), date(2026, 7, 8), date(2026, 7, 9)],
        is_range=True,
    )

    assert result.total_elapsed == 1.0
    assert result.agg_expected["Dismantler 1"] == 1.0
    assert list(result.agg_units) == ["Dismantler 1", "Shared", "Repair 1"]
    assert list(result.agg_category) == ["Dismantler 1", "Shared", "Repair 1"]
    assert result.agg_category["Dismantler 1"] == "Replacement"
    assert result.agg_station_obj["Dismantler 1"] is replacement_station
    assert result.agg_station_obj["Repair 1"] is added_station
    assert result.agg_active_names == {"Dismantler 1", "Repair 1"}


def test_days_and_per_day_must_have_matching_lengths():
    with pytest.raises(ValueError, match=r"zip\(\) argument 2 is shorter than argument 1"):
        aggregate_range([_day(100, "Ana")], [], is_range=False)
