"""Weekly monitor: diff the Odoo calendar-conflict set and keep one Odoo task.

Runs on the in-process warmer (see app.py), gated to ≥7 days via the
calendar_conflict_monitor state row. Best-effort — the warmer logs/swallows.
See docs/superpowers/specs/2026-06-29-calendar-conflict-monitor-design.md
"""

from __future__ import annotations

import html
import logging
from datetime import datetime, timedelta, UTC

from . import calendar_conflicts, db, odoo_client

_log = logging.getLogger(__name__)

THROTTLE = timedelta(days=7)
_TASK_NAME = "Odoo work-schedule conflicts"

# Log the "throttled" state at most once per process so a deploy's boot tick
# surfaces the persisted state (last run + task id) without spamming every 6h.
_throttle_logged = False


def decide(current_ids, reported_ids) -> dict:
    """Pure diff of the conflict employee-id sets.

    Returns {changed, added (sorted ids), removed (sorted ids), now_empty}.
    """
    current = set(current_ids)
    reported = set(reported_ids)
    added = sorted(current - reported)
    removed = sorted(reported - current)
    return {
        "changed": bool(added or removed),
        "added": added,
        "removed": removed,
        # Only meaningful when changed; run_once reads it solely inside the
        # `if changed` branch. "changed AND current set empty" → archive.
        "now_empty": bool(added or removed) and len(current) == 0,
    }


def _load_state() -> dict:
    rows = db.query(
        "SELECT odoo_task_id, reported_emp_ids, last_run_at "
        "FROM calendar_conflict_monitor WHERE id = 1"
    )
    if not rows:
        return {"odoo_task_id": None, "reported_emp_ids": [], "last_run_at": None}
    r = rows[0]
    return {
        "odoo_task_id": r["odoo_task_id"],
        "reported_emp_ids": list(r["reported_emp_ids"] or []),
        "last_run_at": r["last_run_at"],
    }


def _save_state(odoo_task_id, reported_emp_ids, last_run_at) -> None:
    db.execute(
        "INSERT INTO calendar_conflict_monitor (id, odoo_task_id, reported_emp_ids, last_run_at) "
        "VALUES (1, %s, %s, %s) "
        "ON CONFLICT (id) DO UPDATE SET "
        "  odoo_task_id = EXCLUDED.odoo_task_id, "
        "  reported_emp_ids = EXCLUDED.reported_emp_ids, "
        "  last_run_at = EXCLUDED.last_run_at",
        (odoo_task_id, sorted(reported_emp_ids), last_run_at),
    )


def _build_task_body(conflicts) -> str:
    rows = sorted(conflicts, key=lambda c: c["name"].lower())
    items = []
    for c in rows:
        name = html.escape(str(c["name"]))
        cal = html.escape(str(c["cal_name"]))
        if c["verdict"] == "missing_days":
            detail = f"calendar {cal} missing {calendar_conflicts.fmt_days(c['missing'])}"
        elif c["verdict"] == "flexible":
            detail = f"calendar {cal} is flexible / has no fixed hours"
        else:
            detail = "no Odoo work schedule"
        items.append(f"<li>{name} (id {c['odoo_id']}) — {detail}</li>")
    return (
        "<p>These employees' Odoo work schedule has no working hours on a plant "
        "workday, so declaring them absent can't sync to Odoo Time Off. Fix each "
        "one's Working Schedule in Odoo.</p><ul>" + "".join(items) + "</ul>"
    )


def _summary_comment(decision, names_by_id) -> str:
    parts = []
    if decision["added"]:
        parts.append("New conflicts: " + ", ".join(html.escape(names_by_id.get(i, f"id {i}")) for i in decision["added"]))
    if decision["removed"]:
        parts.append("Resolved: " + ", ".join(f"id {i}" for i in decision["removed"]))
    return "; ".join(parts) or "Updated."


def run_once(force: bool = False) -> dict:
    """Weekly check. Best-effort; raises propagate to the warmer (logged/swallowed)."""
    global _throttle_logged
    state = _load_state()
    now = datetime.now(UTC)
    if not force and state["last_run_at"] and (now - state["last_run_at"]) < THROTTLE:
        if not _throttle_logged:
            _log.warning(
                "calendar-conflict monitor: throttled — last run %s, task=%s, %d reported",
                state["last_run_at"], state["odoo_task_id"], len(state["reported_emp_ids"]),
            )
            _throttle_logged = True
        return {"skipped": "throttled"}

    conflicts = calendar_conflicts.current_conflicts()
    names_by_id = {int(c["odoo_id"]): c["name"] for c in conflicts if c.get("odoo_id") is not None}
    current_ids = set(names_by_id)
    reported = set(state["reported_emp_ids"])
    decision = decide(current_ids, reported)
    task_id = state["odoo_task_id"]

    if decision["changed"]:
        if decision["now_empty"]:
            if task_id:
                odoo_client.post_task_message(task_id, "✅ All Odoo work-schedule conflicts resolved.")
                odoo_client.update_task(task_id, active=False)
            task_id = None
        else:
            if task_id:
                try:
                    odoo_client.update_task(task_id, description=_build_task_body(conflicts))
                except Exception:  # noqa: BLE001 -- stored task may have been deleted in Odoo
                    _log.warning(
                        "calendar-conflict task %s update failed; creating a fresh task",
                        task_id, exc_info=True,
                    )
                    task_id = None
            if not task_id:
                task_id = odoo_client.create_feedback_task(
                    project_id=odoo_client.ensure_feedback_project(),
                    name=_TASK_NAME,
                    description_html=_build_task_body(conflicts),
                    assignee_uid=odoo_client.authenticate(),
                    tag_id=None,
                    deadline=(now.date() + timedelta(days=7)).isoformat(),
                )
            odoo_client.post_task_message(task_id, _summary_comment(decision, names_by_id))

    _save_state(odoo_task_id=task_id, reported_emp_ids=current_ids, last_run_at=now)
    # WARNING level so this weekly heartbeat is visible in prod logs (the app
    # sets no logging config, so module-level INFO is dropped by lastResort).
    _log.warning(
        "calendar-conflict monitor: %d conflict(s), changed=%s, task=%s",
        len(current_ids), decision["changed"], task_id,
    )
    return {"changed": decision["changed"], "now_empty": decision["now_empty"], "task_id": task_id, "count": len(current_ids)}
