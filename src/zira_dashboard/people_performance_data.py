"""Load one coherent people-performance day without cross-source guessing."""

from __future__ import annotations

from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, time, timedelta
import logging
import math

from . import (
    attendance_timeline,
    forklift_event_store,
    forklift_score,
    forklift_settings,
    forklift_store,
    production_history,
    settings_store,
    shift_config,
    wc_attributions,
)
from .forklift_event_store import ForkliftCompletionCoverage
from .forklift_ingest import ForkliftCompletionEvent
from .people_performance import (
    BreakSpan,
    DashboardModel,
    ForkliftDayMetric,
    assemble_dashboard,
)


_log = logging.getLogger(__name__)
_LOAD_POOL = ThreadPoolExecutor(max_workers=3, thread_name_prefix="people-performance")
# The existing forklift warmer runs every ten minutes.  Leave one minute for
# scheduler/fetch jitter so a healthy cadence stays available between ticks.
_LIVE_SOURCE_GRACE = timedelta(minutes=11)


@dataclass(frozen=True)
class _ProductionSource:
    catalog: tuple
    totals: tuple
    attribution_rows: tuple[dict, ...]
    error: Exception | None = None


@dataclass(frozen=True)
class _ForkliftSource:
    calendar_start_utc: datetime
    calendar_required_through_utc: datetime
    events: tuple[ForkliftCompletionEvent, ...]
    driver_rows: tuple[dict, ...]
    calls_row: dict | None
    coverage: ForkliftCompletionCoverage | None


def _aware_utc(value: datetime, field_name: str) -> datetime:
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise TypeError(f"{field_name} must be timezone-aware")
    return value.astimezone(UTC)


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _bounds(day: date, now_utc: datetime) -> tuple[datetime, datetime, datetime, bool]:
    if type(day) is not date:
        raise TypeError("day must be a date")
    now = _aware_utc(now_utc, "now_utc")
    start = datetime.combine(
        day, shift_config.shift_start_for(day), tzinfo=shift_config.SITE_TZ
    ).astimezone(UTC)
    end = datetime.combine(
        day, shift_config.shift_end_for(day), tzinfo=shift_config.SITE_TZ
    ).astimezone(UTC)
    if end <= start:
        raise ValueError("shift end must be after shift start")
    is_today = day == now.astimezone(shift_config.SITE_TZ).date()
    cap = min(now, end) if is_today else end
    return start, end, max(start, cap), is_today


def _breaks(day: date) -> tuple[BreakSpan, ...]:
    return tuple(
        BreakSpan(
            datetime.combine(day, item.start, tzinfo=shift_config.SITE_TZ).astimezone(UTC),
            datetime.combine(day, item.end, tzinfo=shift_config.SITE_TZ).astimezone(UTC),
            getattr(item, "name", None) or "Planned break",
        )
        for item in shift_config.breaks_for(day)
    )


def _calendar_bounds(day: date) -> tuple[datetime, datetime]:
    start = datetime.combine(day, time.min, tzinfo=shift_config.SITE_TZ).astimezone(UTC)
    end = datetime.combine(
        day + timedelta(days=1), time.min, tzinfo=shift_config.SITE_TZ
    ).astimezone(UTC)
    return start, end


def _load_production_source(client, day: date, cap: datetime) -> _ProductionSource:
    catalog = tuple(production_history.metered_station_catalog())
    try:
        totals = tuple(production_history.metered_station_totals(client, day, cap))
        attribution_rows = tuple(wc_attributions.for_day(day))
    except Exception as exc:  # noqa: BLE001 - source family degrades independently
        return _ProductionSource(catalog, (), (), exc)
    return _ProductionSource(catalog, totals, attribution_rows)


def _load_forklift_source(
    day: date,
    *,
    now_utc: datetime,
    is_today: bool,
) -> _ForkliftSource:
    calendar_start, calendar_end = _calendar_bounds(day)
    required_through = min(calendar_end, now_utc) if is_today else calendar_end
    coverage = forklift_event_store.completion_coverage_for_day(day)
    read_through = required_through
    if coverage is not None:
        read_through = max(read_through, coverage.covered_through_utc)
    read_through = min(calendar_end, read_through)
    events = forklift_event_store.completion_events_for_range(calendar_start, read_through)
    return _ForkliftSource(
        calendar_start_utc=calendar_start,
        calendar_required_through_utc=required_through,
        events=tuple(events),
        driver_rows=tuple(forklift_store.driver_rows_for_day(day)),
        calls_row=forklift_store.calls_row_for_day(day),
        coverage=coverage,
    )


