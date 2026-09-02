"""Deliver claimed local-feedback records as app-owner Odoo tasks."""

from __future__ import annotations

import html
import logging
import os
import socket
import xmlrpc.client
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from . import feedback_task_delivery as task_delivery
from . import odoo_client
from .feedback_content import safe_page_url
from .feedback_types import feedback_type
from .plant_day import today as _local_today


_log = logging.getLogger(__name__)
_RECOVERABLE_ODOO_ERRORS = (
    odoo_client.OdooConfigError,
    odoo_client.OdooAuthError,
    TimeoutError,
    ConnectionError,
    OSError,
    xmlrpc.client.Error,
)
_AMBIGUOUS_WRITE_ERRORS = (
    TimeoutError,
    ConnectionError,
    OSError,
    xmlrpc.client.Error,
)
_OUTCOMES = frozenset({"delivered", "retried", "blocked", "isolated_error"})
_SOURCE_APP = "GPI Plant Manager (plant)"
_DUPLICATE_TASK_REASON = "More than one matching owner task exists."
_DUPLICATE_SCREENSHOT_REASON = "More than one matching owner screenshot exists."
_TASK_IDENTITY_REASON = "The stored owner task does not match this feedback."
_TASK_STAGE_REASON = "The owner task stage is missing or ambiguous."
_TASK_NOTE_REASON = "More than one matching owner task result note exists."
_TASK_STAGE_BY_STATUS = {
    "requested": "New",
    "in_progress": "In Progress",
    "completed": "Done",
    "declined": "Done",
}


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _time_source(
    now: datetime | None,
    clock: Callable[[], datetime] | None,
) -> Callable[[], datetime]:
    if clock is not None:
        return clock
    if now is not None:
        return lambda: now
    return _utc_now


@dataclass(frozen=True)
class BatchResult:
    attempted: int = 0
    delivered: int = 0
    retried: int = 0
    blocked: int = 0
    isolated_errors: int = 0

    @classmethod
    def from_outcomes(cls, outcomes: list[str]) -> BatchResult:
        if type(outcomes) is not list or any(outcome not in _OUTCOMES for outcome in outcomes):
            raise ValueError("batch outcomes are malformed")
        return cls(
            attempted=len(outcomes),
            delivered=outcomes.count("delivered"),
            retried=outcomes.count("retried"),
            blocked=outcomes.count("blocked"),
            isolated_errors=outcomes.count("isolated_error"),
        )


def task_name(snapshot: task_delivery.FeedbackTaskSnapshot) -> str:
    label = feedback_type(snapshot.task_type).label
    lines = snapshot.message.strip().splitlines()
    first = lines[0] if lines else "feedback"
    if len(first) > 70:
        first = first[:69].rstrip() + "…"
    return f"[GPI-PM-FB-{snapshot.feedback_id}] [{label}] {first}"


def before_attachment_name(feedback_id: int) -> str:
    return f"GPI-PM-FB-{feedback_id}-before.jpg"


def task_description(snapshot: task_delivery.FeedbackTaskSnapshot) -> str:
    """Build escaped Odoo HTML from the immutable local feedback snapshot."""
    body = html.escape(snapshot.message.strip()).replace("\n", "<br>")
    submitter = html.escape(snapshot.submitter or "unknown")
    meta = [
        f"Source app: {_SOURCE_APP}",
        f"Submitted by {submitter}",
        f"Feedback ID: {snapshot.feedback_id}",
    ]
    page_url = safe_page_url(snapshot.page_url)
    if page_url:
        escaped_url = html.escape(page_url, quote=True)
        meta.append(f'Page: <a href="{escaped_url}">{escaped_url}</a>')
    return f"<p>{body}</p><p><small>{' · '.join(meta)}</small></p>"


def task_stage_for(status: str) -> str:
    try:
        return _TASK_STAGE_BY_STATUS[status]
    except (KeyError, TypeError):
        raise ValueError("unsupported feedback lifecycle status") from None


def terminal_note_marker(feedback_id: int, version: int) -> str:
    if type(feedback_id) is not int or feedback_id <= 0:
        raise ValueError("feedback id must be positive")
    if type(version) is not int or version <= 0:
        raise ValueError("feedback version must be positive")
    return f"GPI-PM-FB-{feedback_id}:v{version}"


