from datetime import UTC, datetime, timedelta

import pytest

from zira_dashboard.forklift_ingest import ForkliftCompletionEvent
from zira_dashboard.people_performance import forklift_call_buckets


START = datetime(2026, 8, 28, 11, 0, tzinfo=UTC)


def _event(minutes, *, on_time=None, late=None, event_id="c"):
    return ForkliftCompletionEvent(
        event_id=f"{event_id}-{minutes}",
        driver_id="d1",
        driver_name="Alex",
        created_at_utc=START + timedelta(minutes=minutes),
        workstation_name=None,
        on_time=on_time,
        late=late,
        response_ms=None,
        handling_ms=60000,
    )


def test_calls_use_quarter_hour_buckets_and_unknown_status_is_not_late():
    buckets = forklift_call_buckets(
        (_event(2, on_time=True), _event(14, late=True), _event(16)),
        start_utc=START,
        end_utc=START + timedelta(minutes=30),
    )
    assert [bucket.calls for bucket in buckets] == [2, 1]
    assert buckets[0].late_event_times == (START + timedelta(minutes=14),)
    assert buckets[1].rolling_ontime_pct == pytest.approx(50.0)
    assert buckets[1].rolling_late_count == 1


def test_no_classified_calls_leaves_rolling_line_gap():
    buckets = forklift_call_buckets(
        (_event(2),),
        start_utc=START,
        end_utc=START + timedelta(minutes=15),
    )
    assert buckets[0].calls == 1
    assert buckets[0].rolling_ontime_pct is None


def test_unknown_status_counts_as_volume_but_not_ontime_denominator():
    buckets = forklift_call_buckets(
        (_event(2, on_time=True), _event(3, late=True), _event(4)),
        start_utc=START,
        end_utc=START + timedelta(minutes=15),
    )
    assert buckets[0].calls == 3
    assert buckets[0].rolling_ontime_pct == pytest.approx(50.0)


def test_bucket_keys_follow_plant_quarter_hours_from_a_partial_window():
    partial_start = START + timedelta(minutes=7)
    buckets = forklift_call_buckets(
        (_event(6), _event(7), _event(14), _event(15)),
        start_utc=partial_start,
        end_utc=START + timedelta(minutes=20),
    )
    assert [(bucket.start_utc, bucket.end_utc, bucket.calls) for bucket in buckets] == [
        (partial_start, START + timedelta(minutes=15), 2),
        (START + timedelta(minutes=15), START + timedelta(minutes=20), 1),
    ]
