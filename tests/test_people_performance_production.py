from dataclasses import replace
from datetime import UTC, datetime, timedelta
import math

import pytest

from zira_dashboard.people_performance import (
    BreakSpan,
    production_metric,
    productive_windows,
    rolling_uptime_points,
    weighted_production_summary,
)
from zira_dashboard.production_segments import SegmentScore


START = datetime(2026, 8, 28, 11, 0, tzinfo=UTC)


def _score(actual, goal, start=START, end=START + timedelta(hours=1)):
    return SegmentScore(
        segment_id=1,
        wc_name="Repair 1",
        person_name="Alex Worker",
        start_utc=start,
        end_utc=end,
        source="odoo",
        productive_minutes=60,
        actual_units=actual,
        goal_units=goal,
        runway_units=max(actual, goal),
        is_active=False,
        result="ahead" if actual >= goal else "behind",
        person_odoo_id=44,
    )


def test_productive_windows_subtract_overlapping_breaks_once():
    windows = productive_windows(
        START,
        START + timedelta(hours=1),
        (
            BreakSpan(
                START + timedelta(minutes=10),
                START + timedelta(minutes=25),
                "Break",
            ),
            BreakSpan(
                START + timedelta(minutes=20),
                START + timedelta(minutes=30),
                "Lunch",
            ),
        ),
    )

    assert windows == (
        (START, START + timedelta(minutes=10)),
        (START + timedelta(minutes=30), START + timedelta(hours=1)),
    )


def test_rolling_uptime_excludes_lunch_and_does_not_bridge_no_denominator():
    points = rolling_uptime_points(
        start_utc=START,
        end_utc=START + timedelta(minutes=45),
        available_windows=(
            (START, START + timedelta(minutes=20)),
            (START + timedelta(minutes=30), START + timedelta(minutes=45)),
        ),
        downtime_windows=((START + timedelta(minutes=10), START + timedelta(minutes=20)),),
        step=timedelta(minutes=5),
        window=timedelta(minutes=30),
    )
    by_time = {point.at_utc: point.value_pct for point in points}

    assert by_time[START] is None
    assert by_time[START + timedelta(minutes=20)] == pytest.approx(50.0)
    assert by_time[START + timedelta(minutes=25)] is None
    assert by_time[START + timedelta(minutes=45)] == pytest.approx(100.0)


def test_rolling_uptime_always_includes_non_step_aligned_end():
    end = START + timedelta(minutes=17)

    points = rolling_uptime_points(
        start_utc=START,
        end_utc=end,
        available_windows=((START, end),),
        downtime_windows=(),
    )

    assert points[-1].at_utc == end
    assert points[-1].value_pct == pytest.approx(100.0)


def test_rolling_uptime_rejects_non_positive_step_before_iteration():
    with pytest.raises(ValueError, match="step must be positive"):
        rolling_uptime_points(
            start_utc=START + timedelta(minutes=5),
            end_utc=START,
            available_windows=(),
            downtime_windows=(),
            step=timedelta(0),
        )


def test_rolling_uptime_rejects_non_positive_window():
    with pytest.raises(ValueError, match="window must be positive"):
        rolling_uptime_points(
            start_utc=START,
            end_utc=START,
            available_windows=(),
            downtime_windows=(),
            window=timedelta(0),
        )


def test_rolling_uptime_rejects_reversed_range():
    with pytest.raises(ValueError, match="end_utc must not precede start_utc"):
        rolling_uptime_points(
            start_utc=START + timedelta(minutes=5),
            end_utc=START,
            available_windows=(),
            downtime_windows=(),
        )


def test_production_metric_intersects_stop_with_worker_arrival():
    score = _score(18, 30, START + timedelta(minutes=15), START + timedelta(hours=1))

    metric = production_metric(
        score,
        downtime_windows=((START, START + timedelta(minutes=25)),),
        breaks=(),
    )

    assert metric.downtime_minutes == pytest.approx(10.0)
    assert metric.result == "behind"


def test_weighted_summary_uses_unit_and_goal_sums():
    ahead = production_metric(_score(10, 5), downtime_windows=(), breaks=())
    behind = production_metric(_score(30, 45), downtime_windows=(), breaks=())

    goal_pct, uptime_pct, downtime = weighted_production_summary((ahead, behind))

    assert goal_pct == pytest.approx(80.0)
    assert uptime_pct == pytest.approx(100.0)
    assert downtime == 0


