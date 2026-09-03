"""Persistence for user-submitted feedback (index linking submitter → Odoo task)."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from decimal import Decimal
from types import MappingProxyType
from typing import Mapping
from uuid import UUID, uuid4

from . import db, feedback_task_delivery
from .feedback_image import MAX_OUTPUT_BYTES, OUTPUT_LONG_SIDE, NormalizedImage
from .feedback_types import FEEDBACK_TYPES, feedback_type, feedback_type_or_legacy_bug


_MAX_SIGNED_64 = 9_223_372_036_854_775_807
_WARNING_CLASSES = frozenset({"employee_missing", "employee_ambiguous"})
_DIGEST_RE = re.compile(r"[0-9a-f]{64}", re.ASCII)
_ATTEMPT_BINARY_ROLES = {
    "x_studio_image": "before",
    "x_studio_after_image": "after",
}
REVIEW_RECONCILE_LEASE = timedelta(minutes=10)


class InvalidTransition(ValueError):
    pass


class ProjectionSnapshotUnavailable(ValueError):
    """The exact safe local feedback version could not be snapshotted."""


@dataclass(frozen=True)
class ProjectionSnapshot:
    """One immutable local feedback version and its bounded images."""

    feedback: Mapping[str, object]
    images: Mapping[str, NormalizedImage]


@dataclass(frozen=True)
class RolloutSnapshot:
    """One exact rollout row and bounded images captured under one lock."""

    feedback: Mapping[str, object]
    images: Mapping[str, NormalizedImage]


@dataclass(frozen=True)
class ReviewCandidate:
    """Exact local identities needed to reconcile one Odoo-owned review."""

    feedback_id: int
    task_type: str
    status: str
    projection_version: int
    odoo_task_id: int
    odoo_improvement_id: int
    finished_at: datetime | None = None
    finished_by: str | None = None
    resolution_note: str | None = None
    sync_claim_owner: str | None = None
    sync_claim_token: UUID | None = None
    sync_claim_expires_at: datetime | None = None
    sync_prior_state: str | None = None

    def __post_init__(self) -> None:
        _positive_signed_64(self.feedback_id, "feedback id")
        _positive_signed_64(self.projection_version, "projection version")
        _positive_signed_64(self.odoo_task_id, "task id")
        _positive_signed_64(self.odoo_improvement_id, "remote id")
        canonical_type = feedback_type(self.task_type)
        if canonical_type.behavior != "review" or canonical_type.odoo_value is None:
            raise ValueError("feedback type is not review managed")
        if self.status not in _TRANSITIONS:
            raise ValueError("feedback status is malformed")
        claim_values = (
            self.sync_claim_owner,
            self.sync_claim_token,
            self.sync_claim_expires_at,
            self.sync_prior_state,
        )
        if any(value is not None for value in claim_values):
            if (
                type(self.sync_claim_owner) is not str
                or not self.sync_claim_owner.strip()
                or type(self.sync_claim_token) is not UUID
                or self.sync_claim_expires_at is None
                or self.sync_prior_state not in {"idle", "quarantined"}
            ):
                raise ValueError("review sync claim is malformed")
            _aware_datetime(self.sync_claim_expires_at, "review sync claim expiry")


@dataclass(frozen=True)
class ReviewReconcileLease:
    """One crash-recoverable authority for a complete review batch."""

    owner: str
    token: UUID
    expires_at: datetime

    def __post_init__(self) -> None:
        if type(self.owner) is not str or not self.owner.strip() or len(self.owner) > 128:
            raise ValueError("review reconciliation lease owner is malformed")
        if type(self.token) is not UUID:
            raise ValueError("review reconciliation lease token is malformed")
        _aware_datetime(self.expires_at, "review reconciliation lease expiry")


_TRANSITIONS = {
    "requested": {"in_progress", "completed", "declined"},
    "in_progress": {"completed", "declined"},
    "completed": set(),
    "declined": set(),
}


def _clamp_limit(limit, default: int = 100) -> int:
    try:
        value = int(limit)
    except (TypeError, ValueError):
        value = default
    return max(1, min(value, 500))


def _positive_signed_64(value: object, label: str) -> int:
    if type(value) is not int or not 0 < value <= _MAX_SIGNED_64:
        raise ValueError(f"{label} must be a positive signed-64-bit integer")
    return value


def _nonnegative_signed_64(value: object, label: str) -> int:
    if type(value) is not int or not 0 <= value <= _MAX_SIGNED_64:
        raise ValueError(f"{label} must be a nonnegative signed-64-bit integer")
    return value


def _normalized_submitter_email(value: object) -> str:
    if type(value) is not str:
        raise ValueError("submitter email must be valid")
    normalized = value.strip().casefold()
    if (
        not normalized
        or normalized.count("@") != 1
        or any(character.isspace() for character in normalized)
    ):
        raise ValueError("submitter email must be valid")
    return normalized


def _nonnegative_numeric_aggregate(value: object, label: str) -> int:
    """Convert only PostgreSQL's finite integral numeric aggregate shape."""
    if type(value) is Decimal:
        if (
            not value.is_finite()
            or value != value.to_integral_value()
            or not Decimal(0) <= value <= Decimal(_MAX_SIGNED_64)
        ):
            raise ValueError(f"{label} must be an integral database count")
        value = int(value)
    return _nonnegative_signed_64(value, label)


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


def _snapshot_image(row: Mapping[str, object], feedback_id: int) -> NormalizedImage:
    if type(row.get("feedback_id")) is not int or row.get("feedback_id") != feedback_id:
        raise ProjectionSnapshotUnavailable("image belongs to another feedback record")
    raw_value = row.get("jpeg_bytes")
    if not isinstance(raw_value, (bytes, bytearray, memoryview)):
        raise ProjectionSnapshotUnavailable("image bytes are malformed")
    raw = bytes(raw_value)
    if not raw or len(raw) > MAX_OUTPUT_BYTES:
        raise ProjectionSnapshotUnavailable("image bytes are malformed")

    byte_length = row.get("byte_length")
    if (
        type(byte_length) is not int
        or byte_length != len(raw)
        or not 0 < byte_length <= MAX_OUTPUT_BYTES
    ):
        raise ProjectionSnapshotUnavailable("image length is malformed")
    digest = row.get("sha256")
    if type(digest) is not str or digest != hashlib.sha256(raw).hexdigest():
        raise ProjectionSnapshotUnavailable("image hash is malformed")
    width = row.get("width")
    height = row.get("height")
    if (
        type(width) is not int
        or type(height) is not int
        or not 0 < width <= OUTPUT_LONG_SIDE
        or not 0 < height <= OUTPUT_LONG_SIDE
    ):
        raise ProjectionSnapshotUnavailable("image dimensions are malformed")
    return NormalizedImage(
        jpeg_bytes=raw,
        sha256=digest,
        byte_length=byte_length,
        width=width,
        height=height,
    )


