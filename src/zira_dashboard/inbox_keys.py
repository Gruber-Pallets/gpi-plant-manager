"""Canonical Exception Inbox item keys.

One identity per inbox item, shared by the snapshot rows (exception_inbox) and
the resolve handlers (routes/*) so a logged inbox_events row correlates to the
open item it resolved. Keep these stable: the Phase 4 reconciler joins the open
set to the event log on this key, and the Phase 2b client diffs queue rows
against archived events by it.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime


def time_off(request_id) -> str:
    return f"time_off:{request_id}"


def missing_wc(attendance_id) -> str:
    return f"missing_wc:{attendance_id}"


def missed_punch_out(attendance_id) -> str:
    return f"missed_punch_out:{attendance_id}"


def late(emp_id, day) -> str:
    """`day` is an ISO date string (the plant day)."""
    return f"late:{emp_id}:{day}"


def assignment(wc_name, start_iso) -> str:
    return f"assignment:{wc_name}:{start_iso}"


def plant_schedule(day) -> str:
    """`day` is an ISO date string."""
    return f"plant_schedule:{day}"


def saturday_recruitment(day) -> str:
    """Identity for a closed Saturday recruiting round awaiting preparation."""
    return f"saturday_recruitment:{day}"


def unexpected_worker(day, person_odoo_id) -> str:
    """Identity for a worker clocking in while on approved leave."""
    return f"unexpected_worker:{day}:{person_odoo_id}"


def breakdown(
    wc_name,
    stop_iso,
    person_name=None,
    employee_odoo_id: int | None = None,
) -> str:
    """The incident's own key when person_name is None (the card header /
    dismiss target); a distinct per-operator key otherwise (the Transfer /
    snooze / auto-resolve target for one operator's row)."""
    if employee_odoo_id is not None:
        return f"breakdown:{wc_name}:{stop_iso}:odoo:{int(employee_odoo_id)}"
    if person_name:
        return f"breakdown:{wc_name}:{stop_iso}:{person_name}"
    return f"breakdown:{wc_name}:{stop_iso}"


def odoo_roster_sync() -> str:
    """Identity for an Odoo payload rejected before it can hide the roster."""
    return "odoo_roster_sync:active_status"


def auto_lunch_setting() -> str:
    """Identity for the singleton Auto-Lunch non-Live warning."""
    return "auto_lunch:setting"


def attendance_issue_key(
    kind: str,
    employee_odoo_id: int,
    attendance_ids: Sequence[int],
    start_utc: datetime,
) -> str:
    """Identity for one timeline issue, excluding its moving end time."""
    ids = ",".join(str(value) for value in sorted(attendance_ids))
    return f"{kind}:{employee_odoo_id}:{ids}:{start_utc.isoformat()}"


def production_run_key(wc_name: str, start_utc: datetime) -> str:
    """Identity for one distinct uncovered production run."""
    return f"production_unassigned_run:{wc_name}:{start_utc.isoformat()}"


def production_source_unavailable(day) -> str:
    """Identity for one plant day's strict production-source failure."""
    return f"production_source_unavailable:{day.isoformat()}"


def attendance_source_stale_key() -> str:
    """Singleton identity for freshness of the shared attendance mirror."""
    return "attendance_source_stale:odoo_attendance_mirror"


def attendance_cutover_blocked(cutover_at: datetime) -> str:
    """Stable urgent rollout failure identity from the scheduled UTC boundary."""
    if not isinstance(cutover_at, datetime) or cutover_at.utcoffset() is None:
        raise ValueError("cutover_at must be timezone-aware")
    return f"attendance_cutover_blocked:{cutover_at.astimezone(UTC).isoformat()}"
