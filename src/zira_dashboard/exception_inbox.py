"""Daily exception inbox data.

This module composes the existing local alert snapshots into one operational
view. It deliberately avoids fresh Odoo calls; each source is either cached
in-process or read from the local Postgres mirror.
"""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime, time, timedelta

from . import plant_day, saturday_recruiting_store, schedule_store, shift_config, staffing
from . import inbox_keys
from . import time_off_context

_log = logging.getLogger(__name__)


def _capture(errors: list[dict], source: str, call, fallback):
    try:
        return call()
    except Exception as e:  # noqa: BLE001 -- degraded source should not hide whole inbox
        _log.warning("exception inbox source failed (%s): %s", source, e, exc_info=True)
        errors.append({"source": source})
        return fallback


def _auto_lunch_alert(errors: list[dict], current_snapshot):
    snapshot = _capture(
        errors,
        "Auto-Lunch",
        current_snapshot,
        None,
    )
    if snapshot is None:
        return None
    if snapshot.degraded:
        errors.append({"source": "Auto-Lunch"})
    return snapshot.alert


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


def _unexpected_worker_rows(events: list[dict]) -> list[dict]:
    """Shape unresolved leave clock-ins with actionable staffing guidance."""
    from .routes import staffing as staffing_routes

    shortages_by_day: dict[date, list[str]] = {}

    def shortages_for(day: date) -> list[str]:
        if day in shortages_by_day:
            return shortages_by_day[day]
        schedule = staffing.load_schedule(day)
        enabled = set(schedule.auto_enabled_work_centers)
        shortages = []
        for location in staffing.LOCATIONS:
            if location.name not in enabled:
                continue
            minimum = max(0, int(staffing_routes._effective_minimum(location)))
            assigned = len((schedule.assignments or {}).get(location.name, ()))
            if assigned < minimum:
                shortages.append(f"{location.name} ({assigned}/{minimum})")
        shortages_by_day[day] = shortages
        return shortages

    rows = []
    for event in events:
        event_day = event.get("day")
        if not isinstance(event_day, date):
            event_day = date.fromisoformat(str(event_day))
        shortages = shortages_for(event_day)
        detail = f"Clocked in at {event.get('clock_in_wc') or 'an unknown work center'}"
        if shortages:
            detail += f" · Staffing below minimum: {', '.join(shortages)}."
        else:
            detail += " · No enabled work centers are below minimum."
        rows.append(
            {
                "name": event.get("person_name") or f"Worker #{event.get('person_odoo_id')}",
                "label": "Unexpected clock-in",
                "detail": detail,
                "priority": "urgent",
                "badge": "Staffing",
                "href": f"/staffing?day={event_day.isoformat()}",
                "row_key": _row_key(
                    "unexpected_worker", event_day.isoformat(), event.get("person_odoo_id")
                ),
                "item_key": inbox_keys.unexpected_worker(
                    event_day.isoformat(), event.get("person_odoo_id")
                ),
            }
        )
    return rows


_SCHEDULE_REMINDER_CUTOFF = time(13, 30)


def _next_business_day(day: date) -> date:
    nxt = day + timedelta(days=1)
    for _ in range(14):
        try:
            if shift_config.is_workday(nxt):
                return nxt
        except Exception:
            try:
                work_weekdays = schedule_store.current().work_weekdays
            except Exception:
                work_weekdays = frozenset()
            work_weekdays = work_weekdays or frozenset({0, 1, 2, 3, 4})
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

    return 1, [
        {
            "name": "Plant Schedule",
            "label": target_day.strftime("%A, %b %-d"),
            "detail": "Not published",
            "priority": "warn",
            "badge": "Publish",
            "href": f"/staffing?day={target_day.isoformat()}",
            "row_key": _row_key("plant_schedule", target_day.isoformat()),
            "item_key": inbox_keys.plant_schedule(target_day.isoformat()),
        }
    ]


def _saturday_staffing_actions(today: date) -> tuple[int, list[dict]]:
    """Surface closed volunteer rounds until a manager opens their schedule."""
    bundles = saturday_recruiting_store.list_closed_unprepared(today)
    rows = []
    for bundle in bundles:
        day = bundle.recruitment.day
        committed = sum(item.status == "committed" for item in bundle.commitments)
        rows.append(
            {
                "name": "Saturday recruitment",
                "label": day.strftime("%A, %b %-d"),
                "detail": f"{committed} committed · Ready to schedule",
                "priority": "warn",
                "badge": "Schedule",
                "href": f"/staffing?day={day.isoformat()}",
                "row_key": _row_key("saturday_recruitment", day.isoformat()),
                "item_key": inbox_keys.saturday_recruitment(day.isoformat()),
            }
        )
    return len(rows), rows


