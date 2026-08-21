"""Bounded, inert rollout analysis and short local feedback migration helpers."""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime
from typing import Mapping

from . import feedback_store
from . import feedback_sync_store as sync_store
from .feedback_sync import _saved_dates_match_contract
from .feedback_projection import (
    BinaryEvidence,
    Projection,
    build_projection,
    resolve_employee_id,
    source_id_for,
    verify_readback,
)
from .odoo_improvements import (
    SOURCE_VALUE,
    TARGET_FIELDS,
    ContractError,
    ImprovementContract,
    TargetIdentityError,
    TargetInspection,
)


MAX_SIGNED_64 = 9_223_372_036_854_775_807
MAX_BATCH_SIZE = 100
_STATUSES = frozenset({"requested", "in_progress", "completed", "declined"})
_STAGE_STATUS = {
    "New": "requested",
    "Waiting": "requested",
    "In Progress": "in_progress",
    "Done": "completed",
    "Rejected": "declined",
}
_COUNT_KEYS = frozenset(
    {
        "synchronized",
        "due",
        "deferred",
        "in_flight",
        "quarantined",
        "version_lag",
    }
)
_SELECTION_DIAGNOSTICS = frozenset(
    {
        "x_studio_status:Requested",
        "x_studio_status:In-Progress",
        "x_studio_status:Completed",
        "x_studio_status:Declined",
        "x_studio_type:Digital",
        "x_studio_type:Digital - New Feature",
        "x_studio_type:Physical",
    }
)


def _positive_signed_64(value: object, label: str) -> int:
    if type(value) is not int or not 0 < value <= MAX_SIGNED_64:
        raise ValueError(f"{label} must be a positive signed-64-bit integer")
    return value


def _nonnegative_signed_64(value: object, label: str) -> int:
    if type(value) is not int or not 0 <= value <= MAX_SIGNED_64:
        raise ValueError(f"{label} must be a nonnegative signed-64-bit integer")
    return value


def _aware_datetime(value: object, label: str) -> datetime:
    if type(value) is not datetime or value.tzinfo is None:
        raise ValueError(f"{label} must be timezone-aware")
    try:
        offset = value.utcoffset()
    except (OverflowError, ValueError):
        offset = None
    if offset is None:
        raise ValueError(f"{label} must be timezone-aware")
    return value


def _batch_size(value: object, label: str) -> int:
    if type(value) is not int:
        raise ValueError(f"{label} must be an exact integer")
    return max(1, min(value, MAX_BATCH_SIZE))


def _id_tuple(value: object, label: str, *, maximum: int = MAX_BATCH_SIZE) -> tuple[int, ...]:
    if type(value) is not tuple or len(value) > maximum:
        raise ValueError(f"{label} must be a bounded tuple")
    previous = 0
    for item in value:
        safe = _positive_signed_64(item, label)
        if safe <= previous:
            raise ValueError(f"{label} must be unique and ordered")
        previous = safe
    return value


def _diagnostic_tuple(
    value: object,
    label: str,
    *,
    allowed: frozenset[str],
) -> tuple[str, ...]:
    if (
        type(value) is not tuple
        or len(value) > len(TARGET_FIELDS) + len(_SELECTION_DIAGNOSTICS)
        or any(type(item) is not str or item not in allowed for item in value)
        or tuple(sorted(set(value))) != value
    ):
        raise ValueError(f"{label} diagnostics are malformed")
    return value


