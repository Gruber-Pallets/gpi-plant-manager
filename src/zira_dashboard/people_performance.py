"""Pure people-performance interval metrics and timeline values."""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
import math
from numbers import Real
from typing import Literal, TypeAlias

from . import shift_config
from .attendance_timeline import LocationSpan
from .forklift_ingest import ForkliftCompletionEvent
from .production_segments import SegmentScore


RoleKey: TypeAlias = Literal["production", "forklift", "other"]
SectionKey: TypeAlias = Literal["production", "forklift", "other"]
MetricState: TypeAlias = Literal["ahead", "behind", "neutral", "unavailable"]
TimeWindow: TypeAlias = tuple[datetime, datetime]
BreakdownKey: TypeAlias = tuple[int, str, str] | tuple[str, str]


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
class ProductionHoverPoint:
    at_utc: datetime
    actual_units: float
    goal_units: float
    uptime_pct: float | None


@dataclass(frozen=True)
class ProductionMetric:
    actual_units: float
    goal_units: float
    productive_minutes: float
    downtime_minutes: float
    result: MetricState
    rolling_uptime: tuple[RollingPoint, ...]
    hover_points: tuple[ProductionHoverPoint, ...] = ()


@dataclass(frozen=True)
class ForkliftBucket:
    start_utc: datetime
    end_utc: datetime
    calls: int
    late_event_times: tuple[datetime, ...]
    rolling_ontime_pct: float | None
    rolling_late_count: int


@dataclass(frozen=True)
class ForkliftDayMetric:
    calls: int
    on_time: int
    late: int
    handling_minutes: float
    score: float | None
    ontime_floor_pct: float
    timeline_available: bool = True


@dataclass(frozen=True)
class TimelineInterval:
    key: str
    start_utc: datetime
    end_utc: datetime
    location_name: str
    location_status: str
    role: RoleKey
    is_transfer: bool
    is_open: bool = False
    metric_available: bool = True
    production: ProductionMetric | None = None
    forklift_buckets: tuple[ForkliftBucket, ...] = ()


@dataclass(frozen=True)
class PersonRow:
    employee_odoo_id: int
    person_name: str
    is_active: bool
    status: str
    primary_role: RoleKey
    section: SectionKey
    intervals: tuple[TimelineInterval, ...]
    breaks: tuple[BreakSpan, ...]
    attention_reasons: tuple[str, ...]
    summary: tuple[tuple[str, str], ...]
    sort_key: tuple
    unattached_forklift_calls: int = 0


@dataclass(frozen=True)
class DashboardModel:
    day: date
    is_today: bool
    as_of_utc: datetime
    window_start_utc: datetime
    window_end_utc: datetime
    rows: tuple[PersonRow, ...]
    breaks: tuple[BreakSpan, ...] = ()
    source_warnings: tuple[str, ...] = ()


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


def _minute_points(start_utc: datetime, end_utc: datetime) -> tuple[datetime, ...]:
    values = [start_utc]
    cursor = start_utc.replace(second=0, microsecond=0) + timedelta(minutes=1)
    while cursor < end_utc:
        values.append(cursor)
        cursor += timedelta(minutes=1)
    if values[-1] != end_utc:
        values.append(end_utc)
    return tuple(values)


