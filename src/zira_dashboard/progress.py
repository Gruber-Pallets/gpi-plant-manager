"""Bucket per-station samples into fixed-width time windows for progress charts."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from typing import Callable, Iterable

from . import shift_config
from .leaderboard import StationTotal
from .settings_store import station_target
from .shift_config import SITE_TZ, breaks_for, shift_end_for, shift_start_for, work_weekdays

# Type alias: target_fn(b_start_local, b_end_local) -> expected units in that bucket.
TargetFn = Callable[[datetime, datetime], float]


def _in_any_break(breaks_iter, t: time) -> bool:
    return any(b.start <= t < b.end for b in breaks_iter)


def _default_target(
    group: list[StationTotal],
    b_start_local: datetime,
    b_end_local: datetime,
) -> float:
    """Default target: per-station hourly × overlap with active intervals."""
    total = 0.0
    for st in group:
        per_hr = station_target(st.station)
        if per_hr <= 0:
            continue
        for ai_start_utc, ai_end_utc in st.active_intervals:
            ai_start = ai_start_utc.astimezone(SITE_TZ)
            ai_end = ai_end_utc.astimezone(SITE_TZ)
            overlap_start = max(ai_start, b_start_local)
            overlap_end = min(ai_end, b_end_local)
            if overlap_end > overlap_start:
                overlap_min = (overlap_end - overlap_start).total_seconds() / 60.0
                total += per_hr * overlap_min / 60.0
    return total


def progress_buckets(
    group: Iterable[StationTotal],
    day: date,
    now_utc: datetime,
    bucket_minutes: int = 15,
    target_fn: TargetFn | None = None,
    align_to_standard: bool = False,
) -> list[dict]:
    """Return one dict per 15-min bucket from shift start to min(now, shift end).

    Breaks are skipped. If ``target_fn`` is provided, the caller computes each
    bucket's target — useful when the route knows about staffing and wants to
    apply rules (first-60-min staffing-based, transfer-rule afterwards) that
    this module on its own can't know about. Otherwise, falls back to the
    default per-station active-interval calculation.

    When ``align_to_standard`` is True, anchor bucket boundaries to the
    GLOBAL shift hours (shift_start/shift_end/breaks) rather than the
    per-day custom-hours-aware variants. Used by the recycling route in
    multi-day range mode so all days share a common 15-min grid.
    """
    group = list(group)
    if not group or day.weekday() not in work_weekdays():
        return []

    # All samples, converted to site-local time.
    samples: list[tuple[datetime, int]] = []
    for st in group:
        for ts_utc, units in st.samples:
            samples.append((ts_utc.astimezone(SITE_TZ), units))

    if align_to_standard:
        s_start = shift_config.shift_start()
        s_end = shift_config.shift_end()
        day_breaks = shift_config.breaks()
    else:
        s_start = shift_start_for(day)
        s_end = shift_end_for(day)
        day_breaks = breaks_for(day)

    start = datetime.combine(day, s_start, tzinfo=SITE_TZ)
    end = datetime.combine(day, s_end, tzinfo=SITE_TZ)
    edge = min(now_utc.astimezone(SITE_TZ), end)
    if edge <= start:
        return []

    buckets: list[dict] = []
    cursor = start
    delta = timedelta(minutes=bucket_minutes)
    while cursor < edge:
        b_start = cursor
        b_end = cursor + delta
        cursor = b_end
        if _in_any_break(day_breaks, b_start.time()):
            continue  # wholly inside a break period
        actual = sum(u for ts, u in samples if b_start <= ts < b_end)
        # For the current (in-progress) bucket, don't penalize — show only actual.
        in_progress = b_end > edge
        if in_progress:
            tgt = actual
        elif target_fn is not None:
            tgt = target_fn(b_start, b_end)
        else:
            tgt = _default_target(group, b_start, b_end)
        buckets.append(
            {
                "label": b_start.strftime("%H:%M"),
                "actual": actual,
                "target": int(round(tgt)),
                "in_progress": in_progress,
            }
        )
    return buckets