def _production_values(
    *,
    source: _ProductionSource,
    client,
    day: date,
    spans,
    start: datetime,
    end: datetime,
    cap: datetime,
    is_today: bool,
) -> tuple[
    tuple,
    dict[str, tuple[tuple[datetime, datetime], ...]],
    dict,
    set[str],
    tuple[str, ...],
    bool,
]:
    metered_names = {station.name for station in source.catalog}
    if source.error is not None:
        return (), {}, {}, metered_names, ("Production data unavailable",), False
    totals_by_name = {}
    duplicate_total_names: set[str] = set()
    for total in source.totals:
        name = getattr(getattr(total, "station", None), "name", None)
        if not isinstance(name, str):
            continue
        if name in totals_by_name:
            duplicate_total_names.add(name)
        else:
            totals_by_name[name] = total
    scores = []
    downtime_by_wc: dict[str, tuple[tuple[datetime, datetime], ...]] = {}
    warnings: list[str] = []
    for station in source.catalog:
        total = totals_by_name.get(station.name)
        available = (
            station.name not in duplicate_total_names
            and total is not None
            and not bool(getattr(total, "truncated", True))
        )
        if available:
            try:
                target = float(settings_store.station_target(station))
                available = math.isfinite(target) and target > 0
            except Exception:  # noqa: BLE001 - one bad goal only affects its WC
                available = False
        if available and (
            total.station.meter_id != station.meter_id or total.station.name != station.name
        ):
            available = False
        if not available:
            warnings.append(f"Production metric unavailable: {station.name}")
            continue
        station_spans = tuple(span for span in spans if span.app_work_center_name == station.name)
        try:
            station_scores = production_history.production_scores_for_timeline(
                client,
                day,
                station_spans,
                now_utc=cap,
                is_today=is_today,
                window_start_utc=start,
                window_end_utc=end,
                station_totals=(total,),
                attribution_rows=source.attribution_rows,
            )
        except Exception:  # noqa: BLE001 - malformed meter facts stay per-WC
            _log.warning("people production metric unavailable", extra={"wc": station.name})
            warnings.append(f"Production metric unavailable: {station.name}")
            continue
        scores.extend(station_scores)
        downtime_by_wc[station.name] = tuple(total.downtime_intervals)
    breakdowns = wc_attributions.breakdown_windows_for_day(day, rows=list(source.attribution_rows))
    return (
        tuple(scores),
        downtime_by_wc,
        breakdowns,
        metered_names,
        tuple(warnings),
        True,
    )


def _coverage_is_complete(source: _ForkliftSource, *, is_today: bool) -> bool:
    coverage = source.coverage
    calls_row = source.calls_row
    if coverage is None or calls_row is None:
        return False
    required = source.calendar_required_through_utc
    if is_today:
        required -= _LIVE_SOURCE_GRACE
    if coverage.covered_through_utc < required:
        return False
    if coverage.raw_event_count != len(source.events):
        return False
    try:
        return int(calls_row["total_calls"]) == coverage.raw_event_count
    except (KeyError, TypeError, ValueError):
        return False


def _driver_identity_evidence(source: _ForkliftSource) -> dict[str, set[str]]:
    evidence: dict[str, set[str]] = {}
    for event in source.events:
        evidence.setdefault(event.driver_id, set()).add(event.driver_name)
    for row in source.driver_rows:
        driver_id = str(row.get("driver_id") or "").strip()
        name = str(row.get("name") or "").strip()
        if driver_id and name:
            evidence.setdefault(driver_id, set()).add(name)
    return evidence