@dataclass(frozen=True)
class PreflightReport:
    database_uuid_matches: bool
    company_matches: bool
    fields_ok: bool
    missing_fields: tuple[str, ...]
    wrong_types: tuple[str, ...]
    missing_selections: tuple[str, ...]
    source_value_present: bool
    required_source_value: str = SOURCE_VALUE
    wrong_relations: tuple[str, ...] = ()
    readonly_fields: tuple[str, ...] = ()
    wrong_selections: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for value in (
            self.database_uuid_matches,
            self.company_matches,
            self.fields_ok,
            self.source_value_present,
        ):
            if type(value) is not bool:
                raise ValueError("preflight flags must be booleans")
        if self.required_source_value != SOURCE_VALUE:
            raise ValueError("preflight source value is fixed")
        target_names = frozenset(TARGET_FIELDS)
        _diagnostic_tuple(self.missing_fields, "missing field", allowed=target_names)
        _diagnostic_tuple(self.wrong_types, "wrong type", allowed=target_names)
        _diagnostic_tuple(self.wrong_relations, "wrong relation", allowed=target_names)
        _diagnostic_tuple(self.readonly_fields, "readonly field", allowed=target_names)
        _diagnostic_tuple(
            self.missing_selections,
            "missing selection",
            allowed=_SELECTION_DIAGNOSTICS,
        )
        _diagnostic_tuple(
            self.wrong_selections,
            "wrong selection",
            allowed=frozenset({"x_studio_status", "x_studio_type"}),
        )
        expected_ok = (
            not any(
                (
                    self.missing_fields,
                    self.wrong_types,
                    self.wrong_relations,
                    self.readonly_fields,
                    self.missing_selections,
                    self.wrong_selections,
                )
            )
            and self.source_value_present
        )
        if self.fields_ok is not expected_ok:
            raise ValueError("preflight fields flag does not match diagnostics")


@dataclass(frozen=True)
class DryRunReport:
    requested_batch_size: int
    feedback_ids: tuple[int, ...]
    projected_ids: tuple[int, ...]
    skipped_ids: tuple[int, ...]
    create_ids: tuple[int, ...]
    adopt_ids: tuple[int, ...]
    update_ids: tuple[int, ...]
    duplicate_ids: tuple[int, ...]
    ownership_conflict_ids: tuple[int, ...]
    employee_missing_count: int
    employee_ambiguous_count: int
    before_image_count: int
    after_image_count: int
    next_after_id: int

    def __post_init__(self) -> None:
        if (
            type(self.requested_batch_size) is not int
            or not 1 <= self.requested_batch_size <= MAX_BATCH_SIZE
        ):
            raise ValueError("requested batch size is malformed")
        feedback_ids = _id_tuple(self.feedback_ids, "feedback ids")
        projected_ids = _id_tuple(self.projected_ids, "projected ids")
        skipped_ids = _id_tuple(self.skipped_ids, "skipped ids")
        outcomes = (
            _id_tuple(self.create_ids, "create ids"),
            _id_tuple(self.adopt_ids, "adopt ids"),
            _id_tuple(self.update_ids, "update ids"),
            _id_tuple(self.duplicate_ids, "duplicate ids"),
            _id_tuple(self.ownership_conflict_ids, "ownership conflict ids"),
        )
        if set(projected_ids) | set(skipped_ids) != set(feedback_ids):
            raise ValueError("dry-run classifications do not cover the selected rows")
        if set(projected_ids) & set(skipped_ids):
            raise ValueError("dry-run projected and skipped rows overlap")
        flattened = [item for values in outcomes for item in values]
        if len(flattened) != len(set(flattened)) or set(flattened) != set(projected_ids):
            raise ValueError("dry-run compound outcomes are not exclusive")
        for label, value, maximum in (
            ("employee missing", self.employee_missing_count, 2 * MAX_BATCH_SIZE),
            ("employee ambiguous", self.employee_ambiguous_count, 2 * MAX_BATCH_SIZE),
            ("before image", self.before_image_count, MAX_BATCH_SIZE),
            ("after image", self.after_image_count, MAX_BATCH_SIZE),
        ):
            count = _nonnegative_signed_64(value, f"{label} count")
            if count > maximum:
                raise ValueError(f"{label} count exceeds its batch bound")
        next_after_id = _nonnegative_signed_64(self.next_after_id, "next after id")
        if feedback_ids and next_after_id != feedback_ids[-1]:
            raise ValueError("next after id must equal the last selected feedback id")