def projection_snapshot(feedback_id: int, projection_version: int) -> ProjectionSnapshot:
    """Read one exact local version and its images in one short transaction."""
    safe_feedback_id = _positive_signed_64(feedback_id, "feedback id")
    safe_version = _positive_signed_64(projection_version, "projection version")
    with db.cursor() as cur:
        cur.execute(
            "SELECT id, message, task_type, created_at, submitter, status, "
            "finished_at, finished_by, resolution_note, projection_version, "
            "lifecycle_origin FROM feedback "
            "WHERE id = %s AND projection_version = %s "
            "AND lifecycle_origin IN ('local', 'legacy_project_task') "
            "AND status IS NOT NULL FOR SHARE",
            (safe_feedback_id, safe_version),
        )
        row = cur.fetchone()
        cur.execute(
            "SELECT feedback_id, role, jpeg_bytes, sha256, byte_length, width, height "
            "FROM feedback_images WHERE feedback_id = %s ORDER BY role LIMIT 3",
            (safe_feedback_id,),
        )
        image_rows = cur.fetchall()

    if not isinstance(row, Mapping):
        raise ProjectionSnapshotUnavailable("exact local feedback version is unavailable")
    feedback = dict(row)
    if (
        type(feedback.get("id")) is not int
        or feedback.get("id") != safe_feedback_id
        or type(feedback.get("projection_version")) is not int
        or feedback.get("projection_version") != safe_version
        or feedback.get("lifecycle_origin") not in {"local", "legacy_project_task"}
        or feedback.get("status") not in _TRANSITIONS
    ):
        raise ProjectionSnapshotUnavailable("exact local feedback version is unavailable")
    if type(image_rows) is not list or len(image_rows) > 2:
        raise ProjectionSnapshotUnavailable("feedback image rows are malformed")

    images: dict[str, NormalizedImage] = {}
    for image_row in image_rows:
        if not isinstance(image_row, Mapping):
            raise ProjectionSnapshotUnavailable("feedback image row is malformed")
        role = image_row.get("role")
        if type(role) is not str or role not in {"before", "after"}:
            raise ProjectionSnapshotUnavailable("feedback image role is malformed")
        if role in images:
            raise ProjectionSnapshotUnavailable("duplicate image role")
        images[role] = _snapshot_image(image_row, safe_feedback_id)

    return ProjectionSnapshot(
        feedback=MappingProxyType(feedback),
        images=MappingProxyType(images),
    )


def lifecycle_state(feedback_id: int) -> Mapping[str, object]:
    """Read the exact local lifecycle fields needed by operator commands."""
    safe_feedback_id = _positive_signed_64(feedback_id, "feedback id")
    with db.cursor() as cur:
        cur.execute(
            "SELECT f.id, f.status, f.lifecycle_origin, f.projection_version, "
            "td.state AS task_sync_state, td.desired_version AS task_desired_version, "
            "td.last_synced_version AS task_last_synced_version, "
            "td.desired_contract_version AS task_desired_contract_version, "
            "td.last_synced_contract_version AS task_last_synced_contract_version "
            "FROM feedback f LEFT JOIN feedback_task_delivery td ON td.feedback_id = f.id "
            "WHERE f.id = %s",
            (safe_feedback_id,),
        )
        row = cur.fetchone()

    if not isinstance(row, Mapping):
        raise InvalidTransition("feedback lifecycle state is unavailable")
    state = dict(row)
    if (
        set(state)
        != {
            "id",
            "status",
            "lifecycle_origin",
            "projection_version",
            "task_sync_state",
            "task_desired_version",
            "task_last_synced_version",
            "task_desired_contract_version",
            "task_last_synced_contract_version",
        }
        or type(state.get("id")) is not int
        or state.get("id") != safe_feedback_id
        or state.get("status") not in _TRANSITIONS
        or state.get("lifecycle_origin") != "local"
        or type(state.get("projection_version")) is not int
        or not 0 < state["projection_version"] <= _MAX_SIGNED_64
        or state.get("task_sync_state")
        not in {"pending", "in_flight", "attention", "delivered", "blocked"}
        or type(state.get("task_desired_version")) is not int
        or not 0 < state["task_desired_version"] <= _MAX_SIGNED_64
        or type(state.get("task_last_synced_version")) is not int
        or not 0 <= state["task_last_synced_version"] <= state["task_desired_version"]
        or type(state.get("task_desired_contract_version")) is not int
        or not 0
        < state["task_desired_contract_version"]
        <= _MAX_SIGNED_64
        or type(state.get("task_last_synced_contract_version")) is not int
        or not 0
        <= state["task_last_synced_contract_version"]
        <= state["task_desired_contract_version"]
    ):
        raise InvalidTransition("feedback lifecycle state is unavailable")
    return MappingProxyType(state)


def attempt_image_snapshot(
    feedback_id: int,
    binary_evidence: Mapping[str, Mapping[str, object]],
) -> Mapping[str, NormalizedImage]:
    """Copy only image bytes required by one immutable saved attempt."""
    safe_feedback_id = _positive_signed_64(feedback_id, "feedback id")
    if not isinstance(binary_evidence, Mapping):
        raise ProjectionSnapshotUnavailable("saved image evidence is malformed")
    expected: dict[str, tuple[str, int]] = {}
    for field_name, evidence in binary_evidence.items():
        if (
            type(field_name) is not str
            or field_name not in _ATTEMPT_BINARY_ROLES
            or not isinstance(evidence, Mapping)
            or set(evidence) != {"sha256", "byte_length"}
        ):
            raise ProjectionSnapshotUnavailable("saved image evidence is malformed")
        digest = evidence.get("sha256")
        byte_length = evidence.get("byte_length")
        if (
            type(digest) is not str
            or _DIGEST_RE.fullmatch(digest) is None
            or type(byte_length) is not int
            or not 0 < byte_length <= MAX_OUTPUT_BYTES
        ):
            raise ProjectionSnapshotUnavailable("saved image evidence is malformed")
        expected[field_name] = (digest, byte_length)
    if not expected:
        return MappingProxyType({})

    required_roles = sorted(_ATTEMPT_BINARY_ROLES[field_name] for field_name in expected)
    with db.cursor() as cur:
        cur.execute(
            "SELECT feedback_id, role, jpeg_bytes, sha256, byte_length, width, height "
            "FROM feedback_images WHERE feedback_id = %s AND role = ANY(%s) "
            "AND byte_length > 0 AND byte_length <= %s "
            "AND octet_length(jpeg_bytes) > 0 "
            "AND octet_length(jpeg_bytes) <= %s "
            "AND octet_length(jpeg_bytes) = byte_length "
            "ORDER BY role LIMIT 2",
            (
                safe_feedback_id,
                required_roles,
                MAX_OUTPUT_BYTES,
                MAX_OUTPUT_BYTES,
            ),
        )
        rows = cur.fetchall()
    if type(rows) is not list or len(rows) != len(required_roles) or len(rows) > 2:
        raise ProjectionSnapshotUnavailable("feedback image rows are malformed")

    by_role: dict[str, Mapping[str, object]] = {}
    for row in rows:
        if not isinstance(row, Mapping):
            raise ProjectionSnapshotUnavailable("feedback image row is malformed")
        role = row.get("role")
        if type(role) is not str or role not in {"before", "after"}:
            raise ProjectionSnapshotUnavailable("feedback image role is malformed")
        if role in by_role:
            raise ProjectionSnapshotUnavailable("duplicate image role")
        by_role[role] = row
    if set(by_role) != set(required_roles):
        raise ProjectionSnapshotUnavailable("saved image roles are unavailable")

    selected: dict[str, NormalizedImage] = {}
    for field_name, (digest, byte_length) in expected.items():
        row = by_role.get(_ATTEMPT_BINARY_ROLES[field_name])
        if row is None:
            raise ProjectionSnapshotUnavailable("saved image is unavailable")
        image = _snapshot_image(row, safe_feedback_id)
        if image.sha256 != digest or image.byte_length != byte_length:
            raise ProjectionSnapshotUnavailable("saved image evidence changed")
        selected[field_name] = image
    return MappingProxyType(selected)


def record_sync_warning(
    feedback_id: int,
    projection_version: int,
    warning_class: str,
) -> None:
    """Persist one safe warning without an email or remote payload."""
    safe_feedback_id = _positive_signed_64(feedback_id, "feedback id")
    safe_version = _positive_signed_64(projection_version, "projection version")
    if type(warning_class) is not str or warning_class not in _WARNING_CLASSES:
        raise ValueError("unsupported feedback sync warning")
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO feedback_odoo_warnings "
            "(feedback_id, projection_version, warning_class) VALUES (%s, %s, %s) "
            "ON CONFLICT (feedback_id, projection_version, warning_class) DO NOTHING",
            (safe_feedback_id, safe_version, warning_class),
        )


