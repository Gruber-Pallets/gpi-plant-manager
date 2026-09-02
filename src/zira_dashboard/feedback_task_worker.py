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
    task_delivery.mark_delivered(attachment_or_outcome, now=time_source())
    return "delivered"


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


__all__ = ["BatchResult", "before_attachment_name", "process_claim", "run_batch", "task_description", "task_name"]