def _row_key(kind: str, *parts) -> str:
    return ":".join([kind, *(str(p) for p in parts if p not in (None, ""))])


_PENDING_TIME_OFF_WHERE = "r.state IN ('draft', 'draft_edit', 'confirm', 'validate1')"


def _pending_time_off_counts(today: date) -> tuple[int, int]:
    from . import db

    count_rows = db.query(
        "SELECT COUNT(*) AS n, COUNT(*) FILTER (WHERE r.date_to < %s) AS past_due_n "
        f"FROM time_off_requests r WHERE {_PENDING_TIME_OFF_WHERE}",
        (today,),
    )
    if not count_rows:
        return 0, 0
    return int(count_rows[0]["n"] or 0), int(count_rows[0]["past_due_n"] or 0)


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
        (limit,),
    )
    shaped = []
    for r in rows:
        past_due = r["date_to"] < today
        shaped.append(
            {
                "id": r["id"],
                "person_odoo_id": r["person_odoo_id"],
                "date_from": r["date_from"],
                "date_to": r["date_to"],
                "name": r["name"],
                "label": _time_off_label(r),
                "detail": f"{r['leave_type']} · {str(r['state']).replace('_', ' ')}",
                "state": r["state"],
                "sync_error": r.get("sync_error"),
                "past_due": past_due,
                "priority": "urgent" if past_due else "info",
                "badge": "Past due" if past_due else "Approval",
                "row_key": _row_key("time_off", r["id"], r["state"]),
                "item_key": inbox_keys.time_off(r["id"]),
                "action": {
                    "type": "time_off",
                    "request_id": r["id"],
                    "state": r["state"],
                    "odoo_leave_id": r.get("odoo_leave_id"),
                },
            }
        )
    coverage = time_off_context.coverage_breakdowns_for(shaped)
    for row in shaped:
        row["coverage"] = coverage.get(row["id"])
    return int(rows[0].get("total_count") or 0) if rows else 0, shaped


_TIER_RANK = {"urgent": 0, "warn": 1, "info": 2, "normal": 2, "muted": 3}

_ATTENDANCE_SECTION_META = {
    "attendance_missing_location": ("Odoo Location Missing", "bad"),
    "attendance_unmapped_location": ("Unknown Odoo Work Center", "bad"),
    "attendance_conflicting_location": ("Mixed-Up Odoo Locations", "bad"),
    "attendance_duplicate_location": ("Duplicate Odoo Location", "warn"),
    "attendance_department_repair_failed": ("Odoo Department Repair", "bad"),
    "attendance_source_stale": ("Odoo Location Source", "bad"),
    "production_source_unavailable": ("Production Source", "bad"),
    "production_unassigned_run": ("Production Without a Worker", "bad"),
}


def _now_utc() -> datetime:
    value = plant_day.now()
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _attendance_snapshot(today: date, source_errors: list[dict]):
    from . import attendance_exceptions

    try:
        snapshot = attendance_exceptions.build_snapshot(today, now_utc=_now_utc())
    except Exception as exc:  # noqa: BLE001 - rollout lookup must not hide legacy inbox
        _log.warning("exception inbox attendance source failed: %s", exc, exc_info=True)
        config = attendance_exceptions._safe_rollout_config()
        production_mode, _state_error = attendance_exceptions._production_mode_for_day(
            today,
            now_utc=_now_utc(),
            config=config,
        )
        snapshot = attendance_exceptions.AttendanceExceptionSnapshot(
            day=today,
            mode=config.mode,
            production_mode=production_mode,
            baseline_complete=False,
            fresh=False,
            complete=False,
            issues=(),
            source_errors=("Attendance Timeline",),
        )
    for source in snapshot.source_errors:
        if not any(error.get("source") == source for error in source_errors):
            source_errors.append({"source": source})
    return snapshot


def _timeline_owns_location(snapshot) -> bool:
    return bool(snapshot.baseline_complete and snapshot.mode in ("shadow", "live"))


