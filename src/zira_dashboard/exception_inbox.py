"""Daily exception inbox data.

This module composes the existing local alert snapshots into one operational
view. It deliberately avoids fresh Odoo calls; each source is either cached
in-process or read from the local Postgres mirror.
"""

from __future__ import annotations

import logging
from datetime import date, time, timedelta

from . import plant_day, schedule_store, staffing
from . import inbox_keys

_log = logging.getLogger(__name__)


def _capture(errors: list[dict], source: str, call, fallback):
    try:
        return call()
    except Exception as e:  # noqa: BLE001 -- degraded source should not hide whole inbox
        _log.warning("exception inbox source failed (%s): %s", source, e, exc_info=True)
        errors.append({"source": source})
        return fallback


def _plural(n: int, singular: str, plural: str | None = None) -> str:
    word = singular if n == 1 else (plural or f"{singular}s")
    return f"{n} {word}"


def _time_off_label(row: dict) -> str:
    start = row.get("date_from")
    end = row.get("date_to")
    if start and end and start != end:
        return f"{start} to {end}"
    return str(start or end or "")


def _work_center_names() -> list[str]:
    from . import staffing

    return [loc.name for loc in staffing.LOCATIONS]


_SCHEDULE_REMINDER_CUTOFF = time(13, 30)


def _next_business_day(day: date) -> date:
    work_weekdays = schedule_store.current().work_weekdays or frozenset({0, 1, 2, 3, 4})
    nxt = day + timedelta(days=1)
    for _ in range(14):
        if nxt.weekday() in work_weekdays:
            return nxt
        nxt += timedelta(days=1)
    return day + timedelta(days=1)


def _plant_schedule_reminder() -> tuple[int, list[dict]]:
    now = plant_day.now()
    if now.time() < _SCHEDULE_REMINDER_CUTOFF:
        return 0, []

    target_day = _next_business_day(now.date())
    sched = staffing.load_schedule(target_day)
    if sched.published:
        return 0, []

    return 1, [{
        "name": "Plant Schedule",
        "label": target_day.strftime("%A, %b %-d"),
        "detail": "Not published",
        "priority": "warn",
        "badge": "Publish",
        "href": f"/staffing?day={target_day.isoformat()}",
        "row_key": _row_key("plant_schedule", target_day.isoformat()),
        "item_key": inbox_keys.plant_schedule(target_day.isoformat()),
    }]


def _row_key(kind: str, *parts) -> str:
    return ":".join([kind, *(str(p) for p in parts if p not in (None, ""))])


_PENDING_TIME_OFF_WHERE = (
    "state IN ('draft', 'draft_edit', 'confirm', 'validate1') "
    "AND date_to >= %s"
)


def _pending_time_off_count(today: date) -> int:
    from . import db

    count_rows = db.query(
        f"SELECT COUNT(*) AS n FROM time_off_requests WHERE {_PENDING_TIME_OFF_WHERE}",
        (today,),
    )
    return int(count_rows[0]["n"] if count_rows else 0)


