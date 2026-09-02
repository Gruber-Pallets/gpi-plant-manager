import json
import math
from dataclasses import replace
from datetime import timedelta

import pytest

from tests.people_performance_fixtures import DAY, END, START, busy_dashboard_model, span
from zira_dashboard import people_performance_view
from zira_dashboard.people_performance import BreakSpan, ProductionHoverPoint, assemble_dashboard
from zira_dashboard.people_performance_view import dashboard_context


def _row_named(context: dict, name: str) -> dict:
    return next(
        row
        for section in context["sections"]
        for row in section["rows"]
        if row["person_name"] == name
    )


def test_dashboard_model_carries_assembled_breaks():
    model = busy_dashboard_model()

    assert [(item.start_utc, item.end_utc, item.label) for item in model.breaks] == [
        (
            START + timedelta(minutes=270),
            START + timedelta(minutes=300),
            "Planned break",
        )
    ]


def test_presenter_uses_only_shift_and_break_start_markers():
    model = replace(
        busy_dashboard_model(),
        breaks=(
            BreakSpan(
                START + timedelta(hours=2),
                START + timedelta(hours=2, minutes=15),
                "Morning break",
            ),
            BreakSpan(
                START + timedelta(hours=4),
                START + timedelta(hours=4, minutes=30),
                "Lunch",
            ),
            BreakSpan(END - timedelta(minutes=15), END, "Cleanup"),
        ),
    )

    context = dashboard_context(model)

    assert [(item["kind"], item["left_pct"]) for item in context["schedule_markers"]] == [
        ("start", 0.0),
        ("break", 25.0),
        ("break", 50.0),
        ("break", 96.875),
        ("end", 100.0),
    ]
    assert [item["label"] for item in context["schedule_time_groups"]] == [
        "6:00 AM",
        "8:00",
        "10:00",
        "1:45 · 2:00 PM",
    ]
    assert "Cleanup starts at 1:45 PM" in context["schedule_markers"][-2]["aria_label"]


def test_schedule_markers_deduplicate_equal_break_and_shift_end_times():
    model = replace(
        busy_dashboard_model(),
        breaks=(BreakSpan(END, END, "End marker"),),
    )

    context = dashboard_context(model)

    assert [item["left_pct"] for item in context["schedule_markers"]].count(100.0) == 1
    assert context["schedule_time_groups"][-1]["edge"] == "end"


@pytest.mark.parametrize(
    ("boundary", "label", "expected"),
    (
        (START, "Opening huddle", "Shift starts at 6:00 AM; Opening huddle starts at 6:00 AM"),
        (END, "Closing cleanup", "Shift ends at 2:00 PM; Closing cleanup starts at 2:00 PM"),
    ),
)
def test_schedule_markers_keep_break_names_at_shift_boundaries(boundary, label, expected):
    model = replace(
        busy_dashboard_model(),
        breaks=(BreakSpan(boundary, boundary, label),),
    )

    marker = next(
        item
        for item in dashboard_context(model)["schedule_markers"]
        if item["left_pct"] == (0.0 if boundary == START else 100.0)
    )

    assert marker["kind"] == ("start" if boundary == START else "end")
    assert marker["aria_label"] == expected


def test_dense_schedule_expands_the_shared_timeline_track():
    breaks = tuple(
        BreakSpan(
            START + timedelta(minutes=15 * index),
            START + timedelta(minutes=15 * index + 5),
            f"Break {index}",
        )
        for index in range(1, 31)
    )

    context = dashboard_context(replace(busy_dashboard_model(), breaks=breaks))

    assert len(context["schedule_markers"]) == 32
    assert context["schedule_track_width_rem"] > 38


def test_schedule_markers_keep_shift_boundaries_for_out_of_window_breaks():
    model = replace(
        busy_dashboard_model(),
        breaks=(
            BreakSpan(
                START - timedelta(minutes=15),
                START - timedelta(minutes=5),
                "Early break",
            ),
            BreakSpan(
                END + timedelta(minutes=5),
                END + timedelta(minutes=15),
                "Late break",
            ),
        ),
    )

    context = dashboard_context(model)

    assert [item["kind"] for item in context["schedule_markers"]] == ["start", "end"]
    assert [item["aria_label"] for item in context["schedule_markers"]] == [
        "Shift starts at 6:00 AM",
        "Shift ends at 2:00 PM",
    ]


