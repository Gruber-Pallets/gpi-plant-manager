from dataclasses import replace

from tests.people_performance_fixtures import busy_dashboard_model
from zira_dashboard.people_performance_view import dashboard_context


def _row_named(context: dict, name: str) -> dict:
    return next(
        row
        for section in context["sections"]
        for row in section["rows"]
        if row["person_name"] == name
    )


def test_presenter_uses_one_axis_and_preserves_short_intervals():
    context = dashboard_context(busy_dashboard_model())
    row = _row_named(context, "Mia Mixed")

    assert context["axis_labels"][0]["left_pct"] == 0.0
    assert context["axis_labels"][-1]["left_pct"] == 100.0
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
    assert interval["key"].endswith(":open")
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