def create_submission(
    *,
    message: str,
    submitter: str,
    submitter_employee_odoo_id: int,
    page_url: str | None,
    task_type: str,
    status: str = "requested",
    before_image: NormalizedImage | None = None,
) -> int:
    """Atomically save new feedback, its optional image, and Odoo sync intent."""
    canonical_type = feedback_type(task_type)
    if canonical_type.odoo_value is None:
        raise ValueError("unsupported feedback type")
    if status != "requested":
        raise ValueError("new feedback must start requested")
    safe_employee_id = _positive_signed_64(
        submitter_employee_odoo_id, "submitter employee id"
    )
    safe_submitter = _normalized_submitter_email(submitter)
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO feedback "
            "(submitter, submitter_employee_odoo_id, page_url, task_type, message, "
            "status, lifecycle_origin, "
            "projection_version, updated_at) "
            "VALUES (%s, %s, %s, %s, %s, 'requested', 'local', 1, now()) "
            "RETURNING id",
            (safe_submitter, safe_employee_id, page_url, task_type, message),
        )
        feedback_id = int(cur.fetchone()["id"])
        if before_image is not None:
            cur.execute(
                "INSERT INTO feedback_images "
                "(feedback_id, role, jpeg_bytes, sha256, byte_length, width, height) "
                "VALUES (%s, 'before', %s, %s, %s, %s, %s)",
                (
                    feedback_id,
                    before_image.jpeg_bytes,
                    before_image.sha256,
                    before_image.byte_length,
                    before_image.width,
                    before_image.height,
                ),
            )
        cur.execute(
            "INSERT INTO feedback_odoo_sync "
            "(feedback_id, desired_version, last_synced_version, due_at, state) "
            "VALUES (%s, 1, 0, now(), 'idle')",
            (feedback_id,),
        )
        feedback_task_delivery.enqueue_submission(
            cur,
            feedback_id,
            desired_version=1,
            desired_status="requested",
        )
        return feedback_id


def for_submitter(submitter: str | None, limit: int = 100) -> list[dict]:
    """Return one submitter's feedback rows, newest first."""
    return db.query(
        "SELECT id, created_at, submitter, page_url, task_type, odoo_task_id, message, status "
        "FROM feedback WHERE submitter = %s ORDER BY id DESC LIMIT %s",
        (submitter, _clamp_limit(limit)),
    )


def acquire_review_reconcile_lease(
    *, owner: str, now: datetime
) -> ReviewReconcileLease | None:
    """Atomically acquire or reclaim the singleton review batch lease."""
    current = _aware_datetime(now, "review reconciliation lease time")
    if type(owner) is not str or not owner.strip() or len(owner) > 128:
        raise ValueError("review reconciliation lease owner is malformed")
    token = uuid4()
    expires = current + REVIEW_RECONCILE_LEASE
    with db.cursor() as cur:
        cur.execute(
            "UPDATE feedback_odoo_backfill_state SET review_lease_owner = %s, "
            "review_lease_token = %s, review_lease_expires_at = %s, updated_at = %s "
            "WHERE id = 1 AND (review_lease_token IS NULL "
            "OR review_lease_expires_at <= %s) "
            "RETURNING review_lease_owner, review_lease_token, review_lease_expires_at",
            (owner.strip(), token, expires, current, current),
        )
        row = cur.fetchone()
    if row is None:
        return None
    if not isinstance(row, Mapping):
        raise ProjectionSnapshotUnavailable(
            "review reconciliation lease response is malformed"
        )
    lease = ReviewReconcileLease(
        owner=row.get("review_lease_owner"),
        token=row.get("review_lease_token"),
        expires_at=row.get("review_lease_expires_at"),
    )
    if lease.owner != owner.strip() or lease.token != token or lease.expires_at != expires:
        raise ProjectionSnapshotUnavailable(
            "review reconciliation lease response is malformed"
        )
    return lease


def renew_review_reconcile_lease(
    lease: ReviewReconcileLease, *, now: datetime
) -> ReviewReconcileLease | None:
    """Extend only an unexpired lease still owned by the exact token."""
    if type(lease) is not ReviewReconcileLease:
        raise ValueError("review reconciliation lease is malformed")
    current = _aware_datetime(now, "review reconciliation renewal time")
    expires = current + REVIEW_RECONCILE_LEASE
    with db.cursor() as cur:
        cur.execute(
            "UPDATE feedback_odoo_backfill_state SET review_lease_expires_at = %s, "
            "updated_at = %s WHERE id = 1 AND review_lease_owner = %s "
            "AND review_lease_token = %s AND review_lease_expires_at > %s "
            "RETURNING review_lease_owner, review_lease_token, review_lease_expires_at",
            (expires, current, lease.owner, lease.token, current),
        )
        row = cur.fetchone()
    if row is None:
        return None
    if not isinstance(row, Mapping):
        raise ProjectionSnapshotUnavailable(
            "review reconciliation renewal response is malformed"
        )
    renewed = ReviewReconcileLease(
        owner=row.get("review_lease_owner"),
        token=row.get("review_lease_token"),
        expires_at=row.get("review_lease_expires_at"),
    )
    if renewed.owner != lease.owner or renewed.token != lease.token or renewed.expires_at != expires:
        raise ProjectionSnapshotUnavailable(
            "review reconciliation renewal response is malformed"
        )
    return renewed


def release_review_reconcile_lease(
    lease: ReviewReconcileLease, *, now: datetime
) -> bool:
    """Release only the singleton lease owned by the exact token."""
    if type(lease) is not ReviewReconcileLease:
        raise ValueError("review reconciliation lease is malformed")
    current = _aware_datetime(now, "review reconciliation release time")
    with db.cursor() as cur:
        cur.execute(
            "UPDATE feedback_odoo_backfill_state SET review_lease_owner = NULL, "
            "review_lease_token = NULL, review_lease_expires_at = NULL, updated_at = %s "
            "WHERE id = 1 AND review_lease_owner = %s AND review_lease_token = %s "
            "RETURNING id",
            (current, lease.owner, lease.token),
        )
        row = cur.fetchone()
    return isinstance(row, Mapping) and row.get("id") == 1