def test_schedule_without_breaks_has_only_shift_boundaries():
    context = dashboard_context(replace(busy_dashboard_model(), breaks=()))

    assert [item["kind"] for item in context["schedule_markers"]] == ["start", "end"]
    assert [item["label"] for item in context["schedule_time_groups"]] == [
        "6:00 AM",
        "2:00 PM",
    ]


def test_presenter_preserves_short_intervals():
    context = dashboard_context(busy_dashboard_model())
    row = _row_named(context, "Mia Mixed")

    short = next(item for item in row["intervals"] if item["location_name"] == "Repair 2")
    assert short["width_pct"] > 0
    assert short["aria_label"].startswith("Transferred to Repair 2")


def test_attention_filter_keeps_every_reason_state_inside_fixed_sections():
    context = dashboard_context(busy_dashboard_model(), attention_only=True)

    assert [section["key"] for section in context["sections"]] == [
        "production",
        "forklift",
        "other",
    ]
    reasons = {
        reason
        for section in context["sections"]
        for row in section["rows"]
        for reason in row["attention_reasons"]
    }
    assert {
        "behind goal",
        "late call in last 30 minutes",
        "location missing",
    } <= reasons


def test_production_accessible_name_includes_status_uptime_and_downtime():
    context = dashboard_context(busy_dashboard_model())
    interval = _row_named(context, "Amy Behind")["intervals"][0]

    assert "Repair 1, 6:00 AM. Working now." in interval["aria_label"]
    assert "Behind goal" in interval["aria_label"]
    assert "Uptime 97%" in interval["aria_label"]
    assert "Downtime 15 minutes" in interval["aria_label"]
    assert "Productive time 480 minutes" in interval["aria_label"]


@pytest.mark.parametrize(
    ("source_status", "status_label"),
    (
        ("missing_required_location", "location missing"),
        ("conflicting_location", "location conflicting"),
        ("unmapped_location", "location unmapped"),
    ),
)
def test_uncertain_location_is_explicitly_unavailable_instead_of_neutral(
    source_status, status_label
):
    model = assemble_dashboard(
        day=DAY,
        as_of_utc=END,
        window_start_utc=START,
        window_end_utc=END,
        spans=(span(99, "Uncertain Worker", 0, 480, None, source_status),),
        production_scores=(),
        downtime_by_wc={},
        breakdown_exclusions_by_person_wc={},
        forklift_events_by_employee_id={},
        forklift_day_metrics_by_employee_id={},
        breaks=(),
        metered_wc_names={"Repair 1"},
        source_warnings=(),
        is_today=True,
    )

    interval = _row_named(dashboard_context(model), "Uncertain Worker")["intervals"][0]

    assert interval["state"] == "unavailable"
    assert "Location unavailable" in interval["detail"]
    assert status_label in interval["detail"]
    assert "No metered goal applies" not in interval["detail"]


def test_exempt_location_remains_a_neutral_non_metered_interval():
    model = assemble_dashboard(
        day=DAY,
        as_of_utc=END,
        window_start_utc=START,
        window_end_utc=END,
        spans=(span(100, "Exempt Worker", 0, 480, None, "exempt_no_location"),),
        production_scores=(),
        downtime_by_wc={},
        breakdown_exclusions_by_person_wc={},
        forklift_events_by_employee_id={},
        forklift_day_metrics_by_employee_id={},
        breaks=(),
        metered_wc_names={"Repair 1"},
        source_warnings=(),
        is_today=True,
    )

    interval = _row_named(dashboard_context(model), "Exempt Worker")["intervals"][0]

    assert interval["state"] == "neutral"
    assert "No metered goal applies" in interval["detail"]


