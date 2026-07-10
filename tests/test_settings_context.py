"""DB-free contract tests for the settings page's pure context builders."""

from datetime import time
from types import SimpleNamespace

import pytest

from zira_dashboard import settings_context


def test_schedule_context_preserves_template_shape_and_input_identity():
    weekday_names = ["Mon", "Tue"]
    schedule = SimpleNamespace(
        shift_start=time(6, 0),
        shift_end=time(14, 30),
        work_weekdays={4, 0, 2},
        breaks=[SimpleNamespace(start=time(9, 0), end=time(9, 15), name="Break")],
    )

    context = settings_context.schedule_context(schedule, weekday_names)

    assert context == {
        "shift_start": "06:00",
        "shift_end": "14:30",
        "work_weekdays": [0, 2, 4],
        "weekday_names": weekday_names,
        "breaks": [{"start": "09:00", "end": "09:15", "name": "Break"}],
    }
    assert context["weekday_names"] is weekday_names


def test_work_center_rows_keep_skill_default_people_and_identity_rules():
    required_skills = ["Repair", "Quality"]
    groups = ["Repair"]
    default_people = ["Ana"]
    locations = [
        SimpleNamespace(meter_id=None, name="Repair 1", bay="R1"),
        SimpleNamespace(meter_id="meter-2", name="Repair 2", bay="R2"),
    ]
    levels = {
        "zoe": {"Repair": 4, "Quality": 2},
        "ana": {"Repair": 3, "Quality": 3},
        "luis": {"Repair": 4, "Quality": 4},
    }
    people = [
        SimpleNamespace(
            name="Zoe", reserve=False, level=lambda skill: levels["zoe"][skill]
        ),
        SimpleNamespace(
            name="Luis", reserve=True, level=lambda skill: levels["luis"][skill]
        ),
        SimpleNamespace(
            name="Ana", reserve=False, level=lambda skill: levels["ana"][skill]
        ),
    ]
    effective = {
        "required_skills": required_skills,
        "min_ops": 1,
        "max_ops": None,
        "goal_per_day": 100,
        "note": "Keep shape",
        "groups": groups,
        "department": "Recycled",
        "default_people": default_people,
    }
    effective_calls = []

    def effective_for(location):
        effective_calls.append(location)
        if location is locations[1]:
            return {**effective, "required_skills": [], "max_ops": 2}
        return effective

    rows = settings_context.work_center_rows(locations, people, effective_for)

    assert effective_calls == locations
    assert rows[0] == {
        "key": "name:Repair 1",
        "name": "Repair 1",
        "bay": "R1",
        "required_skills": required_skills,
        "min_ops": 1,
        "max_ops": "",
        "goal": 100,
        "note": "Keep shape",
        "groups": groups,
        "department": "Recycled",
        "default_people": default_people,
        "default_pool": [
            {"name": "Ana", "level": 3, "reserve": False},
            {"name": "Zoe", "level": 2, "reserve": False},
            {"name": "Luis", "level": 4, "reserve": True},
        ],
    }
    assert rows[0]["required_skills"] is required_skills
    assert rows[0]["groups"] is groups
    assert rows[0]["default_people"] is default_people
    assert rows[1]["key"] == "meter-2"
    assert rows[1]["max_ops"] == 2
    assert [person["level"] for person in rows[1]["default_pool"]] == [2, 2, 2]


def test_group_summary_preserves_override_display_and_store_call_order():
    calls = []

    def recorded(label, value):
        def call(kind, name=None):
            calls.append((label, kind, name))
            return value[name] if name is not None else list(value)

        return call

    rows = settings_context.group_summary(
        "group",
        all_names=recorded("names", {"Repair": None, "Trim": None}),
        members=recorded("members", {"Repair": ["Repair 1", "Repair 2"], "Trim": []}),
        auto_goal=recorded("auto", {"Repair": 200, "Trim": 50}),
        override_goal=recorded("override", {"Repair": None, "Trim": 75}),
        effective_goal=recorded("effective", {"Repair": 200, "Trim": 75}),
    )

    assert rows == [
        {"name": "Repair", "count": 2, "auto": 200, "override": "", "effective": 200},
        {"name": "Trim", "count": 0, "auto": 50, "override": 75, "effective": 75},
    ]
    assert calls == [
        ("names", "group", None),
        ("members", "group", "Repair"),
        ("auto", "group", "Repair"),
        ("override", "group", "Repair"),
        ("effective", "group", "Repair"),
        ("members", "group", "Trim"),
        ("auto", "group", "Trim"),
        ("override", "group", "Trim"),
        ("effective", "group", "Trim"),
    ]