def review_reconcile_candidates(limit: int) -> list[ReviewCandidate]:
    """Return a durable rotating batch after both remote identities are known."""
    if type(limit) is not int or not 1 <= limit <= 500:
        raise ValueError("review reconciliation limit must be from 1 through 500")
    review_types = [
        item.value
        for item in FEEDBACK_TYPES
        if item.behavior == "review" and item.odoo_value is not None
    ]
    select = (
        "SELECT f.id AS feedback_id, f.task_type, f.status, f.projection_version, "
        "td.odoo_task_id, s.odoo_improvement_id, f.finished_at, f.finished_by, "
        "f.resolution_note FROM feedback f "
        "JOIN feedback_task_delivery td ON td.feedback_id = f.id "
        "JOIN feedback_odoo_sync s ON s.feedback_id = f.id "
        "WHERE f.lifecycle_origin = 'local' AND f.task_type = ANY(%s) "
        "AND f.status IS NOT NULL AND td.odoo_task_id IS NOT NULL "
        "AND s.odoo_improvement_id IS NOT NULL AND td.state = 'delivered' "
        "AND td.claim_owner IS NULL AND td.claim_token IS NULL "
        "AND td.claim_expires_at IS NULL AND s.state IN ('idle', 'quarantined') "
        "AND s.claim_owner IS NULL AND s.claim_token IS NULL "
        "AND s.claim_expires_at IS NULL AND s.active_attempt_id IS NULL "
    )
    try:
        with db.cursor() as cur:
            cur.execute(
                "SELECT last_review_feedback_id FROM feedback_odoo_backfill_state "
                "WHERE id = 1 FOR UPDATE",
                (),
            )
            state = cur.fetchone()
            if not isinstance(state, Mapping):
                raise ValueError("review reconciliation cursor is unavailable")
            cursor_id = _nonnegative_signed_64(
                state.get("last_review_feedback_id"), "review reconciliation cursor"
            )
            cur.execute(
                f"{select}AND f.id > %s ORDER BY f.id LIMIT %s",
                (review_types, cursor_id, limit),
            )
            rows = cur.fetchall()
            if type(rows) is not list or len(rows) > limit:
                raise ValueError("review reconciliation candidates are malformed")
            if len(rows) < limit and cursor_id > 0:
                remaining = limit - len(rows)
                cur.execute(
                    f"{select}AND f.id <= %s ORDER BY f.id LIMIT %s",
                    (review_types, cursor_id, remaining),
                )
                wrapped = cur.fetchall()
                if type(wrapped) is not list or len(wrapped) > remaining:
                    raise ValueError("review reconciliation candidates are malformed")
                rows.extend(wrapped)
            candidates = [ReviewCandidate(**dict(row)) for row in rows]
            ids = [item.feedback_id for item in candidates]
            if len(ids) != len(set(ids)):
                raise ValueError("review reconciliation candidates are malformed")
            if candidates:
                next_cursor = candidates[-1].feedback_id
                cur.execute(
                    "UPDATE feedback_odoo_backfill_state "
                    "SET last_review_feedback_id = %s, updated_at = now() "
                    "WHERE id = 1 AND last_review_feedback_id = %s "
                    "RETURNING last_review_feedback_id",
                    (next_cursor, cursor_id),
                )
                advanced = cur.fetchone()
                if (
                    not isinstance(advanced, Mapping)
                    or advanced.get("last_review_feedback_id") != next_cursor
                ):
                    raise ValueError("review reconciliation cursor did not advance")
            return candidates
    except (TypeError, ValueError):
        raise ProjectionSnapshotUnavailable(
            "review reconciliation candidates are malformed"
        ) from None


def claim_review_candidate(
    candidate: ReviewCandidate,
    lease: ReviewReconcileLease,
    *,
    now: datetime,
) -> ReviewCandidate | None:
    """Claim the exact generic-sync row before any review reference RPC."""
    if type(candidate) is not ReviewCandidate or candidate.sync_claim_token is not None:
        raise ValueError("review candidate is malformed")
    if type(lease) is not ReviewReconcileLease:
        raise ValueError("review reconciliation lease is malformed")
    current = _aware_datetime(now, "review candidate claim time")
    if lease.expires_at <= current:
        return None
    with db.cursor() as cur:
        cur.execute(
            "SELECT feedback_id, odoo_improvement_id, state, claim_owner, claim_token, "
            "claim_expires_at, active_attempt_id FROM feedback_odoo_sync "
            "WHERE feedback_id = %s FOR UPDATE",
            (candidate.feedback_id,),
        )
        row = cur.fetchone()
        if (
            not isinstance(row, Mapping)
            or row.get("feedback_id") != candidate.feedback_id
            or row.get("odoo_improvement_id") != candidate.odoo_improvement_id
            or row.get("state") not in {"idle", "quarantined"}
            or row.get("claim_owner") is not None
            or row.get("claim_token") is not None
            or row.get("claim_expires_at") is not None
            or row.get("active_attempt_id") is not None
        ):
            return None
        prior_state = row["state"]
        cur.execute(
            "UPDATE feedback_odoo_sync SET state = 'in_flight', claim_owner = %s, "
            "claim_token = %s, claim_expires_at = %s, updated_at = %s "
            "WHERE feedback_id = %s AND odoo_improvement_id = %s AND state = %s "
            "AND claim_owner IS NULL AND claim_token IS NULL "
            "AND claim_expires_at IS NULL AND active_attempt_id IS NULL "
            "RETURNING feedback_id",
            (
                lease.owner,
                lease.token,
                lease.expires_at,
                current,
                candidate.feedback_id,
                candidate.odoo_improvement_id,
                prior_state,
            ),
        )
        claimed = cur.fetchone()
        if not isinstance(claimed, Mapping) or claimed.get("feedback_id") != candidate.feedback_id:
            raise InvalidTransition("review candidate claim conflicted")
    return replace(
        candidate,
        sync_claim_owner=lease.owner,
        sync_claim_token=lease.token,
        sync_claim_expires_at=lease.expires_at,
        sync_prior_state=prior_state,
    )


def release_review_candidate(candidate: ReviewCandidate, *, now: datetime) -> bool:
    """Release an exact review-owned generic-sync claim after a nonfinal outcome."""
    if type(candidate) is not ReviewCandidate or candidate.sync_claim_token is None:
        raise ValueError("claimed review candidate is malformed")
    current = _aware_datetime(now, "review candidate release time")
    with db.cursor() as cur:
        cur.execute(
            "UPDATE feedback_odoo_sync SET state = %s, claim_owner = NULL, "
            "claim_token = NULL, claim_expires_at = NULL, updated_at = %s "
            "WHERE feedback_id = %s AND odoo_improvement_id = %s "
            "AND state = 'in_flight' AND claim_owner = %s AND claim_token = %s "
            "AND active_attempt_id IS NULL RETURNING feedback_id",
            (
                candidate.sync_prior_state,
                current,
                candidate.feedback_id,
                candidate.odoo_improvement_id,
                candidate.sync_claim_owner,
                candidate.sync_claim_token,
            ),
        )
        row = cur.fetchone()
    return isinstance(row, Mapping) and row.get("feedback_id") == candidate.feedback_id


_REVIEW_ATTENTION_CODES = frozenset(
    {
        "review_task_identity_mismatch",
        "review_reference_identity_mismatch",
        "review_reference_link_mismatch",
        "review_lifecycle_mismatch",
        "review_terminal_conflict",
    }
)


def record_review_attention(
    candidate: ReviewCandidate,
    code: str,
    *,
    now: datetime,
) -> None:
    """Quarantine generic reference delivery with one fixed, non-sensitive code."""
    if type(candidate) is not ReviewCandidate:
        raise ValueError("review candidate is malformed")
    if type(code) is not str or code not in _REVIEW_ATTENTION_CODES:
        raise ValueError("review attention code is unsupported")
    current = _aware_datetime(now, "review attention time")
    claimed = candidate.sync_claim_token is not None
    authority_sql = (
        "AND state = 'in_flight' AND claim_owner = %s AND claim_token = %s "
        "AND active_attempt_id IS NULL "
        if claimed
        else "AND state <> 'in_flight' AND claim_owner IS NULL "
        "AND claim_token IS NULL AND claim_expires_at IS NULL "
        "AND active_attempt_id IS NULL "
    )
    authority_params = (
        (candidate.sync_claim_owner, candidate.sync_claim_token) if claimed else ()
    )
    with db.cursor() as cur:
        cur.execute(
            "UPDATE feedback_odoo_sync SET state = 'quarantined', "
            "claim_owner = NULL, claim_token = NULL, claim_expires_at = NULL, "
            "quarantine_reason = %s, quarantined_at = %s, updated_at = %s "
            "WHERE feedback_id = %s AND odoo_improvement_id = %s "
            f"{authority_sql}RETURNING feedback_id",
            (
                code,
                current,
                current,
                candidate.feedback_id,
                candidate.odoo_improvement_id,
                *authority_params,
            ),
        )
        row = cur.fetchone()
        if not isinstance(row, Mapping) or row.get("feedback_id") != candidate.feedback_id:
            raise InvalidTransition("review attention state conflicted with sync work")