def test_forklift_accessible_name_includes_calls_ontime_and_late_count():
    context = dashboard_context(busy_dashboard_model())
    interval = _row_named(context, "Ben Driver")["intervals"][0]

    assert "2 forklift calls" in interval["aria_label"]
    assert "Latest rolling on-time 0%" in interval["aria_label"]
    assert "1 late call" in interval["aria_label"]


def test_presenter_preserves_stable_open_interval_key():
    context = dashboard_context(busy_dashboard_model())
    interval = _row_named(context, "Amy Behind")["intervals"][0]

    assert interval["is_open"] is True
    assert interval["key"] == f"44:production:Repair 1:{START.isoformat()}"
    assert "6:00 AM. Working now." in interval["aria_label"]
    assert "to 2:00 PM" not in interval["aria_label"]


def test_presenter_names_the_end_time_for_a_closed_interval():
    context = dashboard_context(busy_dashboard_model())
    interval = _row_named(context, "Mia Mixed")["intervals"][0]

    assert interval["is_open"] is False
    assert "6:00 AM to 7:30 AM" in interval["aria_label"]


def test_presenter_exposes_five_minute_interval_in_nonoverlapping_short_list():
    context = dashboard_context(busy_dashboard_model())
    row = _row_named(context, "Mia Mixed")

    short = next(item for item in row["short_intervals"] if item["location_name"] == "Repair 2")
    assert short["needs_touch_target"] is True
    assert "7:30 AM to 7:35 AM" in short["time_label"]
    assert all(item["location_name"] != "Repair 1" for item in row["short_intervals"])


def test_location_color_is_stable_when_another_location_appears():
    model = busy_dashboard_model()
    original = _row_named(dashboard_context(model), "Amy Behind")["intervals"][0]
    amy = next(row for row in model.rows if row.person_name == "Amy Behind")
    added = replace(
        amy.intervals[0],
        key="extra-location",
        location_name="AAA temporary center",
        is_open=False,
    )
    expanded = replace(
        model,
        rows=tuple(
            replace(row, intervals=(*row.intervals, added)) if row is amy else row
            for row in model.rows
        ),
    )

    refreshed = next(
        item
        for item in _row_named(dashboard_context(expanded), "Amy Behind")["intervals"]
        if item["location_name"] == "Repair 1"
    )
    assert refreshed["location_class"] == original["location_class"]


def test_production_hover_values_are_cumulative_finite_and_timestamped():
    context = dashboard_context(busy_dashboard_model())
    row = _row_named(context, "Mia Mixed")
    production = [item for item in row["intervals"] if item["role"] == "production"]

    assert type(production[0]["hover_points"][0]) is tuple
    assert production[0]["hover_points"][0][0] == production[0]["hover_start_ms"]
    assert production[-1]["hover_points"][-1][0] == production[-1]["hover_end_ms"]
    assert (
        production[-1]["hover_points"][-1][1]
        >= production[0]["hover_points"][-1][1]
    )
    assert (
        production[-1]["hover_points"][-1][2]
        >= production[0]["hover_points"][-1][2]
    )
    assert all(
        math.isfinite(value)
        for item in production
        for point in item["hover_points"]
        for value in (point[1], point[2])
    )


def test_nonproduction_intervals_do_not_receive_production_hover_values():
    context = dashboard_context(busy_dashboard_model())
    forklift = _row_named(context, "Ben Driver")["intervals"][0]

    assert forklift["hover_points"] == ()
    assert forklift["hover_start_ms"] is None
    assert forklift["hover_end_ms"] is None


def test_presenter_fails_closed_when_hover_values_are_non_finite(monkeypatch):
    poisoned = ProductionHoverPoint(START, float("inf"), 10.0, 95.0)
    monkeypatch.setattr(
        people_performance_view,
        "cumulative_production_hover_points",
        lambda intervals: {
            item.key: (poisoned,) for item in intervals if item.role == "production"
        },
    )

    context = dashboard_context(replace(busy_dashboard_model(), source_warnings=()))
    serialized = json.dumps(context)

    assert "Infinity" not in serialized
    assert all(
        item["hover_points"] == ()
        for section in context["sections"]
        for row in section["rows"]
        for item in row["intervals"]
        if item["role"] == "production"
    )
