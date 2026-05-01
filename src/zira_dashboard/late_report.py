"""Late / absence report: declared absences + snoozes + late-people query.

Two-table data layer (manual_absences, late_snoozes — see db.py) plus a
pure helper that filters StratusTime attendance into "late" rows for the
report.

`late_people_for_day` is the only function that reasons about thresholds.
Everything else is straightforward CRUD.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Iterable

from . import db


LATE_THRESHOLD_MINUTES = 15
DEFAULT_SNOOZE_MINUTES = 30


def declare_absent(day, emp_id: str, name: str) -> None:
    db.execute(
        """
        INSERT INTO manual_absences (day, emp_id, name)
        VALUES (%s, %s, %s)
        ON CONFLICT (day, emp_id) DO UPDATE SET name = EXCLUDED.name
        """,
        (day, str(emp_id), name),
    )


def undo_absent(day, emp_id: str) -> None:
    db.execute(
        "DELETE FROM manual_absences WHERE day = %s AND emp_id = %s",
        (day, str(emp_id)),
    )


def absences_for_day(day) -> list[dict]:
    """Return list of {emp_id, name, declared_at} for declared absences."""
    return db.query(
        "SELECT emp_id, name, declared_at FROM manual_absences WHERE day = %s",
        (day,),
    )


def absent_names_for_day(day) -> set[str]:
    return {r["name"] for r in absences_for_day(day)}


def absent_emp_ids_for_day(day) -> set[str]:
    return {r["emp_id"] for r in absences_for_day(day)}


def snooze(day, emp_id: str, name: str, minutes: int = DEFAULT_SNOOZE_MINUTES) -> None:
    until = datetime.now(timezone.utc) + timedelta(minutes=minutes)
    db.execute(
        """
        INSERT INTO late_snoozes (day, emp_id, name, until_utc)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (day, emp_id) DO UPDATE SET
          until_utc = EXCLUDED.until_utc,
          name = EXCLUDED.name,
          created_at = now()
        """,
        (day, str(emp_id), name, until),
    )


def active_snoozes(day) -> list[dict]:
    """Return list of {emp_id, name, until_utc} for snoozes that haven't expired."""
    return db.query(
        """
        SELECT emp_id, name, until_utc
        FROM late_snoozes
        WHERE day = %s AND until_utc > now()
        ORDER BY until_utc ASC
        """,
        (day,),
    )


def cleanup_expired_snoozes(day) -> None:
    """Best-effort cleanup so the table doesn't grow unbounded."""
    db.execute(
        "DELETE FROM late_snoozes WHERE day < %s OR until_utc < now()",
        (day,),
    )


def late_people_for_day(
    day,
    scheduled_emp_ids: Iterable[str],
    attendance: dict,
    now_local: datetime,
    shift_start_local: datetime,
    threshold_minutes: int = LATE_THRESHOLD_MINUTES,
) -> list[dict]:
    """Return the actionable list of late people for `day`.

    A person is "late" if:
      - They are scheduled (`emp_id in scheduled_emp_ids`)
      - Their attendance status is `no_punch` (haven't clocked in today)
      - Now is past shift_start + threshold_minutes
      - They are NOT already declared absent for `day`
      - They are NOT currently snoozed

    Result rows: {emp_id, name, minutes_late}.
    Attendance dict shape matches stratustime_client.attendance_for_day.
    Caller supplies `now_local` and `shift_start_local` so this stays pure
    (testable without mocking time).
    """
    scheduled = {str(e) for e in scheduled_emp_ids}
    if now_local <= shift_start_local + timedelta(minutes=threshold_minutes):
        return []

    absent_ids = absent_emp_ids_for_day(day)
    snoozed_ids = {s["emp_id"] for s in active_snoozes(day)}

    minutes_past_start = int((now_local - shift_start_local).total_seconds() // 60)

    out: list[dict] = []
    for emp_id, info in attendance.items():
        if emp_id not in scheduled:
            continue
        if info.get("status") != "no_punch":
            continue
        if emp_id in absent_ids or emp_id in snoozed_ids:
            continue
        out.append({
            "emp_id": emp_id,
            "minutes_late": minutes_past_start,
        })
    return out