def _production_hover_points(
    score: SegmentScore,
    *,
    available_windows: Sequence[TimeWindow],
    rolling_uptime: Sequence[RollingPoint],
) -> tuple[ProductionHoverPoint, ...]:
    credited = tuple(sorted(score.unit_points, key=lambda point: point.at_utc))
    if any(
        not _is_finite_number(point.units)
        or point.units < 0
        or not isinstance(point.at_utc, datetime)
        or point.at_utc.utcoffset() is None
        or point.at_utc < score.start_utc
        or point.at_utc >= score.end_utc
        for point in credited
    ):
        return ()
    if abs(sum(point.units for point in credited) - score.actual_units) > 1e-6:
        return ()
    available_minutes = _intersection_minutes(
        score.start_utc, score.end_utc, available_windows
    )
    if available_minutes <= 0:
        return ()
    rate = score.goal_units / available_minutes
    values = []
    for at_utc in _minute_points(score.start_utc, score.end_utc):
        actual = sum(point.units for point in credited if point.at_utc <= at_utc)
        elapsed = _intersection_minutes(score.start_utc, at_utc, available_windows)
        is_available = any(left < at_utc <= right for left, right in available_windows)
        uptime = (
            next(
                (
                    point.value_pct
                    for point in reversed(rolling_uptime)
                    if point.at_utc <= at_utc
                ),
                None,
            )
            if is_available
            else None
        )
        values.append(
            ProductionHoverPoint(
                at_utc,
                actual,
                min(score.goal_units, rate * elapsed),
                uptime,
            )
        )
    return tuple(values)


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
    rolling_uptime = rolling_uptime_points(
        start_utc=score.start_utc,
        end_utc=score.end_utc,
        available_windows=available,
        downtime_windows=eligible_stops,
    )
    hover_points = _production_hover_points(
        score,
        available_windows=available,
        rolling_uptime=rolling_uptime,
    )
    return ProductionMetric(
        actual_units=score.actual_units,
        goal_units=score.goal_units,
        productive_minutes=available_minutes,
        downtime_minutes=downtime,
        result=state,
        rolling_uptime=rolling_uptime,
        hover_points=hover_points,
    )


def cumulative_production_hover_points(
    intervals: Sequence[TimelineInterval],
) -> dict[str, tuple[ProductionHoverPoint, ...]]:
    production_keys = [
        interval.key for interval in intervals if interval.role == "production"
    ]
    if len(set(production_keys)) != len(production_keys):
        raise ValueError("production interval keys must be unique")
    actual_base = 0.0
    goal_base = 0.0
    trusted = True
    result = {}
    for interval in intervals:
        if interval.role != "production":
            continue
        metric = interval.production
        if (
            not trusted
            or not interval.metric_available
            or metric is None
            or metric.result == "unavailable"
            or not metric.hover_points
        ):
            trusted = False
            result[interval.key] = ()
            continue
        cumulative_points = []
        for point in metric.hover_points:
            actual_candidate = actual_base + point.actual_units
            goal_candidate = goal_base + point.goal_units
            if not (
                math.isfinite(actual_candidate) and math.isfinite(goal_candidate)
            ):
                trusted = False
                cumulative_points = []
                break
            cumulative_points.append(
                ProductionHoverPoint(
                    point.at_utc,
                    actual_candidate,
                    goal_candidate,
                    point.uptime_pct,
                )
            )
        result[interval.key] = tuple(cumulative_points)
        if not trusted:
            continue
        actual_base += metric.actual_units
        goal_base += metric.goal_units
    return result


def _scoreable_production_totals(
    metrics: Sequence[ProductionMetric],
) -> tuple[float, float, float, float] | None:
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
    if not scoreable:
        return None
    totals = (
        sum((metric.actual_units for metric in scoreable), 0.0),
        sum((metric.goal_units for metric in scoreable), 0.0),
        sum((metric.productive_minutes for metric in scoreable), 0.0),
        sum((metric.downtime_minutes for metric in scoreable), 0.0),
    )
    return totals if all(_is_finite_number(value) for value in totals) else None


def weighted_production_summary(
    metrics: Sequence[ProductionMetric],
) -> tuple[float | None, float | None, float]:
    """Return weighted goal, uptime, and downtime for scoreable intervals."""
    totals = _scoreable_production_totals(metrics)
    if totals is None:
        return None, None, 0.0
    actual, goal, available, downtime = totals
    goal_pct = (actual / goal) * 100.0 if goal > 0 else None
    uptime_pct = (
        (max(0.0, available - downtime) / available) * 100.0
        if available > 0
        else None
    )
    if goal_pct is not None and not _is_finite_number(goal_pct):
        return None, None, 0.0
    if uptime_pct is not None and not _is_finite_number(uptime_pct):
        return None, None, 0.0
    return goal_pct, uptime_pct, downtime


