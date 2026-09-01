from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime

from . import db
from .forklift_ingest import ForkliftCompletionEvent


@dataclass(frozen=True)
class ForkliftCompletionCoverage:
    day: date
    covered_through_utc: datetime
    raw_event_count: int
    successful_at: datetime


def _aware_utc(value: datetime, field_name: str) -> datetime:
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise TypeError(f"{field_name} must be timezone-aware")
    return value.astimezone(UTC)


def upsert_completion_events(events: Sequence[ForkliftCompletionEvent]) -> int:
    rows = list(events)
    if not rows:
        return 0
    sql = """
        INSERT INTO forklift_completion_events (
            external_id, driver_id, driver_name, created_at_utc,
            workstation_name, on_time, late, response_ms, handling_ms,
            ingested_at, updated_at
        ) VALUES %s
        ON CONFLICT (external_id) DO UPDATE SET
            driver_id=EXCLUDED.driver_id,
            driver_name=EXCLUDED.driver_name,
            created_at_utc=EXCLUDED.created_at_utc,
            workstation_name=EXCLUDED.workstation_name,
            on_time=EXCLUDED.on_time,
            late=EXCLUDED.late,
            response_ms=EXCLUDED.response_ms,
            handling_ms=EXCLUDED.handling_ms,
            updated_at=now()
    """
    with db.cursor() as cur:
        db.execute_values(
            cur,
            sql,
            [
                (
                    event.event_id,
                    event.driver_id,
                    event.driver_name,
                    event.created_at_utc,
                    event.workstation_name,
                    event.on_time,
                    event.late,
                    event.response_ms,
                    event.handling_ms,
                )
                for event in rows
            ],
            template="(%s,%s,%s,%s,%s,%s,%s,%s,%s,now(),now())",
        )
    return len(rows)


def completion_events_for_range(
    start_utc: datetime, end_utc: datetime
) -> tuple[ForkliftCompletionEvent, ...]:
    rows = db.query(
        "SELECT external_id, driver_id, driver_name, created_at_utc, "
        "workstation_name, on_time, late, response_ms, handling_ms "
        "FROM forklift_completion_events "
        "WHERE created_at_utc >= %s AND created_at_utc < %s "
        "ORDER BY created_at_utc, external_id",
        (start_utc, end_utc),
    )
    return tuple(
        ForkliftCompletionEvent(
            event_id=row["external_id"],
            driver_id=row["driver_id"],
            driver_name=row["driver_name"],
            created_at_utc=row["created_at_utc"],
            workstation_name=row["workstation_name"],
            on_time=row["on_time"],
            late=row["late"],
            response_ms=row["response_ms"],
            handling_ms=row["handling_ms"],
        )
        for row in rows
    )


def record_completion_coverage(
    day: date,
    *,
    covered_through_utc: datetime,
    raw_event_count: int,
) -> None:
    """Record proof only after a successful aggregate and raw-event write."""
    if type(day) is not date:
        raise TypeError("day must be a date")
    if isinstance(raw_event_count, bool) or not isinstance(raw_event_count, int):
        raise TypeError("raw_event_count must be an integer")
    if raw_event_count < 0:
        raise ValueError("raw_event_count cannot be negative")
    covered = _aware_utc(covered_through_utc, "covered_through_utc")
    db.execute(
        "INSERT INTO forklift_completion_coverage "
        "(day, covered_through_utc, raw_event_count, successful_at) "
        "VALUES (%s, %s, %s, now()) "
        "ON CONFLICT (day) DO UPDATE SET "
        "covered_through_utc=EXCLUDED.covered_through_utc, "
        "raw_event_count=EXCLUDED.raw_event_count, successful_at=now()",
        (day, covered, raw_event_count),
    )


def completion_coverage_for_day(day: date) -> ForkliftCompletionCoverage | None:
    if type(day) is not date:
        raise TypeError("day must be a date")
    rows = db.query(
        "SELECT day, covered_through_utc, raw_event_count, successful_at "
        "FROM forklift_completion_coverage WHERE day = %s",
        (day,),
    )
    if not rows:
        return None
    row = rows[0]
    return ForkliftCompletionCoverage(
        day=row["day"],
        covered_through_utc=_aware_utc(
            row["covered_through_utc"], "covered_through_utc"
        ),
        raw_event_count=int(row["raw_event_count"]),
        successful_at=_aware_utc(row["successful_at"], "successful_at"),
    )
