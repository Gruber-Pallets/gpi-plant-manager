"""Presentation-only geometry and labels for the People dashboard."""

from __future__ import annotations

from dataclasses import dataclass
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
from .people_performance_warnings import DashboardWarning, WarningKind, warning_key


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
_MIN_SCHEDULE_TRACK_WIDTH_REM = 34.0
_TIME_LABEL_CHARACTER_WIDTH_REM = 0.5
_TIME_LABEL_GAP_REM = 0.75


def _break_time(value: datetime) -> str:
    return value.astimezone(shift_config.SITE_TZ).strftime("%-I:%M")


def _schedule_markers(model: DashboardModel) -> tuple[dict, ...]:
    breaks_by_time: dict[datetime, list[str]] = {}
    for item in model.breaks:
        if model.window_start_utc <= item.start_utc <= model.window_end_utc:
            breaks_by_time.setdefault(item.start_utc, []).append(f"{item.label} starts")

    times = {model.window_start_utc, model.window_end_utc, *breaks_by_time}
    markers = []
    for value in sorted(times):
        descriptions = []
        if value == model.window_start_utc:
            kind = "start"
            descriptions.append("Shift starts")
        elif value == model.window_end_utc:
            kind = "end"
            descriptions.append("Shift ends")
        else:
            kind = "break"
        descriptions.extend(breaks_by_time.get(value, ()))
        if value == model.window_end_utc and value == model.window_start_utc:
            descriptions.append("Shift ends")
        full_time = _time(value)
        markers.append(
            {
                "left_pct": _pct(value, model.window_start_utc, model.window_end_utc),
                "kind": kind,
                "visible_label": full_time if kind != "break" else _break_time(value),
                "aria_label": "; ".join(
                    f"{description} at {full_time}" for description in descriptions
                ),
            }
        )
    return tuple(markers)


def _schedule_time_groups(markers: tuple[dict, ...]) -> tuple[dict, ...]:
    groups: list[list[dict]] = []
    for marker in markers:
        if groups and marker["left_pct"] - groups[-1][0]["left_pct"] < _MIN_TIME_LABEL_GAP_PCT:
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


def _schedule_track_width_rem(groups: tuple[dict, ...]) -> float:
    def label_width(group: dict) -> float:
        return max(2.5, len(group["label"]) * _TIME_LABEL_CHARACTER_WIDTH_REM)

    def left_offset(group: dict) -> float:
        width = label_width(group)
        if group["edge"] == "start":
            return 0.0
        if group["edge"] == "end":
            return width
        return width / 2.0

    def right_offset(group: dict) -> float:
        width = label_width(group)
        if group["edge"] == "start":
            return width
        if group["edge"] == "end":
            return 0.0
        return width / 2.0

    required = _MIN_SCHEDULE_TRACK_WIDTH_REM
    for group in groups:
        position = group["left_pct"] / 100.0
        if position > 0:
            required = max(required, left_offset(group) / position)
        if position < 1:
            required = max(required, right_offset(group) / (1.0 - position))
    for previous, current in zip(groups, groups[1:], strict=False):
        gap = (current["left_pct"] - previous["left_pct"]) / 100.0
        if gap > 0:
            required = max(
                required,
                (right_offset(previous) + left_offset(current) + _TIME_LABEL_GAP_REM) / gap,
            )
    return round(required, 2)


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


def _filter_summary(
    *,
    status_filter: str | None,
    attention_only: bool,
    visible: int,
    total: int,
    working: int,
    earlier: int,
) -> str:
    if status_filter is None and not attention_only:
        return ""
    if status_filter == "working":
        denominator, label = working, "working now"
    elif status_filter == "earlier":
        denominator, label = earlier, "worked earlier"
    else:
        denominator, label = total, "people"
    attention = " who need attention" if attention_only else ""
    return f"Showing {visible} of {denominator} {label}{attention}."


@dataclass(frozen=True)
class DashboardWarningGroup:
    key: str
    kind: WarningKind
    label: str
    summary: str
    members: tuple[DashboardWarning, ...]
    count: int | None = None


