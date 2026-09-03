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
from html.parser import HTMLParser
from typing import Literal

from . import feedback_task_delivery as task_delivery
from . import odoo_client
from .feedback_content import safe_page_url
from .feedback_types import feedback_type
from .odoo_improvements import (
    ContractError,
    GateClosed,
    ImprovementContract,
    ImprovementsAuthenticationError,
    ImprovementsClient,
    ImprovementsConfigError,
    MalformedMutationResponse,
    TargetIdentityError,
)
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
_AMBIGUOUS_TASK_CREATE_ERRORS = _AMBIGUOUS_WRITE_ERRORS + (
    odoo_client.OdooTaskPayloadError,
)
_OUTCOMES = frozenset({"delivered", "retried", "blocked", "isolated_error"})
_SOURCE_APP = "GPI Plant Manager (plant)"
_DUPLICATE_TASK_REASON = "More than one matching owner task exists."
_DUPLICATE_SCREENSHOT_REASON = "More than one matching owner screenshot exists."
_ATTACHMENT_IDENTITY_REASON = "The stored owner screenshot does not match this feedback."
_TASK_IDENTITY_REASON = "The stored owner task does not match this feedback."
_TASK_STAGE_REASON = "The owner task stage is missing or ambiguous."
_TASK_NOTE_REASON = "More than one matching owner task result note exists."
_REVIEW_SETUP_REASON = "The Odoo review setup is missing or ambiguous."
_REVIEW_REFERENCE_REASON = "The Odoo review reference is missing or ambiguous."
_REVIEW_LINK_REASON = "The Odoo review reference link conflicts with this task."
_REVIEW_ASSIGNEE_LOGIN = "dale@gruberpallets.com"
_TASK_STAGE_BY_STATUS = {
    "requested": "New",
    "in_progress": "In Progress",
    "completed": "Done",
    "declined": "Done",
}

# project.task.state is Odoo's own "Status", separate from the stage: moving a task
# to the folded Done stage leaves it at In Progress, and the other GPI apps (Sales
# Manager, OS Manager) decide open-vs-closed from it. So a finished request closes
# the Status too, in the words those apps show — Done for completed, Cancelled
# (their "Declined") for declined.
_TASK_STATE_BY_STATUS = {
    "requested": "01_in_progress",
    "in_progress": "01_in_progress",
    "completed": "1_done",
    "declined": "1_canceled",
}
_CLOSED_TASK_STATES = frozenset({"1_done", "1_canceled"})