def test_work_schedule_context_preserves_order_fallback_and_display_callable():
    displays = []
    hours_one = {"0": (time(6), time(14, 30))}
    hours_two = {}
    overrides = [
        SimpleNamespace(
            resource_calendar_id=20,
            name="Early",
            work_hours=hours_one,
            rounding=SimpleNamespace(
                in_before_min=1, in_after_min=2, out_before_min=3, out_after_min=4
            ),
        ),
        SimpleNamespace(
            resource_calendar_id=10,
            name="",
            work_hours=hours_two,
            rounding=SimpleNamespace(
                in_before_min=5, in_after_min=6, out_before_min=7, out_after_min=8
            ),
        ),
    ]

    def hours_display(hours):
        displays.append(hours)
        return "display-one" if hours is hours_one else "display-two"

    assert settings_context.work_schedule_context(overrides, hours_display) == [
        {
            "resource_calendar_id": 20,
            "name": "Early",
            "hours_display": "display-one",
            "in_before_min": 1,
            "in_after_min": 2,
            "out_before_min": 3,
            "out_after_min": 4,
        },
        {
            "resource_calendar_id": 10,
            "name": "Schedule 10",
            "hours_display": "display-two",
            "in_before_min": 5,
            "in_after_min": 6,
            "out_before_min": 7,
            "out_after_min": 8,
        },
    ]
    assert displays == [hours_one, hours_two]


def test_rounding_system_context_preserves_order_and_windows():
    systems = [
        SimpleNamespace(
            id=2,
            name="Second",
            rounding=SimpleNamespace(
                in_before_min=11, in_after_min=12, out_before_min=13, out_after_min=14
            ),
        ),
        SimpleNamespace(
            id=1,
            name="First",
            rounding=SimpleNamespace(
                in_before_min=21, in_after_min=22, out_before_min=23, out_after_min=24
            ),
        ),
    ]

    assert settings_context.rounding_system_context(systems) == [
        {
            "id": 2,
            "name": "Second",
            "in_before_min": 11,
            "in_after_min": 12,
            "out_before_min": 13,
            "out_after_min": 14,
        },
        {
            "id": 1,
            "name": "First",
            "in_before_min": 21,
            "in_after_min": 22,
            "out_before_min": 23,
            "out_after_min": 24,
        },
    ]


def test_department_rounding_context_preserves_department_order_and_missing_values():
    departments = ["New", "Recycled", "Unmapped"]

    assert settings_context.department_rounding_context(
        departments, {"Recycled": 7, "New": None}
    ) == [
        {"department": "New", "system_id": None},
        {"department": "Recycled", "system_id": 7},
        {"department": "Unmapped", "system_id": None},
    ]


def test_saturday_schedule_context_preserves_format_and_break_order():
    schedule = SimpleNamespace(
        shift_start=time(5, 5),
        shift_end=time(12, 0),
        breaks=[
            SimpleNamespace(start=time(7, 5), end=time(7, 15), name="First"),
            SimpleNamespace(start=time(10, 0), end=time(10, 20), name="Second"),
        ],
    )

    assert settings_context.saturday_schedule_context(schedule) == {
        "shift_start": "05:05",
        "shift_end": "12:00",
        "breaks": [
            {"start": "07:05", "end": "07:15", "name": "First"},
            {"start": "10:00", "end": "10:20", "name": "Second"},
        ],
    }


@pytest.mark.parametrize(
    ("enabled", "observe_only", "mode"),
    [(False, False, "off"), (False, True, "off"), (True, True, "observe"), (True, False, "live")],
)
def test_auto_lunch_context_preserves_mode_mapping(enabled, observe_only, mode):
    settings = SimpleNamespace(
        enabled=enabled,
        observe_only=observe_only,
        flex_after_hours=5.5,
        flex_minutes=30,
    )

    assert settings_context.auto_lunch_context(settings) == {
        "mode": mode,
        "flex_after_hours": 5.5,
        "flex_minutes": 30,
    }
