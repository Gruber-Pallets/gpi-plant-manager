from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from . import db
from .forklift_ingest import ForkliftCompletionEvent


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
