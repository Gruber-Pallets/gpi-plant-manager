"""Pull Odoo-owned review task lifecycle into the shared reference row."""

from __future__ import annotations

import os
import xmlrpc.client
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from . import feedback_store, odoo_client
from .feedback_projection import review_lifecycle_fields
from .feedback_review_events import ReviewEvent, encode_review_event, parse_review_events
from .feedback_task_worker import _has_exact_review_source_metadata
from .feedback_types import REVIEW_TASK_PROJECT, REVIEW_TASK_STAGES, feedback_type
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


_TERMINAL = frozenset({"Completed", "Declined"})
_LOCAL_STATUS = {
    "Requested": "requested",
    "In-Progress": "in_progress",
    "Completed": "completed",
    "Declined": "declined",
}
_UNKNOWN_WRITE_ERRORS = (
    TimeoutError,
    ConnectionError,
    OSError,
    xmlrpc.client.ProtocolError,
    MalformedMutationResponse,
)
_READ_RETRY_ERRORS = (
    TimeoutError,
    ConnectionError,
    OSError,
    xmlrpc.client.Error,
)
_OUTCOMES = frozenset({"adopted", "unchanged", "attention", "retry", "isolated_error"})


@dataclass(frozen=True)
class ReviewLifecycleProjection:
    status: str
    fields: dict[str, object]
    event: ReviewEvent | None


@dataclass(frozen=True)
class ReconcileResult:
    scanned: int = 0
    adopted: int = 0
    unchanged: int = 0
    attention: int = 0
    retried: int = 0
    isolated_errors: int = 0
    skipped: str | None = None

    @classmethod
    def from_outcomes(cls, outcomes: list[str]) -> ReconcileResult:
        if type(outcomes) is not list or any(item not in _OUTCOMES for item in outcomes):
            raise ValueError("review reconciliation outcomes are malformed")
        return cls(
            scanned=len(outcomes),
            adopted=outcomes.count("adopted"),
            unchanged=outcomes.count("unchanged"),
            attention=outcomes.count("attention"),
            retried=outcomes.count("retry"),
            isolated_errors=outcomes.count("isolated_error"),
        )


def _event_time(event: ReviewEvent) -> datetime | None:
    if type(event) is not ReviewEvent or type(event.occurred_at) is not str:
        return None
    if not event.occurred_at.endswith("Z"):
        return None
    try:
        parsed = datetime.fromisoformat(f"{event.occurred_at[:-1]}+00:00")
    except ValueError:
        return None
    if parsed.utcoffset() is None:
        return None
    return parsed.astimezone(UTC)


def _latest(events: Sequence[ReviewEvent], actions: frozenset[str]) -> ReviewEvent | None:
    if not isinstance(events, Sequence) or isinstance(events, (str, bytes)):
        return None
    valid: list[tuple[datetime, ReviewEvent]] = []
    seen_ids: set[str] = set()
    for item in events:
        if type(item) is not ReviewEvent or item.event_id in seen_ids:
            return None
        try:
            encode_review_event(item)
        except ValueError:
            return None
        seen_ids.add(item.event_id)
        occurred = _event_time(item)
        if occurred is None:
            return None
        if item.action in actions:
            valid.append((occurred, item))
    if not valid:
        return None
    latest_time = max(pair[0] for pair in valid)
    latest = [item for occurred, item in valid if occurred == latest_time]
    return latest[0] if len(latest) == 1 else None