@dataclass(frozen=True)
class LegacyApplyReport:
    applied_ids: tuple[int, ...]
    idempotent_ids: tuple[int, ...]
    skipped_ids: tuple[int, ...]

    def __post_init__(self) -> None:
        groups = (
            _id_tuple(self.applied_ids, "applied ids"),
            _id_tuple(self.idempotent_ids, "idempotent ids"),
            _id_tuple(self.skipped_ids, "skipped ids"),
        )
        flattened = [item for group in groups for item in group]
        if len(flattened) > MAX_BATCH_SIZE or len(flattened) != len(set(flattened)):
            raise ValueError("legacy apply outcomes must be bounded and exclusive")


@dataclass(frozen=True)
class LegacyMigrationReport:
    selected_ids: tuple[int, ...]
    applied_ids: tuple[int, ...]
    idempotent_ids: tuple[int, ...]
    skipped_ids: tuple[int, ...]
    next_after_id: int

    def __post_init__(self) -> None:
        selected = _id_tuple(self.selected_ids, "selected ids")
        groups = (
            _id_tuple(self.applied_ids, "applied ids"),
            _id_tuple(self.idempotent_ids, "idempotent ids"),
            _id_tuple(self.skipped_ids, "skipped ids"),
        )
        flattened = [item for group in groups for item in group]
        if len(flattened) != len(set(flattened)) or set(flattened) != set(selected):
            raise ValueError("legacy migration outcomes must cover selected ids")
        next_after_id = _nonnegative_signed_64(self.next_after_id, "next after id")
        if selected and next_after_id != selected[-1]:
            raise ValueError("next after id must equal the last selected feedback id")

    @property
    def selected_count(self) -> int:
        return len(self.selected_ids)

    @property
    def applied_count(self) -> int:
        return len(self.applied_ids)

    @property
    def idempotent_count(self) -> int:
        return len(self.idempotent_ids)

    @property
    def skipped_count(self) -> int:
        return len(self.skipped_ids)


@dataclass(frozen=True)
class EnqueueReport:
    feedback_ids: tuple[int, ...]
    next_cursor: int

    def __post_init__(self) -> None:
        feedback_ids = _id_tuple(self.feedback_ids, "enqueued feedback ids")
        next_cursor = _nonnegative_signed_64(self.next_cursor, "history cursor")
        if feedback_ids and next_cursor != feedback_ids[-1]:
            raise ValueError("history cursor must equal the last selected feedback id")


@dataclass(frozen=True)
class CanaryReport:
    """Privacy-safe proof of one exact verified canary version."""

    feedback_id: int
    projection_version: int
    target_identity_ok: bool
    synchronized: bool
    verified_attempt: bool
    compound_match_count: int
    compound_matches_saved: bool
    readback_matches: bool

    def __post_init__(self) -> None:
        _positive_signed_64(self.feedback_id, "feedback id")
        _positive_signed_64(self.projection_version, "projection version")
        for value in (
            self.target_identity_ok,
            self.synchronized,
            self.verified_attempt,
            self.compound_matches_saved,
            self.readback_matches,
        ):
            if value is not True:
                raise ValueError("canary proof flags must be exactly true")
        if type(self.compound_match_count) is not int or self.compound_match_count != 1:
            raise ValueError("canary compound match count must be exactly one")


def propose_legacy_status(stage: object) -> str | None:
    """Map only one exact finite legacy stage name."""
    if type(stage) is not str:
        return None
    return _STAGE_STATUS.get(stage)


def _preflight_from_inspection(inspection: object) -> PreflightReport:
    if type(inspection) is not TargetInspection:
        raise ContractError("target inspection response was malformed")
    fields_ok = (
        not any(
            (
                inspection.missing_fields,
                inspection.wrong_types,
                inspection.wrong_relations,
                inspection.readonly_fields,
                inspection.missing_selections,
                inspection.wrong_selections,
            )
        )
        and inspection.source_value_present
    )
    return PreflightReport(
        database_uuid_matches=inspection.database_uuid_matches,
        company_matches=inspection.company_matches,
        fields_ok=fields_ok,
        missing_fields=tuple(inspection.missing_fields),
        wrong_types=tuple(inspection.wrong_types),
        missing_selections=tuple(inspection.missing_selections),
        source_value_present=inspection.source_value_present,
        wrong_relations=tuple(inspection.wrong_relations),
        readonly_fields=tuple(inspection.readonly_fields),
        wrong_selections=tuple(inspection.wrong_selections),
    )