def terminal_note_html(snapshot: task_delivery.FeedbackTaskSnapshot) -> str:
    if snapshot.status not in {"completed", "declined"}:
        raise ValueError("terminal task note requires terminal feedback")
    note = (snapshot.resolution_note or "").strip()
    if not note:
        raise ValueError("terminal task note is missing")
    label = "Completed" if snapshot.status == "completed" else "Declined"
    marker = terminal_note_marker(snapshot.feedback_id, snapshot.projection_version)
    return (
        f"<p><strong>{label}:</strong> {html.escape(note)}</p>"
        f"<p><small>{marker}</small></p>"
    )


def _retry(claim: task_delivery.TaskDeliveryClaim, clock: Callable[[], datetime]) -> str:
    task_delivery.schedule_retry(claim, now=clock())
    return "retried"


def _block(
    claim: task_delivery.TaskDeliveryClaim,
    reason: str,
    clock: Callable[[], datetime],
) -> str:
    task_delivery.block(claim, reason, now=clock())
    return "blocked"


def _record_task_match(
    claim: task_delivery.TaskDeliveryClaim,
    task_ids: list[int],
    clock: Callable[[], datetime],
) -> task_delivery.TaskDeliveryClaim | str | None:
    if len(task_ids) > 1:
        return _block(claim, _DUPLICATE_TASK_REASON, clock)
    if task_ids:
        return task_delivery.record_task_id(claim, task_id=task_ids[0], now=clock())
    return None


def _record_attachment_match(
    claim: task_delivery.TaskDeliveryClaim,
    attachment_ids: list[int],
    clock: Callable[[], datetime],
) -> task_delivery.TaskDeliveryClaim | str | None:
    if len(attachment_ids) > 1:
        return _block(claim, _DUPLICATE_SCREENSHOT_REASON, clock)
    if attachment_ids:
        return task_delivery.record_before_attachment(
            claim, attachment_id=attachment_ids[0], now=clock()
        )
    return None


def _resolve_uncertain_task_create(
    claim: task_delivery.TaskDeliveryClaim,
    *,
    project_id: int,
    name: str,
    clock: Callable[[], datetime],
) -> task_delivery.TaskDeliveryClaim | str:
    try:
        task_ids = odoo_client.find_feedback_task_ids(project_id, name)
        matched = _record_task_match(claim, task_ids, clock)
    except _RECOVERABLE_ODOO_ERRORS:
        _log.exception("could not recheck owner task after uncertain create")
        return _retry(claim, clock)
    if matched is None:
        return _retry(claim, clock)
    return matched


def _deliver_task(
    claim: task_delivery.TaskDeliveryClaim,
    snapshot: task_delivery.FeedbackTaskSnapshot,
    clock: Callable[[], datetime],
) -> task_delivery.TaskDeliveryClaim | str:
    if claim.task_id is not None:
        return claim

    name = task_name(snapshot)
    try:
        project_id = odoo_client.ensure_feedback_project()
        task_ids = odoo_client.find_feedback_task_ids(project_id, name)
    except _RECOVERABLE_ODOO_ERRORS:
        _log.exception("could not look up owner task for feedback %s", claim.feedback_id)
        return _retry(claim, clock)
    matched = _record_task_match(claim, task_ids, clock)
    if matched is not None:
        return matched

    try:
        assignee_uid = odoo_client.authenticate()
        tag_id = odoo_client.ensure_feedback_tag(feedback_type(snapshot.task_type).label)
    except _RECOVERABLE_ODOO_ERRORS:
        _log.exception("could not prepare owner task for feedback %s", claim.feedback_id)
        return _retry(claim, clock)

    claim = task_delivery.renew_claim(claim, now=clock())
    try:
        task_id = odoo_client.create_feedback_task(
            project_id=project_id,
            name=name,
            description_html=task_description(snapshot),
            assignee_uid=assignee_uid,
            tag_id=tag_id,
            deadline=_local_today().isoformat(),
        )
    except _AMBIGUOUS_WRITE_ERRORS:
        _log.exception("uncertain owner task create for feedback %s", claim.feedback_id)
        return _resolve_uncertain_task_create(
            claim, project_id=project_id, name=name, clock=clock
        )
    except _RECOVERABLE_ODOO_ERRORS:
        _log.exception("could not deliver owner task for feedback %s", claim.feedback_id)
        return _retry(claim, clock)
    return task_delivery.record_task_id(claim, task_id=task_id, now=clock())


