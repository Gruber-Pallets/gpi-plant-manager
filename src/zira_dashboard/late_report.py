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


def declare_absent(day, emp_id: str, name: str, reason: str | None = None) -> None:
    db.execute(
        """
        INSERT INTO manual_absences (day, emp_id, name, reason)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (day, emp_id) DO UPDATE SET
          name = EXCLUDED.name,
          reason = EXCLUDED.reason
        """,
        (day, str(emp_id), name, reason),
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


def clear_time_off_request(day, request_id) -> None:
    """Mark a StratusTime time-off request as cleared for `day`. Filters
    out the entry from time_off_entries_for_day, hiding the partial pill
    on the scheduler and removing the person from the Time Off list.
    Doesn't touch StratusTime — purely a local override."""
    db.execute(
        """
        INSERT INTO cleared_time_off (day, request_id) VALUES (%s, %s)
        ON CONFLICT (day, request_id) DO NOTHING
        """,
        (day, int(request_id)),
    )


def restore_time_off_request(day, request_id) -> None:
    """Undo clear_time_off_request — partial reappears on next render."""
    db.execute(
        "DELETE FROM cleared_time_off WHERE day = %s AND request_id = %s",
        (day, int(request_id)),
    )


def cleared_request_ids_for_day(day) -> set[int]:
    rows = db.query(
        "SELECT request_id FROM cleared_time_off WHERE day = %s",
        (day,),
    )
    return {int(r["request_id"]) for r in rows}


def clear_non_work_shift(day, emp_id: str) -> None:
    """Hide a StratusTime non-work-shift entry (manager-entered Unpaid
    Time, etc.) for `day` + `emp_id`. The V1 punch endpoint doesn't
    expose a stable per-entry id, so we key on (day, emp_id)."""
    db.execute(
        """
        INSERT INTO cleared_non_work_shifts (day, emp_id) VALUES (%s, %s)
        ON CONFLICT (day, emp_id) DO NOTHING
        """,
        (day, str(emp_id)),
    )


def restore_non_work_shift(day, emp_id: str) -> None:
    db.execute(
        "DELETE FROM cleared_non_work_shifts WHERE day = %s AND emp_id = %s",
        (day, str(emp_id)),
    )


def cleared_non_work_emp_ids_for_day(day) -> set[str]:
    rows = db.query(
        "SELECT emp_id FROM cleared_non_work_shifts WHERE day = %s",
        (day,),
    )
    return {str(r["emp_id"]) for r in rows}


def cleared_non_work_for_day(day) -> list[dict]:
    return db.query(
        """
        SELECT emp_id, declared_at
        FROM cleared_non_work_shifts
        WHERE day = %s
        ORDER BY declared_at ASC
        """,
        (day,),
    )


def clear_partial_by_name(day, name: str) -> None:
    """Catch-all clear: hide a partial entry on `day` for `name`. Works
    regardless of whether the underlying entry has a request_id, emp_id,
    or neither. Uses the scheduler's roster name as the key."""
    db.execute(
        """
        INSERT INTO cleared_partials_by_name (day, name) VALUES (%s, %s)
        ON CONFLICT (day, name) DO NOTHING
        """,
        (day, name),
    )


def restore_partial_by_name(day, name: str) -> None:
    db.execute(
        "DELETE FROM cleared_partials_by_name WHERE day = %s AND name = %s",
        (day, name),
    )


def cleared_partial_names_for_day(day) -> set[str]:
    rows = db.query(
        "SELECT name FROM cleared_partials_by_name WHERE day = %s",
        (day,),
    )
    return {r["name"] for r in rows}


def cleared_partial_names_for_range(start_d, end_d) -> dict:
    rows = db.query(
        "SELECT day, name FROM cleared_partials_by_name WHERE day BETWEEN %s AND %s",
        (start_d, end_d),
    )
    out: dict = {}
    for r in rows:
        out.setdefault(r["day"], set()).add(r["name"])
    return out


def cleared_partial_names_today_list(day) -> list[dict]:
    return db.query(
        """
        SELECT name, declared_at
        FROM cleared_partials_by_name
        WHERE day = %s
        ORDER BY declared_at ASC
        """,
        (day,),
    )


def cleared_request_ids_for_range(start_d, end_d) -> dict:
    """Bulk version of cleared_request_ids_for_day for [start_d, end_d].
    Returns {date: set(request_id, ...)}. One DB query for the whole range."""
    rows = db.query(
        "SELECT day, request_id FROM cleared_time_off WHERE day BETWEEN %s AND %s",
        (start_d, end_d),
    )
    out: dict = {}
    for r in rows:
        out.setdefault(r["day"], set()).add(int(r["request_id"]))
    return out


def cleared_non_work_emp_ids_for_range(start_d, end_d) -> dict:
    rows = db.query(
        "SELECT day, emp_id FROM cleared_non_work_shifts WHERE day BETWEEN %s AND %s",
        (start_d, end_d),
    )
    out: dict = {}
    for r in rows:
        out.setdefault(r["day"], set()).add(str(r["emp_id"]))
    return out


def absences_for_range(start_d, end_d) -> dict:
    """Bulk version of absences_for_day. {date: [{emp_id, name, declared_at}, ...]}."""
    rows = db.query(
        "SELECT day, emp_id, name, declared_at FROM manual_absences "
        "WHERE day BETWEEN %s AND %s",
        (start_d, end_d),
    )
    out: dict = {}
    for r in rows:
        out.setdefault(r["day"], []).append({
            "emp_id": r["emp_id"],
            "name": r["name"],
            "declared_at": r["declared_at"],
        })
    return out


def cleared_partials_for_day(day) -> list[dict]:
    """Return list of {request_id, declared_at} for the Time Off
    'Cleared today' restore footer."""
    return db.query(
        """
        SELECT request_id, declared_at
        FROM cleared_time_off
        WHERE day = %s
        ORDER BY declared_at ASC
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


def late_people_for_day_v2(
    day,
    scheduled_emp_ids: Iterable[str],
    unscheduled_emp_ids: Iterable[str],
    attendance: dict,
    now_local: datetime,
    shift_start_local: datetime,
    absent_ids: set[str],
    snoozed_ids: set[str],
    already_recorded_late_ids: set[str],
    threshold_minutes: int = LATE_THRESHOLD_MINUTES,
) -> dict:
    """Three-section structured output for /api/late-report.

    Returns:
      {
        "scheduled_late":   [{emp_id, minutes_late}, ...],
        "unscheduled_late": [{emp_id}, ...],
        "needs_reason":     [{emp_id, minutes_late}, ...],
      }

    Args mirror late_people_for_day plus:
      - unscheduled_emp_ids: active non-reserve people not on today's
        schedule. They join scheduled_emp_ids in the no_punch check.
      - already_recorded_late_ids: emp_ids that already have a row in
        late_arrivals for `day`. Suppresses needs_reason entries once
        a manager has captured the reason.

    Pure: no DB calls, no cache lookups. Caller passes everything in.
    """
    scheduled = {str(e) for e in scheduled_emp_ids}
    unscheduled = {str(e) for e in unscheduled_emp_ids}
    if now_local <= shift_start_local + timedelta(minutes=threshold_minutes):
        return {"scheduled_late": [], "unscheduled_late": [], "needs_reason": []}

    minutes_past_start = int((now_local - shift_start_local).total_seconds() // 60)

    scheduled_late: list[dict] = []
    unscheduled_late: list[dict] = []
    needs_reason: list[dict] = []

    for emp_id, info in attendance.items():
        if emp_id in absent_ids or emp_id in snoozed_ids:
            continue
        status = info.get("status")
        if status == "no_punch":
            if emp_id in scheduled:
                scheduled_late.append({
                    "emp_id": emp_id,
                    "minutes_late": minutes_past_start,
                })
            elif emp_id in unscheduled:
                unscheduled_late.append({"emp_id": emp_id})
        elif status == "late":
            if emp_id in already_recorded_late_ids:
                continue
            if emp_id in scheduled or emp_id in unscheduled:
                needs_reason.append({
                    "emp_id": emp_id,
                    "minutes_late": int(info.get("minutes_late") or 0),
                })

    return {
        "scheduled_late": scheduled_late,
        "unscheduled_late": unscheduled_late,
        "needs_reason": needs_reason,
    }


def save_late_arrival(day, emp_id: str, name: str, reason: str | None = None) -> None:
    """Record a late-arrival event for `day` + `emp_id`. Idempotent — a
    second save with a different reason overwrites the first."""
    db.execute(
        """
        INSERT INTO late_arrivals (day, emp_id, name, reason)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (day, emp_id) DO UPDATE SET
          name = EXCLUDED.name,
          reason = EXCLUDED.reason
        """,
        (day, str(emp_id), name, reason),
    )


def late_arrivals_for_day(day) -> set[str]:
    """Set of emp_ids that already have a late-arrival record for `day`.
    Used by /api/late-report to suppress 'needs reason' rows once
    they've been handled."""
    rows = db.query(
        "SELECT emp_id FROM late_arrivals WHERE day = %s",
        (day,),
    )
    return {r["emp_id"] for r in rows}


def absences_history_for_name(name: str, start_d, end_d) -> list[dict]:
    """Per-day absence history for `name` within [start_d, end_d].
    Newest first. Each row: {day, reason}."""
    rows = db.query(
        """
        SELECT day, reason
        FROM manual_absences
        WHERE name = %s AND day BETWEEN %s AND %s
        ORDER BY day DESC
        """,
        (name, start_d, end_d),
    )
    return [{"day": r["day"], "reason": r["reason"]} for r in rows]


def late_arrivals_history_for_name(name: str, start_d, end_d) -> list[dict]:
    """Per-day late-arrival history for `name` within [start_d, end_d].
    Newest first. Each row: {day, reason}."""
    rows = db.query(
        """
        SELECT day, reason
        FROM late_arrivals
        WHERE name = %s AND day BETWEEN %s AND %s
        ORDER BY day DESC
        """,
        (name, start_d, end_d),
    )
    return [{"day": r["day"], "reason": r["reason"]} for r in rows]