def preflight(client) -> PreflightReport:
    """Run a fresh sanitized read-only identity and target-contract inspection."""
    inspect = getattr(client, "inspect_target", None)
    if not callable(inspect):
        raise ContractError("client does not support target inspection")
    return _preflight_from_inspection(inspect())


def _validated_rollout_rows(value: object, *, after_id: int, limit: int) -> list[dict]:
    if type(value) is not list or len(value) > limit:
        raise ValueError("feedback rollout rows were malformed")
    required = {
        "id",
        "message",
        "task_type",
        "created_at",
        "submitter",
        "status",
        "finished_at",
        "finished_by",
        "resolution_note",
        "projection_version",
        "lifecycle_origin",
        "legacy_lifecycle_migrated_at",
        "updated_at",
        "odoo_task_id",
        "odoo_improvement_id",
    }
    rows: list[dict] = []
    previous = after_id
    for row in value:
        if not isinstance(row, Mapping) or not required <= set(row):
            raise ValueError("feedback rollout row was malformed")
        detached = dict(row)
        feedback_id = _positive_signed_64(detached.get("id"), "feedback id")
        if feedback_id <= previous:
            raise ValueError("feedback rollout rows were unordered or duplicated")
        _positive_signed_64(detached.get("projection_version"), "projection version")
        task_id = detached.get("odoo_task_id")
        if task_id is not None:
            _positive_signed_64(task_id, "legacy task id")
        remote_id = detached.get("odoo_improvement_id")
        if remote_id is not None:
            _positive_signed_64(remote_id, "remote id")
        status = detached.get("status")
        origin = detached.get("lifecycle_origin")
        if status is not None and status not in _STATUSES:
            raise ValueError("feedback rollout status was malformed")
        if origin not in {None, "local", "legacy_project_task"}:
            raise ValueError("feedback rollout origin was malformed")
        if (origin is None) is not (status is None):
            raise ValueError("feedback rollout authority was malformed")
        previous = feedback_id
        rows.append(detached)
    return rows


def _legacy_stages(client, task_ids: list[int]) -> dict[int, str | None]:
    if not task_ids:
        return {}
    result = client.read_legacy_task_stages(list(task_ids))
    if type(result) is not list or len(result) > len(task_ids):
        raise ContractError("legacy task stage response was malformed")
    expected = set(task_ids)
    stages: dict[int, str | None] = {}
    for row in result:
        if not isinstance(row, Mapping):
            raise ContractError("legacy task stage response was malformed")
        task_id = _positive_signed_64(row.get("id"), "legacy task id")
        if task_id not in expected or task_id in stages or "stage_id" not in row:
            raise ContractError("legacy task stage response was malformed")
        stage = row.get("stage_id")
        if stage is False or stage is None:
            stages[task_id] = None
        elif (
            type(stage) is list
            and len(stage) == 2
            and type(stage[0]) is int
            and 0 < stage[0] <= MAX_SIGNED_64
            and type(stage[1]) is str
        ):
            stages[task_id] = stage[1]
        else:
            raise ContractError("legacy task stage response was malformed")
    return stages


def _compound_rows(value: object, source_id: str) -> list[dict]:
    if type(value) is not list or len(value) > 3:
        raise ContractError("exact lookup response was malformed")
    rows: list[dict] = []
    seen: set[int] = set()
    for row in value:
        if not isinstance(row, Mapping):
            raise ContractError("exact lookup response was malformed")
        remote_id = _positive_signed_64(row.get("id"), "remote id")
        if (
            remote_id in seen
            or row.get("x_studio_source") != SOURCE_VALUE
            or row.get("x_studio_source_id") != source_id
        ):
            raise ContractError("exact lookup response was malformed")
        seen.add(remote_id)
        rows.append(dict(row))
    return rows