def _pending_time_off(today: date, limit: int = 8) -> tuple[int, list[dict]]:
    from . import db

    rows = db.query(
        "SELECT r.id, r.person_odoo_id, r.odoo_leave_id, "
        "COALESCE(p.name, '#' || r.person_odoo_id::text) AS name, "
        "r.shape, r.state, r.date_from, r.date_to, r.hour_from, r.hour_to, "
        "r.sync_error, COALESCE(lt.name, 'Time off') AS leave_type, "
        "COUNT(*) OVER () AS total_count "
        "FROM time_off_requests r "
        "LEFT JOIN people p ON p.odoo_id = r.person_odoo_id "
        "LEFT JOIN leave_types_cache lt ON lt.holiday_status_id = r.holiday_status_id "
        f"WHERE {_PENDING_TIME_OFF_WHERE} "
        "ORDER BY r.date_from, lower(COALESCE(p.name, '#' || r.person_odoo_id::text)) "
        "LIMIT %s",
        (today, limit),
    )
    shaped = [
        {
            "id": r["id"],
            "name": r["name"],
            "label": _time_off_label(r),
            "detail": f"{r['leave_type']} · {str(r['state']).replace('_', ' ')}",
            "state": r["state"],
            "sync_error": r.get("sync_error"),
            "priority": "info",
            "badge": "Approval",
            "row_key": _row_key("time_off", r["id"], r["state"]),
            "item_key": inbox_keys.time_off(r["id"]),
            "action": {
                "type": "time_off",
                "request_id": r["id"],
                "state": r["state"],
                "odoo_leave_id": r.get("odoo_leave_id"),
            },
        }
        for r in rows
    ]
    return int(rows[0].get("total_count") or 0) if rows else 0, shaped


_TIER_RANK = {"urgent": 0, "warn": 1, "info": 2, "normal": 2, "muted": 3}


def _queue_from_sections(sections: list[dict]) -> list[dict]:
    """Flatten section rows into one queue: urgency tier first (urgent → warn →
    info → muted/follow-up), preserving each section's order within a tier.
    Empty sections contribute nothing. Each row is tagged with its category."""
    tagged = []
    for section_order, section in enumerate(sections):
        for row_index, row in enumerate(section.get("rows") or []):
            tagged.append((
                _TIER_RANK.get(row.get("priority", "normal"), 2),
                section_order,
                row_index,
                {
                    **row,
                    "section_id": section["id"],
                    "category_label": section["title"],
                    "tone": section["tone"],
                },
            ))
    tagged.sort(key=lambda t: (t[0], t[1], t[2]))
    return [t[3] for t in tagged]


def build_summary() -> dict:
    from . import missing_wc, missed_punch_out
    from .routes import staffing as staffing_routes

    today = plant_day.today()
    source_errors: list[dict] = []
    assignments = _capture(
        source_errors, "Assignments To Do", staffing_routes.assignments_todo_payload, {}
    )
    late = _capture(source_errors, "Late / Absence", staffing_routes.late_report_payload, {})
    missing_rows = _capture(source_errors, "Missing Work Center", missing_wc.current_rows, [])
    missed_rows = _capture(source_errors, "Missed Punch Out", missed_punch_out.current_rows, [])
    schedule_count = _capture(
        source_errors, "Plant Schedule", lambda: _plant_schedule_reminder()[0], 0
    )
    pending_count = _capture(
        source_errors, "Pending Time Off", lambda: _pending_time_off_count(today), 0
    )

    assignment_count = int(assignments.get("count") or 0)
    late_count = int(late.get("count") or 0)
    missing_count = len(missing_rows)
    missed_count = len(missed_rows)
    urgent_total = (
        len(late.get("scheduled_late") or [])
        + len(late.get("unscheduled_late") or [])
        + missing_count
        + missed_count
    )
    total = (
        assignment_count
        + schedule_count
        + late_count
        + missing_count
        + missed_count
        + pending_count
    )
    return {
        "today": today.isoformat(),
        "generated_at": plant_day.now().strftime("%-I:%M %p"),
        "total": total,
        "urgent_total": urgent_total,
        "follow_up_total": len(late.get("snoozed") or []),
        "source_errors": source_errors,
        "sections": {
            "assignments": assignment_count,
            "plant_schedule": schedule_count,
            "late": late_count,
            "missing_wc": missing_count,
            "missed_punch_out": missed_count,
            "time_off": pending_count,
        },
    }