def adopt_review_lifecycle(
    candidate: ReviewCandidate,
    *,
    status: str,
    finished_at: datetime | None,
    finished_by_employee_id: int | None,
    resolution_note: str | None,
    now: datetime,
) -> bool:
    """Atomically adopt verified Odoo review state without enqueueing either outbox."""
    if type(candidate) is not ReviewCandidate:
        raise ValueError("review candidate is malformed")
    if status not in _TRANSITIONS:
        raise ValueError("review lifecycle status is unsupported")
    current_time = _aware_datetime(now, "review adoption time")
    terminal = status in {"completed", "declined"}
    if terminal:
        terminal_time = _aware_datetime(finished_at, "review finish time")
        employee_id = _positive_signed_64(
            finished_by_employee_id, "review employee id"
        )
        if type(resolution_note) is not str or not resolution_note.strip():
            raise ValueError("terminal review requires a result note")
        note = resolution_note.strip()
        finished_by = f"odoo_employee:{employee_id}"
    else:
        if any(
            value is not None
            for value in (finished_at, finished_by_employee_id, resolution_note)
        ):
            raise ValueError("nonterminal review cannot include terminal detail")
        terminal_time = None
        note = None
        finished_by = None

    with db.cursor() as cur:
        cur.execute(
            "SELECT f.id AS feedback_id, f.task_type, f.status, f.lifecycle_origin, "
            "f.projection_version, f.finished_at, f.finished_by, f.resolution_note, "
            "td.odoo_task_id, td.state AS task_delivery_state, "
            "td.claim_owner AS task_claim_owner, td.claim_token AS task_claim_token, "
            "td.claim_expires_at AS task_claim_expires_at, s.odoo_improvement_id, "
            "s.state AS sync_state, s.claim_owner AS sync_claim_owner, "
            "s.claim_token AS sync_claim_token, "
            "s.claim_expires_at AS sync_claim_expires_at, "
            "s.active_attempt_id FROM feedback f "
            "JOIN feedback_task_delivery td ON td.feedback_id = f.id "
            "JOIN feedback_odoo_sync s ON s.feedback_id = f.id "
            "WHERE f.id = %s FOR UPDATE OF f, td, s",
            (candidate.feedback_id,),
        )
        locked = cur.fetchone()
        if not isinstance(locked, Mapping):
            raise InvalidTransition("review lifecycle state is unavailable")
        claimed = candidate.sync_claim_token is not None
        sync_authority_matches = (
            locked.get("sync_state") == "in_flight"
            and locked.get("sync_claim_owner") == candidate.sync_claim_owner
            and locked.get("sync_claim_token") == candidate.sync_claim_token
            and locked.get("sync_claim_expires_at") == candidate.sync_claim_expires_at
            if claimed
            else locked.get("sync_state") != "in_flight"
            and locked.get("sync_claim_owner") is None
            and locked.get("sync_claim_token") is None
            and locked.get("sync_claim_expires_at") is None
        )
        if (
            locked.get("feedback_id") != candidate.feedback_id
            or locked.get("task_type") != candidate.task_type
            or locked.get("status") != candidate.status
            or locked.get("lifecycle_origin") != "local"
            or locked.get("projection_version") != candidate.projection_version
            or locked.get("odoo_task_id") != candidate.odoo_task_id
            or locked.get("odoo_improvement_id") != candidate.odoo_improvement_id
            or locked.get("active_attempt_id") is not None
            or not sync_authority_matches
            or locked.get("task_delivery_state") != "delivered"
            or locked.get("task_claim_owner") is not None
            or locked.get("task_claim_token") is not None
            or locked.get("task_claim_expires_at") is not None
        ):
            raise InvalidTransition("review lifecycle state changed before adoption")
        old_status = locked["status"]
        if status != old_status and status not in _TRANSITIONS.get(old_status, set()):
            raise InvalidTransition("review lifecycle cannot move backward")
        if old_status in {"completed", "declined"} and (
            status != old_status
            or locked.get("finished_at") != terminal_time
            or locked.get("finished_by") != finished_by
            or locked.get("resolution_note") != note
        ):
            raise InvalidTransition("terminal review lifecycle is immutable")
        changed = (
            status != old_status
            or locked.get("finished_at") != terminal_time
            or locked.get("finished_by") != finished_by
            or locked.get("resolution_note") != note
        )
        version = candidate.projection_version + int(changed)
        cur.execute(
            "UPDATE feedback SET status = %s, finished_at = %s, finished_by = %s, "
            "resolution_note = %s, projection_version = %s, updated_at = %s "
            "WHERE id = %s AND projection_version = %s AND status = %s "
            "RETURNING id, projection_version",
            (
                status,
                terminal_time,
                finished_by,
                note,
                version,
                current_time,
                candidate.feedback_id,
                candidate.projection_version,
                candidate.status,
            ),
        )
        feedback_row = cur.fetchone()
        if (
            not isinstance(feedback_row, Mapping)
            or feedback_row.get("id") != candidate.feedback_id
            or feedback_row.get("projection_version") != version
        ):
            raise InvalidTransition("review feedback adoption conflicted")
        sync_authority_sql = (
            "AND state = 'in_flight' AND claim_owner = %s AND claim_token = %s "
            "AND active_attempt_id IS NULL "
            if claimed
            else "AND state <> 'in_flight' AND claim_owner IS NULL "
            "AND claim_token IS NULL AND claim_expires_at IS NULL "
            "AND active_attempt_id IS NULL "
        )
        sync_authority_params = (
            (candidate.sync_claim_owner, candidate.sync_claim_token) if claimed else ()
        )
        cur.execute(
            "UPDATE feedback_odoo_sync SET desired_version = %s, "
            "last_synced_version = %s, state = 'idle', claim_owner = NULL, "
            "claim_token = NULL, claim_expires_at = NULL, active_attempt_id = NULL, "
            "attempt_count = 0, last_error_class = NULL, last_error_summary = NULL, "
            "quarantine_reason = NULL, quarantined_at = NULL, updated_at = %s "
            "WHERE feedback_id = %s AND odoo_improvement_id = %s "
            f"{sync_authority_sql}RETURNING feedback_id",
            (
                version,
                version,
                current_time,
                candidate.feedback_id,
                candidate.odoo_improvement_id,
                *sync_authority_params,
            ),
        )
        sync_row = cur.fetchone()
        if not isinstance(sync_row, Mapping) or sync_row.get("feedback_id") != candidate.feedback_id:
            raise InvalidTransition("review sync adoption conflicted")
        cur.execute(
            "UPDATE feedback_task_delivery SET desired_version = %s, "
            "last_synced_version = %s, desired_status = %s, state = 'delivered', "
            "last_synced_contract_version = desired_contract_version, "
            "claim_owner = NULL, claim_token = NULL, claim_expires_at = NULL, "
            "attempt_count = 0, last_error_summary = NULL, blocked_reason = NULL, "
            "updated_at = %s WHERE feedback_id = %s AND odoo_task_id = %s "
            "AND state = 'delivered' AND claim_owner IS NULL "
            "AND claim_token IS NULL AND claim_expires_at IS NULL "
            "RETURNING feedback_id",
            (
                version,
                version,
                status,
                current_time,
                candidate.feedback_id,
                candidate.odoo_task_id,
            ),
        )
        task_row = cur.fetchone()
        if not isinstance(task_row, Mapping) or task_row.get("feedback_id") != candidate.feedback_id:
            raise InvalidTransition("review task adoption conflicted")
    return changed


