"""Presentation-only geometry and labels for the People dashboard."""

from __future__ import annotations

from datetime import datetime
import math

from . import shift_config
from .people_performance import (
    DashboardModel,
    PersonRow,
    ProductionHoverPoint,
    TimelineInterval,
    cumulative_production_hover_points,
)


_SECTION_LABELS = {
    "production": "Metered production",
    "forklift": "Tablet forklift",
    "other": "Other non-metered people",
}
_SECTION_KEYS = ("production", "forklift", "other")
_LOCATION_CLASSES = tuple(f"location-{index}" for index in range(1, 9))


def _pct(value: datetime, start: datetime, end: datetime) -> float:
    total = (end - start).total_seconds()
    if total <= 0:
        return 0.0
    offset = 100.0 * (value - start).total_seconds() / total
    return max(0.0, min(100.0, offset))


def _time(value: datetime) -> str:
    return value.astimezone(shift_config.SITE_TZ).strftime("%-I:%M %p")


_MIN_TIME_LABEL_GAP_PCT = 8.0


def _break_time(value: datetime) -> str:
    return value.astimezone(shift_config.SITE_TZ).strftime("%-I:%M")


def _schedule_markers(model: DashboardModel) -> tuple[dict, ...]:
    candidates = [
        (model.window_start_utc, "start", "Shift starts"),
        *((item.start_utc, "break", f"{item.label} starts") for item in model.breaks),
        (model.window_end_utc, "end", "Shift ends"),
    ]
    markers = []
    seen = set()
    for value, kind, description in sorted(
        candidates,
        key=lambda item: (item[0], item[1] == "break"),
    ):
        clamped = min(max(value, model.window_start_utc), model.window_end_utc)
        if clamped in seen:
            continue
        seen.add(clamped)
        full_time = _time(clamped)
        markers.append(
            {
                "left_pct": _pct(clamped, model.window_start_utc, model.window_end_utc),
                "kind": kind,
                "visible_label": full_time if kind != "break" else _break_time(clamped),
                "aria_label": f"{description} at {full_time}",
            }
        )
    return tuple(markers)


def _schedule_time_groups(markers: tuple[dict, ...]) -> tuple[dict, ...]:
    groups: list[list[dict]] = []
    for marker in markers:
        if groups and marker["left_pct"] - groups[-1][-1]["left_pct"] < _MIN_TIME_LABEL_GAP_PCT:
            groups[-1].append(marker)
        else:
            groups.append([marker])
    return tuple(
        {
            "left_pct": sum(item["left_pct"] for item in group) / len(group),
            "label": " · ".join(item["visible_label"] for item in group),
            "edge": (
                "start"
                if group[0]["left_pct"] == 0.0
                else "end"
                if group[-1]["left_pct"] == 100.0
                else "middle"
            ),
        }
        for group in groups
    )


def _epoch_ms(value: datetime) -> int:
    return round(value.timestamp() * 1000)


def _hover_point_view(point: ProductionHoverPoint) -> tuple[int, float, float, float | None]:
    return (
        _epoch_ms(point.at_utc),
        round(point.actual_units, 6),
        round(point.goal_units, 6),
        None if point.uptime_pct is None else round(point.uptime_pct, 6),
    )


def _hover_points_view(
    points: tuple[ProductionHoverPoint, ...],
) -> tuple[tuple[int, float, float, float | None], ...]:
    try:
        if any(
            not math.isfinite(point.at_utc.timestamp())
            or not math.isfinite(point.actual_units)
            or not math.isfinite(point.goal_units)
            or (
                point.uptime_pct is not None
                and not math.isfinite(point.uptime_pct)
            )
            for point in points
        ):
            return ()
        return tuple(_hover_point_view(point) for point in points)
    except (AttributeError, OverflowError, OSError, TypeError, ValueError):
        return ()


def _line_runs(points: list[dict]) -> tuple[tuple[dict, ...], ...]:
    runs: list[list[dict]] = []
    current: list[dict] = []
    for point in points:
        if point["y"] is None:
            if current:
                runs.append(current)
                current = []
        else:
            current.append(point)
    if current:
        runs.append(current)
    return tuple(tuple(run) for run in runs if len(run) >= 2)