def _deliver_before_attachment(
    claim: task_delivery.TaskDeliveryClaim,
    snapshot: task_delivery.FeedbackTaskSnapshot,
    clock: Callable[[], datetime],
) -> task_delivery.TaskDeliveryClaim | str:
    if snapshot.before_image is None or claim.before_attachment_id is not None:
        return claim
    if claim.task_id is None:
        raise RuntimeError("before attachment has no durable owner task")

    filename = before_attachment_name(claim.feedback_id)
    try:
        attachment_ids = odoo_client.find_feedback_attachment_ids(claim.task_id, filename)
    except _RECOVERABLE_ODOO_ERRORS:
        _log.exception("could not look up owner-task image attachment")
        return _retry(claim, clock)
    matched = _record_attachment_match(claim, attachment_ids, clock)
    if matched is not None:
        return matched

    claim = task_delivery.renew_claim(claim, now=clock())
    try:
        attachment_id = odoo_client.add_task_attachment(
            claim.task_id, filename, "image/jpeg", snapshot.before_image.jpeg_bytes
        )
    except _AMBIGUOUS_WRITE_ERRORS:
        _log.exception("uncertain owner-task image attachment for feedback %s", claim.feedback_id)
        try:
            attachment_ids = odoo_client.find_feedback_attachment_ids(claim.task_id, filename)
            matched = _record_attachment_match(claim, attachment_ids, clock)
        except _RECOVERABLE_ODOO_ERRORS:
            _log.exception("could not recheck owner-task image attachment")
            return _retry(claim, clock)
        if matched is None:
            return _retry(claim, clock)
        return matched
    except _RECOVERABLE_ODOO_ERRORS:
        _log.exception("could not deliver owner-task image attachment")
        return _retry(claim, clock)
    return task_delivery.record_before_attachment(claim, attachment_id=attachment_id, now=clock())


def _task_identity_matches(
    remote: object,
    *,
    task_id: int,
    project_id: int,
    name: str,
) -> bool:
    return (
        isinstance(remote, dict)
        and remote.get("id") == task_id
        and remote.get("project_id") == project_id
        and remote.get("name") == name
        and remote.get("active") is True
    )