def _attendance_issue_row(issue) -> dict:
    start_iso = issue.start_utc.isoformat()
    end_iso = issue.end_utc.isoformat() if issue.end_utc is not None else None
    raw_labels = list(issue.raw_work_center_labels)
    affected_workers = [
        {"employee_odoo_id": employee_id, "employee_name": name}
        for employee_id, name in issue.affected_workers
    ]
    label_by_kind = {
        "attendance_missing_location": "No Odoo work center",
        "attendance_unmapped_location": raw_labels[0] if raw_labels else "Unknown Odoo work center",
        "attendance_conflicting_location": "Overlapping Odoo work centers",
        "attendance_duplicate_location": "Duplicate Odoo work-center rows",
        "attendance_department_repair_failed": "Department repair failed",
        "attendance_source_stale": "Odoo location data is stale",
        "production_source_unavailable": "Production could not be matched safely",
        "production_unassigned_run": f"{issue.units:g} units"
        if issue.units is not None
        else "Unassigned production",
    }
    detail_parts = []
    if issue.app_work_center_name:
        detail_parts.append(issue.app_work_center_name)
    if start_iso:
        detail_parts.append(f"{start_iso} to {end_iso or 'open'}")
    if raw_labels and issue.kind != "attendance_unmapped_location":
        detail_parts.append(", ".join(raw_labels))
    if issue.sample_count is not None:
        detail_parts.append(_plural(issue.sample_count, "sample"))
    return {
        "name": issue.employee_name or issue.app_work_center_name or "Odoo attendance",
        "label": label_by_kind.get(issue.kind, issue.kind.replace("_", " ").title()),
        "detail": " · ".join(detail_parts),
        "priority": issue.priority,
        "badge": "Shadow comparison"
        if issue.comparison_only
        else ("Follow-up" if issue.priority == "muted" else "Needs decision"),
        "row_key": issue.item_key,
        "item_key": issue.item_key,
        "action": None,
        "kind": issue.kind,
        "employee_odoo_id": issue.employee_odoo_id,
        "employee_name": issue.employee_name,
        "attendance_ids": list(issue.attendance_ids),
        "start_utc": start_iso,
        "end_utc": end_iso,
        "raw_work_center_labels": raw_labels,
        "odoo_work_center_ids": list(issue.odoo_work_center_ids),
        "affected_workers": affected_workers,
        "app_work_center_name": issue.app_work_center_name,
        "units": issue.units,
        "sample_count": issue.sample_count,
        "reason": issue.reason,
        "comparison_only": issue.comparison_only,
        "target_odoo_department_id": issue.target_odoo_department_id,
    }


def _attendance_sections(snapshot) -> list[dict]:
    if not _timeline_owns_location(snapshot):
        return []
    sections = []
    for kind, (title, tone) in _ATTENDANCE_SECTION_META.items():
        rows = [_attendance_issue_row(issue) for issue in snapshot.issues_for(kind)]
        sections.append(
            {
                "id": kind,
                "title": title,
                "count": len(rows),
                "tone": tone,
                "action_key": None,
                "action_label": None,
                "empty": "All clear",
                "context": {},
                "complete": snapshot.complete,
                "rows": rows,
            }
        )
    return sections


def _queue_from_sections(sections: list[dict]) -> list[dict]:
    """Flatten section rows into one queue: urgency tier first (urgent → warn →
    info → muted/follow-up), preserving each section's order within a tier.
    Empty sections contribute nothing. Each row is tagged with its category."""
    tagged = []
    for section_order, section in enumerate(sections):
        for row_index, row in enumerate(section.get("rows") or []):
            tagged.append(
                (
                    _TIER_RANK.get(row.get("priority", "normal"), 2),
                    section_order,
                    row_index,
                    {
                        **row,
                        "section_id": section["id"],
                        "category_label": section["title"],
                        "tone": section["tone"],
                    },
                )
            )
    tagged.sort(key=lambda t: (t[0], t[1], t[2]))
    return [t[3] for t in tagged]