def _projection_event(
    task: Mapping[str, object], events: Sequence[ReviewEvent]
) -> tuple[str, ReviewEvent | None] | None:
    if not isinstance(task, Mapping):
        return None
    state = task.get("state")
    users = task.get("user_ids")
    write_date = task.get("write_date")
    if (
        task.get("active") is not True
        or task.get("project_name") != REVIEW_TASK_PROJECT
        or task.get("stage_name") not in REVIEW_TASK_STAGES.values()
        or type(task.get("project_id")) is not int
        or task["project_id"] <= 0
        or type(task.get("stage_id")) is not int
        or task["stage_id"] <= 0
        or type(users) is not list
        or len(users) != 1
        or type(users[0]) is not int
        or users[0] <= 0
        or type(write_date) is not str
    ):
        return None
    try:
        written_at = datetime.strptime(write_date, "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)
    except ValueError:
        return None
    if state == "01_in_progress":
        return ("Requested", None) if len(events) == 0 else None
    if state == "03_approved":
        selected = _latest(events, frozenset({"accept", "assign", "move_l10"}))
        if selected is None:
            return None
        latest_assignment = _latest(events, frozenset({"assign"}))
        if any(item.action == "assign" for item in events) and latest_assignment is None:
            return None
        if latest_assignment is not None and latest_assignment.target_odoo_user_id != users[0]:
            return None
        if _event_time(selected) > written_at:
            return None
        return "In-Progress", selected
    if state == "1_canceled":
        selected = _latest(events, frozenset({"decline"}))
        if selected is None or not (selected.detail or "").strip():
            return None
        if _event_time(selected) > written_at:
            return None
        return "Declined", selected
    if state == "1_done":
        selected = _latest(events, frozenset({"complete"}))
        if selected is None or not (selected.detail or "").strip():
            return None
        if _event_time(selected) > written_at:
            return None
        return "Completed", selected
    return None


def task_lifecycle(task: Mapping[str, object], events: Sequence[ReviewEvent]) -> str:
    """Map one task and its structured review events without any I/O."""
    selected = _projection_event(task, events)
    return "attention" if selected is None else selected[0]


def review_lifecycle_projection(
    task: Mapping[str, object],
    events: Sequence[ReviewEvent],
    *,
    stop_type: str,
) -> ReviewLifecycleProjection:
    selected = _projection_event(task, events)
    if selected is None:
        return ReviewLifecycleProjection("attention", {}, None)
    status, selected_event = selected
    fields = review_lifecycle_fields(
        status=status,
        occurred_at=selected_event.occurred_at if status in _TERMINAL else None,
        employee_id=selected_event.actor_employee_id if status in _TERMINAL else None,
        detail=selected_event.detail if status in _TERMINAL else None,
        stop_type=stop_type,
    )
    return ReviewLifecycleProjection(status, fields, selected_event)


def _linked_id(value: object) -> int | None:
    if value is None or value is False:
        return None
    if (
        type(value) is not list
        or len(value) != 2
        or type(value[0]) is not int
        or value[0] <= 0
        or type(value[1]) is not str
    ):
        raise ValueError("linked identity is malformed")
    return value[0]


def _task_identity_matches(
    candidate: feedback_store.ReviewCandidate,
    task: Mapping[str, object],
    *,
    project_id: int,
    stage_id: int,
) -> bool:
    description = task.get("description")
    return (
        task.get("id") == candidate.odoo_task_id
        and task.get("active") is True
        and task.get("project_id") == project_id
        and task.get("project_name") == REVIEW_TASK_PROJECT
        and task.get("stage_id") == stage_id
        and task.get("stage_name") in REVIEW_TASK_STAGES.values()
        and type(task.get("write_date")) is str
        and type(description) is str
        and _has_exact_review_source_metadata(description, f"GPI-PM-FB-{candidate.feedback_id}")
    )


def _reference_identity_code(
    candidate: feedback_store.ReviewCandidate,
    reference: Mapping[str, object],
) -> str | None:
    expected_type = feedback_type(candidate.task_type).odoo_value
    if (
        reference.get("id") != candidate.odoo_improvement_id
        or reference.get("x_studio_source") != "GPI Plant Manager"
        or reference.get("x_studio_source_id") != f"GPI-PM-FB-{candidate.feedback_id}"
        or reference.get("x_studio_type") != expected_type
        or _linked_id(reference.get("x_studio_linked_wo")) is not None
    ):
        return "review_reference_identity_mismatch"
    if _linked_id(reference.get("x_studio_linked_task")) != candidate.odoo_task_id:
        return "review_reference_link_mismatch"
    return None


def _read_reference(client: ImprovementsClient, candidate: feedback_store.ReviewCandidate):
    return client.read_improvement(
        candidate.odoo_improvement_id,
        [
            "x_studio_source",
            "x_studio_source_id",
            "x_studio_type",
            "x_studio_status",
            "x_studio_date_stop",
            "x_studio_completed_by",
            "x_studio_notes",
            "x_studio_linked_task",
            "x_studio_linked_wo",
        ],
        full_binary=False,
    )


def _field_matches(field_name: str, expected: object, actual: object) -> bool:
    if field_name == "x_studio_completed_by":
        return _linked_id(actual) == expected
    return type(actual) is type(expected) and actual == expected


def _lifecycle_matches(
    reference: Mapping[str, object], projection: ReviewLifecycleProjection
) -> bool:
    return all(
        field_name in reference and _field_matches(field_name, expected, reference[field_name])
        for field_name, expected in projection.fields.items()
    )


def _local_terminal_matches(
    candidate: feedback_store.ReviewCandidate,
    projection: ReviewLifecycleProjection,
) -> bool:
    if candidate.status not in {"completed", "declined"}:
        return True
    selected = projection.event
    if selected is None:
        return False
    return (
        candidate.status == _LOCAL_STATUS[projection.status]
        and candidate.finished_at == _event_time(selected)
        and candidate.finished_by == f"odoo_employee:{selected.actor_employee_id}"
        and candidate.resolution_note == (selected.detail or "").strip()
    )


def _reference_conflict_code(
    reference: Mapping[str, object],
    projection: ReviewLifecycleProjection,
) -> str | None:
    current = reference.get("x_studio_status")
    if current in _TERMINAL and (
        current != projection.status or not _lifecycle_matches(reference, projection)
    ):
        return "review_terminal_conflict"
    if current in {"Requested", "In-Progress"} and any(
        reference.get(field_name) is not False
        for field_name in (
            "x_studio_date_stop",
            "x_studio_completed_by",
            "x_studio_notes",
        )
    ):
        return "review_lifecycle_mismatch"
    return None


def _lifecycle_readback(
    reference: Mapping[str, object],
    projection: ReviewLifecycleProjection,
) -> tuple[str | None, bool]:
    try:
        conflict_code = _reference_conflict_code(reference, projection)
        matches = _lifecycle_matches(reference, projection)
    except (TypeError, ValueError):
        return "review_lifecycle_mismatch", False
    return conflict_code, matches


def _attention(candidate: feedback_store.ReviewCandidate, code: str, now: datetime) -> str:
    feedback_store.record_review_attention(candidate, code, now=now)
    return "attention"


def process_candidate(
    candidate: feedback_store.ReviewCandidate,
    *,
    client: ImprovementsClient,
    now: datetime,
) -> str:
    """Reconcile one exact task/reference pair, with at most one write RPC."""
    if type(candidate) is not feedback_store.ReviewCandidate:
        raise ValueError("review candidate is malformed")
    if type(now) is not datetime or now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("review reconciliation time must be timezone-aware")
    try:
        task = odoo_client.read_feedback_review_task(candidate.odoo_task_id)
    except odoo_client.OdooTaskPayloadError:
        return _attention(candidate, "review_task_identity_mismatch", now)
    except _READ_RETRY_ERRORS:
        return "retry"
    try:
        project_id = odoo_client.ensure_review_project()
        stage_name = task.get("stage_name")
        if type(stage_name) is not str:
            raise ValueError("review task stage is malformed")
        stage_id = odoo_client.ensure_review_stage(project_id, stage_name)
    except _READ_RETRY_ERRORS:
        return "retry"
    except (ValueError, odoo_client.OdooTaskPayloadError):
        return _attention(candidate, "review_task_identity_mismatch", now)
    if not _task_identity_matches(candidate, task, project_id=project_id, stage_id=stage_id):
        return _attention(candidate, "review_task_identity_mismatch", now)
    events = parse_review_events(task["description"])
    try:
        contract = client.read_contract()
    except _READ_RETRY_ERRORS:
        return "retry"
    except (
        ImprovementsConfigError,
        ImprovementsAuthenticationError,
        TargetIdentityError,
        ContractError,
    ):
        return _attention(candidate, "review_reference_identity_mismatch", now)
    if type(contract) is not ImprovementContract:
        return _attention(candidate, "review_reference_identity_mismatch", now)
    projection = review_lifecycle_projection(task, events, stop_type=contract.stop_type)
    if projection.status == "attention":
        return _attention(candidate, "review_lifecycle_mismatch", now)
    desired_local = _LOCAL_STATUS[projection.status]
    if candidate.status in {"completed", "declined"} and not _local_terminal_matches(
        candidate, projection
    ):
        return _attention(candidate, "review_terminal_conflict", now)
    try:
        exact = client.find_exact(f"GPI-PM-FB-{candidate.feedback_id}")
        if len(exact) != 1 or exact[0].get("id") != candidate.odoo_improvement_id:
            return _attention(candidate, "review_reference_identity_mismatch", now)
        reference = _read_reference(client, candidate)
        identity_code = _reference_identity_code(candidate, reference)
    except _READ_RETRY_ERRORS:
        return "retry"
    except (ValueError, ContractError, TypeError):
        return _attention(candidate, "review_reference_identity_mismatch", now)
    if identity_code is not None:
        return _attention(candidate, identity_code, now)
    conflict_code, lifecycle_matches = _lifecycle_readback(reference, projection)
    if conflict_code is not None:
        return _attention(candidate, conflict_code, now)
    if not lifecycle_matches:
        try:
            client.write_improvement(
                candidate.odoo_improvement_id,
                projection.fields,
                feedback_id=candidate.feedback_id,
                expected_contract=contract,
            )
        except _UNKNOWN_WRITE_ERRORS:
            try:
                reference = _read_reference(client, candidate)
            except _READ_RETRY_ERRORS:
                return "retry"
            try:
                identity_code = _reference_identity_code(candidate, reference)
            except (ValueError, TypeError):
                return _attention(candidate, "review_reference_identity_mismatch", now)
            if identity_code is not None:
                return _attention(candidate, identity_code, now)
            conflict_code, lifecycle_matches = _lifecycle_readback(reference, projection)
            if conflict_code is not None:
                return _attention(candidate, conflict_code, now)
            if not lifecycle_matches:
                return "retry"
        except (GateClosed, TargetIdentityError, ContractError):
            return _attention(candidate, "review_reference_identity_mismatch", now)
        else:
            try:
                reference = _read_reference(client, candidate)
            except _READ_RETRY_ERRORS:
                return "retry"
            try:
                identity_code = _reference_identity_code(candidate, reference)
            except (ValueError, TypeError):
                return _attention(candidate, "review_reference_identity_mismatch", now)
            conflict_code, lifecycle_matches = _lifecycle_readback(reference, projection)
            if identity_code is not None:
                return _attention(candidate, "review_reference_identity_mismatch", now)
            if conflict_code is not None:
                return _attention(candidate, conflict_code, now)
            if not lifecycle_matches:
                return _attention(candidate, "review_reference_identity_mismatch", now)

    selected_event = projection.event
    try:
        changed = feedback_store.adopt_review_lifecycle(
            candidate,
            status=desired_local,
            finished_at=(
                _event_time(selected_event)
                if projection.status in _TERMINAL and selected_event is not None
                else None
            ),
            finished_by_employee_id=(
                selected_event.actor_employee_id
                if projection.status in _TERMINAL and selected_event is not None
                else None
            ),
            resolution_note=(
                selected_event.detail
                if projection.status in _TERMINAL and selected_event is not None
                else None
            ),
            now=now,
        )
    except feedback_store.InvalidTransition:
        if candidate.status in {"completed", "declined"}:
            return _attention(candidate, "review_terminal_conflict", now)
        return "retry"
    return "adopted" if changed else "unchanged"


def _write_enabled() -> bool:
    return (
        os.environ.get("ODOO_SHARED_REPORTING_WRITE_ENABLED") == "true"
        and os.environ.get("ODOO_IMPROVEMENTS_WRITE_ENABLED") == "true"
    )


def run_batch(limit: int = 50) -> ReconcileResult:
    """Reconcile and isolate at most fifty exact review pairs."""
    if type(limit) is not int or not 1 <= limit <= 50:
        raise ValueError("review reconciliation limit must be from 1 through 50")
    if not _write_enabled():
        return ReconcileResult(skipped="write_gates_closed")
    now = datetime.now(UTC)
    lease = feedback_store.acquire_review_reconcile_lease(
        owner="feedback-review-reconciler",
        now=now,
    )
    if lease is None:
        return ReconcileResult(skipped="lease_unavailable")
    try:
        client = ImprovementsClient.from_env()
        client.assert_worker_enabled()
        candidates = feedback_store.review_reconcile_candidates(limit)
        outcomes: list[str] = []
        for index, candidate in enumerate(candidates):
            current = datetime.now(UTC)
            renewed = feedback_store.renew_review_reconcile_lease(lease, now=current)
            if renewed is None:
                outcomes.extend("retry" for _item in candidates[index:])
                break
            lease = renewed
            claimed = feedback_store.claim_review_candidate(candidate, lease, now=current)
            if claimed is None:
                outcomes.append("retry")
                continue
            try:
                outcomes.append(process_candidate(claimed, client=client, now=current))
            except Exception:
                outcomes.append("isolated_error")
            finally:
                if claimed.sync_claim_token is not None:
                    feedback_store.release_review_candidate(
                        claimed,
                        now=datetime.now(UTC),
                    )
        return ReconcileResult.from_outcomes(outcomes)
    finally:
        feedback_store.release_review_reconcile_lease(
            lease,
            now=datetime.now(UTC),
        )


__all__ = [
    "ReconcileResult",
    "ReviewLifecycleProjection",
    "process_candidate",
    "review_lifecycle_projection",
    "run_batch",
    "task_lifecycle",
]