def for_admin(limit: int = 200) -> list[dict]:
    """Return local feedback with its current durable sync state."""
    rows = db.query(
        "SELECT f.id, f.created_at, f.submitter, f.page_url, f.task_type, "
        "f.message, f.status, f.finished_at, f.finished_by, f.resolution_note, "
        "f.projection_version, s.state AS sync_state, s.desired_version, "
        "s.last_synced_version, td.state AS task_delivery_state, "
        "td.desired_version AS task_delivery_desired_version, "
        "td.last_synced_version AS task_delivery_last_synced_version, "
        "td.desired_contract_version AS task_delivery_desired_contract_version, "
        "td.last_synced_contract_version AS task_delivery_last_synced_contract_version, "
        "td.odoo_task_id AS task_delivery_task_id, "
        "td.before_attachment_id AS task_delivery_attachment_id, "
        "td.last_error_summary AS task_delivery_error, "
        "td.blocked_reason AS task_delivery_block_reason, "
        "EXISTS (SELECT 1 FROM feedback_images bi "
        "WHERE bi.feedback_id = f.id AND bi.role = 'before') AS has_before_image, "
        "EXISTS (SELECT 1 FROM feedback_images ai "
        "WHERE ai.feedback_id = f.id AND ai.role = 'after') AS has_after_image "
        "FROM feedback f LEFT JOIN feedback_odoo_sync s ON s.feedback_id = f.id "
        "LEFT JOIN feedback_task_delivery td ON td.feedback_id = f.id "
        "WHERE f.lifecycle_origin = 'local' ORDER BY f.id DESC LIMIT %s",
        (_clamp_limit(limit, default=200),),
    )
    for row in rows:
        canonical_type = feedback_type_or_legacy_bug(row.get("task_type"))
        row["type_label"] = canonical_type.label
        row["task_delivery_label"], row["task_delivery_note"] = (
            feedback_task_delivery.admin_status_for(row)
        )
        row["review_managed"] = canonical_type.behavior == "review"
        task_id = row.get("task_delivery_task_id")
        row["review_task_id"] = (
            task_id
            if row["review_managed"] and type(task_id) is int and 0 < task_id <= _MAX_SIGNED_64
            else None
        )
        if row["review_managed"]:
            row["task_delivery_label"] = "Managed in Odoo"
            row["task_delivery_note"] = None
        row.pop("task_delivery_state", None)
        row.pop("task_delivery_desired_version", None)
        row.pop("task_delivery_last_synced_version", None)
        row.pop("task_delivery_desired_contract_version", None)
        row.pop("task_delivery_last_synced_contract_version", None)
        row.pop("task_delivery_task_id", None)
        row.pop("task_delivery_attachment_id", None)
        row.pop("task_delivery_error", None)
        row.pop("task_delivery_block_reason", None)
    return rows


def feedback_after(after_id: int, limit: int) -> list[dict]:
    """Return one validated, detached rollout page ordered by local feedback ID."""
    safe_after_id = _nonnegative_signed_64(after_id, "after id")
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("feedback rollout limit must be an integer from 1 through 100")
    rows = db.query(
        "SELECT id, message, task_type, created_at, submitter, status, finished_at, "
        "finished_by, resolution_note, projection_version, lifecycle_origin, "
        "legacy_lifecycle_migrated_at, updated_at, odoo_task_id, "
        "(SELECT s.odoo_improvement_id FROM feedback_odoo_sync s "
        "WHERE s.feedback_id = feedback.id) AS odoo_improvement_id "
        "FROM feedback WHERE id > %s ORDER BY id LIMIT %s",
        (safe_after_id, limit),
    )
    if type(rows) is not list or len(rows) > limit:
        raise ProjectionSnapshotUnavailable("feedback rollout rows are malformed")

    detached: list[dict] = []
    previous = safe_after_id
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
    for row in rows:
        if not isinstance(row, Mapping) or not required <= set(row):
            raise ProjectionSnapshotUnavailable("feedback rollout row is malformed")
        item = dict(row)
        feedback_id = item.get("id")
        if type(feedback_id) is not int or not previous < feedback_id <= _MAX_SIGNED_64:
            raise ProjectionSnapshotUnavailable("feedback rollout rows are unordered or malformed")
        _positive_signed_64(item.get("projection_version"), "projection version")
        task_id = item.get("odoo_task_id")
        if task_id is not None:
            _positive_signed_64(task_id, "legacy task id")
        remote_id = item.get("odoo_improvement_id")
        if remote_id is not None:
            _positive_signed_64(remote_id, "remote id")
        status = item.get("status")
        origin = item.get("lifecycle_origin")
        if status is not None and status not in _TRANSITIONS:
            raise ProjectionSnapshotUnavailable("feedback rollout status is malformed")
        if origin not in {None, "local", "legacy_project_task"}:
            raise ProjectionSnapshotUnavailable("feedback rollout origin is malformed")
        if (origin is None) is not (status is None):
            raise ProjectionSnapshotUnavailable("feedback rollout authority is malformed")
        previous = feedback_id
        detached.append(item)
    return detached


def rollout_snapshot(
    *,
    feedback_id: int,
    expected_projection_version: int,
    expected_odoo_task_id: int | None,
) -> RolloutSnapshot:
    """Lock and detach one exact rollout row plus validated bounded images."""
    safe_feedback_id = _positive_signed_64(feedback_id, "feedback id")
    safe_version = _positive_signed_64(
        expected_projection_version,
        "projection version",
    )
    if expected_odoo_task_id is None:
        safe_task_id = None
    else:
        safe_task_id = _positive_signed_64(expected_odoo_task_id, "legacy task id")

    with db.cursor() as cur:
        cur.execute(
            "SELECT f.id, f.message, f.task_type, f.created_at, f.submitter, "
            "f.status, f.finished_at, f.finished_by, f.resolution_note, "
            "f.projection_version, f.lifecycle_origin, "
            "f.legacy_lifecycle_migrated_at, f.updated_at, f.odoo_task_id, "
            "s.odoo_improvement_id FROM feedback f "
            "LEFT JOIN feedback_odoo_sync s ON s.feedback_id = f.id "
            "WHERE f.id = %s AND f.projection_version = %s "
            "AND f.odoo_task_id IS NOT DISTINCT FROM %s FOR SHARE OF f",
            (safe_feedback_id, safe_version, safe_task_id),
        )
        row = cur.fetchone()
        feedback = _detach_rollout_feedback(
            row,
            feedback_id=safe_feedback_id,
            projection_version=safe_version,
            odoo_task_id=safe_task_id,
        )

        cur.execute(
            "SELECT role, byte_length, width, height, "
            "octet_length(jpeg_bytes) AS stored_byte_length "
            "FROM feedback_images WHERE feedback_id = %s "
            "ORDER BY role LIMIT 3",
            (safe_feedback_id,),
        )
        metadata_rows = cur.fetchall()
        image_metadata = _validated_rollout_image_metadata(metadata_rows)

        images: dict[str, NormalizedImage] = {}
        roles = list(image_metadata)
        if roles:
            cur.execute(
                "SELECT feedback_id, role, jpeg_bytes, sha256, byte_length, width, height "
                "FROM feedback_images WHERE feedback_id = %s AND role = ANY(%s) "
                "AND byte_length > 0 AND byte_length <= %s "
                "AND octet_length(jpeg_bytes) > 0 "
                "AND octet_length(jpeg_bytes) <= %s "
                "AND octet_length(jpeg_bytes) = byte_length "
                "ORDER BY role LIMIT 2",
                (
                    safe_feedback_id,
                    roles,
                    MAX_OUTPUT_BYTES,
                    MAX_OUTPUT_BYTES,
                ),
            )
            image_rows = cur.fetchall()
            if type(image_rows) is not list or len(image_rows) != len(roles):
                raise ProjectionSnapshotUnavailable("feedback image rows are malformed")
            for expected_role, image_row in zip(roles, image_rows, strict=True):
                if not isinstance(image_row, Mapping) or image_row.get("role") != expected_role:
                    raise ProjectionSnapshotUnavailable("feedback image rows are malformed")
                image = _snapshot_image(image_row, safe_feedback_id)
                expected_length, expected_width, expected_height = image_metadata[expected_role]
                if (
                    image.byte_length != expected_length
                    or image.width != expected_width
                    or image.height != expected_height
                ):
                    raise ProjectionSnapshotUnavailable("feedback image metadata changed")
                images[expected_role] = image

    return RolloutSnapshot(
        feedback=MappingProxyType(feedback),
        images=MappingProxyType(images),
    )