def _forklift_values(
    *,
    source: _ForkliftSource,
    spans,
    start: datetime,
    cap: datetime,
    is_today: bool,
) -> tuple[
    dict[int, tuple[ForkliftCompletionEvent, ...]],
    dict[int, ForkliftDayMetric],
    tuple[str, ...],
    bool,
]:
    if not _coverage_is_complete(source, is_today=is_today):
        return {}, {}, ("Forklift data unavailable",), False
    known_employee_ids = {span.employee_odoo_id for span in spans}
    evidence = _driver_identity_evidence(source)
    resolved = forklift_store.resolve_forklift_driver_ids(
        evidence, allowed_employee_ids=known_employee_ids
    )
    unsafe_driver_ids = {driver_id for driver_id, names in evidence.items() if len(names) != 1}
    claimed = Counter(resolved.values())
    identity_conflict = bool(unsafe_driver_ids) or any(count > 1 for count in claimed.values())
    resolved = {
        driver_id: employee_id
        for driver_id, employee_id in resolved.items()
        if driver_id not in unsafe_driver_ids
        and claimed[employee_id] == 1
        and employee_id in known_employee_ids
    }
    rows_by_driver: dict[str, dict] = {}
    duplicate_rows: set[str] = set()
    for row in source.driver_rows:
        driver_id = str(row.get("driver_id") or "").strip()
        if not driver_id:
            continue
        if driver_id in rows_by_driver:
            duplicate_rows.add(driver_id)
        rows_by_driver[driver_id] = row
    config = forklift_settings.resolve(
        forklift_settings.current(), algo_throughput=0.0
    ).score_config()
    events_by_driver: dict[str, list[ForkliftCompletionEvent]] = {}
    for item in source.events:
        events_by_driver.setdefault(item.driver_id, []).append(item)
    display_events_by_employee: dict[int, tuple[ForkliftCompletionEvent, ...]] = {}
    metrics_by_employee: dict[int, ForkliftDayMetric] = {}
    if not source.events:
        try:
            driver_rows_are_zero = all(int(row["calls"]) == 0 for row in source.driver_rows)
        except (KeyError, TypeError, ValueError):
            driver_rows_are_zero = False
        if driver_rows_are_zero:
            # A reconciled plant-wide zero proves every clocked-in tablet
            # driver's count without requiring a name-based identity guess.
            for span in spans:
                employee_id = span.employee_odoo_id
                if (
                    isinstance(employee_id, int)
                    and not isinstance(employee_id, bool)
                    and span.status in {"valid", "stale_open_location"}
                    and span.app_work_center_name == "Tablets"
                ):
                    display_events_by_employee[employee_id] = ()
                    metrics_by_employee[employee_id] = ForkliftDayMetric(
                        calls=0,
                        on_time=0,
                        late=0,
                        handling_minutes=0.0,
                        score=None,
                        ontime_floor_pct=float(config.ontime_floor),
                        timeline_available=True,
                    )
    timeline_incomplete = False
    for driver_id, employee_id in resolved.items():
        calendar_events = tuple(events_by_driver.get(driver_id, ()))
        row = rows_by_driver.get(driver_id)
        try:
            row_calls = int(row["calls"]) if row is not None else -1
        except (KeyError, TypeError, ValueError):
            row_calls = -1
        complete = (
            driver_id not in duplicate_rows
            and row is not None
            and row_calls == len(calendar_events)
        )
        shift_events = tuple(
            event for event in calendar_events if start <= event.created_at_utc < cap
        )
        on_time = sum(event.on_time is True and event.late is not True for event in shift_events)
        late = sum(event.late is True for event in shift_events)
        handling_minutes = sum(max(0, event.handling_ms or 0) for event in shift_events) / 60000.0
        responses = tuple(
            max(0, event.response_ms) for event in shift_events if event.response_ms is not None
        )
        elapsed_ms = max(0.0, (cap - start).total_seconds() * 1000.0)
        score_breakdown = forklift_score.daily_score(
            {
                "calls": len(shift_events),
                "on_time": on_time,
                "late": late,
                "avg_ms": round(sum(responses) / len(responses)) if responses else 0,
                "utilization_pct": (
                    100.0 * handling_minutes * 60000.0 / elapsed_ms if elapsed_ms else 0.0
                ),
            },
            config,
        )
        metrics_by_employee[employee_id] = ForkliftDayMetric(
            calls=len(shift_events),
            on_time=on_time,
            late=late,
            handling_minutes=handling_minutes,
            score=(score_breakdown.score if complete and score_breakdown else None),
            ontime_floor_pct=float(config.ontime_floor),
            timeline_available=complete,
        )
        display_events_by_employee[employee_id] = shift_events if complete else ()
        timeline_incomplete = timeline_incomplete or not complete
    unmatched = sum(
        start <= event.created_at_utc < cap and event.driver_id not in resolved
        for event in source.events
    )
    warnings = []
    if identity_conflict:
        warnings.append("Forklift driver identity conflict")
    if unmatched:
        warnings.append(f"Unmatched forklift calls: {unmatched}")
    if timeline_incomplete or duplicate_rows:
        warnings.append("Forklift timeline incomplete")
    return (
        display_events_by_employee,
        metrics_by_employee,
        tuple(warnings),
        True,
    )