def build_summary() -> dict:
    from . import (
        auto_lunch_guard,
        machine_breakdown,
        missed_punch_out,
        missing_wc,
        odoo_sync,
        unexpected_worker,
    )
    from .routes import staffing as staffing_routes

    today = plant_day.today()
    source_errors: list[dict] = []
    attendance_snapshot = _attendance_snapshot(today, source_errors)
    strict_production = attendance_snapshot.production_mode == "strict"
    assignments = (
        {"count": 0, "items": [], "people": []}
        if strict_production
        else _capture(
            source_errors, "Assignments To Do", staffing_routes.assignments_todo_payload, {}
        )
    )
    late = _capture(source_errors, "Late / Absence", staffing_routes.late_report_payload, {})
    missing_rows = (
        []
        if _timeline_owns_location(attendance_snapshot)
        else _capture(source_errors, "Missing Work Center", missing_wc.current_rows, [])
    )
    missed_rows = _capture(source_errors, "Missed Punch Out", missed_punch_out.current_rows, [])
    breakdown_rows = _capture(
        source_errors, "Machine Breakdown", machine_breakdown.current_rows, []
    )
    unexpected_rows = _capture(
        source_errors, "Unexpected Workers", lambda: unexpected_worker.open_events(today), []
    )
    roster_sync_alert = _capture(
        source_errors, "Timeclock Roster", odoo_sync.roster_sync_alert, None
    )
    auto_lunch_alert = _auto_lunch_alert(
        source_errors,
        auto_lunch_guard.current_snapshot,
    )
    schedule_count = _capture(
        source_errors, "Plant Schedule", lambda: _plant_schedule_reminder()[0], 0
    )
    saturday_recruiting_count = _capture(
        source_errors, "Saturday Recruiting", lambda: _saturday_staffing_actions(today)[0], 0
    )
    pending_count, pending_urgent_count = _capture(
        source_errors, "Pending Time Off", lambda: _pending_time_off_counts(today), (0, 0)
    )

    assignment_count = int(assignments.get("count") or 0)
    late_count = int(late.get("count") or 0)
    missing_count = len(missing_rows)
    missed_count = len(missed_rows)
    breakdown_count = len(breakdown_rows)
    roster_sync_count = int(roster_sync_alert is not None)
    auto_lunch_count = int(auto_lunch_alert is not None)
    attendance_counts = (
        {kind: len(attendance_snapshot.issues_for(kind)) for kind in _ATTENDANCE_SECTION_META}
        if _timeline_owns_location(attendance_snapshot)
        else {}
    )
    attendance_urgent_count = sum(
        issue.priority == "urgent" for issue in attendance_snapshot.issues
    )
    attendance_follow_up_count = sum(
        issue.priority == "muted" for issue in attendance_snapshot.issues
    )
    urgent_total = (
        len(late.get("scheduled_late") or [])
        + len(late.get("unscheduled_late") or [])
        + missing_count
        + missed_count
        + len(unexpected_rows)
        + pending_urgent_count
        + sum(1 for r in breakdown_rows if r.get("priority") == "urgent")
        + roster_sync_count
        + auto_lunch_count
        + attendance_urgent_count
    )
    total = (
        assignment_count
        + schedule_count
        + saturday_recruiting_count
        + late_count
        + missing_count
        + missed_count
        + len(unexpected_rows)
        + pending_count
        + breakdown_count
        + roster_sync_count
        + auto_lunch_count
        + sum(attendance_counts.values())
    )
    summary = {
        "today": today.isoformat(),
        "generated_at": plant_day.now().strftime("%-I:%M %p"),
        "total": total,
        "urgent_total": urgent_total,
        "follow_up_total": len(late.get("snoozed") or []) + attendance_follow_up_count,
        "source_errors": source_errors,
        "sections": {
            "assignments": assignment_count,
            "plant_schedule": schedule_count,
            "saturday_recruiting": saturday_recruiting_count,
            "late": late_count,
            "missing_wc": missing_count,
            "missed_punch_out": missed_count,
            "unexpected_workers": len(unexpected_rows),
            "time_off": pending_count,
            "breakdown": breakdown_count,
            "odoo_roster_sync": roster_sync_count,
            "auto_lunch": auto_lunch_count,
            **attendance_counts,
        },
    }
    if _timeline_owns_location(attendance_snapshot):
        summary["sections"].pop("missing_wc", None)
    return summary