def _detach_rollout_feedback(
    row: object,
    *,
    feedback_id: int,
    projection_version: int,
    odoo_task_id: int | None,
) -> dict[str, object]:
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
    if not isinstance(row, Mapping) or not required <= set(row):
        raise ProjectionSnapshotUnavailable("exact rollout feedback version is unavailable")
    detached = dict(row)
    if (
        type(detached.get("id")) is not int
        or detached.get("id") != feedback_id
        or type(detached.get("projection_version")) is not int
        or detached.get("projection_version") != projection_version
        or detached.get("odoo_task_id") != odoo_task_id
    ):
        raise ProjectionSnapshotUnavailable("exact rollout feedback version is unavailable")
    if odoo_task_id is not None:
        try:
            _positive_signed_64(detached.get("odoo_task_id"), "legacy task id")
        except ValueError as exc:
            raise ProjectionSnapshotUnavailable(
                "exact rollout feedback version is unavailable"
            ) from exc
    remote_id = detached.get("odoo_improvement_id")
    if remote_id is not None:
        try:
            _positive_signed_64(remote_id, "remote id")
        except ValueError as exc:
            raise ProjectionSnapshotUnavailable(
                "exact rollout feedback version is unavailable"
            ) from exc
    status = detached.get("status")
    origin = detached.get("lifecycle_origin")
    if (
        (status is not None and status not in _TRANSITIONS)
        or origin not in {None, "local", "legacy_project_task"}
        or ((origin is None) is not (status is None))
    ):
        raise ProjectionSnapshotUnavailable("exact rollout feedback version is unavailable")
    return detached


def _validated_rollout_image_metadata(
    rows: object,
) -> dict[str, tuple[int, int, int]]:
    if type(rows) is not list or len(rows) > 2:
        raise ProjectionSnapshotUnavailable("feedback image metadata is malformed")
    metadata: dict[str, tuple[int, int, int]] = {}
    previous_role = ""
    for row in rows:
        if not isinstance(row, Mapping):
            raise ProjectionSnapshotUnavailable("feedback image metadata is malformed")
        role = row.get("role")
        byte_length = row.get("byte_length")
        stored_byte_length = row.get("stored_byte_length")
        width = row.get("width")
        height = row.get("height")
        if (
            type(role) is not str
            or role not in {"before", "after"}
            or role in metadata
            or role <= previous_role
            or type(byte_length) is not int
            or type(stored_byte_length) is not int
            or byte_length != stored_byte_length
            or not 0 < byte_length <= MAX_OUTPUT_BYTES
            or type(width) is not int
            or type(height) is not int
            or not 0 < width <= OUTPUT_LONG_SIDE
            or not 0 < height <= OUTPUT_LONG_SIDE
        ):
            raise ProjectionSnapshotUnavailable("feedback image metadata is malformed")
        metadata[role] = (byte_length, width, height)
        previous_role = role
    return metadata


def apply_legacy_status(
    *,
    feedback_id: int,
    expected_odoo_task_id: int,
    expected_projection_version: int,
    status: str,
    now: datetime,
) -> bool:
    """Persist one exact legacy stage and its sync intent atomically."""
    safe_feedback_id = _positive_signed_64(feedback_id, "feedback id")
    safe_task_id = _positive_signed_64(expected_odoo_task_id, "legacy task id")
    safe_version = _positive_signed_64(
        expected_projection_version,
        "projection version",
    )
    resulting_version = _positive_signed_64(
        safe_version + 1,
        "resulting projection version",
    )
    if type(status) is not str or status not in _TRANSITIONS:
        raise ValueError("unsupported legacy feedback status")
    current = _aware_datetime(now, "legacy migration time")
    with db.cursor() as cur:
        cur.execute(
            "UPDATE feedback SET status = %s, "
            "lifecycle_origin = 'legacy_project_task', "
            "legacy_lifecycle_migrated_at = %s, "
            "projection_version = projection_version + 1, updated_at = %s "
            "WHERE id = %s AND status IS NULL AND lifecycle_origin IS NULL "
            "AND odoo_task_id = %s AND projection_version = %s "
            "AND finished_at IS NULL "
            "AND finished_by IS NULL AND resolution_note IS NULL "
            "RETURNING projection_version",
            (
                status,
                current,
                current,
                safe_feedback_id,
                safe_task_id,
                safe_version,
            ),
        )
        updated = cur.fetchone()
        if updated is None:
            cur.execute(
                "SELECT f.odoo_task_id, f.status, f.lifecycle_origin, "
                "f.legacy_lifecycle_migrated_at, f.updated_at, f.projection_version, "
                "f.finished_at, f.finished_by, f.resolution_note, "
                "s.feedback_id AS sync_feedback_id, s.desired_version "
                "FROM feedback f LEFT JOIN feedback_odoo_sync s "
                "ON s.feedback_id = f.id WHERE f.id = %s FOR SHARE OF f",
                (safe_feedback_id,),
            )
            existing = cur.fetchone()
            exact_existing = (
                isinstance(existing, Mapping)
                and existing.get("odoo_task_id") == safe_task_id
                and existing.get("status") == status
                and existing.get("lifecycle_origin") == "legacy_project_task"
                and existing.get("sync_feedback_id") == safe_feedback_id
                and existing.get("finished_at") is None
                and existing.get("finished_by") is None
                and existing.get("resolution_note") is None
            )
            if exact_existing:
                try:
                    migrated_at = _aware_datetime(
                        existing.get("legacy_lifecycle_migrated_at"),
                        "legacy migration time",
                    )
                    updated_at = _aware_datetime(
                        existing.get("updated_at"),
                        "feedback update time",
                    )
                    stored_version = _positive_signed_64(
                        existing.get("projection_version"),
                        "projection version",
                    )
                    desired_version = _positive_signed_64(
                        existing.get("desired_version"),
                        "desired version",
                    )
                    if (
                        migrated_at != updated_at
                        or stored_version != resulting_version
                        or desired_version != resulting_version
                    ):
                        exact_existing = False
                except ValueError:
                    exact_existing = False
            if exact_existing:
                return False
            raise InvalidTransition("legacy feedback migration conflicted with local state")
        if not isinstance(updated, Mapping):
            raise InvalidTransition("legacy feedback migration returned malformed state")
        try:
            version = _positive_signed_64(
                updated.get("projection_version"),
                "projection version",
            )
        except ValueError as exc:
            raise InvalidTransition("legacy feedback migration returned malformed state") from exc
        if version != resulting_version:
            raise InvalidTransition("legacy feedback migration returned malformed state")
        cur.execute(
            "INSERT INTO feedback_odoo_sync "
            "(feedback_id, desired_version, last_synced_version, due_at, state) "
            "VALUES (%s, %s, 0, %s, 'idle') "
            "ON CONFLICT (feedback_id) DO UPDATE SET desired_version = "
            "GREATEST(feedback_odoo_sync.desired_version, EXCLUDED.desired_version) "
            "RETURNING feedback_id, desired_version",
            (safe_feedback_id, version, current),
        )
        sync_rows = cur.fetchall()
        if (
            type(sync_rows) is not list
            or len(sync_rows) != 1
            or not isinstance(sync_rows[0], Mapping)
            or sync_rows[0].get("feedback_id") != safe_feedback_id
        ):
            raise InvalidTransition("legacy feedback sync state was malformed")
        try:
            desired_version = _positive_signed_64(
                sync_rows[0].get("desired_version"),
                "desired version",
            )
        except ValueError as exc:
            raise InvalidTransition("legacy feedback sync state was malformed") from exc
        if desired_version != version:
            raise InvalidTransition("legacy feedback sync state was malformed")
        return True