def _count_label(value: int, singular: str) -> str:
    return f"{value} {singular}{'' if value == 1 else 's'}"


def _production_detail(item: TimelineInterval) -> str:
    metric = item.production
    if not item.metric_available or metric is None or metric.result == "unavailable":
        return "Production status unavailable. Uptime unavailable. Downtime unavailable."
    uptime_pct = (
        100.0
        * max(0.0, metric.productive_minutes - metric.downtime_minutes)
        / metric.productive_minutes
        if metric.productive_minutes > 0
        else None
    )
    uptime = "unavailable" if uptime_pct is None else f"{uptime_pct:.0f}%"
    return (
        f"{metric.result.title()} goal. "
        f"{metric.actual_units:.1f} credited units against a "
        f"{metric.goal_units:.1f} goal. Uptime {uptime}. "
        f"Downtime {metric.downtime_minutes:.0f} minutes. "
        f"Productive time {metric.productive_minutes:.0f} minutes."
    )


def _forklift_detail(item: TimelineInterval) -> str:
    if not item.metric_available:
        return "Forklift calls, on-time percentage, and late calls unavailable."
    calls = sum(bucket.calls for bucket in item.forklift_buckets)
    late = sum(len(bucket.late_event_times) for bucket in item.forklift_buckets)
    latest_ontime = next(
        (
            bucket.rolling_ontime_pct
            for bucket in reversed(item.forklift_buckets)
            if bucket.rolling_ontime_pct is not None
        ),
        None,
    )
    ontime = "unavailable" if latest_ontime is None else f"{latest_ontime:.0f}%"
    return (
        f"{_count_label(calls, 'forklift call')}. "
        f"Latest rolling on-time {ontime}. {_count_label(late, 'late call')}."
    )


def _interval_detail(item: TimelineInterval) -> str:
    if item.is_open:
        header = f"{item.location_name}, {_time(item.start_utc)}. Working now."
    else:
        header = f"{item.location_name}, {_time(item.start_utc)} to {_time(item.end_utc)}."
    if item.role == "other" and not item.metric_available:
        metric_detail = (
            "Location unavailable because the attendance source reports "
            f"{item.location_status}. Goal and uptime unavailable."
        )
    elif item.role == "production":
        metric_detail = _production_detail(item)
    elif item.role == "forklift":
        metric_detail = _forklift_detail(item)
    else:
        metric_detail = "No metered goal applies."
    return f"{header} {metric_detail}"


def _interval_state(item: TimelineInterval) -> str:
    if not item.metric_available:
        return "unavailable"
    if item.production is not None:
        return item.production.result
    return "neutral"