def dry_run_batch(*, after_id: int, batch_size: int, client) -> DryRunReport:
    """Classify one bounded would-be migration/sync page without local or remote writes."""
    safe_after_id = _nonnegative_signed_64(after_id, "after id")
    limit = _batch_size(batch_size, "dry-run batch size")
    inspect = getattr(client, "inspect_target", None)
    if not callable(inspect):
        raise ContractError("client does not support target inspection")
    inspection = inspect()
    report = _preflight_from_inspection(inspection)
    if (
        not report.database_uuid_matches
        or not report.company_matches
        or not report.fields_ok
        or inspection.start_type is None
        or inspection.stop_type is None
    ):
        raise TargetIdentityError("dedicated Odoo target identity or contract mismatch")

    selected = _validated_rollout_rows(
        feedback_store.feedback_after(safe_after_id, limit),
        after_id=safe_after_id,
        limit=limit,
    )
    task_ids = sorted(
        {
            row["odoo_task_id"]
            for row in selected
            if row["status"] is None and row["odoo_task_id"] is not None
        }
    )
    stages = _legacy_stages(client, task_ids)
    feedback_ids = tuple(row["id"] for row in selected)
    projected_ids: list[int] = []
    skipped_ids: list[int] = []
    outcomes: dict[str, list[int]] = {
        "create": [],
        "adopt": [],
        "update": [],
        "duplicate": [],
        "ownership_conflict": [],
    }
    warning_counts = {"employee_missing": 0, "employee_ambiguous": 0}
    image_counts = {"before": 0, "after": 0}

    for stored in selected:
        feedback_id = stored["id"]
        legacy_status: str | None = None
        if stored["status"] is None:
            stage_name = stages.get(stored["odoo_task_id"])
            legacy_status = propose_legacy_status(stage_name)
            if legacy_status is None:
                skipped_ids.append(feedback_id)
                continue
        snapshot = feedback_store.rollout_snapshot(
            feedback_id=feedback_id,
            expected_projection_version=stored["projection_version"],
            expected_odoo_task_id=stored["odoo_task_id"],
        )
        if type(snapshot) is not feedback_store.RolloutSnapshot:
            raise ValueError("feedback rollout snapshot was malformed")
        projected = dict(snapshot.feedback)
        if (
            projected.get("id") != feedback_id
            or projected.get("projection_version") != stored["projection_version"]
            or projected.get("odoo_task_id") != stored["odoo_task_id"]
            or projected.get("status") != stored["status"]
            or projected.get("lifecycle_origin") != stored["lifecycle_origin"]
        ):
            raise ValueError("feedback rollout snapshot changed selection authority")
        if legacy_status is not None:
            projected.update(
                status=legacy_status,
                lifecycle_origin="legacy_project_task",
                projection_version=stored["projection_version"] + 1,
                finished_at=None,
                finished_by=None,
                resolution_note=None,
            )
        images = snapshot.images
        if not isinstance(images, Mapping) or len(images) > 2:
            raise ValueError("feedback rollout images were malformed")
        for role in images:
            if type(role) is not str or role not in image_counts:
                raise ValueError("feedback rollout image role was malformed")
            image_counts[role] += 1

        def warn(warning_feedback_id: int, version: int, warning_class: str) -> None:
            if warning_feedback_id != feedback_id:
                raise ValueError("employee warning belonged to another feedback row")
            _positive_signed_64(version, "projection version")
            if warning_class not in warning_counts:
                raise ValueError("employee warning class was malformed")
            warning_counts[warning_class] += 1

        def employee_resolver(email: object) -> int | None:
            return resolve_employee_id(
                client,
                email,
                feedback_id=feedback_id,
                projection_version=projected["projection_version"],
                warn=warn,
            )

        projection = build_projection(
            projected,
            images=images,
            employee_lookup=lambda _email: None,
            start_type=inspection.start_type,
            stop_type=inspection.stop_type,
            employee_resolver=employee_resolver,
        )
        source_id = source_id_for(feedback_id)
        if projection.source_id != source_id:
            raise ValueError("projection source identity changed")
        matches = _compound_rows(client.find_exact(source_id), source_id)
        saved_remote_id = projected.get("odoo_improvement_id")
        if len(matches) > 1:
            outcome = "duplicate"
        elif not matches:
            outcome = "create" if saved_remote_id is None else "ownership_conflict"
        elif saved_remote_id is None:
            outcome = "adopt"
        elif matches[0]["id"] == saved_remote_id:
            outcome = "update"
        else:
            outcome = "ownership_conflict"
        outcomes[outcome].append(feedback_id)
        projected_ids.append(feedback_id)

    next_after_id = feedback_ids[-1] if feedback_ids else safe_after_id
    return DryRunReport(
        requested_batch_size=limit,
        feedback_ids=feedback_ids,
        projected_ids=tuple(projected_ids),
        skipped_ids=tuple(skipped_ids),
        create_ids=tuple(outcomes["create"]),
        adopt_ids=tuple(outcomes["adopt"]),
        update_ids=tuple(outcomes["update"]),
        duplicate_ids=tuple(outcomes["duplicate"]),
        ownership_conflict_ids=tuple(outcomes["ownership_conflict"]),
        employee_missing_count=warning_counts["employee_missing"],
        employee_ambiguous_count=warning_counts["employee_ambiguous"],
        before_image_count=image_counts["before"],
        after_image_count=image_counts["after"],
        next_after_id=next_after_id,
    )


