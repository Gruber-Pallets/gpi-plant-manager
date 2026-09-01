"""Pure people-performance interval metrics and timeline values."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
import math
from numbers import Real
from typing import Literal, TypeAlias

from .production_segments import SegmentScore


RoleKey: TypeAlias = Literal["production", "forklift", "other"]
SectionKey: TypeAlias = Literal["production", "forklift", "other"]
MetricState: TypeAlias = Literal["ahead", "behind", "neutral", "unavailable"]
TimeWindow: TypeAlias = tuple[datetime, datetime]


@dataclass(frozen=True)
class BreakSpan:
    start_utc: datetime
    end_utc: datetime
    label: str


@dataclass(frozen=True)
class RollingPoint:
    at_utc: datetime
    value_pct: float | None


@dataclass(frozen=True)
class ProductionMetric:
    actual_units: float
    goal_units: float
    productive_minutes: float
    downtime_minutes: float
    result: MetricState
    rolling_uptime: tuple[RollingPoint, ...]


def _is_finite_number(value: object) -> bool:
    if not isinstance(value, Real) or isinstance(value, bool):
        return False
    try:
        return math.isfinite(float(value))
    except (OverflowError, TypeError, ValueError):
        return False


def _safe_float(value: object) -> float:
    return float(value) if _is_finite_number(value) else 0.0


def _unavailable_metric(score: SegmentScore) -> ProductionMetric:
    return ProductionMetric(
        actual_units=_safe_float(score.actual_units),
        goal_units=_safe_float(score.goal_units),
        productive_minutes=_safe_float(score.productive_minutes),
        downtime_minutes=0.0,
        result="unavailable",
        rolling_uptime=(),
    )


def _intersection_minutes(
    left: datetime,
    right: datetime,
    windows: Sequence[TimeWindow],
) -> float:
    return sum(
        max(0.0, (min(right, end) - max(left, start)).total_seconds() / 60.0)
        for start, end in windows
        if min(right, end) > max(left, start)
    )


def _merge_windows(windows: Sequence[TimeWindow]) -> tuple[TimeWindow, ...]:
    merged: list[list[datetime]] = []
    for start, end in sorted((left, right) for left, right in windows if right > left):
        if not merged or start > merged[-1][1]:
            merged.append([start, end])
        else:
            merged[-1][1] = max(merged[-1][1], end)
    return tuple((start, end) for start, end in merged)


def productive_windows(
    start_utc: datetime,
    end_utc: datetime,
    breaks: Sequence[BreakSpan],
) -> tuple[TimeWindow, ...]:
    """Return the interval pieces remaining after planned exclusions."""
    pieces = [(start_utc, end_utc)]
    for item in sorted(breaks, key=lambda value: value.start_utc):
        next_pieces: list[TimeWindow] = []
        for left, right in pieces:
            if item.end_utc <= left or item.start_utc >= right:
                next_pieces.append((left, right))
            else:
                if left < item.start_utc:
                    next_pieces.append((left, min(right, item.start_utc)))
                if item.end_utc < right:
                    next_pieces.append((max(left, item.end_utc), right))
        pieces = next_pieces
    return tuple((left, right) for left, right in pieces if right > left)


def _rolling_point(
    *,
    at_utc: datetime,
    end_utc: datetime,
    available: Sequence[TimeWindow],
    downtime: Sequence[TimeWindow],
    window: timedelta,
) -> RollingPoint:
    current_window = next(
        (
            (left, right)
            for left, right in available
            if left < at_utc <= right or at_utc == left == end_utc
        ),
        None,
    )
    window_start = max(current_window[0], at_utc - window) if current_window else at_utc
    denominator = _intersection_minutes(window_start, at_utc, available)
    if current_window is None or denominator <= 0:
        value = None
    else:
        stopped = _intersection_minutes(window_start, at_utc, downtime)
        value = 100.0 * max(0.0, denominator - stopped) / denominator
    return RollingPoint(at_utc=at_utc, value_pct=value)


def rolling_uptime_points(
    *,
    start_utc: datetime,
    end_utc: datetime,
    available_windows: Sequence[TimeWindow],
    downtime_windows: Sequence[TimeWindow],
    step: timedelta = timedelta(minutes=5),
    window: timedelta = timedelta(minutes=30),
) -> tuple[RollingPoint, ...]:
    """Calculate a rolling uptime series without bridging unavailable gaps."""
    if step <= timedelta(0):
        raise ValueError("step must be positive")
    if window <= timedelta(0):
        raise ValueError("window must be positive")
    if end_utc < start_utc:
        raise ValueError("end_utc must not precede start_utc")
    available = _merge_windows(available_windows)
    downtime = _merge_windows(downtime_windows)
    points: list[RollingPoint] = []
    at = start_utc
    while at <= end_utc:
        points.append(
            _rolling_point(
                at_utc=at,
                end_utc=end_utc,
                available=available,
                downtime=downtime,
                window=window,
            )
        )
        at += step
    if not points or points[-1].at_utc != end_utc:
        points.append(
            _rolling_point(
                at_utc=end_utc,
                end_utc=end_utc,
                available=available,
                downtime=downtime,
                window=window,
            )
        )
    return tuple(points)


def production_metric(
    score: SegmentScore,
    *,
    downtime_windows: Sequence[TimeWindow],
    breaks: Sequence[BreakSpan],
    excluded_windows: Sequence[TimeWindow] = (),
) -> ProductionMetric:
    """Build one segment's goal and uptime metric from timestamped windows."""
    metric_values = (
        score.actual_units,
        score.goal_units,
        score.productive_minutes,
    )
    if not all(_is_finite_number(value) for value in metric_values):
        return _unavailable_metric(score)
    if (
        score.start_utc is None
        or score.end_utc is None
        or score.end_utc <= score.start_utc
        or score.goal_units <= 0
        or score.productive_minutes <= 0
    ):
        return _unavailable_metric(score)

    exclusions = tuple(breaks) + tuple(
        BreakSpan(left, right, "Approved machine breakdown") for left, right in excluded_windows
    )
    available = productive_windows(score.start_utc, score.end_utc, exclusions)
    available_minutes = max(0.0, score.productive_minutes)
    clipped_stops = _merge_windows(
        tuple(
            (max(score.start_utc, left), min(score.end_utc, right))
            for left, right in downtime_windows
            if min(score.end_utc, right) > max(score.start_utc, left)
        )
    )
    eligible_stops = _merge_windows(
        tuple(
            (max(stop_start, available_start), min(stop_end, available_end))
            for stop_start, stop_end in clipped_stops
            for available_start, available_end in available
            if min(stop_end, available_end) > max(stop_start, available_start)
        )
    )
    downtime = min(
        available_minutes,
        sum((right - left).total_seconds() / 60.0 for left, right in eligible_stops),
    )
    if not _is_finite_number(downtime):
        return _unavailable_metric(score)
    state: MetricState = "ahead" if score.actual_units >= score.goal_units else "behind"
    return ProductionMetric(
        actual_units=score.actual_units,
        goal_units=score.goal_units,
        productive_minutes=available_minutes,
        downtime_minutes=downtime,
        result=state,
        rolling_uptime=rolling_uptime_points(
            start_utc=score.start_utc,
            end_utc=score.end_utc,
            available_windows=available,
            downtime_windows=eligible_stops,
        ),
    )


