"""Pure unit credit and scoring for resolved worker production segments."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from .assignment_windows import WorkSegment


SegmentResult = Literal["ahead", "behind", "neutral"]


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
    live_cap_utc: datetime | None = None,
) -> dict[str, tuple[SegmentCredit, ...]]:
    """Credit samples and total-only fallbacks to individual work segments."""
    ordered = _ordered_segments(segments)
    rows: list[dict] = []
    indices_by_wc: dict[str, list[int]] = {}
    for segment_id, segment in enumerate(ordered):
        minutes = max(
            0.0,
            float(
                productive_minutes(
                    segment.person_name,
                    segment.wc_name,
                    segment.start_utc,
                    segment.end_utc,
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
            "productive_minutes": minutes,
            "actual_units": 0.0,
            "is_active": live_cap_utc is not None
            and segment.end_utc == live_cap_utc,
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
            active_by_person: dict[str, int] = {}
            for index in wc_indices:
                row = rows[index]
                if row["start_utc"] <= timestamp < row["end_utc"]:
                    active_by_person.setdefault(row["person_name"], index)
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
                row.start_utc.timestamp()
                if row.start_utc is not None
                else float("inf"),
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
            if credit.person_name is None or goal <= 0:
                result: SegmentResult = "neutral"
            elif credit.actual_units >= goal:
                result = "ahead"
            else:
                result = "behind"
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
                )
            )
        out[wc_name] = tuple(scored)
    return out