def apply_legacy_batch(
    *,
    rows: list[Mapping[str, object]],
    stages: Mapping[int, str],
    now: datetime,
) -> LegacyApplyReport:
    """Apply only supplied exact legacy stage facts to one bounded local page."""
    current = _aware_datetime(now, "legacy migration time")
    if type(rows) is not list or len(rows) > MAX_BATCH_SIZE:
        raise ValueError("legacy rows must be a list of at most 100 mappings")
    if not isinstance(stages, Mapping) or len(stages) > MAX_BATCH_SIZE:
        raise ValueError("legacy stages must be a bounded mapping")
    safe_stages: dict[int, str] = {}
    for task_id, stage in stages.items():
        safe_task_id = _positive_signed_64(task_id, "legacy task id")
        if type(stage) is not str:
            raise ValueError("legacy stage names must be exact strings")
        safe_stages[safe_task_id] = stage
    normalized: list[tuple[int, int, int]] = []
    seen_task_ids: set[int] = set()
    previous_feedback_id = 0
    for row in rows:
        if not isinstance(row, Mapping) or not {
            "id",
            "odoo_task_id",
            "projection_version",
        } <= set(row):
            raise ValueError("legacy row was malformed")
        feedback_id = _positive_signed_64(row.get("id"), "feedback id")
        task_id = _positive_signed_64(row.get("odoo_task_id"), "legacy task id")
        projection_version = _positive_signed_64(
            row.get("projection_version"),
            "projection version",
        )
        _positive_signed_64(
            projection_version + 1,
            "resulting projection version",
        )
        if feedback_id <= previous_feedback_id or task_id in seen_task_ids:
            raise ValueError("legacy row or task ids were duplicated or unordered")
        normalized.append((feedback_id, task_id, projection_version))
        previous_feedback_id = feedback_id
        seen_task_ids.add(task_id)

    applied: list[int] = []
    idempotent: list[int] = []
    skipped: list[int] = []
    for feedback_id, task_id, projection_version in normalized:
        status = propose_legacy_status(safe_stages.get(task_id))
        if status is None:
            skipped.append(feedback_id)
            continue
        changed = feedback_store.apply_legacy_status(
            feedback_id=feedback_id,
            expected_odoo_task_id=task_id,
            expected_projection_version=projection_version,
            status=status,
            now=current,
        )
        if type(changed) is not bool:
            raise ValueError("legacy migration store result was malformed")
        (applied if changed else idempotent).append(feedback_id)
    return LegacyApplyReport(
        applied_ids=tuple(applied),
        idempotent_ids=tuple(idempotent),
        skipped_ids=tuple(skipped),
    )


