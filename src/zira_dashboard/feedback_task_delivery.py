"""Durable local outbox for independent app-owner task delivery.

This module owns only local persistence and safe presentation summaries. A
separate worker performs remote task delivery after it has claimed a row.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from . import db
from .feedback_image import MAX_OUTPUT_BYTES, OUTPUT_LONG_SIDE, NormalizedImage
from .feedback_types import FEEDBACK_TYPES, feedback_type


_MAX_SIGNED_64 = 9_223_372_036_854_775_807
_MAX_WORKER_ID_LENGTH = 128
TASK_SYNC_CONTRACT_VERSION = 2
_CLAIM_LEASE = timedelta(minutes=2)
_RETRY_SUMMARY = "Odoo task delivery needs attention and will retry."
_BLOCKED_REASON = "Task delivery needs owner review."
_BLOCK_REASONS = frozenset(
    {
        "More than one matching owner task exists.",
        "More than one matching owner screenshot exists.",
        "The stored owner screenshot does not match this feedback.",
        "The stored owner task does not match this feedback.",
        "The owner task stage is missing or ambiguous.",
        "More than one matching owner task result note exists.",
        "The Odoo review setup is missing or ambiguous.",
        "The Odoo review reference is missing or ambiguous.",
        "The Odoo review reference link conflicts with this task.",
    }
)
_MISSING_SUMMARY = "Task delivery record is missing."
_FEEDBACK_TYPE_VALUES = tuple(
    item.value for item in FEEDBACK_TYPES if item.odoo_value is not None
)
_CODING_FEEDBACK_TYPE_VALUES = tuple(
    item.value for item in FEEDBACK_TYPES if item.behavior == "coding"
)


class StateTransitionError(RuntimeError):
    """A task-delivery claim no longer has the expected authority."""


class SnapshotValidationError(ValueError):
    """The task worker could not obtain one safe local feedback snapshot."""


@dataclass(frozen=True)
class TaskDeliveryClaim:
    feedback_id: int
    claim_token: UUID
    task_id: int | None
    before_attachment_id: int | None
    expires_at: datetime
    desired_version: int = 1
    last_synced_version: int = 0
    desired_status: str = "requested"
    desired_contract_version: int = TASK_SYNC_CONTRACT_VERSION
    last_synced_contract_version: int = 0

    def __post_init__(self) -> None:
        _positive_signed_64(self.feedback_id, "feedback id")
        _uuid(self.claim_token, "claim token")
        if self.task_id is not None:
            _positive_signed_64(self.task_id, "task id")
        if self.before_attachment_id is not None:
            _positive_signed_64(self.before_attachment_id, "before attachment id")
        _aware_datetime(self.expires_at, "claim expiration")
        _positive_signed_64(self.desired_version, "desired version")
        _nonnegative_signed_64(self.last_synced_version, "last synchronized version")
        if self.last_synced_version > self.desired_version:
            raise ValueError("task lifecycle versions are inverted")
        _lifecycle_status(self.desired_status)
        _positive_signed_64(self.desired_contract_version, "desired contract version")
        _nonnegative_signed_64(
            self.last_synced_contract_version, "last synchronized contract version"
        )
        if self.last_synced_contract_version > self.desired_contract_version:
            raise ValueError("task contract versions are inverted")


@dataclass(frozen=True)
class FeedbackTaskSnapshot:
    feedback_id: int
    task_type: str
    message: str
    submitter: str | None
    page_url: str | None
    before_image: NormalizedImage | None
    status: str = "requested"
    projection_version: int = 1
    resolution_note: str | None = None


def _positive_signed_64(value: object, label: str) -> int:
    if type(value) is not int or not 0 < value <= _MAX_SIGNED_64:
        raise ValueError(f"{label} must be a positive signed-64-bit integer")
    return value


def _nonnegative_signed_64(value: object, label: str) -> int:
    if type(value) is not int or not 0 <= value <= _MAX_SIGNED_64:
        raise ValueError(f"{label} must be a nonnegative signed-64-bit integer")
    return value


def _uuid(value: object, label: str) -> UUID:
    if type(value) is not UUID:
        raise ValueError(f"{label} must be a UUID")
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


def _worker_id(value: object) -> str:
    if (
        type(value) is not str
        or not value
        or len(value) > _MAX_WORKER_ID_LENGTH
        or value != value.strip()
        or not value.isprintable()
    ):
        raise ValueError("worker id is malformed")
    return value


def _one_row(cursor, operation: str) -> Mapping[str, object]:
    row = cursor.fetchone()
    if not isinstance(row, Mapping):
        raise StateTransitionError(f"{operation} no longer has claim authority")
    return row


def _claim_from_row(row: Mapping[str, object]) -> TaskDeliveryClaim:
    try:
        return TaskDeliveryClaim(
            feedback_id=row.get("feedback_id"),
            claim_token=row.get("claim_token"),
            task_id=row.get("odoo_task_id"),
            before_attachment_id=row.get("before_attachment_id"),
            expires_at=row.get("claim_expires_at"),
            desired_version=row.get("desired_version", 1),
            last_synced_version=row.get("last_synced_version", 0),
            desired_status=row.get("desired_status", "requested"),
            desired_contract_version=row.get(
                "desired_contract_version", TASK_SYNC_CONTRACT_VERSION
            ),
            last_synced_contract_version=row.get("last_synced_contract_version", 0),
        )
    except ValueError:
        raise StateTransitionError("database returned a malformed task delivery claim") from None


def _updated_claim(
    cursor,
    operation: str,
    claim: TaskDeliveryClaim,
) -> TaskDeliveryClaim:
    result = _claim_from_row(_one_row(cursor, operation))
    if result.feedback_id != claim.feedback_id or result.claim_token != claim.claim_token:
        raise StateTransitionError(f"{operation} returned different claim authority")
    return result


def _optional_now(now: datetime | None, label: str) -> datetime:
    if now is None:
        return datetime.now(UTC)
    return _aware_datetime(now, label)


def _snapshot_image(row: Mapping[str, object], feedback_id: int) -> NormalizedImage | None:
    image_columns = (
        "before_feedback_id",
        "jpeg_bytes",
        "sha256",
        "byte_length",
        "width",
        "height",
    )
    if all(row.get(column) is None for column in image_columns):
        return None
    if row.get("before_feedback_id") != feedback_id:
        raise SnapshotValidationError("before image belongs to another feedback record")
    raw_value = row.get("jpeg_bytes")
    if not isinstance(raw_value, (bytes, bytearray, memoryview)):
        raise SnapshotValidationError("before image bytes are malformed")
    raw = bytes(raw_value)
    if not raw or len(raw) > MAX_OUTPUT_BYTES:
        raise SnapshotValidationError("before image bytes are malformed")
    byte_length = row.get("byte_length")
    if (
        type(byte_length) is not int
        or byte_length != len(raw)
        or not 0 < byte_length <= MAX_OUTPUT_BYTES
    ):
        raise SnapshotValidationError("before image length is malformed")
    digest = row.get("sha256")
    if type(digest) is not str or digest != hashlib.sha256(raw).hexdigest():
        raise SnapshotValidationError("before image hash is malformed")
    width = row.get("width")
    height = row.get("height")
    if (
        type(width) is not int
        or type(height) is not int
        or not 0 < width <= OUTPUT_LONG_SIDE
        or not 0 < height <= OUTPUT_LONG_SIDE
    ):
        raise SnapshotValidationError("before image dimensions are malformed")
    return NormalizedImage(
        jpeg_bytes=raw,
        sha256=digest,
        byte_length=byte_length,
        width=width,
        height=height,
    )


def _lifecycle_status(value: object) -> str:
    if value not in {"requested", "in_progress", "completed", "declined"}:
        raise ValueError("feedback lifecycle status is unsupported")
    return str(value)


def enqueue_submission(
    cur,
    feedback_id: int,
    *,
    desired_version: int = 1,
    desired_status: str = "requested",
) -> None:
    """Add a newly saved local feedback record to the owner-task outbox."""
    safe_feedback_id = _positive_signed_64(feedback_id, "feedback id")
    safe_version = _positive_signed_64(desired_version, "desired version")
    safe_status = _lifecycle_status(desired_status)
    cur.execute(
        "INSERT INTO feedback_task_delivery "
        "(feedback_id, state, due_at, desired_version, last_synced_version, desired_status, "
        "desired_contract_version, last_synced_contract_version) "
        "VALUES (%s, 'pending', now(), %s, 0, %s, %s, 0)",
        (safe_feedback_id, safe_version, safe_status, TASK_SYNC_CONTRACT_VERSION),
    )


def enqueue_lifecycle(
    cur,
    feedback_id: int,
    *,
    desired_version: int,
    desired_status: str,
    now: datetime,
) -> None:
    """Atomically make an existing owner task due for one lifecycle version."""
    safe_feedback_id = _positive_signed_64(feedback_id, "feedback id")
    safe_version = _positive_signed_64(desired_version, "desired version")
    safe_status = _lifecycle_status(desired_status)
    current = _aware_datetime(now, "lifecycle intent time")
    cur.execute(
        "UPDATE feedback_task_delivery SET desired_version = %s, desired_status = %s, "
        "desired_contract_version = GREATEST(desired_contract_version, %s), "
        "state = CASE WHEN state IN ('in_flight', 'blocked') THEN state ELSE 'pending' END, "
        "due_at = %s, last_error_summary = NULL, "
        "blocked_reason = CASE WHEN state = 'blocked' THEN blocked_reason ELSE NULL END, "
        "updated_at = %s "
        "WHERE feedback_id = %s "
        "AND (desired_version < %s OR desired_contract_version < %s) "
        "RETURNING feedback_id",
        (
            safe_version,
            safe_status,
            TASK_SYNC_CONTRACT_VERSION,
            current,
            current,
            safe_feedback_id,
            safe_version,
            TASK_SYNC_CONTRACT_VERSION,
        ),
    )
    row = cur.fetchone()
    if not isinstance(row, Mapping) or row.get("feedback_id") != safe_feedback_id:
        raise StateTransitionError("task lifecycle intent is missing or did not advance")


def queue_existing_lifecycle_mismatches(*, limit: int = 100) -> int:
    """Boundedly queue delivered legacy tasks that lag local lifecycle authority."""
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("reconciliation limit must be from 1 through 100")
    with db.cursor() as cursor:
        cursor.execute(
            """
            WITH candidates AS (
              SELECT f.id, f.projection_version, f.status
              FROM feedback f
              JOIN feedback_task_delivery td ON td.feedback_id = f.id
              WHERE f.task_type = ANY(%s)
                AND f.lifecycle_origin = 'local'
                AND f.status IN ('requested', 'in_progress', 'completed', 'declined')
                AND td.odoo_task_id IS NOT NULL
                AND td.state <> 'blocked'
                AND (
                  td.desired_version <> f.projection_version
                  OR td.desired_status <> f.status
                  OR td.last_synced_version < f.projection_version
                  OR td.desired_contract_version < %s
                  OR td.last_synced_contract_version < %s
                )
              ORDER BY f.id
              FOR UPDATE OF td SKIP LOCKED
              LIMIT %s
            ), updated AS (
              UPDATE feedback_task_delivery td
              SET desired_version = candidates.projection_version,
                  desired_status = candidates.status,
                  desired_contract_version = %s,
                  state = 'pending', due_at = now(), attempt_count = 0,
                  claim_owner = NULL, claim_token = NULL, claim_expires_at = NULL,
                  last_error_summary = NULL, blocked_reason = NULL, updated_at = now()
              FROM candidates
              WHERE td.feedback_id = candidates.id
              RETURNING td.feedback_id
            )
            SELECT COUNT(*) AS queued FROM updated
            """,
            (
                list(_CODING_FEEDBACK_TYPE_VALUES),
                TASK_SYNC_CONTRACT_VERSION,
                TASK_SYNC_CONTRACT_VERSION,
                limit,
                TASK_SYNC_CONTRACT_VERSION,
            ),
        )
        row = cursor.fetchone()
    if not isinstance(row, Mapping) or type(row.get("queued")) is not int:
        raise StateTransitionError("task lifecycle reconciliation result is malformed")
    return row["queued"]


def claim_due(
    *,
    now: datetime,
    worker_id: str,
    limit: int = 10,
) -> list[TaskDeliveryClaim]:
    """Claim at most ten due or expired rows for a two-minute lease."""
    current = _aware_datetime(now, "claim time")
    owner = _worker_id(worker_id)
    if type(limit) is not int or limit < 1:
        raise ValueError("claim limit must be a positive integer")
    limit = min(limit, 10)
    claims: list[TaskDeliveryClaim] = []
    with db.cursor() as cursor:
        cursor.execute(
            """
            SELECT feedback_id, odoo_task_id, before_attachment_id,
                   desired_version, last_synced_version, desired_status,
                   desired_contract_version, last_synced_contract_version
            FROM feedback_task_delivery
            WHERE (
                state IN ('pending', 'attention') AND due_at <= %s
            ) OR (
                state = 'in_flight' AND claim_expires_at <= %s
            )
            ORDER BY due_at, feedback_id
            FOR UPDATE SKIP LOCKED
            LIMIT %s
            """,
            (current, current, limit),
        )
        rows = cursor.fetchall()
        if not isinstance(rows, list) or len(rows) > limit:
            raise StateTransitionError("database returned an invalid due-claim result")
        for row in rows:
            if not isinstance(row, Mapping):
                raise StateTransitionError("database returned a malformed due-claim row")
            try:
                feedback_id = _positive_signed_64(row.get("feedback_id"), "feedback id")
                task_id = row.get("odoo_task_id")
                attachment_id = row.get("before_attachment_id")
                desired_version = row.get("desired_version", 1)
                last_synced_version = row.get("last_synced_version", 0)
                desired_status = row.get("desired_status", "requested")
                desired_contract_version = row.get(
                    "desired_contract_version", TASK_SYNC_CONTRACT_VERSION
                )
                last_synced_contract_version = row.get("last_synced_contract_version", 0)
                if task_id is not None:
                    _positive_signed_64(task_id, "task id")
                if attachment_id is not None:
                    _positive_signed_64(attachment_id, "before attachment id")
                _positive_signed_64(desired_version, "desired version")
                _nonnegative_signed_64(last_synced_version, "last synchronized version")
                _lifecycle_status(desired_status)
                _positive_signed_64(desired_contract_version, "desired contract version")
                _nonnegative_signed_64(
                    last_synced_contract_version, "last synchronized contract version"
                )
                if last_synced_contract_version > desired_contract_version:
                    raise ValueError("task contract versions are inverted")
            except ValueError:
                raise StateTransitionError("database returned a malformed due-claim row") from None
            token = uuid4()
            expires = current + _CLAIM_LEASE
            cursor.execute(
                """
                UPDATE feedback_task_delivery
                SET state = 'in_flight', claim_owner = %s, claim_token = %s,
                    claim_expires_at = %s, attempt_count = attempt_count + 1,
                    last_error_summary = NULL, updated_at = %s
                WHERE feedback_id = %s
                  AND (
                    (state IN ('pending', 'attention') AND due_at <= %s)
                    OR (state = 'in_flight' AND claim_expires_at <= %s)
                  )
                  AND odoo_task_id IS NOT DISTINCT FROM %s
                  AND before_attachment_id IS NOT DISTINCT FROM %s
                RETURNING feedback_id, claim_token, odoo_task_id, before_attachment_id,
                          claim_expires_at, desired_version, last_synced_version,
                          desired_status, desired_contract_version,
                          last_synced_contract_version
                """,
                (
                    owner,
                    token,
                    expires,
                    current,
                    feedback_id,
                    current,
                    current,
                    task_id,
                    attachment_id,
                ),
            )
            result = _updated_claim(
                cursor,
                "due claim update",
                TaskDeliveryClaim(
                    feedback_id, token, task_id, attachment_id, expires,
                    desired_version, last_synced_version, desired_status,
                    desired_contract_version, last_synced_contract_version,
                ),
            )
            claims.append(result)
    return claims


def renew_claim(
    claim: TaskDeliveryClaim,
    *,
    now: datetime | None = None,
) -> TaskDeliveryClaim:
    """Extend only the still-current lease before a remote write begins."""
    if type(claim) is not TaskDeliveryClaim:
        raise ValueError("claim is malformed")
    current = _optional_now(now, "claim renewal time")
    expires = current + _CLAIM_LEASE
    with db.cursor() as cursor:
        cursor.execute(
            """
            UPDATE feedback_task_delivery
            SET claim_expires_at = %s, updated_at = %s
            WHERE feedback_id = %s
              AND claim_token = %s
              AND state = 'in_flight'
              AND claim_expires_at = %s
              AND claim_expires_at > %s
            RETURNING feedback_id, claim_token, odoo_task_id, before_attachment_id,
                      claim_expires_at, desired_version, last_synced_version,
                      desired_status, desired_contract_version,
                      last_synced_contract_version
            """,
            (
                expires,
                current,
                claim.feedback_id,
                claim.claim_token,
                claim.expires_at,
                current,
            ),
        )
        renewed = _updated_claim(cursor, "claim renewal", claim)
    if renewed.expires_at != expires:
        raise StateTransitionError("claim renewal returned a different expiration")
    return renewed


def load_snapshot(feedback_id: int) -> FeedbackTaskSnapshot:
    """Read and detach the only local fields a task-delivery worker needs."""
    safe_feedback_id = _positive_signed_64(feedback_id, "feedback id")
    with db.cursor() as cursor:
        cursor.execute(
            """
            SELECT f.id AS feedback_id, f.task_type, f.message, f.submitter,
                   f.page_url, f.lifecycle_origin, f.status,
                   f.projection_version, f.resolution_note,
                   bi.feedback_id AS before_feedback_id, bi.jpeg_bytes,
                   bi.sha256, bi.byte_length, bi.width, bi.height
            FROM feedback f
            LEFT JOIN feedback_images bi
              ON bi.feedback_id = f.id AND bi.role = 'before'
            WHERE f.id = %s
              AND f.lifecycle_origin = 'local'
              AND f.task_type = ANY(%s)
            FOR SHARE OF f
            """,
            (safe_feedback_id, list(_FEEDBACK_TYPE_VALUES)),
        )
        row = cursor.fetchone()
    if not isinstance(row, Mapping):
        raise SnapshotValidationError("local feedback snapshot is unavailable")
    try:
        canonical_type = feedback_type(row.get("task_type"))
        if (
            row.get("feedback_id") != safe_feedback_id
            or row.get("lifecycle_origin") != "local"
            or row.get("status") not in {"requested", "in_progress", "completed", "declined"}
            or type(row.get("projection_version")) is not int
            or row.get("projection_version") <= 0
            or type(row.get("message")) is not str
            or row.get("submitter") is not None and type(row.get("submitter")) is not str
            or row.get("page_url") is not None and type(row.get("page_url")) is not str
        ):
            raise SnapshotValidationError("local feedback snapshot is malformed")
        before_image = _snapshot_image(row, safe_feedback_id)
    except SnapshotValidationError:
        raise
    except (TypeError, ValueError):
        raise SnapshotValidationError("local feedback snapshot is malformed") from None
    return FeedbackTaskSnapshot(
        feedback_id=safe_feedback_id,
        task_type=canonical_type.value,
        message=row["message"],
        submitter=row["submitter"],
        page_url=row["page_url"],
        before_image=before_image,
        status=row["status"],
        projection_version=row["projection_version"],
        resolution_note=row.get("resolution_note"),
    )


def task_id_for_review_reference(feedback_id: int) -> int | None:
    """Return the durable task identity only after review delivery persisted it."""
    safe_feedback_id = _positive_signed_64(feedback_id, "feedback id")
    with db.cursor() as cursor:
        cursor.execute(
            """
            SELECT td.odoo_task_id, td.before_attachment_id,
                   EXISTS (
                       SELECT 1 FROM feedback_images fi
                       WHERE fi.feedback_id = td.feedback_id AND fi.role = 'before'
                   ) AS has_before_image
            FROM feedback_task_delivery td
            WHERE td.feedback_id = %s
            """,
            (safe_feedback_id,),
        )
        row = cursor.fetchone()
    if not isinstance(row, Mapping):
        return None
    task_id = row.get("odoo_task_id")
    if task_id is None:
        return None
    has_before_image = row.get("has_before_image")
    if type(has_before_image) is not bool:
        raise ValueError("attachment readiness is malformed")
    attachment_id = row.get("before_attachment_id")
    if has_before_image and attachment_id is None:
        return None
    if attachment_id is not None:
        _positive_signed_64(attachment_id, "attachment id")
    return _positive_signed_64(task_id, "task id")


def record_task_id(
    claim: TaskDeliveryClaim,
    *,
    task_id: int,
    now: datetime | None = None,
) -> TaskDeliveryClaim:
    """Persist the remote task identity for exactly one current claim."""
    if type(claim) is not TaskDeliveryClaim:
        raise ValueError("claim is malformed")
    saved_task_id = _positive_signed_64(task_id, "task id")
    current = _optional_now(now, "task identity time")
    with db.cursor() as cursor:
        cursor.execute(
            """
            UPDATE feedback_task_delivery
            SET odoo_task_id = %s, updated_at = %s
            WHERE feedback_id = %s
              AND claim_token = %s
              AND state = 'in_flight'
              AND odoo_task_id IS NOT DISTINCT FROM %s
              AND before_attachment_id IS NOT DISTINCT FROM %s
            RETURNING feedback_id, claim_token, odoo_task_id, before_attachment_id,
                      claim_expires_at, desired_version, last_synced_version,
                      desired_status, desired_contract_version,
                      last_synced_contract_version
            """,
            (
                saved_task_id,
                current,
                claim.feedback_id,
                claim.claim_token,
                claim.task_id,
                claim.before_attachment_id,
            ),
        )
        updated = _updated_claim(cursor, "task identity update", claim)
    if updated.task_id != saved_task_id:
        raise StateTransitionError("task identity update returned a different task")
    return updated


def record_before_attachment(
    claim: TaskDeliveryClaim,
    *,
    attachment_id: int,
    now: datetime | None = None,
) -> TaskDeliveryClaim:
    """Persist the optional before-image attachment for one current claim."""
    if type(claim) is not TaskDeliveryClaim or claim.task_id is None:
        raise ValueError("claim has no saved task identity")
    saved_attachment_id = _positive_signed_64(attachment_id, "before attachment id")
    current = _optional_now(now, "before attachment time")
    with db.cursor() as cursor:
        cursor.execute(
            """
            UPDATE feedback_task_delivery
            SET before_attachment_id = %s, updated_at = %s
            WHERE feedback_id = %s
              AND claim_token = %s
              AND state = 'in_flight'
              AND odoo_task_id = %s
              AND before_attachment_id IS NOT DISTINCT FROM %s
            RETURNING feedback_id, claim_token, odoo_task_id, before_attachment_id,
                      claim_expires_at, desired_version, last_synced_version,
                      desired_status, desired_contract_version,
                      last_synced_contract_version
            """,
            (
                saved_attachment_id,
                current,
                claim.feedback_id,
                claim.claim_token,
                claim.task_id,
                claim.before_attachment_id,
            ),
        )
        updated = _updated_claim(cursor, "before attachment update", claim)
    if updated.before_attachment_id != saved_attachment_id:
        raise StateTransitionError("before attachment update returned a different attachment")
    return updated


def mark_delivered(claim: TaskDeliveryClaim, *, now: datetime | None = None) -> None:
    """Finish a claim only when its saved task and required image are present."""
    if type(claim) is not TaskDeliveryClaim or claim.task_id is None:
        raise ValueError("claim has no saved task identity")
    current = _optional_now(now, "delivery completion time")
    with db.cursor() as cursor:
        cursor.execute(
            """
            UPDATE feedback_task_delivery
            SET last_synced_version = %s,
                last_synced_contract_version = %s,
                state = CASE
                  WHEN desired_version = %s AND desired_contract_version = %s
                  THEN 'delivered' ELSE 'pending' END,
                claim_owner = NULL, claim_token = NULL,
                claim_expires_at = NULL, last_error_summary = NULL,
                blocked_reason = NULL,
                due_at = CASE
                  WHEN desired_version > %s OR desired_contract_version > %s
                  THEN %s ELSE due_at END,
                updated_at = %s
            WHERE feedback_id = %s
              AND claim_token = %s
              AND state = 'in_flight'
              AND odoo_task_id = %s
              AND before_attachment_id IS NOT DISTINCT FROM %s
              AND desired_version >= %s
              AND desired_contract_version >= %s
              AND last_synced_version = %s
              AND last_synced_contract_version = %s
              AND (
                NOT EXISTS (
                    SELECT 1 FROM feedback_images
                    WHERE feedback_id = feedback_task_delivery.feedback_id
                      AND role = 'before'
                )
                OR before_attachment_id IS NOT NULL
              )
            RETURNING feedback_id
            """,
            (
                claim.desired_version,
                claim.desired_contract_version,
                claim.desired_version,
                claim.desired_contract_version,
                claim.desired_version,
                claim.desired_contract_version,
                current,
                current,
                claim.feedback_id,
                claim.claim_token,
                claim.task_id,
                claim.before_attachment_id,
                claim.desired_version,
                claim.desired_contract_version,
                claim.last_synced_version,
                claim.last_synced_contract_version,
            ),
        )
        row = _one_row(cursor, "delivery completion")
    if row.get("feedback_id") != claim.feedback_id:
        raise StateTransitionError("delivery completion returned a different feedback record")


def schedule_retry(claim: TaskDeliveryClaim, *, now: datetime) -> None:
    """Release a current claim for its capped exponential retry delay."""
    if type(claim) is not TaskDeliveryClaim:
        raise ValueError("claim is malformed")
    current = _aware_datetime(now, "retry time")
    with db.cursor() as cursor:
        cursor.execute(
            """
            SELECT feedback_id, attempt_count
            FROM feedback_task_delivery
            WHERE feedback_id = %s
              AND claim_token = %s
              AND state = 'in_flight'
            FOR UPDATE
            """,
            (claim.feedback_id, claim.claim_token),
        )
        selected = _one_row(cursor, "retry claim lock")
        if selected.get("feedback_id") != claim.feedback_id:
            raise StateTransitionError("retry claim lock returned a different feedback record")
        try:
            attempt_count = _nonnegative_signed_64(selected.get("attempt_count"), "attempt count")
        except ValueError:
            raise StateTransitionError("retry claim lock returned a malformed attempt count") from None
        if attempt_count < 1:
            raise StateTransitionError("retry claim lock has no delivery attempt")
        due = current + timedelta(seconds=min(60 * 2 ** min(attempt_count, 6), 3600))
        cursor.execute(
            """
            UPDATE feedback_task_delivery
            SET state = 'attention', claim_owner = NULL, claim_token = NULL,
                claim_expires_at = NULL, due_at = %s, last_error_summary = %s,
                updated_at = %s
            WHERE feedback_id = %s
              AND claim_token = %s
              AND state = 'in_flight'
            RETURNING feedback_id, due_at
            """,
            (due, _RETRY_SUMMARY, current, claim.feedback_id, claim.claim_token),
        )
        updated = _one_row(cursor, "retry scheduling")
    if updated.get("feedback_id") != claim.feedback_id or updated.get("due_at") != due:
        raise StateTransitionError("retry scheduling returned a different delivery record")


def block(
    claim: TaskDeliveryClaim,
    reason: str,
    *,
    now: datetime | None = None,
) -> None:
    """Stop a current claim for one safe, exact owner-review reason."""
    if type(claim) is not TaskDeliveryClaim:
        raise ValueError("claim is malformed")
    if type(reason) is not str or reason not in _BLOCK_REASONS:
        raise ValueError("block reason is not supported")
    current = _optional_now(now, "block time")
    with db.cursor() as cursor:
        cursor.execute(
            """
            UPDATE feedback_task_delivery
            SET state = 'blocked', claim_owner = NULL, claim_token = NULL,
                claim_expires_at = NULL, blocked_reason = %s, updated_at = %s
            WHERE feedback_id = %s
              AND claim_token = %s
              AND state = 'in_flight'
            RETURNING feedback_id
            """,
            (reason, current, claim.feedback_id, claim.claim_token),
        )
        row = _one_row(cursor, "delivery block")
    if row.get("feedback_id") != claim.feedback_id:
        raise StateTransitionError("delivery block returned a different feedback record")


def admin_status_for(row: object) -> tuple[str, str | None]:
    """Return owner-facing text from a closed state allowlist only."""
    state = row.get("task_delivery_state") if isinstance(row, Mapping) else row
    if type(state) is not str:
        return "Needs attention", _MISSING_SUMMARY
    if isinstance(row, Mapping):
        desired = row.get("task_delivery_desired_version")
        synced = row.get("task_delivery_last_synced_version")
        desired_contract = row.get("task_delivery_desired_contract_version")
        synced_contract = row.get("task_delivery_last_synced_contract_version")
        task_id = row.get("task_delivery_task_id")
        if (
            type(task_id) is int
            and (
                type(desired) is not int
                or type(synced) is not int
                or desired > synced
                or desired_contract != TASK_SYNC_CONTRACT_VERSION
                or synced_contract != desired_contract
            )
        ):
            return "Task update pending", None
        if (
            state == "delivered"
            and type(task_id) is int
            and type(desired) is int
            and desired == synced
            and desired_contract == TASK_SYNC_CONTRACT_VERSION
            and synced_contract == desired_contract
        ):
            return "Owner task synced", None
    if state in {"pending", "in_flight"}:
        return "Queued for app owner", None
    if state == "attention":
        return "Needs attention", _RETRY_SUMMARY
    if state == "delivered":
        return "Assigned to app owner", None
    if state == "blocked":
        return "Needs attention", _BLOCKED_REASON
    return "Needs attention", _MISSING_SUMMARY
