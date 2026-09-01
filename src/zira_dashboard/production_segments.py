"""Pure unit credit and scoring for resolved worker production segments."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import math
from numbers import Real
from typing import Literal, TypeAlias

from .assignment_windows import WorkSegment


SegmentResult = Literal["ahead", "behind", "neutral"]
PersonAttributionKey: TypeAlias = str | tuple[int, str]


@dataclass(frozen=True)
class SegmentCredit:
    segment_id: int
    wc_name: str
    person_name: str | None
    start_utc: datetime | None
    end_utc: datetime | None
    source: str
    productive_minutes: float
    actual_units: float
    is_active: bool
    person_odoo_id: int | None = None


@dataclass(frozen=True)
class UnassignedRun:
    wc_name: str
    start_utc: datetime
    end_utc: datetime
    units: float
    sample_count: int


def attribution_key(credit: SegmentCredit) -> PersonAttributionKey:
    """Use durable Odoo identity for strict credit and names for legacy credit."""
    if credit.person_name is None:
        raise ValueError("unassigned credit has no person attribution key")
    if credit.person_odoo_id is None:
        return credit.person_name
    return (credit.person_odoo_id, credit.person_name)


def _aware_utc(value: object, field_name: str) -> datetime:
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise TypeError(f"{field_name} must be an aware UTC datetime")
    if value.utcoffset() != timedelta(0):
        raise ValueError(f"{field_name} must be an aware UTC datetime")
    return value.astimezone(UTC)


def unassigned_runs_for_samples(
    samples: Sequence[tuple[datetime, float]],
    assigned_sample_times: set[datetime],
    active_intervals: Sequence[tuple[datetime, datetime]],
    *,
    wc_name: str = "",
) -> tuple[UnassignedRun, ...]:
    """Group adjacent uncovered positive samples inside one production run.

    A run's boundaries are the first and last sample timestamps. A one-sample
    run therefore has equal start and end timestamps. Adjacency is evaluated
    against the original sample order, so an assigned sample always splits two
    uncovered groups even when all three timestamps share an active interval.
    """
    normalized_intervals: list[tuple[datetime, datetime]] = []
    for raw_start, raw_end in active_intervals:
        start = _aware_utc(raw_start, "active interval start")
        end = _aware_utc(raw_end, "active interval end")
        if end <= start:
            raise ValueError("active intervals must have positive duration")
        normalized_intervals.append((start, end))
    normalized_intervals.sort()
    for previous, current in zip(normalized_intervals, normalized_intervals[1:], strict=False):
        if current[0] < previous[1]:
            raise ValueError("active intervals cannot overlap")

    assigned = {
        _aware_utc(timestamp, "assigned sample time") for timestamp in assigned_sample_times
    }
    normalized_samples: list[tuple[datetime, float]] = []
    for raw_timestamp, raw_units in samples:
        timestamp = _aware_utc(raw_timestamp, "sample timestamp")
        if isinstance(raw_units, bool) or not isinstance(raw_units, Real):
            raise TypeError("sample units must be positive numbers")
        units = float(raw_units)
        if not math.isfinite(units) or units <= 0:
            raise ValueError("sample units must be positive")
        normalized_samples.append((timestamp, units))
    if any(
        right[0] < left[0]
        for left, right in zip(normalized_samples, normalized_samples[1:], strict=False)
    ):
        raise ValueError("samples must be ordered by timestamp")

    runs: list[UnassignedRun] = []
    current_start: datetime | None = None
    current_end: datetime | None = None
    current_units = 0.0
    current_count = 0
    current_interval: int | None = None

    def finish() -> None:
        nonlocal current_start, current_end, current_units, current_count
        nonlocal current_interval
        if current_start is not None and current_end is not None:
            runs.append(
                UnassignedRun(
                    wc_name=wc_name,
                    start_utc=current_start,
                    end_utc=current_end,
                    units=current_units,
                    sample_count=current_count,
                )
            )
        current_start = None
        current_end = None
        current_units = 0.0
        current_count = 0
        current_interval = None

    for timestamp, units in normalized_samples:
        interval_index = next(
            (
                index
                for index, (start, end) in enumerate(normalized_intervals)
                if start <= timestamp < end
            ),
            None,
        )
        if timestamp in assigned or interval_index is None:
            finish()
            continue
        if current_start is not None and interval_index != current_interval:
            finish()
        if current_start is None:
            current_start = timestamp
            current_interval = interval_index
        current_end = timestamp
        current_units += units
        current_count += 1
    finish()
    return tuple(runs)


@dataclass(frozen=True)
class SegmentScore:
    segment_id: int
    wc_name: str
    person_name: str | None
    start_utc: datetime | None
    end_utc: datetime | None
    source: str
    productive_minutes: float
    actual_units: float
    goal_units: float
    runway_units: float
    is_active: bool
    result: SegmentResult
    person_odoo_id: int | None = None


def _segment_result(
    person_name: str | None, actual_units: float, goal_units: float
) -> SegmentResult:
    if person_name is None or goal_units <= 0:
        return "neutral"
    return "ahead" if actual_units >= goal_units else "behind"


def _score_order(row: SegmentScore) -> tuple[bool, float, int]:
    return (
        row.start_utc is None,
        row.start_utc.timestamp() if row.start_utc is not None else float("inf"),
        row.segment_id,
    )


def _gap_is_ignored(
    start_utc: datetime,
    end_utc: datetime,
    ignored_gaps: Sequence[tuple[datetime, datetime]],
) -> bool:
    if end_utc <= start_utc:
        return True
    return any(gap_start <= start_utc and end_utc <= gap_end for gap_start, gap_end in ignored_gaps)


def _can_join_display_scores(
    left: SegmentScore,
    right: SegmentScore,
    ignored_gaps: Sequence[tuple[datetime, datetime]],
) -> bool:
    return bool(
        left.person_name is not None
        and left.person_name == right.person_name
        and left.person_odoo_id == right.person_odoo_id
        and left.wc_name == right.wc_name
        and left.end_utc is not None
        and right.start_utc is not None
        and _gap_is_ignored(left.end_utc, right.start_utc, ignored_gaps)
    )


def _join_display_scores(left: SegmentScore, right: SegmentScore) -> SegmentScore:
    actual = left.actual_units + right.actual_units
    goal = left.goal_units + right.goal_units
    return SegmentScore(
        segment_id=min(left.segment_id, right.segment_id),
        wc_name=left.wc_name,
        person_name=left.person_name,
        start_utc=min(value for value in (left.start_utc, right.start_utc) if value is not None),
        end_utc=max(value for value in (left.end_utc, right.end_utc) if value is not None),
        source=left.source if left.source == right.source else "mixed",
        productive_minutes=left.productive_minutes + right.productive_minutes,
        actual_units=actual,
        goal_units=goal,
        runway_units=max(actual, goal),
        is_active=left.is_active or right.is_active,
        result=_segment_result(left.person_name, actual, goal),
        person_odoo_id=left.person_odoo_id,
    )


def coalesce_display_scores(
    scores: Sequence[SegmentScore],
    *,
    ignored_gaps: Sequence[tuple[datetime, datetime]] = (),
) -> tuple[SegmentScore, ...]:
    """Join administrative break splits without changing sample credit."""
    named = sorted(
        (score for score in scores if score.person_name is not None),
        key=_score_order,
    )
    unassigned = [score for score in scores if score.person_name is None]
    merged: list[SegmentScore] = []
    for score in named:
        if merged and _can_join_display_scores(merged[-1], score, ignored_gaps):
            merged[-1] = _join_display_scores(merged[-1], score)
        else:
            merged.append(score)
    return tuple(sorted([*merged, *unassigned], key=_score_order))


def distinct_named_producers(
    scores: Sequence[SegmentScore],
) -> tuple[str, ...]:
    """Return named producers once each, in scored-segment time order."""
    names: list[str] = []
    seen: set[str] = set()
    for score in sorted(scores, key=_score_order):
        person_name = score.person_name
        if not person_name or person_name in seen:
            continue
        seen.add(person_name)
        names.append(person_name)
    return tuple(names)


def worker_coverage_is_split(
    scores: Sequence[SegmentScore],
    *,
    window_start_utc: datetime,
    window_end_utc: datetime,
    ignored_gaps: Sequence[tuple[datetime, datetime]] = (),
) -> bool:
    """Whether named-worker coverage needs independent runway presentation."""
    named = [score for score in scores if score.person_name is not None]
    if not named:
        return False
    if len(named) != 1:
        return True
    score = named[0]
    if score.start_utc is None or score.end_utc is None:
        return True
    starts_in_time = score.start_utc <= window_start_utc or _gap_is_ignored(
        window_start_utc, score.start_utc, ignored_gaps
    )
    ends_in_time = score.end_utc >= window_end_utc or _gap_is_ignored(
        score.end_utc, window_end_utc, ignored_gaps
    )
    return not (starts_in_time and ends_in_time)


def _ordered_segments(segments: Sequence[WorkSegment]) -> list[WorkSegment]:
    return sorted(
        segments,
        key=lambda segment: (
            segment.start_utc.timestamp(),
            segment.end_utc.timestamp(),
            segment.wc_name,
            segment.person_name,
        ),
    )


def credit_work_segments(
    segments: Sequence[WorkSegment],
    *,
    wc_totals: Mapping[str, float],
    samples_by_wc: Mapping[str, Sequence[tuple[datetime, float]]],
    productive_minutes: Callable[[str, str, datetime, datetime], float],
    productive_minutes_for_segment: Callable[[WorkSegment], float] | None = None,
    live_cap_utc: datetime | None = None,
    allow_total_fallback: bool = True,
) -> dict[str, tuple[SegmentCredit, ...]]:
    """Credit samples and total-only fallbacks to individual work segments."""
    ordered = _ordered_segments(segments)
    rows: list[dict] = []
    indices_by_wc: dict[str, list[int]] = {}
    for segment_id, segment in enumerate(ordered):
        minutes = max(
            0.0,
            float(
                (
                    productive_minutes_for_segment(segment)
                    if productive_minutes_for_segment is not None
                    else productive_minutes(
                        segment.person_name,
                        segment.wc_name,
                        segment.start_utc,
                        segment.end_utc,
                    )
                )
            ),
        )
        row = {
            "segment_id": segment_id,
            "wc_name": segment.wc_name,
            "person_name": segment.person_name,
            "start_utc": segment.start_utc,
            "end_utc": segment.end_utc,
            "source": segment.source,
            "person_odoo_id": segment.person_odoo_id,
            "productive_minutes": minutes,
            "actual_units": 0.0,
            "is_active": live_cap_utc is not None and segment.end_utc == live_cap_utc,
        }
        indices_by_wc.setdefault(segment.wc_name, []).append(len(rows))
        rows.append(row)

    sampled_units: dict[str, float] = {}
    unassigned: dict[str, dict] = {}
    for wc_name, samples in samples_by_wc.items():
        wc_indices = indices_by_wc.get(wc_name, [])
        for timestamp, raw_units in sorted(samples, key=lambda item: item[0]):
            units = float(raw_units or 0)
            if units <= 0:
                continue
            sampled_units[wc_name] = sampled_units.get(wc_name, 0.0) + units
            # A live staffing snapshot is only authoritative through its
            # verified cap. Count later samples as sampled so total fallback
            # cannot re-credit them, but do not attach them to a worker or an
            # unassigned row outside that coherent snapshot.
            if live_cap_utc is not None and timestamp >= live_cap_utc:
                continue
            active_by_person: dict[PersonAttributionKey, int] = {}
            for index in wc_indices:
                row = rows[index]
                if row["start_utc"] <= timestamp < row["end_utc"]:
                    identity: PersonAttributionKey = (
                        (row["person_odoo_id"], row["person_name"])
                        if row["person_odoo_id"] is not None
                        else row["person_name"]
                    )
                    active_by_person.setdefault(identity, index)
            if active_by_person:
                share = units / len(active_by_person)
                for index in active_by_person.values():
                    rows[index]["actual_units"] += share
                continue
            bucket = unassigned.setdefault(
                wc_name,
                {
                    "actual_units": 0.0,
                    "start_utc": timestamp,
                    "end_utc": timestamp,
                },
            )
            bucket["actual_units"] += units
            bucket["start_utc"] = min(bucket["start_utc"], timestamp)
            bucket["end_utc"] = max(bucket["end_utc"], timestamp)

    for wc_name, raw_total in wc_totals.items():
        if not allow_total_fallback:
            continue
        remaining = max(
            0.0,
            float(raw_total or 0) - sampled_units.get(wc_name, 0.0),
        )
        if remaining <= 0:
            continue
        eligible = [
            index
            for index in indices_by_wc.get(wc_name, [])
            if rows[index]["productive_minutes"] > 0
        ]
        total_minutes = sum(rows[index]["productive_minutes"] for index in eligible)
        if total_minutes > 0:
            for index in eligible:
                rows[index]["actual_units"] += (
                    remaining * rows[index]["productive_minutes"] / total_minutes
                )
        else:
            bucket = unassigned.setdefault(
                wc_name,
                {"actual_units": 0.0, "start_utc": None, "end_utc": None},
            )
            bucket["actual_units"] += remaining

    for wc_name, bucket in unassigned.items():
        rows.append(
            {
                "segment_id": len(rows),
                "wc_name": wc_name,
                "person_name": None,
                "start_utc": bucket["start_utc"],
                "end_utc": bucket["end_utc"],
                "source": "unassigned",
                "person_odoo_id": None,
                "productive_minutes": 0.0,
                "actual_units": bucket["actual_units"],
                "is_active": False,
            }
        )

    by_wc: dict[str, list[SegmentCredit]] = {}
    for row in rows:
        credit = SegmentCredit(**row)
        by_wc.setdefault(credit.wc_name, []).append(credit)
    for wc_rows in by_wc.values():
        wc_rows.sort(
            key=lambda row: (
                row.start_utc is None,
                row.start_utc.timestamp() if row.start_utc is not None else float("inf"),
                row.segment_id,
            )
        )
    return {wc_name: tuple(wc_rows) for wc_name, wc_rows in by_wc.items()}


def score_work_segments(
    credits_by_wc: Mapping[str, Sequence[SegmentCredit]],
    *,
    target_per_hour: Mapping[str, float],
) -> dict[str, tuple[SegmentScore, ...]]:
    """Add independent goals and result states to credited segments."""
    out: dict[str, tuple[SegmentScore, ...]] = {}
    for wc_name, credits in credits_by_wc.items():
        target = max(0.0, float(target_per_hour.get(wc_name, 0.0) or 0.0))
        scored = []
        for credit in credits:
            goal = (
                target * credit.productive_minutes / 60.0
                if credit.person_name is not None and target > 0
                else 0.0
            )
            result = _segment_result(credit.person_name, credit.actual_units, goal)
            scored.append(
                SegmentScore(
                    segment_id=credit.segment_id,
                    wc_name=credit.wc_name,
                    person_name=credit.person_name,
                    start_utc=credit.start_utc,
                    end_utc=credit.end_utc,
                    source=credit.source,
                    productive_minutes=credit.productive_minutes,
                    actual_units=credit.actual_units,
                    goal_units=goal,
                    runway_units=max(credit.actual_units, goal),
                    is_active=credit.is_active,
                    result=result,
                    person_odoo_id=credit.person_odoo_id,
                )
            )
        out[wc_name] = tuple(scored)
    return out