def migrate_legacy_batch(
    *,
    after_id: int,
    batch_size: int,
    client,
    now: datetime,
) -> LegacyMigrationReport:
    """Inspect and apply one bounded page of exact legacy task stages."""
    safe_after_id = _nonnegative_signed_64(after_id, "after id")
    limit = _batch_size(batch_size, "legacy migration batch size")
    current = _aware_datetime(now, "legacy migration time")
    inspection = getattr(client, "inspect_target", None)
    if not callable(inspection):
        raise ContractError("client does not support target inspection")
    target = inspection()
    preflight_report = _preflight_from_inspection(target)
    if (
        not preflight_report.database_uuid_matches
        or not preflight_report.company_matches
        or not preflight_report.fields_ok
        or target.start_type is None
        or target.stop_type is None
    ):
        raise TargetIdentityError("dedicated Odoo target identity or contract mismatch")

    selected = _validated_rollout_rows(
        feedback_store.feedback_after(safe_after_id, limit),
        after_id=safe_after_id,
        limit=limit,
    )
    selected_ids = tuple(row["id"] for row in selected)
    candidates = [
        {
            "id": row["id"],
            "odoo_task_id": row["odoo_task_id"],
            "projection_version": row["projection_version"],
        }
        for row in selected
        if row["status"] is None and row["odoo_task_id"] is not None
    ]
    task_ids = sorted(row["odoo_task_id"] for row in candidates)
    if len(task_ids) != len(set(task_ids)):
        raise ValueError("legacy task associations were duplicated")
    stage_facts = _legacy_stages(client, task_ids)
    exact_stages = {task_id: stage for task_id, stage in stage_facts.items() if type(stage) is str}
    applied = apply_legacy_batch(rows=candidates, stages=exact_stages, now=current)
    candidate_ids = {row["id"] for row in candidates}
    apply_ids = set(applied.applied_ids) | set(applied.idempotent_ids) | set(applied.skipped_ids)
    if apply_ids != candidate_ids:
        raise ValueError("legacy apply report did not cover candidate ids")
    skipped = tuple(
        sorted(
            set(applied.skipped_ids)
            | {row["id"] for row in selected if row["id"] not in candidate_ids}
        )
    )
    return LegacyMigrationReport(
        selected_ids=selected_ids,
        applied_ids=applied.applied_ids,
        idempotent_ids=applied.idempotent_ids,
        skipped_ids=skipped,
        next_after_id=selected_ids[-1] if selected_ids else safe_after_id,
    )


def enqueue_history_batch(*, batch_size: int, now: datetime) -> EnqueueReport:
    """Run one bounded local-only restart-safe history enqueue page."""
    limit = _batch_size(batch_size, "history batch size")
    current = _aware_datetime(now, "history enqueue time")
    value = feedback_store.enqueue_history_batch(batch_size=limit, now=current)
    if type(value) is EnqueueReport:
        return EnqueueReport(
            feedback_ids=tuple(value.feedback_ids),
            next_cursor=value.next_cursor,
        )
    if not isinstance(value, Mapping) or set(value) != {"feedback_ids", "next_cursor"}:
        raise ValueError("history enqueue store result was malformed")
    feedback_ids = value.get("feedback_ids")
    if type(feedback_ids) is not tuple:
        raise ValueError("history enqueue ids were malformed")
    return EnqueueReport(
        feedback_ids=tuple(feedback_ids),
        next_cursor=value.get("next_cursor"),
    )