def _reconcile_task_lifecycle(
    claim: task_delivery.TaskDeliveryClaim,
    snapshot: task_delivery.FeedbackTaskSnapshot,
    clock: Callable[[], datetime],
) -> str:
    if claim.task_id is None:
        raise RuntimeError("task lifecycle has no durable owner task")
    if (
        claim.desired_version != snapshot.projection_version
        or claim.desired_status != snapshot.status
    ):
        return _retry(claim, clock)
    target_stage = task_stage_for(snapshot.status)
    name = task_name(snapshot)
    try:
        project_ids = odoo_client.find_active_feedback_project_ids()
        if len(project_ids) != 1:
            return _block(claim, _TASK_IDENTITY_REASON, clock)
        project_id = project_ids[0]
        stage_ids = odoo_client.find_feedback_stage_ids(project_id, target_stage)
        if len(stage_ids) != 1:
            return _block(claim, _TASK_STAGE_REASON, clock)
        remote = odoo_client.read_feedback_task(claim.task_id)
    except _RECOVERABLE_ODOO_ERRORS:
        return _retry(claim, clock)
    except (ValueError, odoo_client.OdooTaskPayloadError):
        return _block(claim, _TASK_IDENTITY_REASON, clock)
    if not _task_identity_matches(
        remote, task_id=claim.task_id, project_id=project_id, name=name
    ):
        return _block(claim, _TASK_IDENTITY_REASON, clock)

    stage_id = stage_ids[0]
    if remote.get("stage_id") != stage_id or remote.get("stage_name") != target_stage:
        claim = task_delivery.renew_claim(claim, now=clock())
        try:
            odoo_client.update_task(claim.task_id, stage_id=stage_id)
        except _RECOVERABLE_ODOO_ERRORS:
            return _retry(claim, clock)

    marker: str | None = None
    if snapshot.status in {"completed", "declined"}:
        marker = terminal_note_marker(snapshot.feedback_id, snapshot.projection_version)
        try:
            message_ids = odoo_client.find_task_message_ids(claim.task_id, marker)
        except _RECOVERABLE_ODOO_ERRORS:
            return _retry(claim, clock)
        if len(message_ids) > 1:
            return _block(claim, _TASK_NOTE_REASON, clock)
        if not message_ids:
            claim = task_delivery.renew_claim(claim, now=clock())
            try:
                odoo_client.post_task_message(claim.task_id, terminal_note_html(snapshot))
            except _RECOVERABLE_ODOO_ERRORS:
                try:
                    recovered = odoo_client.find_task_message_ids(claim.task_id, marker)
                except _RECOVERABLE_ODOO_ERRORS:
                    return _retry(claim, clock)
                if len(recovered) > 1:
                    return _block(claim, _TASK_NOTE_REASON, clock)
                if not recovered:
                    return _retry(claim, clock)

    try:
        verified = odoo_client.read_feedback_task(claim.task_id)
        message_ids = (
            odoo_client.find_task_message_ids(claim.task_id, marker)
            if marker is not None
            else []
        )
    except _RECOVERABLE_ODOO_ERRORS:
        return _retry(claim, clock)
    if marker is not None and len(message_ids) > 1:
        return _block(claim, _TASK_NOTE_REASON, clock)
    if (
        not _task_identity_matches(
            verified, task_id=claim.task_id, project_id=project_id, name=name
        )
        or verified.get("stage_id") != stage_id
        or verified.get("stage_name") != target_stage
        or marker is not None and not message_ids
    ):
        return _retry(claim, clock)
    task_delivery.mark_delivered(claim, now=clock())
    return "delivered"


def process_claim(
    claim: task_delivery.TaskDeliveryClaim,
    *,
    now: datetime | None = None,
    clock: Callable[[], datetime] | None = None,
) -> str:
    """Create or adopt one exact owner task and its optional before image."""
    time_source = _time_source(now, clock)
    try:
        snapshot = task_delivery.load_snapshot(claim.feedback_id)
    except task_delivery.SnapshotValidationError:
        _log.exception("could not load local feedback snapshot for owner task")
        return _retry(claim, time_source)

    task_or_outcome = _deliver_task(claim, snapshot, time_source)
    if type(task_or_outcome) is str:
        return task_or_outcome
    attachment_or_outcome = _deliver_before_attachment(task_or_outcome, snapshot, time_source)
    if type(attachment_or_outcome) is str:
        return attachment_or_outcome
    return _reconcile_task_lifecycle(attachment_or_outcome, snapshot, time_source)


def run_batch(
    now: datetime | None = None,
    worker_id: str | None = None,
    limit: int = 10,
    clock: Callable[[], datetime] | None = None,
) -> BatchResult:
    """Claim and independently deliver at most ten local feedback records."""
    if type(limit) is not int:
        raise ValueError("batch limit must be an integer")
    time_source = _time_source(now, clock)
    identity = worker_id or f"{socket.gethostname()}:{os.getpid()}"
    capped = max(1, min(limit, 10))
    outcomes: list[str] = []
    task_delivery.queue_existing_lifecycle_mismatches()
    for _ in range(capped):
        claims = task_delivery.claim_due(now=time_source(), worker_id=identity, limit=1)
        if not claims:
            break
        claim = claims[0]
        try:
            outcomes.append(process_claim(claim, clock=time_source))
        except Exception:
            _log.exception("isolated owner task delivery failure for feedback %s", claim.feedback_id)
            outcomes.append("isolated_error")
    return BatchResult.from_outcomes(outcomes)


__all__ = [
    "BatchResult", "before_attachment_name", "process_claim", "run_batch",
    "task_description", "task_name", "task_stage_for", "terminal_note_html",
    "terminal_note_marker",
]