def warning_groups(
    warnings: tuple[DashboardWarning, ...],
) -> tuple[DashboardWarningGroup, ...]:
    production = tuple(
        sorted(
            (
                item
                for item in warnings
                if item.kind == "production_metric_unavailable"
            ),
            key=lambda item: item.subject.casefold(),
        )
    )
    production_group = (
        DashboardWarningGroup(
            key=warning_key(
                "production_metric_unavailable", "production-meters"
            ),
            kind="production_metric_unavailable",
            label="Production Meters Unavailable",
            summary=(
                f"{len(production)} production meter"
                f"{' is' if len(production) == 1 else 's are'} unavailable."
            ),
            members=production,
            count=len(production),
        )
        if production
        else None
    )
    groups: list[DashboardWarningGroup] = []
    inserted_production = False
    for warning in warnings:
        if warning.kind == "production_metric_unavailable":
            if not inserted_production and production_group is not None:
                groups.append(production_group)
                inserted_production = True
            continue
        groups.append(
            DashboardWarningGroup(
                key=warning.key,
                kind=warning.kind,
                label=warning.label,
                summary=warning.summary,
                members=(warning,),
            )
        )
    return tuple(groups)


def warning_summary_view(warning: DashboardWarning) -> dict:
    return {
        "key": warning.key,
        "kind": warning.kind,
        "label": warning.label,
        "summary": warning.summary,
    }


def warning_group_summary_view(group: DashboardWarningGroup) -> dict:
    return {
        "key": group.key,
        "kind": group.kind,
        "label": group.label,
        "summary": group.summary,
        "count": group.count,
        "accessible_label": (
            f"{group.label}: {group.count}"
            if group.count is not None
            else group.label
        ),
    }


def warning_detail_context(warning: DashboardWarning | None) -> dict:
    if warning is None:
        return {
            "state": "cleared",
            "title": "Issue cleared",
            "summary": "Plant Manager checked again and this warning is no longer active.",
            "impact": "The People page now shows the latest available information.",
            "facts": (),
            "checked_at": "",
            "last_success_at": "",
            "actions": (),
        }
    return {
        "state": "open",
        "key": warning.key,
        "kind": warning.kind,
        "title": warning.title,
        "summary": warning.summary,
        "impact": warning.impact,
        "subject": warning.subject,
        "facts": warning.facts,
        "checked_at": _time(warning.checked_at_utc),
        "last_success_at": (
            _time(warning.last_success_at_utc)
            if warning.last_success_at_utc is not None
            else ""
        ),
        "actions": tuple(
            {
                "action_id": action.action_id,
                "label": action.label,
                "href": action.href,
            }
            for action in warning.actions
        ),
    }


def dashboard_context(
    model: DashboardModel,
    *,
    status_filter: str | None = None,
    attention_only: bool = False,
) -> dict:
    """Convert a validated dashboard model into deterministic template values."""
    if status_filter not in (None, "working", "earlier"):
        raise ValueError("unknown People status filter")
    names = {item.location_name for row in model.rows for item in row.intervals}
    location_classes = {
        name: _LOCATION_CLASSES[
            sum(ord(character) for character in name.casefold()) % len(_LOCATION_CLASSES)
        ]
        for name in names
    }
    status_rows = tuple(
        row
        for row in model.rows
        if status_filter is None
        or (status_filter == "working" and row.is_active)
        or (status_filter == "earlier" and not row.is_active)
    )
    rows = tuple(
        row for row in status_rows if not attention_only or row.attention_reasons
    )
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
    schedule_time_groups = _schedule_time_groups(schedule_markers)
    total_people = len(model.rows)
    visible_people = len(rows)
    working_now = sum(row.is_active for row in model.rows)
    worked_earlier = sum(not row.is_active for row in model.rows)

    return {
        "day": model.day.isoformat(),
        "is_today": model.is_today,
        "as_of": _time(model.as_of_utc),
        "as_of_iso": model.as_of_utc.isoformat(),
        "sections": sections,
        "schedule_markers": schedule_markers,
        "schedule_time_groups": schedule_time_groups,
        "schedule_track_width_rem": _schedule_track_width_rem(schedule_time_groups),
        "source_warnings": tuple(
            warning_group_summary_view(item)
            for item in warning_groups(model.source_warnings)
        ),
        "working_now": working_now,
        "worked_earlier": worked_earlier,
        "needs_attention": sum(bool(row.attention_reasons) for row in model.rows),
        "status_filter": status_filter,
        "attention_only": attention_only,
        "total_people": total_people,
        "visible_people": visible_people,
        "filtered_empty": bool(
            (status_filter is not None or attention_only) and not visible_people
        ),
        "filter_summary": _filter_summary(
            status_filter=status_filter,
            attention_only=attention_only,
            visible=visible_people,
            total=total_people,
            working=working_now,
            earlier=worked_earlier,
        ),
    }


__all__ = [
    "DashboardWarningGroup",
    "dashboard_context",
    "warning_detail_context",
    "warning_group_summary_view",
    "warning_groups",
    "warning_summary_view",
]