_SECTION_RANK: dict[SectionKey, int] = {
    "production": 0,
    "forklift": 1,
    "other": 2,
}


def _production_subgroup_rank(
    role: RoleKey,
    location_name: str,
    known_no_goal_wc_names: set[str],
    score: SegmentScore | None,
) -> int:
    if role != "production":
        return 0
    if location_name in known_no_goal_wc_names:
        return 1
    if score is None:
        return 0
    if not _is_finite_number(score.goal_units):
        return 1
    return 0 if score.goal_units > 0 else 1


_LOCATION_STATUS = {
    "pending_first_location": "location pending",
    "missing_required_location": "location missing",
    "conflicting_location": "location conflicting",
    "unmapped_location": "location unmapped",
    "stale_open_location": "source stale",
}
_METRIC_ERRORS = (ValueError, TypeError, ArithmeticError)


def _quarter_hour_start(value: datetime) -> datetime:
    local = value.astimezone(shift_config.SITE_TZ)
    local = local.replace(
        minute=(local.minute // 15) * 15,
        second=0,
        microsecond=0,
    )
    return local.astimezone(value.tzinfo)


def forklift_call_buckets(
    events: Sequence[ForkliftCompletionEvent],
    *,
    start_utc: datetime,
    end_utc: datetime,
) -> tuple[ForkliftBucket, ...]:
    """Bucket call volume by 15 minutes and on-time results by 30 minutes."""
    if end_utc < start_utc:
        raise ValueError("end_utc must not precede start_utc")
    relevant = tuple(
        sorted(
            (event for event in events if start_utc <= event.created_at_utc < end_utc),
            key=lambda event: (event.created_at_utc, event.event_id),
        )
    )
    buckets: list[ForkliftBucket] = []
    cursor = _quarter_hour_start(start_utc)
    while cursor < end_utc:
        bucket_start = max(cursor, start_utc)
        bucket_end = min(cursor + timedelta(minutes=15), end_utc)
        calls = tuple(
            event for event in relevant if bucket_start <= event.created_at_utc < bucket_end
        )
        rolling_start = max(start_utc, bucket_end - timedelta(minutes=30))
        rolling = tuple(
            event for event in relevant if rolling_start <= event.created_at_utc < bucket_end
        )
        on_time = sum(event.on_time is True and event.late is not True for event in rolling)
        late = sum(event.late is True for event in rolling)
        denominator = on_time + late
        buckets.append(
            ForkliftBucket(
                start_utc=bucket_start,
                end_utc=bucket_end,
                calls=len(calls),
                late_event_times=tuple(
                    event.created_at_utc for event in calls if event.late is True
                ),
                rolling_ontime_pct=(100.0 * on_time / denominator if denominator else None),
                rolling_late_count=late,
            )
        )
        cursor += timedelta(minutes=15)
    return tuple(buckets)


def _role_for_span(span: LocationSpan, metered_wc_names: set[str]) -> RoleKey:
    location_usable_for_section = span.status in {"valid", "stale_open_location"}
    if not location_usable_for_section:
        return "other"
    location = span.app_work_center_name
    if location == "Tablets":
        return "forklift"
    if location in metered_wc_names:
        return "production"
    return "other"


def _gap_is_break(
    left: datetime,
    right: datetime,
    breaks: Sequence[BreakSpan],
) -> bool:
    return right <= left or any(item.start_utc <= left and right <= item.end_utc for item in breaks)


def _attention_rank(
    *,
    is_active: bool,
    status: str,
    role: RoleKey,
    current_production: ProductionMetric | None,
    current_forklift: ForkliftBucket | None,
    ontime_floor_pct: float,
    metric_available: bool,
) -> tuple[int, tuple[str, ...], float, float]:
    if not is_active:
        return 5, (), 0.0, 100.0
    if status in {
        "location pending",
        "location missing",
        "location conflicting",
        "location unmapped",
        "source stale",
    }:
        return 0, (status,), 0.0, 0.0
    if role != "other" and not metric_available:
        return 0, ("metric unavailable",), 0.0, 0.0
    if role == "production" and current_production:
        goal_pct = (
            100.0 * current_production.actual_units / current_production.goal_units
            if current_production.goal_units > 0
            else None
        )
        current_uptime = next(
            (
                point.value_pct
                for point in reversed(current_production.rolling_uptime)
                if point.value_pct is not None
            ),
            None,
        )
        if goal_pct is not None and goal_pct < 100:
            return (
                1,
                ("behind goal",),
                100.0 - goal_pct,
                current_uptime or 0.0,
            )
        if current_uptime is not None and current_uptime < 90:
            label = "uptime bad" if current_uptime < 80 else "uptime warning"
            return 2, (label,), 0.0, current_uptime
    if role == "forklift" and current_forklift:
        recent_late = current_forklift.rolling_late_count > 0
        pct = current_forklift.rolling_ontime_pct
        if recent_late or (pct is not None and pct < ontime_floor_pct):
            reasons = tuple(
                filter(
                    None,
                    (
                        "late call in last 30 minutes" if recent_late else "",
                        (
                            "below on-time floor"
                            if pct is not None and pct < ontime_floor_pct
                            else ""
                        ),
                    ),
                )
            )
            return 3, reasons, 0.0, pct or 0.0
    return 4, (), 0.0, 100.0


def _location_name(span: LocationSpan) -> str:
    if span.app_work_center_name:
        return span.app_work_center_name
    if span.odoo_work_center_name:
        return span.odoo_work_center_name
    return {
        "pending_first_location": "Location pending",
        "missing_required_location": "Location missing",
        "unmapped_location": "Location unmapped",
        "conflicting_location": "Location conflicting",
        "exempt_no_location": "No metered location",
    }.get(span.status, "Location unavailable")


def _clocked_out_label(value: datetime) -> str:
    local = value.astimezone(shift_config.SITE_TZ)
    return f"clocked out at {local.strftime('%-I:%M %p')}"


def _duration_label(minutes: float) -> str:
    total = max(0, round(minutes))
    hours, remainder = divmod(total, 60)
    if hours and remainder:
        return f"{hours} hr {remainder} min"
    if hours:
        return f"{hours} hr"
    return f"{remainder} min"


def _pct_or_na(value: float | None) -> str:
    return f"{value:.0f}%" if value is not None and _is_finite_number(value) else "N/A"


def _validated_day_metric(value: object | None) -> ForkliftDayMetric | None:
    if value is None:
        return None
    if not isinstance(value, ForkliftDayMetric):
        raise TypeError("forklift day metric has the wrong type")
    counts = (value.calls, value.on_time, value.late)
    if any(isinstance(item, bool) or not isinstance(item, int) or item < 0 for item in counts):
        raise ValueError("forklift day counts must be non-negative integers")
    if value.on_time + value.late > value.calls:
        raise ValueError("classified forklift calls cannot exceed total calls")
    numbers = (value.handling_minutes, value.ontime_floor_pct)
    if not all(_is_finite_number(item) and float(item) >= 0 for item in numbers):
        raise ValueError("forklift durations and floor must be finite and non-negative")
    if value.score is not None and not _is_finite_number(value.score):
        raise ValueError("forklift score must be finite or unavailable")
    return value


def _validated_events(
    value: object | None,
) -> tuple[ForkliftCompletionEvent, ...]:
    if value is None:
        return ()
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise TypeError("forklift events must be a sequence")
    events: list[ForkliftCompletionEvent] = []
    for event in value:
        if not isinstance(event, ForkliftCompletionEvent):
            raise TypeError("forklift event has the wrong type")
        if (
            not isinstance(event.created_at_utc, datetime)
            or event.created_at_utc.utcoffset() is None
        ):
            raise TypeError("forklift event time must be timezone aware")
        if not isinstance(event.event_id, str) or not event.event_id:
            raise TypeError("forklift event ID must be non-empty text")
        events.append(event)
    return tuple(events)


def _production_score_for_span(
    scores: Mapping[tuple[int, str, datetime, datetime], SegmentScore],
    *,
    employee_odoo_id: int,
    span: LocationSpan,
) -> SegmentScore | None:
    if span.app_work_center_name is None:
        return None
    return scores.get(
        (
            employee_odoo_id,
            span.app_work_center_name,
            span.start_utc,
            span.end_utc,
        )
    )


def _breakdown_windows(
    values: Mapping[BreakdownKey, Sequence[tuple[datetime, datetime | None]]],
    *,
    employee_odoo_id: int,
    person_name: str,
    name_is_unique: bool,
    wc_name: str,
    as_of_utc: datetime,
) -> tuple[TimeWindow, ...]:
    raw = tuple(
        window
        for key, windows in values.items()
        if len(key) == 3
        and isinstance(key[0], int)
        and not isinstance(key[0], bool)
        and key[0] == employee_odoo_id
        and key[2] == wc_name
        for window in windows
    )
    if not raw and name_is_unique:
        raw = tuple(values.get((person_name, wc_name), ()))
    return tuple(
        (start, min(as_of_utc, end or as_of_utc))
        for start, end in (raw or ())
        if min(as_of_utc, end or as_of_utc) > start
    )


def _production_summary(
    intervals: Sequence[TimelineInterval],
) -> tuple[tuple[str, str], ...]:
    production_intervals = [item for item in intervals if item.role == "production"]
    metrics = [
        item.production
        for item in production_intervals
        if item.production is not None and item.production.result != "unavailable"
    ]
    complete = bool(production_intervals) and all(
        item.metric_available and item.production is not None for item in production_intervals
    ) and all(_scoreable_production_totals((metric,)) is not None for metric in metrics)
    totals = _scoreable_production_totals(metrics)
    if complete and totals is not None:
        goal_pct, uptime_pct, downtime_minutes = weighted_production_summary(metrics)
        if goal_pct is not None and uptime_pct is not None:
            goal = _pct_or_na(goal_pct)
            uptime = _pct_or_na(uptime_pct)
            downtime = f"{downtime_minutes:.0f} min"
            actual_units, goal_units, _available_minutes, _downtime_minutes = totals
            production = f"{actual_units:.0f}/{goal_units:.0f}"
        else:
            goal = uptime = downtime = production = "N/A"
    else:
        goal = uptime = downtime = production = "N/A"
    return (
        ("Goal", goal),
        ("Uptime", uptime),
        ("Downtime", downtime),
        ("Production", production),
    )


def _forklift_summary(driver_day: ForkliftDayMetric | None) -> tuple[tuple[str, str], ...]:
    if driver_day is None or not driver_day.timeline_available:
        return (
            ("Calls", "N/A"),
            ("On time", "N/A"),
            ("Handling", "N/A"),
            ("Score", "N/A"),
        )
    denominator = driver_day.on_time + driver_day.late
    whole_day_ontime_pct = 100.0 * driver_day.on_time / denominator if denominator else None
    return (
        ("Calls", str(driver_day.calls)),
        ("On time", _pct_or_na(whole_day_ontime_pct)),
        ("Handling", f"{driver_day.handling_minutes:.0f} min"),
        ("Score", "N/A" if driver_day.score is None else f"{driver_day.score:.0f}"),
    )


def _other_summary(
    *,
    spans: Sequence[LocationSpan],
    final_location_name: str,
    is_active: bool,
) -> tuple[tuple[str, str], ...]:
    clocked_minutes = sum((span.end_utc - span.start_utc).total_seconds() / 60.0 for span in spans)
    locations = {_location_name(span) for span in spans}
    clocked_out = _clocked_out_label(spans[-1].end_utc)
    return (
        ("Clocked", _duration_label(clocked_minutes)),
        ("Location", final_location_name),
        ("Locations", str(len(locations))),
        ("Status", "Working now" if is_active else clocked_out),
    )


def _unavailable_interval(
    *,
    employee_odoo_id: int,
    span: LocationSpan,
    role: RoleKey,
    location_name: str,
    location_status: str,
    is_transfer: bool,
    score: SegmentScore | None,
) -> TimelineInterval:
    production = (
        _unavailable_metric(score)
        if role == "production" and score and span.status == "valid"
        else None
    )
    return TimelineInterval(
        key=_interval_key(employee_odoo_id, role, location_name, span.start_utc),
        start_utc=span.start_utc,
        end_utc=span.end_utc,
        location_name=location_name,
        location_status=location_status,
        role=role,
        is_transfer=is_transfer,
        is_open=span.is_open,
        metric_available=False,
        production=production,
    )


def _interval_key(
    employee_odoo_id: int,
    role: RoleKey,
    location_name: str,
    start_utc: datetime,
) -> str:
    return f"{employee_odoo_id}:{role}:{location_name}:{start_utc.isoformat()}"


def _assemble_person_row(
    *,
    employee_odoo_id: int,
    person_name: str,
    spans: Sequence[LocationSpan],
    production_scores: Mapping[tuple[int, str, datetime, datetime], SegmentScore],
    downtime_by_wc: Mapping[str, Sequence[TimeWindow]],
    breakdown_exclusions_by_person_wc: Mapping[
        BreakdownKey, Sequence[tuple[datetime, datetime | None]]
    ],
    forklift_events: Sequence[ForkliftCompletionEvent],
    forklift_day_metric: ForkliftDayMetric | None,
    breaks: Sequence[BreakSpan],
    metered_wc_names: set[str],
    known_no_goal_wc_names: set[str],
    as_of_utc: datetime,
    window_end_utc: datetime,
    is_today: bool,
    name_is_unique: bool,
    production_available: bool,
    forklift_available: bool,
    force_unavailable: bool = False,
) -> PersonRow:
    final_span = spans[-1]
    final_role = _role_for_span(final_span, metered_wc_names)
    final_location = _location_name(final_span)
    is_active = is_today and final_span.is_open
    normal_status = "working now" if is_active else _clocked_out_label(final_span.end_utc)
    status = _LOCATION_STATUS.get(final_span.status, normal_status)

    events = tuple(sorted(forklift_events, key=lambda item: (item.created_at_utc, item.event_id)))
    valid_tablet_spans = tuple(
        span
        for span in spans
        if span.status == "valid" and _role_for_span(span, metered_wc_names) == "forklift"
    )
    unattached_calls = sum(
        not any(
            span.start_utc <= event.created_at_utc < span.end_utc for span in valid_tablet_spans
        )
        for event in events
    )

    intervals: list[TimelineInterval] = []
    previous_role: RoleKey | None = None
    previous_location: str | None = None
    previous_end: datetime | None = None
    for span in spans:
        role = _role_for_span(span, metered_wc_names)
        location = _location_name(span)
        location_status = _LOCATION_STATUS.get(span.status, "valid")
        changed = previous_role is not None and (
            role != previous_role or location != previous_location
        )
        is_transfer = changed
        if (
            not changed
            and previous_end is not None
            and _gap_is_break(previous_end, span.start_utc, breaks)
        ):
            is_transfer = False
        score = _production_score_for_span(
            production_scores,
            employee_odoo_id=employee_odoo_id,
            span=span,
        )
        metric_allowed = not force_unavailable and span.status in {
            "valid",
            "exempt_no_location",
        }
        if role == "production":
            metric_allowed = metric_allowed and production_available and score is not None
        elif role == "forklift":
            metric_allowed = (
                metric_allowed
                and forklift_available
                and forklift_day_metric is not None
                and forklift_day_metric.timeline_available
            )

        if not metric_allowed and role != "other":
            interval = _unavailable_interval(
                employee_odoo_id=employee_odoo_id,
                span=span,
                role=role,
                location_name=location,
                location_status=location_status,
                is_transfer=is_transfer,
                score=score,
            )
        else:
            production: ProductionMetric | None = None
            buckets: tuple[ForkliftBucket, ...] = ()
            if role == "production" and score is not None:
                exclusions = _breakdown_windows(
                    breakdown_exclusions_by_person_wc,
                    employee_odoo_id=employee_odoo_id,
                    person_name=person_name,
                    name_is_unique=name_is_unique,
                    wc_name=score.wc_name,
                    as_of_utc=as_of_utc,
                )
                production = production_metric(
                    score,
                    downtime_windows=downtime_by_wc.get(score.wc_name, ()),
                    breaks=breaks,
                    excluded_windows=exclusions,
                )
                metric_allowed = production.result != "unavailable"
            elif role == "forklift":
                interval_events = tuple(
                    event
                    for event in events
                    if span.start_utc <= event.created_at_utc < span.end_utc
                )
                buckets = forklift_call_buckets(
                    interval_events,
                    start_utc=span.start_utc,
                    end_utc=span.end_utc,
                )
            interval = TimelineInterval(
                key=_interval_key(employee_odoo_id, role, location, span.start_utc),
                start_utc=span.start_utc,
                end_utc=span.end_utc,
                location_name=location,
                location_status=location_status,
                role=role,
                is_transfer=is_transfer,
                is_open=span.is_open,
                metric_available=metric_allowed,
                production=production,
                forklift_buckets=buckets,
            )
        intervals.append(interval)
        previous_role = role
        previous_location = location
        previous_end = span.end_utc

    final_interval = intervals[-1]
    if final_role == "production":
        summary = _production_summary(intervals)
    elif final_role == "forklift":
        summary = _forklift_summary(
            forklift_day_metric if forklift_available and not force_unavailable else None
        )
    else:
        summary = _other_summary(
            spans=spans,
            final_location_name=final_location,
            is_active=is_active,
        )

    current_forklift = (
        final_interval.forklift_buckets[-1] if final_interval.forklift_buckets else None
    )
    ontime_floor = forklift_day_metric.ontime_floor_pct if forklift_day_metric else 0.0
    attention_rank, reasons, deficit, rolling_tiebreak = _attention_rank(
        is_active=is_active,
        status=status,
        role=final_role,
        current_production=final_interval.production,
        current_forklift=current_forklift,
        ontime_floor_pct=ontime_floor,
        metric_available=final_interval.metric_available,
    )
    if force_unavailable:
        attention_rank = 0 if is_active else 5
        reasons = ("metric unavailable",)
        deficit = 0.0
        rolling_tiebreak = 0.0
    final_score = _production_score_for_span(
        production_scores,
        employee_odoo_id=employee_odoo_id,
        span=final_span,
    )
    sort_key = (
        _SECTION_RANK[final_role],
        _production_subgroup_rank(
            final_role,
            final_interval.location_name,
            known_no_goal_wc_names,
            final_score,
        ),
        attention_rank,
        -deficit,
        rolling_tiebreak,
        person_name.casefold(),
        employee_odoo_id,
    )
    return PersonRow(
        employee_odoo_id=employee_odoo_id,
        person_name=person_name,
        is_active=is_active,
        status=status,
        primary_role=final_role,
        section=final_role,
        intervals=tuple(intervals),
        breaks=tuple(breaks),
        attention_reasons=reasons,
        summary=summary,
        sort_key=sort_key,
        unattached_forklift_calls=unattached_calls,
    )


def assemble_dashboard(
    *,
    day: date,
    as_of_utc: datetime,
    window_start_utc: datetime,
    window_end_utc: datetime,
    spans: Sequence[LocationSpan],
    production_scores: Sequence[SegmentScore],
    downtime_by_wc: dict[str, Sequence[TimeWindow]],
    breakdown_exclusions_by_person_wc: Mapping[
        BreakdownKey, Sequence[tuple[datetime, datetime | None]]
    ],
    forklift_events_by_employee_id: Mapping[int, Sequence[ForkliftCompletionEvent]],
    forklift_day_metrics_by_employee_id: Mapping[int, ForkliftDayMetric],
    breaks: Sequence[BreakSpan],
    metered_wc_names: set[str],
    source_warnings: Sequence[str],
    is_today: bool,
    known_no_goal_wc_names: set[str] | None = None,
    production_available: bool = True,
    forklift_available: bool = True,
) -> DashboardModel:
    """Assemble one safely attributed dashboard row per Odoo employee ID."""
    cap = min(as_of_utc, window_end_utc)
    known_no_goal_names = known_no_goal_wc_names or set()
    by_employee: dict[int, list[LocationSpan]] = {}
    for span in spans:
        left = max(window_start_utc, span.start_utc)
        right = min(cap, span.end_utc)
        if right <= left:
            continue
        clipped = LocationSpan(
            employee_odoo_id=span.employee_odoo_id,
            employee_name=span.employee_name,
            start_utc=left,
            end_utc=right,
            status=span.status,
            app_work_center_name=span.app_work_center_name,
            odoo_work_center_id=span.odoo_work_center_id,
            odoo_work_center_name=span.odoo_work_center_name,
            attendance_ids=span.attendance_ids,
            department_repair=span.department_repair,
            is_open=span.is_open,
        )
        by_employee.setdefault(span.employee_odoo_id, []).append(clipped)

    for employee_spans in by_employee.values():
        employee_spans.sort(key=lambda item: (item.start_utc, item.end_utc))

    name_counts = Counter(
        employee_spans[0].employee_name for employee_spans in by_employee.values()
    )

    score_index: dict[tuple[int, str, datetime, datetime], SegmentScore] = {}
    for score in production_scores:
        if score.person_odoo_id is None or score.start_utc is None or score.end_utc is None:
            continue
        score_index[(score.person_odoo_id, score.wc_name, score.start_utc, score.end_utc)] = score

    rows: list[PersonRow] = []
    for employee_odoo_id in sorted(by_employee):
        employee_spans = by_employee[employee_odoo_id]
        person_name = employee_spans[0].employee_name
        name_is_unique = name_counts[person_name] == 1
        raw_events = forklift_events_by_employee_id.get(employee_odoo_id)
        raw_driver_day = forklift_day_metrics_by_employee_id.get(employee_odoo_id)
        events: tuple[ForkliftCompletionEvent, ...] = ()
        try:
            events = _validated_events(raw_events)
            driver_day = _validated_day_metric(raw_driver_day)
            row = _assemble_person_row(
                employee_odoo_id=employee_odoo_id,
                person_name=person_name,
                spans=employee_spans,
                production_scores=score_index,
                downtime_by_wc=downtime_by_wc,
                breakdown_exclusions_by_person_wc=(breakdown_exclusions_by_person_wc),
                forklift_events=events,
                forklift_day_metric=driver_day,
                breaks=breaks,
                metered_wc_names=metered_wc_names,
                known_no_goal_wc_names=known_no_goal_names,
                as_of_utc=as_of_utc,
                window_end_utc=window_end_utc,
                is_today=is_today,
                name_is_unique=name_is_unique,
                production_available=production_available,
                forklift_available=forklift_available,
            )
        except _METRIC_ERRORS:
            row = _assemble_person_row(
                employee_odoo_id=employee_odoo_id,
                person_name=person_name,
                spans=employee_spans,
                production_scores=score_index,
                downtime_by_wc={},
                breakdown_exclusions_by_person_wc={},
                forklift_events=events,
                forklift_day_metric=None,
                breaks=breaks,
                metered_wc_names=metered_wc_names,
                known_no_goal_wc_names=known_no_goal_names,
                as_of_utc=as_of_utc,
                window_end_utc=window_end_utc,
                is_today=is_today,
                name_is_unique=name_is_unique,
                production_available=False,
                forklift_available=False,
                force_unavailable=True,
            )
        rows.append(row)

    return DashboardModel(
        day=day,
        is_today=is_today,
        as_of_utc=as_of_utc,
        window_start_utc=window_start_utc,
        window_end_utc=window_end_utc,
        rows=tuple(sorted(rows, key=lambda row: row.sort_key)),
        breaks=tuple(breaks),
        source_warnings=tuple(dict.fromkeys(source_warnings)),
    )
