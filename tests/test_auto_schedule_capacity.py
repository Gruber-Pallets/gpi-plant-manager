from zira_dashboard.auto_schedule_capacity import analyze_auto_expansion


def test_expansion_uses_largest_open_centers_to_minimize_toggle_count():
    result = analyze_auto_expansion(
        unassigned_people=4,
        disabled_centers=("One", "Two", "Three"),
        open_slots_by_center={"One": 1, "Two": 3, "Three": 2},
        center_order={"One": 0, "Two": 1, "Three": 2},
    )

    assert result.unassigned_people == 4
    assert result.centers_to_enable == 2
    assert result.usable_centers == ("Two", "Three", "One")


def test_expansion_reports_no_count_when_all_disabled_capacity_is_insufficient():
    result = analyze_auto_expansion(
        unassigned_people=4,
        disabled_centers=("One", "Two"),
        open_slots_by_center={"One": 1, "Two": 2},
        center_order={"One": 0, "Two": 1},
    )

    assert result.unassigned_people == 4
    assert result.centers_to_enable is None
    assert result.usable_centers == ("Two", "One")