def load_dashboard(
    day: date,
    client,
    *,
    now_utc: datetime | None = None,
) -> DashboardModel:
    """Load one day with independent attendance, production, and forklift health."""
    now = _aware_utc(_utc_now() if now_utc is None else now_utc, "now_utc")
    start, end, cap, is_today = _bounds(day, now)
    breaks = _breaks(day)
    attendance_future = _LOAD_POOL.submit(
        attendance_timeline.snapshot_for_range,
        start,
        end,
        as_of_utc=cap,
    )
    production_future = _LOAD_POOL.submit(_load_production_source, client, day, cap)
    forklift_future = _LOAD_POOL.submit(
        _load_forklift_source,
        day,
        now_utc=now,
        is_today=is_today,
    )
    try:
        attendance = attendance_future.result()
    except Exception:  # noqa: BLE001 - attendance owns page membership
        _log.warning("people attendance source unavailable")
        return assemble_dashboard(
            day=day,
            as_of_utc=cap,
            window_start_utc=start,
            window_end_utc=end,
            spans=(),
            production_scores=(),
            downtime_by_wc={},
            breakdown_exclusions_by_person_wc={},
            forklift_events_by_employee_id={},
            forklift_day_metrics_by_employee_id={},
            breaks=breaks,
            metered_wc_names=set(),
            source_warnings=("Attendance data unavailable",),
            is_today=is_today,
            production_available=False,
            forklift_available=False,
        )
    spans = (
        attendance.spans
        if is_today
        else tuple(replace(span, is_open=False) for span in attendance.spans)
    )
    source_warnings: list[str] = []
    if is_today and attendance.freshness_blockers:
        source_warnings.append("Attendance source stale")

    try:
        production_source = production_future.result()
        (
            production_scores,
            downtime_by_wc,
            breakdowns,
            metered_wc_names,
            production_warnings,
            production_available,
        ) = _production_values(
            source=production_source,
            client=client,
            day=day,
            spans=spans,
            start=start,
            end=end,
            cap=cap,
            is_today=is_today,
        )
        source_warnings.extend(production_warnings)
    except Exception:  # noqa: BLE001 - production cannot hide attendance
        _log.warning("people production source unavailable")
        production_scores = ()
        downtime_by_wc = {}
        breakdowns = {}
        metered_wc_names = set()
        production_available = False
        source_warnings.append("Production data unavailable")

    try:
        forklift_source = forklift_future.result()
        (
            forklift_events,
            forklift_metrics,
            forklift_warnings,
            forklift_available,
        ) = _forklift_values(
            source=forklift_source,
            spans=spans,
            start=start,
            cap=cap,
            is_today=is_today,
        )
        source_warnings.extend(forklift_warnings)
    except Exception:  # noqa: BLE001 - forklift cannot hide other facts
        _log.warning("people forklift source unavailable")
        forklift_events = {}
        forklift_metrics = {}
        forklift_available = False
        source_warnings.append("Forklift data unavailable")

    return assemble_dashboard(
        day=day,
        as_of_utc=cap,
        window_start_utc=start,
        window_end_utc=end,
        spans=spans,
        production_scores=production_scores,
        downtime_by_wc=downtime_by_wc,
        breakdown_exclusions_by_person_wc=breakdowns,
        forklift_events_by_employee_id=forklift_events,
        forklift_day_metrics_by_employee_id=forklift_metrics,
        breaks=breaks,
        metered_wc_names=metered_wc_names,
        source_warnings=source_warnings,
        is_today=is_today,
        production_available=production_available,
        forklift_available=forklift_available,
    )


__all__ = ["load_dashboard"]