def build_snapshot() -> dict:
    from . import missing_wc, missed_punch_out
    from .routes import staffing as staffing_routes

    today = plant_day.today()
    source_errors: list[dict] = []
    assignments = _capture(
        source_errors, "Assignments To Do", staffing_routes.assignments_todo_payload, {}
    )
    late = _capture(source_errors, "Late / Absence", staffing_routes.late_report_payload, {})
    missing_rows = _capture(source_errors, "Missing Work Center", missing_wc.current_rows, [])
    missed_rows = _capture(source_errors, "Missed Punch Out", missed_punch_out.current_rows, [])
    schedule_count, schedule_rows = _capture(
        source_errors, "Plant Schedule", _plant_schedule_reminder, (0, [])
    )
    pending_count, pending_rows = _capture(
        source_errors, "Pending Time Off", lambda: _pending_time_off(today), (0, [])
    )
    work_centers = _capture(source_errors, "Work Center List", _work_center_names, [])
    # assignments_todo_payload / late_report_payload swallow their own internal
    # errors and return an empty payload with degraded=True (so the page still
    # renders). Surface that as a source error so the reconciler treats the
    # category as "unknown this tick" and never auto-resolves its items.
    for _payload, _label in ((assignments, "Assignments To Do"), (late, "Late / Absence")):
        if isinstance(_payload, dict) and _payload.get("degraded"):
            source_errors.append({"source": _label})

    late_rows: list[dict] = []
    for item in late.get("scheduled_late") or []:
        late_rows.append({
            "name": item.get("name"),
            "label": "Scheduled late",
            "detail": _plural(int(item.get("minutes_late") or 0), "min") + " late",
            "priority": "urgent",
            "badge": "Needs decision",
            "row_key": _row_key("late", "scheduled", item.get("emp_id")),
            "item_key": inbox_keys.late(item.get("emp_id"), today.isoformat()),
            "action": {
                "type": "late_absence",
                "kind": "scheduled",
                "emp_id": item.get("emp_id"),
                "name": item.get("name"),
            },
        })
    for item in late.get("unscheduled_late") or []:
        late_rows.append({
            "name": item.get("name"),
            "label": "Unscheduled late",
            "detail": "No punch yet",
            "priority": "urgent",
            "badge": "Needs decision",
            "row_key": _row_key("late", "unscheduled", item.get("emp_id")),
            "item_key": inbox_keys.late(item.get("emp_id"), today.isoformat()),
            "action": {
                "type": "late_absence",
                "kind": "unscheduled",
                "emp_id": item.get("emp_id"),
                "name": item.get("name"),
            },
        })
    for item in late.get("needs_reason") or []:
        late_rows.append({
            "name": item.get("name"),
            "label": "Reason needed",
            "detail": _plural(int(item.get("minutes_late") or 0), "min") + " late",
            "priority": "warn",
            "badge": "Reason",
            "row_key": _row_key("late_reason", item.get("emp_id")),
            "item_key": inbox_keys.late(item.get("emp_id"), today.isoformat()),
            "action": {
                "type": "late_reason",
                "emp_id": item.get("emp_id"),
                "name": item.get("name"),
            },
        })
    for item in late.get("snoozed") or []:
        mins = int(item.get("mins_remaining") or 0)
        late_rows.append({
            "name": item.get("name"),
            "label": "Snoozed",
            "detail": f"Re-checks in {_plural(mins, 'min')}",
            "priority": "muted",
            "badge": "Follow-up",
            "row_key": _row_key("late_snoozed", item.get("emp_id"), item.get("until_iso")),
            "item_key": inbox_keys.late(item.get("emp_id"), today.isoformat()),
        })

    sections = [
        {
            "id": "assignments",
            "title": "Assignments To Do",
            "count": int(assignments.get("count") or 0),
            "tone": "warn",
            "action_key": "assignments",
            "action_label": "Manage",
            "empty": "All clear",
            "context": {"people": assignments.get("people") or []},
            "rows": [
                {
                    "name": item.get("wc_name"),
                    "label": f"{item.get('units', 0)} units",
                    "detail": f"{item.get('first_label', '')} to {item.get('last_label', '')}",
                    "priority": "warn",
                    "badge": "Credit",
                    "row_key": _row_key("assignment", item.get("wc_name"), item.get("first_iso")),
                    "item_key": inbox_keys.assignment(item.get("wc_name"), item.get("first_iso")),
                    "action": {
                        "type": "assignment",
                        "day": assignments.get("today") or today.isoformat(),
                        "wc_name": item.get("wc_name"),
                        "start_utc": item.get("first_iso"),
                        "end_utc": item.get("last_iso"),
                    },
                }
                for item in assignments.get("items") or []
            ],
        },
        {
            "id": "plant_schedule",
            "title": "Plant Schedule",
            "count": schedule_count,
            "tone": "warn",
            "action_key": None,
            "action_label": None,
            "href": schedule_rows[0]["href"] if schedule_rows else "/staffing",
            "empty": "All clear",
            "context": {},
            "rows": schedule_rows,
        },
        {
            "id": "late",
            "title": "Late / Absence",
            "count": int(late.get("count") or 0),
            "tone": "bad",
            "action_key": "late",
            "action_label": "Manage",
            "empty": "All clear",
            "context": {},
            "rows": late_rows,
        },
        {
            "id": "missing_wc",
            "title": "Missing Work Center",
            "count": len(missing_rows),
            "tone": "bad",
            "action_key": "missing_wc",
            "action_label": "Assign",
            "empty": "All clear",
            "context": {"work_centers": work_centers},
            "rows": [
                {
                    "name": r.get("name"),
                    "label": "No work center",
                    "detail": f"Clocked in {r.get('check_in_label', '')}",
                    "priority": "urgent",
                    "badge": "Fix WC",
                    "row_key": _row_key("missing_wc", r.get("attendance_id")),
                    "item_key": inbox_keys.missing_wc(r.get("attendance_id")),
                    "action": {
                        "type": "missing_wc",
                        "attendance_id": r.get("attendance_id"),
                        "name": r.get("name"),
                    },
                }
                for r in missing_rows
            ],
        },
        {
            "id": "missed_punch_out",
            "title": "Missed Punch Out",
            "count": len(missed_rows),
            "tone": "bad",
            "action_key": "missed_punch_out",
            "action_label": "Correct",
            "empty": "All clear",
            "context": {},
            "rows": [
                {
                    "name": r.get("name"),
                    "label": "Auto-closed at midnight",
                    "detail": f"Clocked in {r.get('check_in_label', '')}",
                    "priority": "urgent",
                    "badge": "Fix time",
                    "row_key": _row_key("missed_punch_out", r.get("attendance_id")),
                    "item_key": inbox_keys.missed_punch_out(r.get("attendance_id")),
                    "action": {
                        "type": "missed_punch_out",
                        "attendance_id": r.get("attendance_id"),
                    },
                }
                for r in missed_rows
            ],
        },
        {
            "id": "time_off",
            "title": "Pending Time Off",
            "count": pending_count,
            "tone": "info",
            "action_key": None,
            "action_label": None,
            "href": "/staffing/time-off/approvals",
            "empty": "All clear",
            "context": {},
            "rows": pending_rows,
        },
    ]
    queue = _queue_from_sections(sections)
    total = sum(int(s["count"]) for s in sections)
    urgent_total = sum(
        1
        for section in sections
        for row in section.get("rows") or []
        if row.get("priority") == "urgent"
    )
    follow_up_total = sum(
        1
        for section in sections
        for row in section.get("rows") or []
        if row.get("priority") == "muted"
    )
    return {
        "today": today.isoformat(),
        "generated_at": plant_day.now().strftime("%-I:%M %p"),
        "total": total,
        "urgent_total": urgent_total,
        "follow_up_total": follow_up_total,
        "source_errors": source_errors,
        "sections": sections,
        "queue": queue,
        "work_centers": work_centers,
        "people": assignments.get("people") or [],
    }