def enqueue_history_batch(*, batch_size: int, now: datetime) -> dict[str, object]:
    """Advance the singleton history cursor and upsert bounded local sync intent."""
    if type(batch_size) is not int:
        raise ValueError("history batch size must be an exact integer")
    limit = max(1, min(batch_size, 100))
    current = _aware_datetime(now, "history enqueue time")
    with db.cursor() as cur:
        cur.execute(
            "SELECT last_feedback_id FROM feedback_odoo_backfill_state WHERE id = 1 FOR UPDATE",
            (),
        )
        state = cur.fetchone()
        if not isinstance(state, Mapping):
            raise ProjectionSnapshotUnavailable("feedback history cursor is unavailable")
        cursor_id = _nonnegative_signed_64(state.get("last_feedback_id"), "history cursor")
        cur.execute(
            "SELECT id, projection_version FROM feedback "
            "WHERE id > %s AND lifecycle_origin IN ('local', 'legacy_project_task') "
            "AND status IS NOT NULL ORDER BY id LIMIT %s",
            (cursor_id, limit),
        )
        selected = cur.fetchall()
        if type(selected) is not list or len(selected) > limit:
            raise ProjectionSnapshotUnavailable("feedback history rows are malformed")
        feedback_ids: list[int] = []
        previous = cursor_id
        versions: list[tuple[int, int]] = []
        for row in selected:
            if not isinstance(row, Mapping):
                raise ProjectionSnapshotUnavailable("feedback history row is malformed")
            feedback_id = _positive_signed_64(row.get("id"), "feedback id")
            version = _positive_signed_64(row.get("projection_version"), "projection version")
            if feedback_id <= previous:
                raise ProjectionSnapshotUnavailable(
                    "feedback history rows are unordered or duplicated"
                )
            feedback_ids.append(feedback_id)
            versions.append((feedback_id, version))
            previous = feedback_id
        for feedback_id, version in versions:
            cur.execute(
                "INSERT INTO feedback_odoo_sync "
                "(feedback_id, desired_version, last_synced_version, due_at, state) "
                "VALUES (%s, %s, 0, %s, 'idle') "
                "ON CONFLICT (feedback_id) DO UPDATE SET desired_version = "
                "GREATEST(feedback_odoo_sync.desired_version, EXCLUDED.desired_version)",
                (feedback_id, version, current),
            )
        if not feedback_ids:
            return {"feedback_ids": (), "next_cursor": cursor_id}
        next_cursor = feedback_ids[-1]
        cur.execute(
            "UPDATE feedback_odoo_backfill_state SET last_feedback_id = %s, "
            "updated_at = %s WHERE id = 1 AND last_feedback_id = %s "
            "RETURNING last_feedback_id",
            (next_cursor, current, cursor_id),
        )
        advanced = cur.fetchone()
        if not isinstance(advanced, Mapping) or advanced.get("last_feedback_id") != next_cursor:
            raise ProjectionSnapshotUnavailable("feedback history cursor did not advance")
        return {"feedback_ids": tuple(feedback_ids), "next_cursor": next_cursor}


def reconciliation_counts(gates_open: bool) -> dict[str, int]:
    """Return one local-only aggregate of mutually explainable sync states."""
    if type(gates_open) is not bool:
        raise ValueError("gate state must be a boolean")
    with db.cursor() as cur:
        cur.execute(
            "WITH gate AS (SELECT %s::boolean AS gates_open) "
            "SELECT "
            "COUNT(*) FILTER (WHERE last_synced_version >= desired_version) AS synchronized, "
            "COUNT(*) FILTER (WHERE last_synced_version < desired_version "
            "AND state = 'quarantined') AS quarantined, "
            "COUNT(*) FILTER (WHERE last_synced_version < desired_version "
            "AND state = 'in_flight') AS in_flight, "
            "COUNT(*) FILTER (WHERE last_synced_version < desired_version "
            "AND state = 'idle' AND gate.gates_open AND due_at <= now()) AS due, "
            "COUNT(*) FILTER (WHERE last_synced_version < desired_version "
            "AND state = 'idle' AND NOT (gate.gates_open AND due_at <= now())) AS deferred, "
            "COALESCE(SUM(GREATEST(desired_version - last_synced_version, 0)), 0) "
            "AS version_lag FROM feedback_odoo_sync CROSS JOIN gate",
            (gates_open,),
        )
        row = cur.fetchone()
    keys = {
        "synchronized",
        "due",
        "deferred",
        "in_flight",
        "quarantined",
        "version_lag",
    }
    if not isinstance(row, Mapping) or set(row) != keys:
        raise ProjectionSnapshotUnavailable("feedback reconciliation row is malformed")
    result: dict[str, int] = {}
    for key in keys:
        if key == "version_lag":
            result[key] = _nonnegative_numeric_aggregate(row.get(key), f"{key} count")
        else:
            result[key] = _nonnegative_signed_64(row.get(key), f"{key} count")
    return result


def transition(
    *,
    feedback_id: int,
    status: str,
    actor: str,
    resolution_note: str | None,
    after_image: NormalizedImage | None,
    now: datetime,
) -> int:
    """Apply one allowed local lifecycle change and return its new version."""
    clean_actor = actor.strip().lower()
    clean_note = (resolution_note or "").strip()
    with db.cursor() as cur:
        cur.execute(
            "SELECT status, lifecycle_origin, projection_version, task_type "
            "FROM feedback WHERE id = %s FOR UPDATE",
            (feedback_id,),
        )
        row = cur.fetchone()
        if row is None:
            raise KeyError(feedback_id)
        if row["lifecycle_origin"] != "local":
            raise InvalidTransition("feedback is not locally managed")
        if feedback_type_or_legacy_bug(row.get("task_type")).behavior == "review":
            raise InvalidTransition("Managed in Odoo")

        current = row["status"]
        if status not in _TRANSITIONS.get(current, set()):
            raise InvalidTransition("feedback is terminal or transition is invalid")

        terminal = status in {"completed", "declined"}
        if terminal and (not clean_actor or not clean_note):
            raise InvalidTransition("terminal feedback requires an actor and resolution note")
        if after_image is not None and not terminal:
            raise InvalidTransition("after image is allowed only for terminal feedback")

        version = int(row["projection_version"]) + 1
        cur.execute(
            "UPDATE feedback SET status = %s, lifecycle_origin = 'local', "
            "finished_at = %s, finished_by = %s, resolution_note = %s, "
            "projection_version = %s, updated_at = %s WHERE id = %s",
            (
                status,
                now if terminal else None,
                clean_actor if terminal else None,
                clean_note if terminal else None,
                version,
                now,
                feedback_id,
            ),
        )
        if after_image is not None:
            cur.execute(
                "INSERT INTO feedback_images "
                "(feedback_id, role, jpeg_bytes, sha256, byte_length, width, height) "
                "VALUES (%s, 'after', %s, %s, %s, %s, %s) "
                "ON CONFLICT (feedback_id, role) DO UPDATE SET "
                "jpeg_bytes = EXCLUDED.jpeg_bytes, sha256 = EXCLUDED.sha256, "
                "byte_length = EXCLUDED.byte_length, width = EXCLUDED.width, "
                "height = EXCLUDED.height, created_at = now()",
                (
                    feedback_id,
                    after_image.jpeg_bytes,
                    after_image.sha256,
                    after_image.byte_length,
                    after_image.width,
                    after_image.height,
                ),
            )
        cur.execute(
            "UPDATE feedback_odoo_sync SET desired_version = %s, due_at = %s, "
            "state = CASE WHEN state IN ('in_flight', 'quarantined') "
            "THEN state ELSE 'idle' END, "
            "updated_at = %s WHERE feedback_id = %s RETURNING feedback_id",
            (version, now, now, feedback_id),
        )
        if cur.fetchone() is None:
            raise InvalidTransition("feedback sync state is missing")
        try:
            feedback_task_delivery.enqueue_lifecycle(
                cur,
                feedback_id,
                desired_version=version,
                desired_status=status,
                now=now,
            )
        except feedback_task_delivery.StateTransitionError as exc:
            raise InvalidTransition("feedback task sync state is missing") from exc
        return version