def _interval_view(
    item: TimelineInterval,
    model: DashboardModel,
    location_class: str,
    production_hover: tuple[ProductionHoverPoint, ...],
) -> dict:
    left = _pct(item.start_utc, model.window_start_utc, model.window_end_utc)
    right = _pct(item.end_utc, model.window_start_utc, model.window_end_utc)
    line_points = []
    if item.production is not None:
        line_points = [
            {
                "x": _pct(point.at_utc, item.start_utc, item.end_utc),
                "y": None if point.value_pct is None else 100.0 - point.value_pct,
            }
            for point in item.production.rolling_uptime
        ]
    elif item.role == "forklift":
        line_points = [
            {
                "x": _pct(bucket.end_utc, item.start_utc, item.end_utc),
                "y": (
                    None if bucket.rolling_ontime_pct is None else 100.0 - bucket.rolling_ontime_pct
                ),
            }
            for bucket in item.forklift_buckets
        ]

    max_calls = max((bucket.calls for bucket in item.forklift_buckets), default=0)
    buckets = []
    for bucket in item.forklift_buckets:
        bucket_left = _pct(bucket.start_utc, item.start_utc, item.end_utc)
        bucket_right = _pct(bucket.end_utc, item.start_utc, item.end_utc)
        buckets.append(
            {
                "left_pct": bucket_left,
                "width_pct": max(0.0, bucket_right - bucket_left),
                "height_pct": 100.0 * bucket.calls / max_calls if max_calls else 0.0,
                "late_markers": tuple(
                    _pct(value, item.start_utc, item.end_utc) for value in bucket.late_event_times
                ),
            }
        )

    detail = _interval_detail(item)
    duration_seconds = (item.end_utc - item.start_utc).total_seconds()
    time_label = (
        f"{_time(item.start_utc)} · Working now"
        if item.is_open
        else f"{_time(item.start_utc)} to {_time(item.end_utc)}"
    )
    return {
        "key": item.key,
        "left_pct": left,
        "width_pct": max(0.01, right - left),
        "location_name": item.location_name,
        "location_class": location_class,
        "role": item.role,
        "state": _interval_state(item),
        "is_transfer": item.is_transfer,
        "is_open": item.is_open,
        "needs_touch_target": duration_seconds <= 30 * 60,
        "time_label": time_label,
        "line_runs": _line_runs(line_points),
        "buckets": tuple(buckets),
        "hover_points": _hover_points_view(production_hover),
        "hover_start_ms": _epoch_ms(item.start_utc) if item.role == "production" else None,
        "hover_end_ms": _epoch_ms(item.end_utc) if item.role == "production" else None,
        "detail": detail,
        "aria_label": f"Transferred to {detail}" if item.is_transfer else detail,
    }


def _row_view(
    row: PersonRow,
    model: DashboardModel,
    location_classes: dict[str, str],
) -> dict:
    production_hover = cumulative_production_hover_points(row.intervals)
    intervals = tuple(
        _interval_view(
            item,
            model,
            location_classes[item.location_name],
            production_hover.get(item.key, ()),
        )
        for item in row.intervals
    )
    return {
        "employee_odoo_id": row.employee_odoo_id,
        "person_name": row.person_name,
        "is_active": row.is_active,
        "status": row.status,
        "primary_role": row.primary_role,
        "attention_reasons": row.attention_reasons,
        "intervals": intervals,
        "short_intervals": tuple(item for item in intervals if item["needs_touch_target"]),
        "breaks": tuple(
            {
                "left_pct": _pct(item.start_utc, model.window_start_utc, model.window_end_utc),
                "width_pct": max(
                    0.0,
                    _pct(item.end_utc, model.window_start_utc, model.window_end_utc)
                    - _pct(
                        item.start_utc,
                        model.window_start_utc,
                        model.window_end_utc,
                    ),
                ),
                "label": item.label,
            }
            for item in row.breaks
        ),
        "summary": row.summary,
        "unattached_forklift_calls": row.unattached_forklift_calls,
    }


def dashboard_context(model: DashboardModel, *, attention_only: bool = False) -> dict:
    """Convert a validated dashboard model into deterministic template values."""
    names = {item.location_name for row in model.rows for item in row.intervals}
    location_classes = {
        name: _LOCATION_CLASSES[
            sum(ord(character) for character in name.casefold()) % len(_LOCATION_CLASSES)
        ]
        for name in names
    }
    rows = tuple(row for row in model.rows if not attention_only or row.attention_reasons)
    sections = tuple(
        {
            "key": key,
            "label": _SECTION_LABELS[key],
            "rows": tuple(
                _row_view(row, model, location_classes) for row in rows if row.section == key
            ),
        }
        for key in _SECTION_KEYS
    )

    schedule_markers = _schedule_markers(model)

    return {
        "day": model.day.isoformat(),
        "is_today": model.is_today,
        "as_of": _time(model.as_of_utc),
        "as_of_iso": model.as_of_utc.isoformat(),
        "sections": sections,
        "schedule_markers": schedule_markers,
        "schedule_time_groups": _schedule_time_groups(schedule_markers),
        "source_warnings": model.source_warnings,
        "working_now": sum(row.is_active for row in model.rows),
        "worked_earlier": sum(not row.is_active for row in model.rows),
        "needs_attention": sum(bool(row.attention_reasons) for row in model.rows),
        "attention_only": attention_only,
    }


__all__ = ["dashboard_context"]