def build_snapshot() -> dict:
    from . import (
        auto_lunch_guard,
        machine_breakdown,
        missed_punch_out,
        missing_wc,
        odoo_sync,
        unexpected_worker,
    )
    from .routes import staffing as staffing_routes

    today = plant_day.today()
    source_errors: list[dict] = []
    attendance_snapshot = _attendance_snapshot(today, source_errors)
    strict_production = attendance_snapshot.production_mode == "strict"
    assignments = (
        {"count": 0, "items": [], "people": []}
        if strict_production
        else _capture(
            source_errors, "Assignments To Do", staffing_routes.assignments_todo_payload, {}
        )
    )
    late = _capture(source_errors, "Late / Absence", staffing_routes.late_report_payload, {})
    missing_rows = (
        []
        if _timeline_owns_location(attendance_snapshot)
        else _capture(source_errors, "Missing Work Center", missing_wc.current_rows, [])
    )
    missed_rows = _capture(source_errors, "Missed Punch Out", missed_punch_out.current_rows, [])
    breakdown_rows = _capture(
        source_errors, "Machine Breakdown", machine_breakdown.current_rows, []
    )
    unexpected_rows = _capture(
        source_errors,
        "Unexpected Workers",
        lambda: _unexpected_worker_rows(unexpected_worker.open_events(today)),
        [],
    )
    roster_sync_alert = _capture(
        source_errors, "Timeclock Roster", odoo_sync.roster_sync_alert, None
    )
    auto_lunch_alert = _auto_lunch_alert(
        source_errors,
        auto_lunch_guard.current_snapshot,
    )
    auto_lunch_count = int(auto_lunch_alert is not None)
    schedule_count, schedule_rows = _capture(
        source_errors, "Plant Schedule", _plant_schedule_reminder, (0, [])
    )
    saturday_recruiting_count, saturday_recruiting_rows = _capture(
        source_errors, "Saturday Recruiting", lambda: _saturday_staffing_actions(today), (0, [])
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
        late_rows.append(
            {
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
                    "scheduled_wc": item.get("scheduled_wc"),
                    "scheduled_start_time": item.get("scheduled_start_time"),
                },
            }
        )
    for item in late.get("unscheduled_late") or []:
        late_rows.append(
            {
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
            }
        )
    for item in late.get("snoozed") or []:
        mins = int(item.get("mins_remaining") or 0)
        late_rows.append(
            {
                "name": item.get("name"),
                "label": "Snoozed",
                "detail": f"Re-checks in {_plural(mins, 'min')}",
                "priority": "muted",
                "badge": "Follow-up",
                "row_key": _row_key("late_snoozed", item.get("emp_id"), item.get("until_iso")),
                "item_key": inbox_keys.late(item.get("emp_id"), today.isoformat()),
            }
        )
    sections = [
        {
            "id": "odoo_roster_sync",
            "title": "Timeclock Roster",
            "count": int(roster_sync_alert is not None),
            "tone": "bad",
            "action_key": None,
            "action_label": None,
            "href": "/staffing/skills",
            "empty": "All clear",
            "context": {},
            "rows": [
                {
                    "name": "Timeclock roster",
                    "label": "Odoo roster update blocked",
                    "detail": (
                        "Odoo sent an unsafe employee list. "
                        "The timeclock is using the last good update."
                    ),
                    "priority": "urgent",
                    "badge": "Roster",
                    "href": "/staffing/skills",
                    "row_key": _row_key("odoo_roster_sync", "active_status"),
                    "item_key": inbox_keys.odoo_roster_sync(),
                }
            ]
            if roster_sync_alert
            else [],
        },
        {
            "id": "auto_lunch",
            "title": "Auto-Lunch",
            "count": auto_lunch_count,
            "tone": "bad",
            "action_key": None,
            "action_label": None,
            "href": "/settings?section=timeclock#auto-lunch-form",
            "empty": "Live",
            "context": {},
            "rows": [auto_lunch_alert] if auto_lunch_alert else [],
        },
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
            "id": "saturday_recruiting",
            "title": "Saturday Recruiting",
            "count": saturday_recruiting_count,
            "tone": "warn",
            "action_key": None,
            "action_label": None,
            "href": saturday_recruiting_rows[0]["href"]
            if saturday_recruiting_rows
            else "/staffing",
            "empty": "All clear",
            "context": {},
            "rows": saturday_recruiting_rows,
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
            "id": "unexpected_workers",
            "title": "Unexpected Workers",
            "count": len(unexpected_rows),
            "tone": "bad",
            "action_key": None,
            "action_label": None,
            "href": f"/staffing?day={today.isoformat()}",
            "empty": "All clear",
            "context": {},
            "rows": unexpected_rows,
        },
        {
            "id": "breakdown",
            "title": "Machine Breakdown",
            "count": len(breakdown_rows),
            "tone": "bad",
            "action_key": "breakdown",
            "action_label": "Handle",
            "empty": "All clear",
            "context": {"work_centers": work_centers},
            "rows": breakdown_rows,
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
    if _timeline_owns_location(attendance_snapshot):
        sections = [section for section in sections if section["id"] != "missing_wc"]
        sections[2:2] = _attendance_sections(attendance_snapshot)
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
