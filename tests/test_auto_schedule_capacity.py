from zira_dashboard.auto_schedule_capacity import analyze_auto_capacity


def test_capacity_keeps_centers_in_plant_order_until_minimums_fit():
    result = analyze_auto_capacity(
        enabled_centers=("Hand Build #2", "Repair 1", "Big Build #1"),
        minimum_by_center={"Hand Build #2": 2, "Repair 1": 1, "Big Build #1": 2},
        manual_count_by_center={},
        available_people=3,
        center_order={"Hand Build #2": 0, "Repair 1": 1, "Big Build #1": 2},
    )

    assert result.required_people == 5
    assert result.available_people == 3
    assert result.shortage == 2
    assert result.centers_to_disable == 1
    assert result.runnable_centers == ("Hand Build #2", "Repair 1")
    assert result.blocked_centers == ("Big Build #1",)


def test_manual_people_reduce_that_center_remaining_minimum():
    result = analyze_auto_capacity(
        enabled_centers=("Hand Build #2", "Repair 1"),
        minimum_by_center={"Hand Build #2": 2, "Repair 1": 1},
        manual_count_by_center={"Hand Build #2": 1},
        available_people=2,
        center_order={"Hand Build #2": 0, "Repair 1": 1},
    )

    assert result.required_people == 2
    assert result.shortage == 0
    assert result.runnable_centers == ("Hand Build #2", "Repair 1")


def test_center_count_to_disable_uses_largest_remaining_crews_first():
    result = analyze_auto_capacity(
        enabled_centers=("One", "Two", "Three"),
        minimum_by_center={"One": 1, "Two": 2, "Three": 3},
        manual_count_by_center={},
        available_people=3,
        center_order={"One": 0, "Two": 1, "Three": 2},
    )

    assert result.shortage == 3
    assert result.centers_to_disable == 1
