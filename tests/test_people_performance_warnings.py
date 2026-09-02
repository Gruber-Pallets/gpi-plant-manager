from datetime import UTC, date, datetime

from zira_dashboard.people_performance_warnings import (
    production_metric_warning,
    unmatched_forklift_warning,
    warning_key,
)


CHECKED = datetime(2026, 9, 2, 14, 30, tzinfo=UTC)
DAY = date(2026, 9, 2)


def test_warning_key_is_stable_opaque_and_subject_specific():
    first = warning_key("production_metric_unavailable", "Trim Saw 1")
    assert first == warning_key("production_metric_unavailable", "Trim Saw 1")
    assert first != warning_key("production_metric_unavailable", "Hand Build #1")
    assert len(first) == 24
    assert "Trim" not in first


def test_missing_goal_warning_exposes_only_relevant_actions():
    warning = production_metric_warning(
        station_name="Trim Saw 1",
        reason_code="missing_goal",
        checked_at_utc=CHECKED,
        day=DAY,
    )
    assert warning.kind == "production_metric_unavailable"
    assert warning.label == "Production metric unavailable: Trim Saw 1"
    assert [action.action_id for action in warning.actions] == [
        "check_again", "open_work_center", "review_settings"
    ]
    assert warning.actions[1].href == "/wc/trim-saw-1?day=2026-09-02"


def test_unmatched_warning_aggregates_identities_without_raw_events():
    warning = unmatched_forklift_warning(
        call_count=135,
        identities=(("driver-7", ("Sam",), 130), ("driver-8", ("Alex", "A."), 5)),
        first_call_utc=datetime(2026, 9, 2, 12, 0, tzinfo=UTC),
        last_call_utc=datetime(2026, 9, 2, 14, 0, tzinfo=UTC),
        checked_at_utc=CHECKED,
        last_success_at_utc=CHECKED,
        day=DAY,
    )
    assert warning.label == "Unmatched forklift calls: 135"
    assert ("Distinct identities", "2") in warning.facts
    assert all("event" not in value.lower() for _, value in warning.facts)
    assert [action.action_id for action in warning.actions] == [
        "check_again", "review_identities"
    ]