def _saved_canary_projection(evidence: sync_store.VerifiedCanaryEvidence) -> Projection:
    attempt = evidence.attempt
    manifest = attempt.manifest
    fields = manifest.get("fields")
    if type(fields) is not dict:
        raise ContractError("saved canary projection was malformed")
    source_id = fields.get("x_studio_source_id")
    if source_id != source_id_for(evidence.feedback_id):
        raise ContractError("saved canary source identity was malformed")
    saved_binaries = attempt.binaries
    binaries: dict[str, BinaryEvidence] = {}
    if saved_binaries:
        try:
            images = feedback_store.attempt_image_snapshot(
                evidence.feedback_id,
                saved_binaries,
            )
        except feedback_store.ProjectionSnapshotUnavailable:
            raise ContractError("saved canary binary evidence was unavailable") from None
        if set(images) != set(saved_binaries):
            raise ContractError("saved canary binary evidence was malformed")
        for field_name, saved in saved_binaries.items():
            image = images.get(field_name)
            if (
                image is None
                or image.sha256 != saved.get("sha256")
                or image.byte_length != saved.get("byte_length")
                or len(image.jpeg_bytes) != image.byte_length
            ):
                raise ContractError("saved canary binary evidence changed")
            binaries[field_name] = BinaryEvidence(
                jpeg_bytes=bytes(image.jpeg_bytes),
                sha256=image.sha256,
                byte_length=image.byte_length,
            )
    try:
        return Projection(
            source_id=source_id,
            fields=fields,
            binaries=binaries,
            manifest=manifest,
            manifest_digest=attempt.manifest_digest,
        )
    except (TypeError, ValueError):
        raise ContractError("saved canary projection was malformed") from None


def canary_report(*, feedback_id: int, client) -> CanaryReport:
    """Read back one exact saved verified canary without any mutation path."""
    safe_feedback_id = _positive_signed_64(feedback_id, "feedback id")
    verify_identity = getattr(client, "verify_target_identity", None)
    if not callable(verify_identity):
        raise ContractError("client does not support target identity verification")
    contract = verify_identity()
    if (
        type(contract) is not ImprovementContract
        or contract.start_type not in {"date", "datetime"}
        or contract.stop_type not in {"date", "datetime"}
    ):
        raise ContractError("target identity verification returned a malformed contract")
    evidence = sync_store.load_verified_canary_evidence(safe_feedback_id)
    if type(evidence) is not sync_store.VerifiedCanaryEvidence:
        raise ContractError("verified canary evidence was malformed")
    projection = _saved_canary_projection(evidence)
    if not _saved_dates_match_contract(projection, contract):
        raise ContractError("saved canary dates do not match the current contract")
    matches = _compound_rows(client.find_exact(projection.source_id), projection.source_id)
    if len(matches) != 1 or matches[0]["id"] != evidence.remote_id:
        raise TargetIdentityError("canary compound identity did not match saved authority")
    read_fields = sorted(set(projection.fields) | set(projection.binaries))
    remote = client.read_improvement(
        evidence.remote_id,
        read_fields,
        full_binary=True,
    )
    verify_readback(projection, remote)
    refreshed = sync_store.load_verified_canary_evidence(safe_feedback_id)
    if type(refreshed) is not sync_store.VerifiedCanaryEvidence or refreshed != evidence:
        raise sync_store.StateTransitionError("verified canary authority changed during readback")
    return CanaryReport(
        feedback_id=evidence.feedback_id,
        projection_version=evidence.projection_version,
        target_identity_ok=True,
        synchronized=True,
        verified_attempt=True,
        compound_match_count=1,
        compound_matches_saved=True,
        readback_matches=True,
    )


def reconciliation_counts() -> dict[str, int]:
    """Return validated local-only counts using only the two exact gate names."""
    gates_open = (
        os.environ.get("ODOO_SHARED_REPORTING_WRITE_ENABLED") == "true"
        and os.environ.get("ODOO_IMPROVEMENTS_WRITE_ENABLED") == "true"
    )
    value = feedback_store.reconciliation_counts(gates_open)
    if not isinstance(value, Mapping) or set(value) != _COUNT_KEYS:
        raise ValueError("feedback reconciliation result was malformed")
    return {
        key: _nonnegative_signed_64(value.get(key), f"{key} count") for key in sorted(_COUNT_KEYS)
    }


__all__ = [
    "CanaryReport",
    "DryRunReport",
    "EnqueueReport",
    "LegacyApplyReport",
    "LegacyMigrationReport",
    "PreflightReport",
    "apply_legacy_batch",
    "canary_report",
    "dry_run_batch",
    "enqueue_history_batch",
    "migrate_legacy_batch",
    "preflight",
    "propose_legacy_status",
    "reconciliation_counts",
]