def test_transfer_uses_each_centers_rate_without_carrying_a_deficit():
    repair = production_metric(
        _score(9, 5, START, START + timedelta(minutes=30)),
        downtime_windows=(),
        breaks=(),
    )
    dismantler = production_metric(
        SegmentScore(
            segment_id=2,
            wc_name="Dismantler 1",
            person_name="Alex Worker",
            start_utc=START + timedelta(minutes=30),
            end_utc=START + timedelta(minutes=60),
            source="odoo",
            productive_minutes=30,
            actual_units=15,
            goal_units=20,
            runway_units=20,
            is_active=True,
            result="behind",
            person_odoo_id=44,
        ),
        downtime_windows=(),
        breaks=(),
    )

    assert repair.result == "ahead"
    assert dismantler.result == "behind"
    assert weighted_production_summary((repair, dismantler))[0] == pytest.approx(96.0)


def test_approved_breakdown_is_removed_from_uptime_denominator_and_stop():
    metric = production_metric(
        _score(20, 20),
        downtime_windows=((START, START + timedelta(minutes=30)),),
        excluded_windows=((START + timedelta(minutes=10), START + timedelta(minutes=20)),),
        breaks=(),
    )

    assert metric.downtime_minutes == pytest.approx(20.0)
    assert metric.rolling_uptime[3].value_pct is None


def test_missing_segment_bounds_are_unavailable_and_excluded_from_summary():
    missing = _score(99, 10)
    missing = SegmentScore(
        segment_id=missing.segment_id,
        wc_name=missing.wc_name,
        person_name=missing.person_name,
        start_utc=None,
        end_utc=None,
        source=missing.source,
        productive_minutes=missing.productive_minutes,
        actual_units=missing.actual_units,
        goal_units=missing.goal_units,
        runway_units=missing.runway_units,
        is_active=missing.is_active,
        result=missing.result,
        person_odoo_id=missing.person_odoo_id,
    )

    metric = production_metric(missing, downtime_windows=(), breaks=())

    assert metric.result == "unavailable"
    assert metric.rolling_uptime == ()
    assert weighted_production_summary((metric,)) == (None, None, 0.0)


def test_missing_goal_is_unavailable_instead_of_zero_performance():
    metric = production_metric(_score(7, 0), downtime_windows=(), breaks=())

    assert metric.result == "unavailable"
    assert weighted_production_summary((metric,)) == (None, None, 0.0)


@pytest.mark.parametrize(
    ("field_name", "bad_value"),
    (
        ("actual_units", float("nan")),
        ("actual_units", float("inf")),
        ("goal_units", float("nan")),
        ("goal_units", float("inf")),
        ("productive_minutes", float("nan")),
        ("productive_minutes", float("inf")),
        ("actual_units", 10**1000),
    ),
)
def test_non_finite_score_values_return_safe_unavailable_metric(field_name, bad_value):
    score = replace(_score(10, 5), **{field_name: bad_value})

    metric = production_metric(score, downtime_windows=(), breaks=())

    assert metric.result == "unavailable"
    assert metric.rolling_uptime == ()
    assert all(
        math.isfinite(value)
        for value in (
            metric.actual_units,
            metric.goal_units,
            metric.productive_minutes,
            metric.downtime_minutes,
        )
    )
    assert weighted_production_summary((metric,)) == (None, None, 0.0)


@pytest.mark.parametrize(
    "end_utc",
    (
        START,
        START - timedelta(minutes=1),
    ),
)
def test_zero_or_reversed_score_interval_is_unavailable(end_utc):
    metric = production_metric(
        _score(10, 5, start=START, end=end_utc),
        downtime_windows=(),
        breaks=(),
    )

    assert metric.result == "unavailable"
    assert metric.rolling_uptime == ()
    assert weighted_production_summary((metric,)) == (None, None, 0.0)


def test_weighted_summary_ignores_non_finite_external_metric():
    valid = production_metric(_score(10, 5), downtime_windows=(), breaks=())
    malformed = replace(
        valid,
        actual_units=float("nan"),
        downtime_minutes=float("inf"),
    )

    goal_pct, uptime_pct, downtime = weighted_production_summary((valid, malformed))

    assert goal_pct == pytest.approx(200.0)
    assert uptime_pct == pytest.approx(100.0)
    assert downtime == 0.0


def test_weighted_summary_fails_closed_when_finite_values_overflow_aggregate():
    first = production_metric(_score(1e308, 5), downtime_windows=(), breaks=())
    second = production_metric(_score(1e308, 5), downtime_windows=(), breaks=())

    assert weighted_production_summary((first, second)) == (None, None, 0.0)
