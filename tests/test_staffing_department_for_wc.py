"""staffing.department_for_wc maps a work-center name to its static department."""

from zira_dashboard import staffing


def test_known_work_centers_map_to_static_departments():
    assert staffing.department_for_wc("Dismantler 1") == "Recycled"
    assert staffing.department_for_wc("Tablets") == "Supervisor"
    assert staffing.department_for_wc("Truck Driver") == "Transportation"
    assert staffing.department_for_wc("Work Orders") == "Maintenance"


def test_truck_driver_keeps_transportation_department_but_uses_driving_bay():
    location = staffing.location_by_name("Truck Driver")

    assert location is not None
    assert location.bay == "Driving"
    assert location.department == "Transportation"


def test_unknown_or_blank_work_center_returns_none():
    assert staffing.department_for_wc("Nonexistent WC") is None
    assert staffing.department_for_wc("") is None
    assert staffing.department_for_wc(None) is None