def weighted_production_summary(
    metrics: Sequence[ProductionMetric],
) -> tuple[float | None, float | None, float]:
    """Return weighted goal, uptime, and downtime for scoreable intervals."""
    scoreable = [
        metric
        for metric in metrics
        if metric.result in {"ahead", "behind"}
        and all(
            _is_finite_number(value)
            for value in (
                metric.actual_units,
                metric.goal_units,
                metric.productive_minutes,
                metric.downtime_minutes,
            )
        )
        and metric.goal_units > 0
        and metric.productive_minutes > 0
        and metric.downtime_minutes >= 0
    ]
    actual = sum((metric.actual_units for metric in scoreable), 0.0)
    goal = sum((metric.goal_units for metric in scoreable), 0.0)
    available = sum((metric.productive_minutes for metric in scoreable), 0.0)
    downtime = sum((metric.downtime_minutes for metric in scoreable), 0.0)
    if not all(_is_finite_number(value) for value in (actual, goal, available, downtime)):
        return None, None, 0.0
    goal_pct = 100.0 * actual / goal if goal > 0 else None
    uptime_pct = 100.0 * max(0.0, available - downtime) / available if available > 0 else None
    if goal_pct is not None and not _is_finite_number(goal_pct):
        return None, None, 0.0
    if uptime_pct is not None and not _is_finite_number(uptime_pct):
        return None, None, 0.0
    return goal_pct, uptime_pct, downtime