class _ReviewMetadataParser(HTMLParser):
    """Collect paragraph tokens so escaped body text cannot imitate marker elements."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.paragraphs: list[tuple[tuple[str, str], ...]] = []
        self._current: list[tuple[str, str]] | None = None

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag == "p":
            self._current = []
        elif self._current is not None:
            self._current.append(("break" if tag == "br" else "start", tag))

    def handle_endtag(self, tag: str) -> None:
        if tag == "p" and self._current is not None:
            self.paragraphs.append(tuple(self._current))
            self._current = None
        elif self._current is not None:
            self._current.append(("end", tag))

    def handle_data(self, data: str) -> None:
        if self._current is not None:
            self._current.append(("text", data))


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


def task_owner(snapshot: task_delivery.FeedbackTaskSnapshot) -> Literal["coding", "review"]:
    """Route only the three canonical review types away from coding delivery."""
    behavior = feedback_type(snapshot.task_type).behavior
    if behavior not in {"coding", "review"}:
        raise ValueError("feedback type has no task owner")
    return behavior


def task_identity_names(snapshot: task_delivery.FeedbackTaskSnapshot) -> frozenset[str]:
    """Return exact current and known historical names for one feedback task."""
    current = task_name(snapshot)
    names = {current}
    if snapshot.task_type == "feature":
        names.add(current.replace("[New Feature]", "[Feature]", 1))
    return frozenset(names)


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


def review_task_description(snapshot: task_delivery.FeedbackTaskSnapshot) -> str:
    """Build the immutable request body and independent Plant source markers."""
    body = html.escape(snapshot.message.strip(), quote=False).replace("\n", "<br>")
    submitter = html.escape(snapshot.submitter or "unknown", quote=False)
    source_id = f"GPI-PM-FB-{snapshot.feedback_id}"
    return (
        f"<p>{body}</p>"
        f"{_review_source_metadata_prefix(source_id)}"
        f"<strong>Submitted by:</strong> {submitter}</p>"
    )


def _review_source_metadata_prefix(source_id: str) -> str:
    """Return the structured marker element user-controlled escaped text cannot forge."""
    return (
        "<p><strong>Source:</strong> GPI Plant Manager<br>"
        f"<strong>Source ID:</strong> {source_id}<br>"
    )


def _has_exact_review_source_metadata(description: str, source_id: str) -> bool:
    expected = (
        ("start", "strong"),
        ("text", "Source:"),
        ("end", "strong"),
        ("text", " GPI Plant Manager"),
        ("break", "br"),
        ("start", "strong"),
        ("text", "Source ID:"),
        ("end", "strong"),
        ("text", f" {source_id}"),
        ("break", "br"),
    )
    parser = _ReviewMetadataParser()
    parser.feed(description)
    parser.close()
    marker_shape = (
        ("start", "strong"),
        ("text", "Source:"),
        ("end", "strong"),
        None,
        ("break", "br"),
        ("start", "strong"),
        ("text", "Source ID:"),
        ("end", "strong"),
        None,
        ("break", "br"),
    )
    structured = []
    for paragraph in parser.paragraphs:
        for start in range(len(paragraph) - len(marker_shape) + 1):
            window = paragraph[start : start + len(marker_shape)]
            if all(
                wanted is None or window[index] == wanted
                for index, wanted in enumerate(marker_shape)
            ):
                structured.append(window)
    return len(structured) == 1 and structured[0] == expected


def task_stage_for(status: str) -> str:
    try:
        return _TASK_STAGE_BY_STATUS[status]
    except (KeyError, TypeError):
        raise ValueError("unsupported feedback lifecycle status") from None


def task_state_for(status: str) -> str:
    try:
        return _TASK_STATE_BY_STATUS[status]
    except (KeyError, TypeError):
        raise ValueError("unsupported feedback lifecycle status") from None


def task_state_matches(status: str, remote_state: object) -> bool:
    """Whether the task's Odoo Status already agrees with the local lifecycle.

    A finished request needs the exact closed Status. An open request only needs
    an open one: a Waiting or Approved Status another app set is its business.
    """
    target = task_state_for(status)
    if target in _CLOSED_TASK_STATES:
        return remote_state == target
    return isinstance(remote_state, str) and remote_state not in _CLOSED_TASK_STATES


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


def _attachment_id_match(
    claim: task_delivery.TaskDeliveryClaim,
    attachment_ids: list[int],
    clock: Callable[[], datetime],
) -> int | str | None:
    if len(attachment_ids) > 1:
        return _block(claim, _DUPLICATE_SCREENSHOT_REASON, clock)
    if attachment_ids:
        return attachment_ids[0]
    return None


def _verify_and_record_attachment(
    claim: task_delivery.TaskDeliveryClaim,
    *,
    attachment_id: int,
    filename: str,
    clock: Callable[[], datetime],
) -> task_delivery.TaskDeliveryClaim | str:
    if claim.task_id is None:
        raise RuntimeError("attachment readback has no durable owner task")
    try:
        remote = odoo_client.read_feedback_attachment(attachment_id)
    except _RECOVERABLE_ODOO_ERRORS:
        return _retry(claim, clock)
    except (ValueError, odoo_client.OdooTaskPayloadError):
        return _block(claim, _ATTACHMENT_IDENTITY_REASON, clock)
    if remote != {
        "id": attachment_id,
        "name": filename,
        "res_model": "project.task",
        "res_id": claim.task_id,
        "mimetype": "image/jpeg",
    }:
        return _block(claim, _ATTACHMENT_IDENTITY_REASON, clock)
    return task_delivery.record_before_attachment(
        claim,
        attachment_id=attachment_id,
        now=clock(),
    )


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


def _resolve_uncertain_review_task_create(
    claim: task_delivery.TaskDeliveryClaim,
    *,
    project_id: int,
    name: str,
    clock: Callable[[], datetime],
) -> int | str | None:
    try:
        task_ids = odoo_client.find_review_task_ids(project_id, name)
    except _RECOVERABLE_ODOO_ERRORS:
        _log.exception("could not recheck review task after uncertain create")
        return _retry(claim, clock)
    if len(task_ids) > 1:
        return _block(claim, _DUPLICATE_TASK_REASON, clock)
    if not task_ids:
        return None
    return task_ids[0]


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


def _review_contract_ready(
    claim: task_delivery.TaskDeliveryClaim,
    snapshot: task_delivery.FeedbackTaskSnapshot,
    clock: Callable[[], datetime],
) -> tuple[ImprovementsClient, ImprovementContract] | str:
    try:
        client = ImprovementsClient.from_env()
        contract = client.read_contract()
    except (TimeoutError, ConnectionError, OSError, xmlrpc.client.Error):
        return _retry(claim, clock)
    except (
        ImprovementsConfigError,
        ImprovementsAuthenticationError,
        TargetIdentityError,
        ContractError,
    ):
        return _block(claim, _REVIEW_SETUP_REASON, clock)
    odoo_type = feedback_type(snapshot.task_type).odoo_value
    if odoo_type == "2s Improvement" and contract.version < 2:
        return _block(claim, _REVIEW_SETUP_REASON, clock)
    if odoo_type not in {"Physical - Issue", "Physical - Suggestion", "2s Improvement"}:
        raise ValueError("feedback type is not a review type")
    return client, contract


def _deliver_review_task(
    claim: task_delivery.TaskDeliveryClaim,
    snapshot: task_delivery.FeedbackTaskSnapshot,
    clock: Callable[[], datetime],
) -> task_delivery.TaskDeliveryClaim | str:
    name = task_name(snapshot)
    try:
        project_id = odoo_client.ensure_review_project()
        stage_id = odoo_client.ensure_review_stage(project_id, "General")
        users = odoo_client.find_active_users_by_login(_REVIEW_ASSIGNEE_LOGIN, limit=2)
        if len(users) != 1:
            return _block(claim, _REVIEW_SETUP_REASON, clock)
        assignee_uid = users[0]["id"]
        task_ids = odoo_client.find_review_task_ids(project_id, name)
    except _RECOVERABLE_ODOO_ERRORS:
        return _retry(claim, clock)
    except (
        ValueError,
        odoo_client.OdooTaskPayloadError,
        odoo_client.OdooUserPayloadError,
        KeyError,
        TypeError,
    ):
        return _block(claim, _REVIEW_SETUP_REASON, clock)
    if claim.task_id is not None:
        if len(task_ids) > 1 or any(task_id != claim.task_id for task_id in task_ids):
            return _block(claim, _DUPLICATE_TASK_REASON, clock)
        task_id_or_outcome: int | str | None = claim.task_id
    else:
        if len(task_ids) > 1:
            return _block(claim, _DUPLICATE_TASK_REASON, clock)
        task_id_or_outcome = task_ids[0] if task_ids else None
    if task_id_or_outcome is None:
        claim = task_delivery.renew_claim(claim, now=clock())
        try:
            task_id_or_outcome = odoo_client.create_feedback_review_task(
                project_id=project_id,
                stage_id=stage_id,
                name=name,
                description_html=review_task_description(snapshot),
                assignee_uid=assignee_uid,
            )
        except _AMBIGUOUS_TASK_CREATE_ERRORS:
            task_id_or_outcome = _resolve_uncertain_review_task_create(
                claim, project_id=project_id, name=name, clock=clock
            )
        except _RECOVERABLE_ODOO_ERRORS:
            return _retry(claim, clock)
        except ValueError:
            return _block(claim, _REVIEW_SETUP_REASON, clock)
    if type(task_id_or_outcome) is str:
        return task_id_or_outcome
    if task_id_or_outcome is None:
        return _retry(claim, clock)
    task_id = task_id_or_outcome
    try:
        remote = odoo_client.read_feedback_review_task(task_id)
    except _RECOVERABLE_ODOO_ERRORS:
        return _retry(claim, clock)
    except (ValueError, odoo_client.OdooTaskPayloadError):
        return _block(claim, _TASK_IDENTITY_REASON, clock)
    description = remote.get("description")
    source_id = f"GPI-PM-FB-{snapshot.feedback_id}"
    if (
        remote.get("id") != task_id
        or (claim.task_id is None and remote.get("name") != name)
        or remote.get("project_id") != project_id
        or remote.get("stage_id") != stage_id
        or remote.get("stage_name") != "General"
        or remote.get("user_ids") != [assignee_uid]
        or remote.get("state") != "01_in_progress"
        or remote.get("active") is not True
        or type(description) is not str
        or not _has_exact_review_source_metadata(description, source_id)
    ):
        return _block(claim, _TASK_IDENTITY_REASON, clock)
    if claim.task_id is not None:
        return claim
    return task_delivery.record_task_id(claim, task_id=task_id, now=clock())


def _link_review_reference(
    claim: task_delivery.TaskDeliveryClaim,
    client: ImprovementsClient,
    contract: ImprovementContract,
    clock: Callable[[], datetime],
) -> str:
    if claim.task_id is None:
        raise RuntimeError("review link has no durable task identity")
    source_id = f"GPI-PM-FB-{claim.feedback_id}"
    try:
        rows = client.find_exact(source_id)
    except (TimeoutError, ConnectionError, OSError, xmlrpc.client.Error):
        return _retry(claim, clock)
    except (TargetIdentityError, ContractError):
        return _block(claim, _REVIEW_REFERENCE_REASON, clock)
    if not rows:
        return _retry(claim, clock)
    if len(rows) != 1 or type(rows[0]) is not dict or type(rows[0].get("id")) is not int:
        return _block(claim, _REVIEW_REFERENCE_REASON, clock)
    try:
        client.link_task_once(
            rows[0]["id"],
            claim.task_id,
            feedback_id=claim.feedback_id,
            expected_contract=contract,
        )
    except (TimeoutError, ConnectionError, OSError, xmlrpc.client.Error, GateClosed):
        return _retry(claim, clock)
    except (TargetIdentityError, ContractError, MalformedMutationResponse):
        return _block(claim, _REVIEW_LINK_REASON, clock)
    task_delivery.mark_delivered(claim, now=clock())
    return "delivered"


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
    matched_id = _attachment_id_match(claim, attachment_ids, clock)
    if type(matched_id) is str:
        return matched_id
    if matched_id is not None:
        return _verify_and_record_attachment(
            claim,
            attachment_id=matched_id,
            filename=filename,
            clock=clock,
        )

    claim = task_delivery.renew_claim(claim, now=clock())
    try:
        attachment_id = odoo_client.add_task_attachment(
            claim.task_id, filename, "image/jpeg", snapshot.before_image.jpeg_bytes
        )
    except _AMBIGUOUS_TASK_CREATE_ERRORS:
        _log.exception("uncertain owner-task image attachment for feedback %s", claim.feedback_id)
        try:
            attachment_ids = odoo_client.find_feedback_attachment_ids(claim.task_id, filename)
            matched_id = _attachment_id_match(claim, attachment_ids, clock)
        except _RECOVERABLE_ODOO_ERRORS:
            _log.exception("could not recheck owner-task image attachment")
            return _retry(claim, clock)
        if matched_id is None:
            return _retry(claim, clock)
        if type(matched_id) is str:
            return matched_id
        return _verify_and_record_attachment(
            claim,
            attachment_id=matched_id,
            filename=filename,
            clock=clock,
        )
    except _RECOVERABLE_ODOO_ERRORS:
        _log.exception("could not deliver owner-task image attachment")
        return _retry(claim, clock)
    return _verify_and_record_attachment(
        claim,
        attachment_id=attachment_id,
        filename=filename,
        clock=clock,
    )


def _task_identity_matches(
    remote: object,
    *,
    task_id: int,
    project_id: int,
    names: frozenset[str],
) -> bool:
    return (
        isinstance(remote, dict)
        and remote.get("id") == task_id
        and remote.get("project_id") == project_id
        and remote.get("name") in names
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
    names = task_identity_names(snapshot)
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
        remote, task_id=claim.task_id, project_id=project_id, names=names
    ):
        return _block(claim, _TASK_IDENTITY_REASON, clock)

    stage_id = stage_ids[0]
    fields: dict[str, object] = {}
    if remote.get("stage_id") != stage_id or remote.get("stage_name") != target_stage:
        fields["stage_id"] = stage_id
    if not task_state_matches(snapshot.status, remote.get("state")):
        fields["state"] = task_state_for(snapshot.status)
    if fields:
        claim = task_delivery.renew_claim(claim, now=clock())
        try:
            odoo_client.update_task(claim.task_id, **fields)
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
            verified, task_id=claim.task_id, project_id=project_id, names=names
        )
        or verified.get("stage_id") != stage_id
        or verified.get("stage_name") != target_stage
        or not task_state_matches(snapshot.status, verified.get("state"))
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

    owner = task_owner(snapshot)
    if owner == "review":
        contract_or_outcome = _review_contract_ready(claim, snapshot, time_source)
        if type(contract_or_outcome) is str:
            return contract_or_outcome
        review_client, review_contract = contract_or_outcome
        task_or_outcome = _deliver_review_task(claim, snapshot, time_source)
    else:
        task_or_outcome = _deliver_task(claim, snapshot, time_source)
    if type(task_or_outcome) is str:
        return task_or_outcome
    attachment_or_outcome = _deliver_before_attachment(task_or_outcome, snapshot, time_source)
    if type(attachment_or_outcome) is str:
        return attachment_or_outcome
    if owner == "review":
        return _link_review_reference(
            attachment_or_outcome,
            review_client,
            review_contract,
            time_source,
        )
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
    "review_task_description", "task_description", "task_identity_names", "task_name", "task_owner",
    "task_stage_for", "terminal_note_html",
    "terminal_note_marker",
]
