from zira_dashboard import current_schedule_validation, staffing


def _person(name: str, **skills: int) -> staffing.Person:
    return staffing.Person(name=name, skills=skills)


def test_trim_saw_warning_tracks_the_current_pair():
    locations = (
        staffing.Location("Trim Saw 1", "Trim Saw", "Bay 4", "Recycled", None, 2, 2),
    )
    common = dict(
        roster=[
            _person("Level Two", **{"Trim Saw": 2}),
            _person("Level One", **{"Trim Saw": 1}),
            _person("Green", **{"Trim Saw": 3}),
        ],
        enabled_centers={"Trim Saw 1"},
        locations=locations,
        minimums={"Trim Saw 1": 2},
        capacities={"Trim Saw 1": 2},
        required_skills={"Trim Saw 1": ("Trim Saw",)},
        full_day_off_names=set(),
        trim_saw_centers={"Trim Saw 1"},
        training_trainees_by_center={},
        exact_defaults={},
        group_defaults={},
        user_group_centers={},
    )

    unsafe = current_schedule_validation.validate_current_assignments(
        assignments={"Trim Saw 1": ["Level Two", "Level One"]}, **common
    )
    safe = current_schedule_validation.validate_current_assignments(
        assignments={"Trim Saw 1": ["Green", "Level One"]}, **common
    )

    assert "no_safe_complete_crew" in {issue.code for issue in unsafe}
    assert "no_safe_complete_crew" not in {issue.code for issue in safe}


def test_current_validation_reports_duplicate_capacity_and_unqualified_people():
    locations = (
        staffing.Location("Repair 1", "Repair", "Bay 1", "Recycled", None, 1, 1),
        staffing.Location("Repair 2", "Repair", "Bay 1", "Recycled", None, 1, 1),
    )
    issues = current_schedule_validation.validate_current_assignments(
        roster=[_person("Alex", Repair=0), _person("Bea", Repair=1)],
        assignments={"Repair 1": ["Alex", "Bea"], "Repair 2": ["Bea"]},
        enabled_centers={"Repair 1"},
        locations=locations,
        minimums={"Repair 1": 1},
        capacities={"Repair 1": 1},
        required_skills={"Repair 1": ("Repair",)},
        full_day_off_names=set(),
        trim_saw_centers=set(),
        training_trainees_by_center={},
        exact_defaults={},
        group_defaults={},
        user_group_centers={},
    )

    assert {issue.code for issue in issues} == {
        "person_assigned_multiple_centers",
        "center_capacity_exceeded",
        "assignment_unqualified",
    }


def test_current_validation_removes_minimum_warning_when_the_visible_crew_is_safe():
    locations = (
        staffing.Location("Repair 1", "Repair", "Bay 1", "Recycled", None, 2, 2),
    )
    common = dict(
        roster=[_person("Green", Repair=3), _person("Trainee", Repair=0)],
        enabled_centers={"Repair 1"},
        locations=locations,
        minimums={"Repair 1": 2},
        capacities={"Repair 1": 2},
        required_skills={"Repair 1": ("Repair",)},
        full_day_off_names=set(),
        trim_saw_centers=set(),
        training_trainees_by_center={"Repair 1": {"Trainee"}},
        exact_defaults={"Repair 1": ["Green"]},
        group_defaults={},
        user_group_centers={},
    )

    short = current_schedule_validation.validate_current_assignments(
        assignments={"Repair 1": ["Trainee"]}, **common
    )
    safe = current_schedule_validation.validate_current_assignments(
        assignments={"Repair 1": ["Green", "Trainee"]}, **common
    )

    assert {issue.code for issue in short} >= {
        "training_partner_missing",
        "center_minimum_unmet",
    }
    assert safe == ()


def test_current_validation_reports_unavailable_and_unhonored_defaults():
    locations = (
        staffing.Location("Repair 1", "Repair", "Bay 1", "Recycled", None, 1, 1),
    )
    issues = current_schedule_validation.validate_current_assignments(
        roster=[_person("Off Today", Repair=3), _person("Defaulted", Repair=3)],
        assignments={"Repair 1": ["Off Today"]},
        enabled_centers={"Repair 1"},
        locations=locations,
        minimums={"Repair 1": 1},
        capacities={"Repair 1": 1},
        required_skills={"Repair 1": ("Repair",)},
        full_day_off_names={"Off Today"},
        trim_saw_centers=set(),
        training_trainees_by_center={},
        exact_defaults={"Repair 1": ["Defaulted"]},
        group_defaults={},
        user_group_centers={},
    )

    assert {issue.code for issue in issues} >= {
        "assignment_unavailable",
        "center_minimum_unmet",
        "exact_default_violation",
    }


def test_current_validation_ignores_unknown_trim_saw_assignees():
    locations = (
        staffing.Location("Trim Saw 1", "Trim Saw", "Bay 4", "Recycled", None, 2, 2),
    )

    issues = current_schedule_validation.validate_current_assignments(
        roster=[
            _person("Green", **{"Trim Saw": 3}),
            _person("Level One", **{"Trim Saw": 1}),
        ],
        assignments={"Trim Saw 1": ["Green", "Not On Roster", "Level One"]},
        enabled_centers={"Trim Saw 1"},
        locations=locations,
        minimums={"Trim Saw 1": 2},
        capacities={"Trim Saw 1": 2},
        required_skills={"Trim Saw 1": ("Trim Saw",)},
        full_day_off_names=set(),
        trim_saw_centers={"Trim Saw 1"},
        training_trainees_by_center={},
        exact_defaults={},
        group_defaults={},
        user_group_centers={},
    )

    assert issues == ()


def test_current_validation_requires_a_non_trainee_green_training_partner():
    locations = (
        staffing.Location("Repair 1", "Repair", "Bay 1", "Recycled", None, 2, 2),
    )

    issues = current_schedule_validation.validate_current_assignments(
        roster=[_person("Trainee A", Repair=3), _person("Trainee B", Repair=3)],
        assignments={"Repair 1": ["Trainee A", "Trainee B"]},
        enabled_centers={"Repair 1"},
        locations=locations,
        minimums={"Repair 1": 2},
        capacities={"Repair 1": 2},
        required_skills={"Repair 1": ("Repair",)},
        full_day_off_names=set(),
        trim_saw_centers=set(),
        training_trainees_by_center={"Repair 1": {"Trainee A", "Trainee B"}},
        exact_defaults={},
        group_defaults={},
        user_group_centers={},
    )

    assert {issue.code for issue in issues} >= {
        "training_partner_missing",
        "center_minimum_unmet",
    }
