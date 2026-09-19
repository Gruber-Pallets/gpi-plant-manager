"""Exception Inbox rows for quick-punch fixer jobs that need a look.

Failed fixer jobs from the last 14 days, and active jobs that already wrote
to Odoo and are still retrying, surface until a manager marks them checked.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
import json
import logging

from . import attendance_corrections, inbox_keys, inbox_log

log = logging.getLogger(__name__)

_LOOKBACK = timedelta(days=14)
_STUCK_AFTER = timedelta(minutes=30)
_ACK_ACTION = "quick_punch_failure_ack"
_FAILED_ACTION = "quick_punch_failed"


def _summary(row: Mapping[str, object]) -> dict[str, str | None]:
    value = row.get("audit_summary")
    if isinstance(value, (str, bytes, bytearray)):
        try:
            value = json.loads(value)
        except ValueError:
            value = None
    if not isinstance(value, Mapping):
        return {"person_name": None, "before": None, "after": None}
    return {
        "person_name": value.get("person_name") if isinstance(value.get("person_name"), str) else None,
        "before": value.get("before") if isinstance(value.get("before"), str) else None,
        "after": value.get("after") if isinstance(value.get("after"), str) else None,
    }


def _detail_mapping(value: object) -> dict[str, object]:
    if isinstance(value, (str, bytes, bytearray)):
        try:
            value = json.loads(value)
        except ValueError:
            return {}
    return dict(value) if isinstance(value, Mapping) else {}


def reason_text(detail: Mapping[str, object], *, stuck: bool = False) -> str:
    """Plain-language reason for the inbox row."""
    if stuck:
        return "Odoo partly changed, still retrying"
    code = str(detail.get("reason_code") or detail.get("outcome") or "")
    parts: list[str] = []
    completed = detail.get("completed_operations")
    if isinstance(completed, int) and completed > 0:
        parts.append("Odoo was partly changed")
    elif detail.get("uncertain_operation") is True:
        parts.append("Odoo may have been changed")
    if code == "attempt_limit":
        parts.append("Odoo kept refusing the change")
    elif "source_changed" in code:
        parts.append("Someone changed these punches in Odoo first")
    if not parts:
        return "Quick-punch fix couldn't finish — check Odoo"
    return " — ".join(parts)


def _row_for(
    job: Mapping[str, object],
    *,
    person_name: str | None,
    before: str | None,
    detail: Mapping[str, object],
    stuck: bool,
) -> dict[str, object]:
    job_id = int(job["id"])
    alert_key = inbox_keys.quick_punch_alert(job_id)
    reason = reason_text(detail, stuck=stuck)
    name = person_name or "Unknown"
    label = (
        "Odoo partly changed, still retrying"
        if stuck
        else "Quick-punch fix couldn't finish — check Odoo"
    )
    detail_text = " · ".join(part for part in (before, reason) if part)
    return {
        "name": name,
        "label": label,
        "detail": detail_text,
        "priority": "urgent",
        "badge": "Odoo",
        "href": "/exceptions",
        "row_key": alert_key,
        "item_key": alert_key,
        "action": {"type": "quick_punch_ack", "job_id": job_id},
    }


def _acked_keys(alert_keys: Sequence[str]) -> set[str]:
    return inbox_log.item_keys_with_action(alert_keys, _ACK_ACTION)


def _failure_events(item_keys: Sequence[str]) -> dict[str, dict[str, object]]:
    if not item_keys:
        return {}
    from . import db

    rows = db.query(
        "SELECT DISTINCT ON (item_key) item_key, person_name, before_value, "
        "outcome, detail FROM inbox_events "
        "WHERE action = %s AND item_key = ANY(%s) "
        "ORDER BY item_key, resolved_at DESC",
        (_FAILED_ACTION, list(item_keys)),
    )
    return {str(row["item_key"]): row for row in rows if row.get("item_key")}


def _job_rows(db, job_sql: str, since: datetime, now: datetime):
    failed = db.query(
        "SELECT id, item_key, status, attempt_count, created_at, updated_at, "
        "completed_operations, audit_summary FROM attendance_correction_jobs "
        f"WHERE {job_sql} AND status = 'failed' "
        "AND COALESCE(completed_at, updated_at, created_at) >= %s "
        "ORDER BY id DESC",
        (since,),
    )
    stuck = db.query(
        "SELECT id, item_key, status, attempt_count, created_at, updated_at, "
        "completed_operations, audit_summary FROM attendance_correction_jobs "
        f"WHERE {job_sql} "
        "AND status IN ('planned','applying','verifying','recalculating') "
        "AND (attempt_count >= %s OR created_at <= %s) "
        "ORDER BY id DESC",
        (attendance_corrections.QUICK_PUNCH_MAX_ATTEMPTS, now - _STUCK_AFTER),
    )
    return failed, stuck


def current_rows(*, now_utc: datetime | None = None) -> list[dict[str, object]]:
    """Urgent inbox rows for failed and stuck-partial fixer jobs."""
    from . import db

    now = now_utc or datetime.now(UTC)
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    since = now - _LOOKBACK
    job_sql = attendance_corrections.QUICK_PUNCH_JOB_SQL
    try:
        failed, stuck = _job_rows(db, job_sql, since, now)
        events = _failure_events([str(row["item_key"]) for row in failed if row.get("item_key")])
        candidates: list[tuple[int, dict[str, object]]] = []
        for job in failed:
            summary = _summary(job)
            event = events.get(str(job.get("item_key") or ""), {})
            detail = _detail_mapping(event.get("detail"))
            if event.get("outcome") and "reason_code" not in detail:
                detail["reason_code"] = event["outcome"]
            person = event.get("person_name") or summary["person_name"]
            before = event.get("before_value") or summary["before"]
            candidates.append(
                (
                    int(job["id"]),
                    _row_for(job, person_name=person, before=before, detail=detail, stuck=False),
                )
            )
        for job in stuck:
            if not attendance_corrections._completed_operation_records(job):
                continue
            summary = _summary(job)
            candidates.append(
                (
                    int(job["id"]),
                    _row_for(
                        job,
                        person_name=summary["person_name"],
                        before=summary["before"],
                        detail={"completed_operations": 1},
                        stuck=True,
                    ),
                )
            )
        acked = _acked_keys([row["item_key"] for _job_id, row in candidates])
        return [row for _job_id, row in candidates if row["item_key"] not in acked]
    except Exception as exc:
        if "DATABASE_URL" not in str(exc):
            log.warning("quick-punch inbox could not read fixer jobs", exc_info=True)
        return []


def acknowledge(
    job_id: int,
    *,
    actor_upn: str | None,
    actor_name: str | None,
) -> int:
    """Record that a manager checked a fixer failure. Returns the event id."""
    item_key = inbox_keys.quick_punch_alert(job_id)
    return inbox_log.record_event(
        item_kind=attendance_corrections.QUICK_PUNCH_ITEM_KIND,
        item_key=item_key,
        person_name=None,
        category_label=attendance_corrections.QUICK_PUNCH_CATEGORY_LABEL,
        action=_ACK_ACTION,
        outcome="Checked",
        actor_upn=actor_upn,
        actor_name=actor_name,
        source="inbox",
        detail={"job_id": job_id},
    )
