"""Keep one Odoo task in sync with payroll Work Entries needing review."""

from __future__ import annotations

import html
import logging
from datetime import UTC, datetime, timedelta

from . import odoo_client, payroll_work_entry_store as store
from .payroll_work_entry_rules import Decision


_log = logging.getLogger(__name__)

_TASK_NAME = "Payroll work entries need review"
_REASON_TEXT = {
    "ambiguous_regular_entries": "more than one regular Work Entry",
    "regular_not_attendance_linked": "regular Work Entry is not linked to Attendance",
    "attendance_overtime_not_positive": "approved Attendance overtime is not positive",
    "unapproved_overtime": "Attendance overtime is not approved",
    "attendance_overtime_mismatch": "Attendance worked and approved overtime disagree",
    "payroll_overtime_mismatch": "Payroll and Attendance overtime disagree",
    "regular_excess_not_half_hour": (
        "regular difference is not the known 30-minute defect"
    ),
    "non_draft_work_entry": "one or more Work Entries are no longer draft",
    "conflicting_work_entry": "Odoo marks a Work Entry as conflicting",
    "negative_target": "the safe correction would be negative",
    "fresh_state_changed": "the Work Entry changed before correction",
    "write_failed": "Odoo refused the correction",
    "verification_failed": "Odoo did not retain the correction",
    "audit_failed": "the correction succeeded but its local audit failed",
    "missing_candidate_group": "the recent candidate was absent from the batch reread",
    "fresh_read_failed": "Plant Manager could not reread the Work Entry",
    "invalid_numeric_data": "some hour details are missing or are not real numbers",
}


def _escape(value: object) -> str:
    return html.escape(str(value), quote=True)


def _issue_sort_key(decision: Decision) -> tuple[str, object, str, str]:
    return (
        decision.employee_name.lower(),
        decision.work_date,
        decision.issue_key,
        decision.employee_name,
    )


def _unique_issues(issues: list[Decision]) -> list[Decision]:
    unique: dict[str, Decision] = {}
    for decision in sorted(issues, key=_issue_sort_key):
        unique.setdefault(decision.issue_key, decision)
    return sorted(unique.values(), key=_issue_sort_key)


def _build_task_body(issues: list[Decision]) -> str:
    """Render deterministic, escaped review details for the singleton task."""
    items = []
    for decision in _unique_issues(issues):
        explanations = ", ".join(
            _escape(_REASON_TEXT.get(code, code)) for code in decision.reason_codes
        )
        items.append(
            "<li>"
            f"<strong>{_escape(decision.employee_name)}</strong> — "
            f"{_escape(decision.work_date.isoformat())}<br>"
            f"Attendance regular: {_escape(f'{decision.attendance_regular:.4f}')} · "
            f"Payroll regular: {_escape(f'{decision.work_regular:.4f}')}<br>"
            f"Attendance overtime: {_escape(f'{decision.attendance_overtime:.4f}')} · "
            f"Payroll overtime: {_escape(f'{decision.work_overtime:.4f}')}<br>"
            f"Work Entry: {_escape(decision.work_entry_id)} · "
            f"Reason: {explanations}"
            "</li>"
        )
    return (
        "<p>Plant Manager found payroll Work Entries that need a person to review "
        "them. No automatic change was made.</p><ul>"
        + "".join(items)
        + "</ul>"
    )


def _create_task(issues: list[Decision], now: datetime) -> int:
    return odoo_client.create_feedback_task(
        project_id=odoo_client.ensure_feedback_project(),
        name=_TASK_NAME,
        description_html=_build_task_body(issues),
        assignee_uid=odoo_client.authenticate(),
        tag_id=None,
        deadline=(now.date() + timedelta(days=7)).isoformat(),
    )


def _validate_issues(issues: list[Decision]) -> None:
    if any(not isinstance(item, Decision) or item.kind != "review" for item in issues):
        raise ValueError("issues must contain only review Decision objects")


def sync_review_task(
    issues: list[Decision], now: datetime | None = None
) -> dict[str, bool | int | None]:
    """Synchronize the complete payroll review set to one Odoo task."""
    _validate_issues(issues)
    current_time = now or datetime.now(UTC)
    current_issues = _unique_issues(issues)
    current_keys = sorted({decision.issue_key for decision in current_issues})

    with store.monitor_lock():
        state = store.load_monitor_state()
        previous_keys = sorted(set(state["reported_issue_keys"]))
        task_id = state["odoo_task_id"]
        issue_set_changed = current_keys != previous_keys

        if not current_issues:
            if task_id is None and not issue_set_changed:
                return {"changed": False, "task_id": None, "count": 0}
            if task_id is not None:
                odoo_client.post_task_message(
                    task_id, "✅ All payroll Work Entry review items resolved."
                )
                odoo_client.update_task(task_id, active=False)
            store.save_monitor_state(None, [], current_time)
            return {"changed": True, "task_id": None, "count": 0}

        if not issue_set_changed and task_id is not None:
            if task_id in odoo_client.fetch_task_stage_names([task_id]):
                return {
                    "changed": False,
                    "task_id": task_id,
                    "count": len(current_issues),
                }
            task_id = None

        if issue_set_changed and task_id is not None:
            try:
                odoo_client.update_task(
                    task_id, description=_build_task_body(current_issues)
                )
            except Exception:  # noqa: BLE001 -- the stored Odoo task may be gone
                _log.warning(
                    "payroll review task %s update failed; creating a fresh task",
                    task_id,
                    exc_info=True,
                )
                task_id = None

        if task_id is None:
            task_id = _create_task(current_issues, current_time)

        odoo_client.post_task_message(
            task_id,
            f"Payroll review list updated: {len(current_issues)} item(s) need review.",
        )
        store.save_monitor_state(task_id, current_keys, current_time)
        return {
            "changed": True,
            "task_id": task_id,
            "count": len(current_issues),
        }
