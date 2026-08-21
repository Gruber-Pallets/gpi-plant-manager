"""Durable claims and immutable mutation attempts for the feedback mirror.

Every public operation validates its authority before opening a short local
transaction.  No function in this module performs an Odoo or network call.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import timedelta, datetime
from uuid import UUID, uuid4

from psycopg2.extras import Json

from . import db
from .feedback_projection import BinaryEvidence, Projection


MAX_SIGNED_64 = 9_223_372_036_854_775_807
MAX_MUTATION_ATTEMPTS = 8
MAX_WORKER_ID_LENGTH = 128
MAX_SAFE_CLASS_LENGTH = 64
MAX_SAFE_SUMMARY_LENGTH = 256
MAX_REVIEWER_LENGTH = 128
CLAIM_LEASE = timedelta(minutes=5)
DUPLICATE_RISK_WARNING = "duplicate_risk_requires_remote_review"
RETRY_DELAYS = (
    timedelta(minutes=1),
    timedelta(minutes=2),
    timedelta(minutes=4),
    timedelta(minutes=8),
    timedelta(minutes=16),
    timedelta(minutes=32),
    timedelta(minutes=60),
)

_SAFE_CLASS_RE = re.compile(r"[a-z][a-z0-9_]{0,63}", re.ASCII)
_DIGEST_RE = re.compile(r"[0-9a-f]{64}", re.ASCII)
_SOURCE_ID_RE = re.compile(r"GPI-PM-FB-([1-9][0-9]*)", re.ASCII)
_ATTEMPT_STATES = frozenset(
    {
        "prepared",
        "dispatch_marked",
        "rpc_succeeded",
        "verified",
        "definitive_failed",
        "ambiguous",
    }
)
_RECOVERABLE_ATTEMPT_STATES = frozenset({"prepared", "dispatch_marked", "rpc_succeeded"})
_MUTATION_KINDS = frozenset({"create", "update"})
_DEFINITIVE_FAILURE_SUMMARIES = {
    "gate_closed_before_rpc": "The sharing gate closed before the Odoo change.",
    "identity_read_failed": "The Odoo identity check could not finish.",
    "odoo_fault": "Odoo refused the feedback change.",
}
_UNPREPARED_READ_FAILURE_SUMMARIES = {
    "identity_read_failed": "The Odoo feedback lookup could not finish.",
    "odoo_fault": "Odoo could not complete the feedback lookup.",
}
_QUARANTINE_SUMMARIES = {
    "ambiguous_mutation": "The Odoo result is unclear, so this feedback stopped.",
    "ambiguous_stale_dispatch": "Sharing stopped after it may have started.",
    "contract_changed_before_dispatch": "The Odoo field setup changed before sharing.",
    "compound_identity_changed_before_dispatch": (
        "The Odoo feedback record changed before sharing."
    ),
    "duplicate_compound_identity": "More than one Odoo record has this feedback number.",
    "local_binary_evidence_changed": "The saved picture no longer matches this step.",
    "local_projection_unavailable": "The saved local feedback version could not be read.",
    "readback_mismatch": "The Odoo copy did not match the saved feedback.",
    "saved_id_ownership_conflict": "The saved Odoo record no longer matches this feedback.",
    "target_identity_or_contract_mismatch": "The Odoo target or fields changed.",
}
_QUARANTINE_REASONS = frozenset({*_QUARANTINE_SUMMARIES, "retry_exhausted"})
_OPERATOR_ATTEMPT_STATES = frozenset(
    {"prepared", "dispatch_marked", "rpc_succeeded", "definitive_failed", "ambiguous"}
)
_DISPOSITIONS = frozenset({"keep", "release-definitive", "supersede-and-retry"})
_BINARY_FIELDS = {
    "x_studio_image": ("before_sha256", "before_byte_length"),
    "x_studio_after_image": ("after_sha256", "after_byte_length"),
}
_NONBINARY_FIELDS = frozenset(
    {
        "x_name",
        "x_studio_source_id",
        "x_studio_date_start",
        "x_studio_type",
        "x_studio_status",
        "x_studio_source",
        "x_studio_submitted_by",
        "x_studio_date_stop",
        "x_studio_completed_by",
        "x_studio_notes",
    }
)


class StateTransitionError(RuntimeError):
    """A claim or attempt no longer has the exact expected authority."""


def _positive_signed_64(value: object, label: str) -> int:
    if type(value) is not int or not 0 < value <= MAX_SIGNED_64:
        raise ValueError(f"{label} must be a positive signed-64-bit integer")
    return value


def _nonnegative_signed_64(value: object, label: str) -> int:
    if type(value) is not int or not 0 <= value <= MAX_SIGNED_64:
        raise ValueError(f"{label} must be a nonnegative signed-64-bit integer")
    return value


def _attempt_count(value: object) -> int:
    count = _nonnegative_signed_64(value, "attempt count")
    if count > MAX_MUTATION_ATTEMPTS:
        raise ValueError("attempt count exceeds the mutation limit")
    return count


def _uuid(value: object, label: str) -> UUID:
    if type(value) is not UUID:
        raise ValueError(f"{label} must be a UUID")
    return value


def _optional_uuid(value: object, label: str) -> UUID | None:
    if value is None:
        return None
    return _uuid(value, label)


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


def _optional_aware_datetime(value: object, label: str) -> datetime | None:
    if value is None:
        return None
    return _aware_datetime(value, label)


def _worker_id(value: object) -> str:
    if (
        type(value) is not str
        or not value
        or len(value) > MAX_WORKER_ID_LENGTH
        or value != value.strip()
        or not value.isprintable()
    ):
        raise ValueError("worker id is malformed")
    return value


def _safe_class(value: object, label: str) -> str:
    if (
        type(value) is not str
        or len(value) > MAX_SAFE_CLASS_LENGTH
        or _SAFE_CLASS_RE.fullmatch(value) is None
    ):
        raise ValueError(f"{label} is malformed")
    return value


def _safe_summary(value: object, label: str, *, optional: bool = False) -> str | None:
    if optional and value is None:
        return None
    if (
        type(value) is not str
        or not value
        or len(value) > MAX_SAFE_SUMMARY_LENGTH
        or not value.isprintable()
        or any(character in value for character in "\r\n\t")
    ):
        raise ValueError(f"{label} is malformed")
    return value


def _mutation_kind(value: object) -> str:
    if type(value) is not str or value not in _MUTATION_KINDS:
        raise ValueError("mutation kind is malformed")
    return value


def _attempt_state(value: object) -> str:
    if type(value) is not str or value not in _ATTEMPT_STATES:
        raise ValueError("attempt state is malformed")
    return value


def _known_summary(value: object, summaries: Mapping[str, str], label: str) -> tuple[str, str]:
    safe_value = _safe_class(value, label)
    try:
        return safe_value, summaries[safe_value]
    except KeyError:
        raise ValueError(f"{label} is not supported") from None


def _canonical_json(value: object) -> str:
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
    except (TypeError, ValueError):
        raise ValueError("manifest is not canonical JSON") from None


def _canonical_manifest(
    manifest: object,
    manifest_digest: object,
    *,
    feedback_id: int,
) -> tuple[str, dict[str, dict[str, object]]]:
    """Validate persisted manifest shape without requiring private image bytes."""
    if type(manifest) is not dict or set(manifest) != {"fields", "binary_evidence"}:
        raise ValueError("manifest shape is malformed")
    fields = manifest.get("fields")
    evidence = manifest.get("binary_evidence")
    if type(fields) is not dict or type(evidence) is not dict:
        raise ValueError("manifest shape is malformed")
    if any(type(name) is not str for name in fields):
        raise ValueError("manifest field name is malformed")
    if fields.keys() - _NONBINARY_FIELDS:
        raise ValueError("manifest field is not allowlisted")
    if any("token" in name.casefold() for name in fields):
        raise ValueError("token fields are forbidden from manifest")
    for value in fields.values():
        if type(value) not in {str, int}:
            raise ValueError("manifest field value is not canonical")
    source_id = fields.get("x_studio_source_id")
    source = fields.get("x_studio_source")
    if type(source_id) is not str or type(source) is not str:
        raise ValueError("manifest source identity is missing")
    match = _SOURCE_ID_RE.fullmatch(source_id)
    if match is None or int(match.group(1)) != feedback_id or source != "GPI Plant Manager":
        raise ValueError("manifest source identity is malformed")
    canonical_evidence: dict[str, dict[str, object]] = {}
    for field_name, item in evidence.items():
        if type(field_name) is not str or field_name not in _BINARY_FIELDS:
            raise ValueError("manifest binary field is not allowlisted")
        if type(item) is not dict or set(item) != {"sha256", "byte_length"}:
            raise ValueError("manifest binary evidence is malformed")
        digest = item.get("sha256")
        length = item.get("byte_length")
        if type(digest) is not str or _DIGEST_RE.fullmatch(digest) is None:
            raise ValueError("manifest binary hash is malformed")
        if type(length) is not int or not 0 < length <= 5 * 1024 * 1024:
            raise ValueError("manifest binary length is malformed")
        canonical_evidence[field_name] = {
            "sha256": digest,
            "byte_length": length,
        }
    encoded = _canonical_json({"fields": dict(fields), "binary_evidence": canonical_evidence})
    calculated = hashlib.sha256(encoded.encode()).hexdigest()
    if (
        type(manifest_digest) is not str
        or _DIGEST_RE.fullmatch(manifest_digest) is None
        or manifest_digest != calculated
    ):
        raise ValueError("manifest digest does not match canonical state")
    return encoded, canonical_evidence


def _validate_row_evidence(
    row: Mapping[str, object], manifest_evidence: Mapping[str, Mapping[str, object]]
) -> None:
    for field_name, (hash_column, length_column) in _BINARY_FIELDS.items():
        saved_hash = row.get(hash_column)
        saved_length = row.get(length_column)
        expected = manifest_evidence.get(field_name)
        if expected is None:
            if saved_hash is not None or saved_length is not None:
                raise ValueError("persisted binary evidence does not match manifest")
        elif saved_hash != expected["sha256"] or saved_length != expected["byte_length"]:
            raise ValueError("persisted binary evidence does not match manifest")


@dataclass(frozen=True)
class Claim:
    feedback_id: int
    desired_version: int
    last_synced_version: int
    odoo_improvement_id: int | None
    claim_owner: str
    claim_token: UUID
    claim_expires_at: datetime
    active_attempt_id: UUID | None
    attempt_count: int

    def __post_init__(self) -> None:
        _positive_signed_64(self.feedback_id, "feedback id")
        _positive_signed_64(self.desired_version, "desired version")
        _nonnegative_signed_64(self.last_synced_version, "last synchronized version")
        if self.last_synced_version > self.desired_version:
            raise ValueError("last synchronized version exceeds desired version")
        if self.odoo_improvement_id is not None:
            _positive_signed_64(self.odoo_improvement_id, "remote id")
        _worker_id(self.claim_owner)
        _uuid(self.claim_token, "claim token")
        _aware_datetime(self.claim_expires_at, "claim expiration")
        _optional_uuid(self.active_attempt_id, "active attempt id")
        _attempt_count(self.attempt_count)


@dataclass(frozen=True, init=False)
class Attempt:
    attempt_id: UUID
    feedback_id: int
    projection_version: int
    mutation_kind: str
    remote_id: int | None
    manifest_digest: str
    state: str
    dispatch_marked_at: datetime | None
    rpc_succeeded_at: datetime | None
    readback_at: datetime | None
    settled_at: datetime | None
    outcome_detail: str | None
    created_at: datetime | None
    updated_at: datetime | None
    _manifest_json: str
    _binaries_json: str

    def __init__(
        self,
        *,
        attempt_id: UUID,
        feedback_id: int,
        projection_version: int,
        mutation_kind: str,
        remote_id: int | None,
        manifest: Mapping[str, object],
        manifest_digest: str,
        binaries: Mapping[str, object],
        state: str,
        dispatch_marked_at: datetime | None = None,
        rpc_succeeded_at: datetime | None = None,
        readback_at: datetime | None = None,
        settled_at: datetime | None = None,
        outcome_detail: str | None = None,
        created_at: datetime | None = None,
        updated_at: datetime | None = None,
    ):
        safe_attempt_id = _uuid(attempt_id, "attempt id")
        safe_feedback_id = _positive_signed_64(feedback_id, "feedback id")
        safe_version = _positive_signed_64(projection_version, "projection version")
        safe_kind = _mutation_kind(mutation_kind)
        if remote_id is not None:
            remote_id = _positive_signed_64(remote_id, "remote id")
        if safe_kind == "update" and remote_id is None:
            raise ValueError("update attempt requires a remote id")
        safe_state = _attempt_state(state)
        if (
            safe_kind == "create"
            and safe_state
            in {
                "rpc_succeeded",
                "verified",
            }
            and remote_id is None
        ):
            raise ValueError("successful create attempt requires a remote id")
        manifest_json, manifest_evidence = _canonical_manifest(
            dict(manifest) if type(manifest) is dict else manifest,
            manifest_digest,
            feedback_id=safe_feedback_id,
        )
        if type(binaries) is not dict:
            raise ValueError("attempt binaries are malformed")
        supplied_evidence: dict[str, dict[str, object]] = {}
        for field_name, item in binaries.items():
            if type(item) is BinaryEvidence:
                supplied_evidence[field_name] = {
                    "sha256": item.sha256,
                    "byte_length": item.byte_length,
                }
            elif type(item) is dict:
                supplied_evidence[field_name] = dict(item)
            else:
                raise ValueError("attempt binaries are malformed")
        if supplied_evidence != manifest_evidence:
            raise ValueError("attempt binaries do not match manifest evidence")
        safe_outcome = _safe_summary(outcome_detail, "attempt outcome", optional=True)
        object.__setattr__(self, "attempt_id", safe_attempt_id)
        object.__setattr__(self, "feedback_id", safe_feedback_id)
        object.__setattr__(self, "projection_version", safe_version)
        object.__setattr__(self, "mutation_kind", safe_kind)
        object.__setattr__(self, "remote_id", remote_id)
        object.__setattr__(self, "manifest_digest", manifest_digest)
        object.__setattr__(self, "state", safe_state)
        object.__setattr__(
            self,
            "dispatch_marked_at",
            _optional_aware_datetime(dispatch_marked_at, "dispatch time"),
        )
        object.__setattr__(
            self,
            "rpc_succeeded_at",
            _optional_aware_datetime(rpc_succeeded_at, "RPC success time"),
        )
        object.__setattr__(
            self,
            "readback_at",
            _optional_aware_datetime(readback_at, "readback time"),
        )
        object.__setattr__(
            self,
            "settled_at",
            _optional_aware_datetime(settled_at, "settlement time"),
        )
        object.__setattr__(self, "outcome_detail", safe_outcome)
        object.__setattr__(
            self,
            "created_at",
            _optional_aware_datetime(created_at, "attempt creation time"),
        )
        object.__setattr__(
            self,
            "updated_at",
            _optional_aware_datetime(updated_at, "attempt update time"),
        )
        object.__setattr__(self, "_manifest_json", manifest_json)
        object.__setattr__(self, "_binaries_json", _canonical_json(manifest_evidence))

    @property
    def manifest(self) -> dict[str, object]:
        """Return a detached canonical manifest copy."""
        return json.loads(self._manifest_json)

    @property
    def binaries(self) -> dict[str, dict[str, object]]:
        """Return detached hash/length evidence; raw image bytes are never stored."""
        return json.loads(self._binaries_json)


@dataclass(frozen=True)
class QuarantineItem:
    """One privacy-safe bounded operator-list row."""

    feedback_id: int
    attempt_id: UUID | None
    reason: str
    state: str
    quarantined_at: datetime
    attempt_count: int

    def __post_init__(self) -> None:
        _positive_signed_64(self.feedback_id, "feedback id")
        _optional_uuid(self.attempt_id, "attempt id")
        if type(self.reason) is not str or self.reason not in _QUARANTINE_REASONS:
            raise ValueError("quarantine reason is malformed")
        if self.state != "quarantined":
            raise ValueError("quarantine state is malformed")
        _aware_datetime(self.quarantined_at, "quarantine time")
        _attempt_count(self.attempt_count)


@dataclass(frozen=True)
class QuarantineDispositionResult:
    """Sanitized result of one local-only reviewed disposition."""

    feedback_id: int
    attempt_id: UUID
    disposition: str
    state: str
    desired_version: int
    warning: str | None

    def __post_init__(self) -> None:
        _positive_signed_64(self.feedback_id, "feedback id")
        _uuid(self.attempt_id, "attempt id")
        if type(self.disposition) is not str or self.disposition not in _DISPOSITIONS:
            raise ValueError("quarantine disposition is malformed")
        expected_state = "quarantined" if self.disposition == "keep" else "idle"
        if self.state != expected_state:
            raise ValueError("quarantine disposition state is malformed")
        _positive_signed_64(self.desired_version, "desired version")
        expected_warning = (
            DUPLICATE_RISK_WARNING if self.disposition == "supersede-and-retry" else None
        )
        if self.warning != expected_warning:
            raise ValueError("quarantine disposition warning is malformed")


@dataclass(frozen=True, repr=False)
class VerifiedCanaryEvidence:
    """Exact local synchronized authority and its immutable verified attempt."""

    feedback_id: int
    projection_version: int
    remote_id: int
    attempt: Attempt

    def __post_init__(self) -> None:
        _positive_signed_64(self.feedback_id, "feedback id")
        _positive_signed_64(self.projection_version, "projection version")
        _positive_signed_64(self.remote_id, "remote id")
        if (
            type(self.attempt) is not Attempt
            or self.attempt.feedback_id != self.feedback_id
            or self.attempt.projection_version != self.projection_version
            or self.attempt.remote_id != self.remote_id
            or self.attempt.state != "verified"
        ):
            raise ValueError("verified canary attempt is malformed")
        try:
            dispatch = _aware_datetime(self.attempt.dispatch_marked_at, "dispatch time")
            rpc_succeeded = _aware_datetime(
                self.attempt.rpc_succeeded_at,
                "RPC success time",
            )
            readback = _aware_datetime(self.attempt.readback_at, "readback time")
            settled = _aware_datetime(self.attempt.settled_at, "settlement time")
        except ValueError:
            raise ValueError("verified canary transition times are malformed") from None
        if not dispatch <= rpc_succeeded <= readback or readback != settled:
            raise ValueError("verified canary transition chronology is malformed")

    def __repr__(self) -> str:
        return "VerifiedCanaryEvidence(<redacted>)"


def _rows(cursor) -> list[Mapping[str, object]]:
    values = list(cursor.fetchall())
    if any(not isinstance(row, Mapping) for row in values):
        raise StateTransitionError("database returned malformed state")
    return values


def _one_row(cursor, label: str) -> Mapping[str, object]:
    values = _rows(cursor)
    if len(values) != 1:
        raise StateTransitionError(f"{label} lost its state fence")
    return values[0]


def _claim_from_row(row: Mapping[str, object]) -> Claim:
    try:
        return Claim(
            feedback_id=row.get("feedback_id"),
            desired_version=row.get("desired_version"),
            last_synced_version=row.get("last_synced_version"),
            odoo_improvement_id=row.get("odoo_improvement_id"),
            claim_owner=row.get("claim_owner"),
            claim_token=row.get("claim_token"),
            claim_expires_at=row.get("claim_expires_at"),
            active_attempt_id=row.get("active_attempt_id"),
            attempt_count=row.get("attempt_count"),
        )
    except (KeyError, TypeError, ValueError):
        raise StateTransitionError("database returned malformed claim state") from None


def _attempt_from_row(row: Mapping[str, object]) -> Attempt:
    try:
        manifest_json, evidence = _canonical_manifest(
            row.get("manifest"),
            row.get("manifest_digest"),
            feedback_id=_positive_signed_64(row.get("feedback_id"), "feedback id"),
        )
        _validate_row_evidence(row, evidence)
        return Attempt(
            attempt_id=row.get("attempt_id"),
            feedback_id=row.get("feedback_id"),
            projection_version=row.get("projection_version"),
            mutation_kind=row.get("mutation_kind"),
            remote_id=row.get("remote_id"),
            manifest=json.loads(manifest_json),
            manifest_digest=row.get("manifest_digest"),
            binaries=evidence,
            state=row.get("state", row.get("attempt_state")),
            dispatch_marked_at=row.get("dispatch_marked_at"),
            rpc_succeeded_at=row.get("rpc_succeeded_at"),
            readback_at=row.get("readback_at"),
            settled_at=row.get("settled_at"),
            outcome_detail=row.get("outcome_detail"),
            created_at=row.get("created_at"),
            updated_at=row.get("updated_at"),
        )
    except (KeyError, TypeError, ValueError):
        raise StateTransitionError("database returned malformed attempt state") from None


def _same_immutable_attempt(left: Attempt, right: Attempt) -> bool:
    return (
        left.attempt_id == right.attempt_id
        and left.feedback_id == right.feedback_id
        and left.projection_version == right.projection_version
        and left.mutation_kind == right.mutation_kind
        and left.manifest_digest == right.manifest_digest
        and left.manifest == right.manifest
        and left.binaries == right.binaries
    )


def _updated_attempt(
    value: Attempt,
    *,
    state: str,
    now: datetime,
    remote_id: int | None = None,
    outcome_detail: str | None = None,
) -> Attempt:
    return Attempt(
        attempt_id=value.attempt_id,
        feedback_id=value.feedback_id,
        projection_version=value.projection_version,
        mutation_kind=value.mutation_kind,
        remote_id=value.remote_id if remote_id is None else remote_id,
        manifest=value.manifest,
        manifest_digest=value.manifest_digest,
        binaries=value.binaries,
        state=state,
        dispatch_marked_at=(now if state == "dispatch_marked" else value.dispatch_marked_at),
        rpc_succeeded_at=(now if state == "rpc_succeeded" else value.rpc_succeeded_at),
        readback_at=value.readback_at,
        settled_at=now if state == "verified" else value.settled_at,
        outcome_detail=outcome_detail,
        created_at=value.created_at,
        updated_at=now,
    )


def _validate_selected_sync_row(row: Mapping[str, object]) -> None:
    try:
        feedback_id = _positive_signed_64(row.get("feedback_id"), "feedback id")
        desired = _positive_signed_64(row.get("desired_version"), "desired version")
        last = _nonnegative_signed_64(row.get("last_synced_version"), "last synchronized version")
        if last >= desired:
            raise ValueError("selected feedback is not due")
        remote_id = row.get("odoo_improvement_id")
        if remote_id is not None:
            _positive_signed_64(remote_id, "remote id")
        _optional_uuid(row.get("active_attempt_id"), "active attempt id")
        _attempt_count(row.get("attempt_count"))
        del feedback_id
    except (TypeError, ValueError):
        raise StateTransitionError("database returned malformed due state") from None


def retry_due(now: datetime, attempt_count: int) -> datetime:
    """Return the exact bounded retry instant for a one-based attempt count."""
    current = _aware_datetime(now, "retry time")
    if type(attempt_count) is not int or attempt_count <= 0:
        raise ValueError("attempt count must be positive")
    index = min(attempt_count - 1, len(RETRY_DELAYS) - 1)
    return current + RETRY_DELAYS[index]


def claim_due(
    *,
    now: datetime,
    worker_id: str,
    limit: int = 10,
    canary_feedback_id: int | None = None,
) -> list[Claim]:
    """Claim at most ten due rows under one select/update transaction."""
    current = _aware_datetime(now, "claim time")
    owner = _worker_id(worker_id)
    if type(limit) is not int or not 1 <= limit <= 10:
        raise ValueError("claim limit must be an integer from 1 through 10")
    if canary_feedback_id is not None:
        canary_feedback_id = _positive_signed_64(canary_feedback_id, "canary feedback id")
    claimed: list[Claim] = []
    with db.cursor() as cursor:
        cursor.execute(
            """
            SELECT s.feedback_id, s.desired_version, s.last_synced_version,
                   s.odoo_improvement_id, s.active_attempt_id, s.attempt_count
            FROM feedback_odoo_sync s
            WHERE s.state = 'idle'
              AND s.due_at <= %s
              AND s.last_synced_version < s.desired_version
              AND (%s::bigint IS NULL OR s.feedback_id = %s)
            ORDER BY s.due_at, s.feedback_id
            FOR UPDATE SKIP LOCKED
            LIMIT %s
            """,
            (current, canary_feedback_id, canary_feedback_id, limit),
        )
        selected = _rows(cursor)
        if len(selected) > limit:
            raise StateTransitionError("database exceeded the bounded claim limit")
        for row in selected:
            _validate_selected_sync_row(row)
            token = uuid4()
            _uuid(token, "generated claim token")
            expires = current + CLAIM_LEASE
            cursor.execute(
                """
                UPDATE feedback_odoo_sync
                SET state = 'in_flight', claim_owner = %s, claim_token = %s,
                    claim_expires_at = %s, updated_at = %s
                WHERE feedback_id = %s
                  AND state = 'idle'
                  AND desired_version = %s
                  AND last_synced_version = %s
                  AND odoo_improvement_id IS NOT DISTINCT FROM %s
                  AND active_attempt_id IS NOT DISTINCT FROM %s
                  AND attempt_count = %s
                RETURNING feedback_id, desired_version, last_synced_version,
                          odoo_improvement_id, claim_owner, claim_token,
                          claim_expires_at, active_attempt_id, attempt_count
                """,
                (
                    owner,
                    token,
                    expires,
                    current,
                    row["feedback_id"],
                    row["desired_version"],
                    row["last_synced_version"],
                    row["odoo_improvement_id"],
                    row["active_attempt_id"],
                    row["attempt_count"],
                ),
            )
            result = _claim_from_row(_one_row(cursor, "claim update"))
            if (
                result.feedback_id != row["feedback_id"]
                or result.claim_owner != owner
                or result.claim_token != token
                or result.claim_expires_at != expires
            ):
                raise StateTransitionError("claim update returned different authority")
            claimed.append(result)
    return claimed


def load_active_attempt(claim: Claim) -> Attempt | None:
    """Load the exact active attempt for an owned claim, if one exists."""
    if type(claim) is not Claim:
        raise ValueError("claim is malformed")
    with db.cursor() as cursor:
        cursor.execute(
            """
            SELECT a.attempt_id, a.feedback_id, a.projection_version,
                   a.mutation_kind, a.remote_id, a.manifest, a.manifest_digest,
                   a.before_sha256, a.before_byte_length,
                   a.after_sha256, a.after_byte_length, a.state,
                   a.dispatch_marked_at, a.rpc_succeeded_at, a.readback_at,
                   a.settled_at, a.outcome_detail, a.created_at, a.updated_at
            FROM feedback_odoo_sync s
            LEFT JOIN feedback_odoo_attempts a
              ON a.feedback_id = s.feedback_id
             AND a.attempt_id = s.active_attempt_id
            WHERE s.feedback_id = %s
              AND s.state = 'in_flight'
              AND s.desired_version >= %s
              AND s.last_synced_version = %s
              AND s.odoo_improvement_id IS NOT DISTINCT FROM %s
              AND s.claim_owner = %s
              AND s.claim_token = %s
              AND s.active_attempt_id IS NOT DISTINCT FROM %s
              AND s.attempt_count = %s
              AND (
                  a.attempt_id IS NULL
                  OR (
                      a.projection_version > s.last_synced_version
                      AND a.projection_version <= s.desired_version
                  )
              )
            """,
            (
                claim.feedback_id,
                claim.desired_version,
                claim.last_synced_version,
                claim.odoo_improvement_id,
                claim.claim_owner,
                claim.claim_token,
                claim.active_attempt_id,
                claim.attempt_count,
            ),
        )
        row = _one_row(cursor, "active attempt load")
        if row.get("attempt_id") is None:
            if claim.active_attempt_id is not None:
                raise StateTransitionError("active attempt is missing")
            return None
        loaded = _attempt_from_row(row)
        if claim.active_attempt_id != loaded.attempt_id:
            raise StateTransitionError("active attempt identity changed")
        _validate_attempt_claim_relation(claim, loaded)
        return loaded


def release_stale_unprepared_claim(claim: Claim, now: datetime) -> bool:
    """Release an owned unprepared claim only after desired truth advanced."""
    if type(claim) is not Claim or claim.active_attempt_id is not None:
        raise ValueError("claim is not an unprepared owned claim")
    current = _aware_datetime(now, "stale claim release time")
    with db.cursor() as cursor:
        cursor.execute(
            """
            SELECT feedback_id, desired_version, last_synced_version,
                   odoo_improvement_id, claim_owner, claim_token,
                   claim_expires_at, active_attempt_id, attempt_count
            FROM feedback_odoo_sync
            WHERE feedback_id = %s
              AND state = 'in_flight'
              AND desired_version >= %s
              AND last_synced_version = %s
              AND odoo_improvement_id IS NOT DISTINCT FROM %s
              AND claim_owner = %s
              AND claim_token = %s
              AND claim_expires_at = %s
              AND active_attempt_id IS NULL
              AND attempt_count = %s
            FOR UPDATE
            """,
            (
                claim.feedback_id,
                claim.desired_version,
                claim.last_synced_version,
                claim.odoo_improvement_id,
                claim.claim_owner,
                claim.claim_token,
                claim.claim_expires_at,
                claim.attempt_count,
            ),
        )
        selected = _claim_from_row(_one_row(cursor, "stale unprepared claim lock"))
        if (
            selected.feedback_id != claim.feedback_id
            or selected.desired_version < claim.desired_version
            or selected.last_synced_version != claim.last_synced_version
            or selected.odoo_improvement_id != claim.odoo_improvement_id
            or selected.claim_owner != claim.claim_owner
            or selected.claim_token != claim.claim_token
            or selected.claim_expires_at != claim.claim_expires_at
            or selected.active_attempt_id is not None
            or selected.attempt_count != claim.attempt_count
        ):
            raise StateTransitionError("stale claim lock returned different authority")
        if selected.desired_version == claim.desired_version:
            return False
        cursor.execute(
            """
            UPDATE feedback_odoo_sync
            SET state = 'idle', claim_owner = NULL, claim_token = NULL,
                claim_expires_at = NULL, due_at = %s, updated_at = %s
            WHERE feedback_id = %s
              AND state = 'in_flight'
              AND desired_version = %s
              AND desired_version > %s
              AND last_synced_version = %s
              AND odoo_improvement_id IS NOT DISTINCT FROM %s
              AND claim_owner = %s
              AND claim_token = %s
              AND claim_expires_at = %s
              AND active_attempt_id IS NULL
              AND attempt_count = %s
            RETURNING feedback_id, desired_version
            """,
            (
                current,
                current,
                claim.feedback_id,
                selected.desired_version,
                claim.desired_version,
                claim.last_synced_version,
                claim.odoo_improvement_id,
                claim.claim_owner,
                claim.claim_token,
                claim.claim_expires_at,
                claim.attempt_count,
            ),
        )
        updated = _one_row(cursor, "stale unprepared claim release")
        if (
            updated.get("feedback_id") != claim.feedback_id
            or updated.get("desired_version") != selected.desired_version
        ):
            raise StateTransitionError("stale claim release returned different authority")
    return True


def defer_unprepared_read_failure(
    claim: Claim,
    error_class: str,
    now: datetime,
) -> bool:
    """Safely release owned unprepared work after a pre-mutation Odoo read failure."""
    if type(claim) is not Claim or claim.active_attempt_id is not None:
        raise ValueError("claim is not an unprepared owned claim")
    safe_error_class, safe_summary = _known_summary(
        error_class,
        _UNPREPARED_READ_FAILURE_SUMMARIES,
        "error class",
    )
    current = _aware_datetime(now, "unprepared read failure time")
    with db.cursor() as cursor:
        cursor.execute(
            """
            SELECT feedback_id, desired_version, last_synced_version,
                   odoo_improvement_id, claim_owner, claim_token,
                   claim_expires_at, active_attempt_id, attempt_count
            FROM feedback_odoo_sync
            WHERE feedback_id = %s
              AND state = 'in_flight'
              AND desired_version >= %s
              AND last_synced_version = %s
              AND odoo_improvement_id IS NOT DISTINCT FROM %s
              AND claim_owner = %s
              AND claim_token = %s
              AND claim_expires_at = %s
              AND active_attempt_id IS NULL
              AND attempt_count = %s
            FOR UPDATE
            """,
            (
                claim.feedback_id,
                claim.desired_version,
                claim.last_synced_version,
                claim.odoo_improvement_id,
                claim.claim_owner,
                claim.claim_token,
                claim.claim_expires_at,
                claim.attempt_count,
            ),
        )
        selected = _claim_from_row(_one_row(cursor, "unprepared read failure lock"))
        if (
            selected.feedback_id != claim.feedback_id
            or selected.desired_version < claim.desired_version
            or selected.last_synced_version != claim.last_synced_version
            or selected.odoo_improvement_id != claim.odoo_improvement_id
            or selected.claim_owner != claim.claim_owner
            or selected.claim_token != claim.claim_token
            or selected.claim_expires_at != claim.claim_expires_at
            or selected.active_attempt_id is not None
            or selected.attempt_count != claim.attempt_count
        ):
            raise StateTransitionError("unprepared read failure lock returned different authority")
        due = (
            current
            if selected.desired_version > claim.desired_version
            else current + RETRY_DELAYS[0]
        )
        cursor.execute(
            """
            UPDATE feedback_odoo_sync
            SET state = 'idle', claim_owner = NULL, claim_token = NULL,
                claim_expires_at = NULL, due_at = %s,
                last_error_class = %s, last_error_summary = %s,
                updated_at = %s
            WHERE feedback_id = %s
              AND state = 'in_flight'
              AND desired_version = %s
              AND desired_version >= %s
              AND last_synced_version = %s
              AND odoo_improvement_id IS NOT DISTINCT FROM %s
              AND claim_owner = %s
              AND claim_token = %s
              AND claim_expires_at = %s
              AND active_attempt_id IS NULL
              AND attempt_count = %s
            RETURNING feedback_id, desired_version, due_at
            """,
            (
                due,
                safe_error_class,
                safe_summary,
                current,
                claim.feedback_id,
                selected.desired_version,
                claim.desired_version,
                claim.last_synced_version,
                claim.odoo_improvement_id,
                claim.claim_owner,
                claim.claim_token,
                claim.claim_expires_at,
                claim.attempt_count,
            ),
        )
        updated = _one_row(cursor, "unprepared read failure release")
        if (
            updated.get("feedback_id") != claim.feedback_id
            or updated.get("desired_version") != selected.desired_version
            or updated.get("due_at") != due
        ):
            raise StateTransitionError("unprepared read failure returned different authority")
    return True


def _lock_unprepared_claim(cursor, claim: Claim) -> Claim:
    cursor.execute(
        """
        SELECT feedback_id, desired_version, last_synced_version,
               odoo_improvement_id, claim_owner, claim_token,
               claim_expires_at, active_attempt_id, attempt_count
        FROM feedback_odoo_sync
        WHERE feedback_id = %s
          AND state = 'in_flight'
          AND desired_version = %s
          AND last_synced_version = %s
          AND odoo_improvement_id IS NOT DISTINCT FROM %s
          AND claim_owner = %s
          AND claim_token = %s
          AND active_attempt_id IS NULL
          AND attempt_count = %s
        FOR UPDATE
        """,
        (
            claim.feedback_id,
            claim.desired_version,
            claim.last_synced_version,
            claim.odoo_improvement_id,
            claim.claim_owner,
            claim.claim_token,
            claim.attempt_count,
        ),
    )
    locked = _claim_from_row(_one_row(cursor, "claim lock"))
    if locked != claim:
        raise StateTransitionError("claim lock returned different authority")
    return locked


def _validate_attempt_claim_relation(claim: Claim, attempt: Attempt) -> None:
    if type(claim) is not Claim or type(attempt) is not Attempt:
        raise ValueError("claim or attempt is malformed")
    if attempt.feedback_id != claim.feedback_id:
        raise ValueError("attempt does not belong to claim feedback")
    if claim.active_attempt_id not in {None, attempt.attempt_id}:
        raise ValueError("attempt is not active for claim")
    if not (claim.last_synced_version < attempt.projection_version <= claim.desired_version):
        raise ValueError("attempt does not belong to claim projection window")


def _lock_owned_attempt(
    cursor,
    claim: Claim,
    attempt: Attempt,
    *,
    expected_states: tuple[str, ...],
) -> Attempt:
    _validate_attempt_claim_relation(claim, attempt)
    if not expected_states or any(state not in _ATTEMPT_STATES for state in expected_states):
        raise ValueError("expected attempt state is malformed")
    cursor.execute(
        """
        SELECT a.attempt_id, a.feedback_id, a.projection_version,
               a.mutation_kind, a.remote_id, a.manifest, a.manifest_digest,
               a.before_sha256, a.before_byte_length,
               a.after_sha256, a.after_byte_length, a.state,
               a.dispatch_marked_at, a.rpc_succeeded_at, a.readback_at,
               a.settled_at, a.outcome_detail, a.created_at, a.updated_at,
               s.desired_version AS sync_desired_version,
               s.last_synced_version AS sync_last_synced_version,
               s.odoo_improvement_id AS sync_remote_id,
               s.attempt_count AS sync_attempt_count
        FROM feedback_odoo_sync s
        JOIN feedback_odoo_attempts a
          ON a.feedback_id = s.feedback_id
         AND a.attempt_id = s.active_attempt_id
        WHERE s.feedback_id = %s
          AND s.state = 'in_flight'
          AND s.desired_version >= %s
          AND s.last_synced_version = %s
          AND (
              (a.state = 'rpc_succeeded'
               AND s.odoo_improvement_id = a.remote_id)
              OR
              (a.state <> 'rpc_succeeded'
               AND s.odoo_improvement_id IS NOT DISTINCT FROM %s)
          )
          AND s.claim_owner = %s
          AND s.claim_token = %s
          AND s.active_attempt_id = %s
          AND s.attempt_count = %s
          AND a.attempt_id = %s
          AND a.projection_version = %s
          AND a.projection_version > s.last_synced_version
          AND a.projection_version <= s.desired_version
          AND a.state = ANY(%s)
        FOR UPDATE OF s, a
        """,
        (
            claim.feedback_id,
            claim.desired_version,
            claim.last_synced_version,
            claim.odoo_improvement_id,
            claim.claim_owner,
            claim.claim_token,
            attempt.attempt_id,
            claim.attempt_count,
            attempt.attempt_id,
            attempt.projection_version,
            list(expected_states),
        ),
    )
    row = _one_row(cursor, "attempt lock")
    locked = _attempt_from_row(row)
    if locked.state not in expected_states or not _same_immutable_attempt(locked, attempt):
        raise StateTransitionError("attempt lock returned different authority")
    try:
        desired = _positive_signed_64(row.get("sync_desired_version"), "desired version")
        last = _nonnegative_signed_64(
            row.get("sync_last_synced_version"), "last synchronized version"
        )
        remote = row.get("sync_remote_id")
        if remote is not None:
            _positive_signed_64(remote, "remote id")
        count = _attempt_count(row.get("sync_attempt_count"))
    except ValueError:
        raise StateTransitionError("database returned malformed sync authority") from None
    if (
        desired < claim.desired_version
        or last != claim.last_synced_version
        or not last < locked.projection_version <= desired
        or (locked.state == "rpc_succeeded" and remote != locked.remote_id)
        or (locked.state != "rpc_succeeded" and remote != claim.odoo_improvement_id)
        or count != claim.attempt_count
    ):
        raise StateTransitionError("sync authority changed")
    return locked


def prepare_attempt(
    *,
    claim: Claim,
    attempt_id: UUID,
    mutation_kind: str,
    remote_id: int | None,
    manifest: Mapping[str, object],
    manifest_digest: str,
    binaries: Mapping[str, BinaryEvidence],
    now: datetime,
) -> Attempt:
    """Persist immutable canonical evidence before any dispatch can occur."""
    if type(claim) is not Claim or claim.active_attempt_id is not None:
        raise ValueError("claim is not ready for a new attempt")
    safe_attempt_id = _uuid(attempt_id, "attempt id")
    safe_kind = _mutation_kind(mutation_kind)
    current = _aware_datetime(now, "attempt time")
    if safe_kind == "create":
        if remote_id is not None:
            raise ValueError("create attempt cannot start with a remote id")
    else:
        remote_id = _positive_signed_64(remote_id, "remote id")
    if type(manifest) is not dict or type(binaries) is not dict:
        raise ValueError("attempt manifest inputs are malformed")
    fields = manifest.get("fields")
    if type(fields) is not dict:
        raise ValueError("manifest fields are malformed")
    source_id = fields.get("x_studio_source_id")
    projection = Projection(
        source_id=source_id,
        fields=fields,
        binaries=binaries,
        manifest=manifest,
        manifest_digest=manifest_digest,
    )
    if source_id != f"GPI-PM-FB-{claim.feedback_id}":
        raise ValueError("manifest does not belong to claimed feedback")
    if str(claim.claim_token) in _canonical_json(projection.manifest):
        raise ValueError("claim token is forbidden from manifest")
    saved = Attempt(
        attempt_id=safe_attempt_id,
        feedback_id=claim.feedback_id,
        projection_version=claim.desired_version,
        mutation_kind=safe_kind,
        remote_id=remote_id,
        manifest=projection.manifest,
        manifest_digest=projection.manifest_digest,
        binaries=projection.binaries,
        state="prepared",
        created_at=current,
        updated_at=current,
    )
    evidence = saved.binaries
    before = evidence.get("x_studio_image", {})
    after = evidence.get("x_studio_after_image", {})
    with db.cursor() as cursor:
        _lock_unprepared_claim(cursor, claim)
        cursor.execute(
            """
            INSERT INTO feedback_odoo_attempts (
                attempt_id, feedback_id, projection_version, mutation_kind,
                remote_id, manifest, manifest_digest,
                before_sha256, before_byte_length,
                after_sha256, after_byte_length,
                state, created_at, updated_at
            ) VALUES (
                %s, %s, %s, %s, %s, %s, %s,
                %s, %s, %s, %s, 'prepared', %s, %s
            )
            """,
            (
                saved.attempt_id,
                saved.feedback_id,
                saved.projection_version,
                saved.mutation_kind,
                saved.remote_id,
                Json(saved.manifest),
                saved.manifest_digest,
                before.get("sha256"),
                before.get("byte_length"),
                after.get("sha256"),
                after.get("byte_length"),
                current,
                current,
            ),
        )
        cursor.execute(
            """
            UPDATE feedback_odoo_sync
            SET active_attempt_id = %s, updated_at = %s
            WHERE feedback_id = %s
              AND state = 'in_flight'
              AND desired_version = %s
              AND last_synced_version = %s
              AND odoo_improvement_id IS NOT DISTINCT FROM %s
              AND claim_owner = %s
              AND claim_token = %s
              AND active_attempt_id IS NULL
              AND attempt_count = %s
            RETURNING feedback_id
            """,
            (
                saved.attempt_id,
                current,
                claim.feedback_id,
                claim.desired_version,
                claim.last_synced_version,
                claim.odoo_improvement_id,
                claim.claim_owner,
                claim.claim_token,
                claim.attempt_count,
            ),
        )
        _one_row(cursor, "attempt activation")
    return saved


def defer_prepared_for_closed_gate(claim: Claim, attempt: Attempt, now: datetime) -> bool:
    _validate_attempt_claim_relation(claim, attempt)
    current = _aware_datetime(now, "defer time")
    with db.cursor() as cursor:
        _lock_owned_attempt(cursor, claim, attempt, expected_states=("prepared",))
        cursor.execute(
            """
            UPDATE feedback_odoo_sync
            SET state = 'idle', claim_owner = NULL, claim_token = NULL,
                claim_expires_at = NULL, due_at = %s, updated_at = %s
            WHERE feedback_id = %s
              AND state = 'in_flight'
              AND desired_version >= %s
              AND last_synced_version = %s
              AND odoo_improvement_id IS NOT DISTINCT FROM %s
              AND claim_owner = %s
              AND claim_token = %s
              AND active_attempt_id = %s
              AND attempt_count = %s
              AND EXISTS (
                  SELECT 1 FROM feedback_odoo_attempts a
                  WHERE a.feedback_id = %s AND a.attempt_id = %s
                    AND a.projection_version = %s AND a.state = 'prepared'
              )
            RETURNING feedback_id
            """,
            (
                current,
                current,
                claim.feedback_id,
                claim.desired_version,
                claim.last_synced_version,
                claim.odoo_improvement_id,
                claim.claim_owner,
                claim.claim_token,
                attempt.attempt_id,
                claim.attempt_count,
                claim.feedback_id,
                attempt.attempt_id,
                attempt.projection_version,
            ),
        )
        _one_row(cursor, "prepared defer")
    return True


def mark_dispatch(claim: Claim, attempt: Attempt, now: datetime) -> Attempt:
    _validate_attempt_claim_relation(claim, attempt)
    current = _aware_datetime(now, "dispatch time")
    with db.cursor() as cursor:
        locked = _lock_owned_attempt(cursor, claim, attempt, expected_states=("prepared",))
        cursor.execute(
            """
            UPDATE feedback_odoo_attempts
            SET state = 'dispatch_marked', dispatch_marked_at = %s, updated_at = %s
            WHERE feedback_id = %s AND attempt_id = %s
              AND projection_version = %s AND state = %s
            RETURNING attempt_id
            """,
            (
                current,
                current,
                claim.feedback_id,
                attempt.attempt_id,
                attempt.projection_version,
                "prepared",
            ),
        )
        _one_row(cursor, "dispatch mark")
    return _updated_attempt(locked, state="dispatch_marked", now=current)


def mark_rpc_succeeded(
    claim: Claim,
    attempt: Attempt,
    remote_id: int,
    now: datetime,
) -> Attempt:
    _validate_attempt_claim_relation(claim, attempt)
    safe_remote_id = _positive_signed_64(remote_id, "remote id")
    current = _aware_datetime(now, "RPC success time")
    with db.cursor() as cursor:
        locked = _lock_owned_attempt(cursor, claim, attempt, expected_states=("dispatch_marked",))
        if locked.mutation_kind == "update" and locked.remote_id != safe_remote_id:
            raise StateTransitionError("update remote identity changed")
        if locked.mutation_kind == "create" and locked.remote_id is not None:
            raise StateTransitionError("create remote identity was already recorded")
        cursor.execute(
            """
            UPDATE feedback_odoo_attempts
            SET state = 'rpc_succeeded', remote_id = %s,
                rpc_succeeded_at = %s, updated_at = %s
            WHERE feedback_id = %s AND attempt_id = %s
              AND projection_version = %s AND state = %s
              AND remote_id IS NOT DISTINCT FROM %s
            RETURNING attempt_id
            """,
            (
                safe_remote_id,
                current,
                current,
                claim.feedback_id,
                attempt.attempt_id,
                attempt.projection_version,
                "dispatch_marked",
                locked.remote_id,
            ),
        )
        _one_row(cursor, "RPC success attempt")
        cursor.execute(
            """
            UPDATE feedback_odoo_sync
            SET odoo_improvement_id = %s, updated_at = %s
            WHERE feedback_id = %s
              AND state = 'in_flight'
              AND desired_version >= %s
              AND last_synced_version = %s
              AND odoo_improvement_id IS NOT DISTINCT FROM %s
              AND claim_owner = %s AND claim_token = %s
              AND active_attempt_id = %s AND attempt_count = %s
              AND EXISTS (
                  SELECT 1 FROM feedback_odoo_attempts a
                  WHERE a.feedback_id = %s AND a.attempt_id = %s
                    AND a.projection_version = %s AND a.state = 'rpc_succeeded'
                    AND a.remote_id = %s
              )
            RETURNING feedback_id
            """,
            (
                safe_remote_id,
                current,
                claim.feedback_id,
                claim.desired_version,
                claim.last_synced_version,
                claim.odoo_improvement_id,
                claim.claim_owner,
                claim.claim_token,
                attempt.attempt_id,
                claim.attempt_count,
                claim.feedback_id,
                attempt.attempt_id,
                attempt.projection_version,
                safe_remote_id,
            ),
        )
        _one_row(cursor, "RPC success sync")
    return _updated_attempt(locked, state="rpc_succeeded", now=current, remote_id=safe_remote_id)


def schedule_readback(claim: Claim, attempt: Attempt, now: datetime) -> bool:
    _validate_attempt_claim_relation(claim, attempt)
    current = _aware_datetime(now, "readback schedule time")
    with db.cursor() as cursor:
        locked = _lock_owned_attempt(cursor, claim, attempt, expected_states=("rpc_succeeded",))
        cursor.execute(
            """
            UPDATE feedback_odoo_sync
            SET state = 'idle', claim_owner = NULL, claim_token = NULL,
                claim_expires_at = NULL, due_at = %s, updated_at = %s
            WHERE feedback_id = %s AND state = 'in_flight'
              AND desired_version >= %s AND last_synced_version = %s
              AND odoo_improvement_id = %s
              AND claim_owner = %s AND claim_token = %s
              AND active_attempt_id = %s AND attempt_count = %s
              AND EXISTS (
                  SELECT 1 FROM feedback_odoo_attempts a
                  WHERE a.feedback_id = %s AND a.attempt_id = %s
                    AND a.projection_version = %s AND a.state = 'rpc_succeeded'
                    AND a.remote_id = %s
              )
            RETURNING feedback_id
            """,
            (
                current,
                current,
                claim.feedback_id,
                claim.desired_version,
                claim.last_synced_version,
                locked.remote_id,
                claim.claim_owner,
                claim.claim_token,
                attempt.attempt_id,
                claim.attempt_count,
                claim.feedback_id,
                attempt.attempt_id,
                attempt.projection_version,
                locked.remote_id,
            ),
        )
        _one_row(cursor, "readback schedule")
    return True


def settle_verified(
    claim: Claim,
    attempt: Attempt,
    remote_id: int,
    now: datetime,
) -> bool:
    _validate_attempt_claim_relation(claim, attempt)
    safe_remote_id = _positive_signed_64(remote_id, "remote id")
    current = _aware_datetime(now, "settlement time")
    with db.cursor() as cursor:
        locked = _lock_owned_attempt(cursor, claim, attempt, expected_states=("rpc_succeeded",))
        if locked.remote_id != safe_remote_id:
            raise StateTransitionError("settlement remote identity changed")
        cursor.execute(
            """
            UPDATE feedback_odoo_attempts
            SET state = 'verified', readback_at = %s, settled_at = %s,
                updated_at = %s
            WHERE feedback_id = %s AND attempt_id = %s
              AND projection_version = %s AND state = %s AND remote_id = %s
            RETURNING attempt_id
            """,
            (
                current,
                current,
                current,
                claim.feedback_id,
                attempt.attempt_id,
                attempt.projection_version,
                "rpc_succeeded",
                safe_remote_id,
            ),
        )
        _one_row(cursor, "verified attempt")
        cursor.execute(
            """
            UPDATE feedback_odoo_sync
            SET last_synced_version = %s,
                state = 'idle', claim_owner = NULL, claim_token = NULL,
                claim_expires_at = NULL, active_attempt_id = NULL,
                attempt_count = 0, last_error_class = NULL,
                last_error_summary = NULL, quarantine_reason = NULL,
                quarantined_at = NULL,
                due_at = CASE WHEN desired_version > %s THEN %s ELSE due_at END,
                updated_at = %s
            WHERE feedback_id = %s AND state = 'in_flight'
              AND desired_version >= %s
              AND last_synced_version = %s
              AND odoo_improvement_id = %s
              AND claim_owner = %s AND claim_token = %s
              AND active_attempt_id = %s AND attempt_count = %s
              AND EXISTS (
                  SELECT 1 FROM feedback_odoo_attempts a
                  WHERE a.feedback_id = %s AND a.attempt_id = %s
                    AND a.projection_version = %s AND a.state = 'verified'
                    AND a.remote_id = %s
              )
            RETURNING feedback_id, desired_version, last_synced_version
            """,
            (
                attempt.projection_version,
                attempt.projection_version,
                current,
                current,
                claim.feedback_id,
                claim.desired_version,
                claim.last_synced_version,
                safe_remote_id,
                claim.claim_owner,
                claim.claim_token,
                attempt.attempt_id,
                claim.attempt_count,
                claim.feedback_id,
                attempt.attempt_id,
                attempt.projection_version,
                safe_remote_id,
            ),
        )
        row = _one_row(cursor, "verified settlement")
        try:
            desired = _positive_signed_64(row.get("desired_version"), "desired version")
            synchronized = _positive_signed_64(
                row.get("last_synced_version"), "last synchronized version"
            )
        except ValueError:
            raise StateTransitionError("settlement returned malformed version") from None
        if synchronized != attempt.projection_version or desired < synchronized:
            raise StateTransitionError("settlement returned different version")
    return True


def record_definitive_failure(
    claim: Claim,
    attempt: Attempt,
    error_class: str,
    now: datetime,
) -> str:
    _validate_attempt_claim_relation(claim, attempt)
    safe_error_class, safe_summary = _known_summary(
        error_class,
        _DEFINITIVE_FAILURE_SUMMARIES,
        "error class",
    )
    current = _aware_datetime(now, "failure time")
    if type(claim) is not Claim or claim.attempt_count >= MAX_MUTATION_ATTEMPTS:
        raise ValueError("claim has exhausted mutation attempts")
    next_count = claim.attempt_count + 1
    with db.cursor() as cursor:
        locked = _lock_owned_attempt(
            cursor,
            claim,
            attempt,
            expected_states=("prepared", "dispatch_marked"),
        )
        cursor.execute(
            """
            UPDATE feedback_odoo_attempts
            SET state = 'definitive_failed', outcome_detail = %s, updated_at = %s
            WHERE feedback_id = %s AND attempt_id = %s
              AND projection_version = %s AND state = %s
            RETURNING attempt_id
            """,
            (
                safe_error_class,
                current,
                claim.feedback_id,
                attempt.attempt_id,
                attempt.projection_version,
                locked.state,
            ),
        )
        _one_row(cursor, "definitive failure attempt")
        if next_count >= MAX_MUTATION_ATTEMPTS:
            cursor.execute(
                """
                UPDATE feedback_odoo_sync
                SET state = 'quarantined', attempt_count = %s,
                    claim_owner = NULL, claim_token = NULL,
                    claim_expires_at = NULL,
                    last_error_class = %s, last_error_summary = %s,
                    quarantine_reason = 'retry_exhausted', quarantined_at = %s,
                    updated_at = %s
                WHERE feedback_id = %s AND state = 'in_flight'
                  AND desired_version >= %s AND last_synced_version = %s
                  AND odoo_improvement_id IS NOT DISTINCT FROM %s
                  AND claim_owner = %s AND claim_token = %s
                  AND active_attempt_id = %s AND attempt_count = %s
                  AND EXISTS (
                      SELECT 1 FROM feedback_odoo_attempts a
                      WHERE a.feedback_id = %s AND a.attempt_id = %s
                        AND a.projection_version = %s
                        AND a.state = 'definitive_failed'
                  )
                RETURNING feedback_id, attempt_count, state
                """,
                (
                    next_count,
                    safe_error_class,
                    safe_summary,
                    current,
                    current,
                    claim.feedback_id,
                    claim.desired_version,
                    claim.last_synced_version,
                    claim.odoo_improvement_id,
                    claim.claim_owner,
                    claim.claim_token,
                    attempt.attempt_id,
                    claim.attempt_count,
                    claim.feedback_id,
                    attempt.attempt_id,
                    attempt.projection_version,
                ),
            )
            outcome = "quarantined"
        else:
            due = retry_due(current, next_count)
            cursor.execute(
                """
                UPDATE feedback_odoo_sync
                SET state = 'idle', attempt_count = %s, due_at = %s,
                    claim_owner = NULL, claim_token = NULL,
                    claim_expires_at = NULL, active_attempt_id = NULL,
                    last_error_class = %s, last_error_summary = %s,
                    updated_at = %s
                WHERE feedback_id = %s AND state = 'in_flight'
                  AND desired_version >= %s AND last_synced_version = %s
                  AND odoo_improvement_id IS NOT DISTINCT FROM %s
                  AND claim_owner = %s AND claim_token = %s
                  AND active_attempt_id = %s AND attempt_count = %s
                  AND EXISTS (
                      SELECT 1 FROM feedback_odoo_attempts a
                      WHERE a.feedback_id = %s AND a.attempt_id = %s
                        AND a.projection_version = %s
                        AND a.state = 'definitive_failed'
                  )
                RETURNING feedback_id, attempt_count, state
                """,
                (
                    next_count,
                    due,
                    safe_error_class,
                    safe_summary,
                    current,
                    claim.feedback_id,
                    claim.desired_version,
                    claim.last_synced_version,
                    claim.odoo_improvement_id,
                    claim.claim_owner,
                    claim.claim_token,
                    attempt.attempt_id,
                    claim.attempt_count,
                    claim.feedback_id,
                    attempt.attempt_id,
                    attempt.projection_version,
                ),
            )
            outcome = "retry_scheduled"
        result = _one_row(cursor, "definitive failure sync")
        if result.get("attempt_count") != next_count or result.get("state") != (
            "quarantined" if outcome == "quarantined" else "idle"
        ):
            raise StateTransitionError("failure transition returned different state")
    return outcome


def quarantine(
    claim: Claim,
    reason: str,
    now: datetime,
    *,
    attempt: Attempt | None = None,
) -> bool:
    """Quarantine owned work without changing immutable attempt evidence."""
    safe_reason, safe_summary = _known_summary(
        reason,
        _QUARANTINE_SUMMARIES,
        "quarantine reason",
    )
    current = _aware_datetime(now, "quarantine time")
    if type(claim) is not Claim:
        raise ValueError("claim is malformed")
    if attempt is not None:
        _validate_attempt_claim_relation(claim, attempt)
    with db.cursor() as cursor:
        expected_remote_id = claim.odoo_improvement_id
        if attempt is None:
            _lock_unprepared_claim(cursor, claim)
        else:
            locked = _lock_owned_attempt(
                cursor,
                claim,
                attempt,
                expected_states=tuple(sorted(_RECOVERABLE_ATTEMPT_STATES)),
            )
            if locked.state == "rpc_succeeded":
                expected_remote_id = locked.remote_id
            cursor.execute(
                """
                UPDATE feedback_odoo_attempts
                SET state = 'ambiguous', outcome_detail = %s, updated_at = %s
                WHERE feedback_id = %s AND attempt_id = %s
                  AND projection_version = %s AND state = %s
                RETURNING attempt_id
                """,
                (
                    safe_reason,
                    current,
                    claim.feedback_id,
                    attempt.attempt_id,
                    attempt.projection_version,
                    locked.state,
                ),
            )
            _one_row(cursor, "quarantine attempt")
        cursor.execute(
            """
            UPDATE feedback_odoo_sync
            SET state = 'quarantined', claim_owner = NULL, claim_token = NULL,
                claim_expires_at = NULL, quarantine_reason = %s,
                quarantined_at = %s, last_error_class = %s,
                last_error_summary = %s, updated_at = %s
            WHERE feedback_id = %s AND state = 'in_flight'
              AND desired_version >= %s AND last_synced_version = %s
              AND odoo_improvement_id IS NOT DISTINCT FROM %s
              AND claim_owner = %s AND claim_token = %s
              AND active_attempt_id IS NOT DISTINCT FROM %s
              AND attempt_count = %s
            RETURNING feedback_id
            """,
            (
                safe_reason,
                current,
                safe_reason,
                safe_summary,
                current,
                claim.feedback_id,
                claim.desired_version,
                claim.last_synced_version,
                expected_remote_id,
                claim.claim_owner,
                claim.claim_token,
                None if attempt is None else attempt.attempt_id,
                claim.attempt_count,
            ),
        )
        _one_row(cursor, "quarantine sync")
    return True


def _validate_expired_row(row: Mapping[str, object]) -> tuple[UUID | None, str | None]:
    try:
        _positive_signed_64(row.get("feedback_id"), "feedback id")
        desired = _positive_signed_64(row.get("desired_version"), "desired version")
        last = _nonnegative_signed_64(row.get("last_synced_version"), "last synchronized version")
        if last > desired:
            raise ValueError("synchronized version exceeds desired")
        remote = row.get("odoo_improvement_id")
        if remote is not None:
            _positive_signed_64(remote, "remote id")
        _worker_id(row.get("claim_owner"))
        _uuid(row.get("claim_token"), "claim token")
        _aware_datetime(row.get("claim_expires_at"), "claim expiration")
        active_id = _optional_uuid(row.get("active_attempt_id"), "active attempt id")
        _attempt_count(row.get("attempt_count"))
        attempt_id = _optional_uuid(row.get("attempt_id"), "attempt id")
        state = row.get("attempt_state")
        if state is not None:
            state = _attempt_state(state)
        if active_id != attempt_id or (attempt_id is None) != (state is None):
            raise ValueError("expired attempt identity is inconsistent")
        if attempt_id is not None:
            projection = _positive_signed_64(row.get("projection_version"), "projection version")
            if not last < projection <= desired:
                raise ValueError("attempt projection is outside the active version window")
            attempt_remote = row.get("attempt_remote_id")
            if state == "rpc_succeeded":
                attempt_remote = _positive_signed_64(attempt_remote, "attempt remote id")
                if attempt_remote != remote:
                    raise ValueError("RPC attempt remote identity changed")
            elif attempt_remote is not None:
                _positive_signed_64(attempt_remote, "attempt remote id")
        return attempt_id, state
    except (TypeError, ValueError):
        raise StateTransitionError("database returned malformed expired claim") from None


def _release_expired(cursor, row: Mapping[str, object], state: str | None, now: datetime):
    attempt_id = row.get("active_attempt_id")
    cursor.execute(
        """
        UPDATE feedback_odoo_sync s
        SET state = 'idle', claim_owner = NULL, claim_token = NULL,
            claim_expires_at = NULL, due_at = %s, updated_at = %s
        WHERE s.feedback_id = %s AND s.state = 'in_flight'
          AND s.desired_version = %s AND s.last_synced_version = %s
          AND s.odoo_improvement_id IS NOT DISTINCT FROM %s
          AND s.claim_owner = %s AND s.claim_token = %s
          AND s.claim_expires_at = %s
          AND (
              (%s::uuid IS NULL AND s.active_attempt_id IS NULL)
              OR s.active_attempt_id = %s
          )
          AND s.attempt_count = %s
          AND (
              %s::text IS NULL
              OR EXISTS (
                  SELECT 1 FROM feedback_odoo_attempts a
                  WHERE a.feedback_id = s.feedback_id
                    AND a.attempt_id = %s
                    AND a.projection_version = %s
                    AND a.state = %s
                    AND (
                        a.state <> 'rpc_succeeded'
                        OR a.remote_id = s.odoo_improvement_id
                    )
              )
          )
        RETURNING s.feedback_id
        """,
        (
            now,
            now,
            row["feedback_id"],
            row["desired_version"],
            row["last_synced_version"],
            row["odoo_improvement_id"],
            row["claim_owner"],
            row["claim_token"],
            row["claim_expires_at"],
            attempt_id,
            attempt_id,
            row["attempt_count"],
            state,
            attempt_id,
            row.get("projection_version"),
            state,
        ),
    )
    _one_row(cursor, "expired claim release")


def recover_expired_claims(now: datetime, limit: int = 10) -> int:
    """Recover a bounded locked set without ever retrying ambiguous dispatch."""
    current = _aware_datetime(now, "recovery time")
    if type(limit) is not int or not 1 <= limit <= 10:
        raise ValueError("recovery limit must be an integer from 1 through 10")
    with db.cursor() as cursor:
        cursor.execute(
            """
            SELECT s.feedback_id, s.desired_version, s.last_synced_version,
                   s.odoo_improvement_id, s.claim_owner, s.claim_token,
                   s.claim_expires_at, s.active_attempt_id, s.attempt_count,
                   a.attempt_id, a.projection_version, a.state AS attempt_state,
                   a.remote_id AS attempt_remote_id
            FROM feedback_odoo_sync s
            LEFT JOIN feedback_odoo_attempts a
              ON a.feedback_id = s.feedback_id
             AND a.attempt_id = s.active_attempt_id
            WHERE s.state = 'in_flight' AND s.claim_expires_at <= %s
            ORDER BY s.claim_expires_at, s.feedback_id
            FOR UPDATE OF s SKIP LOCKED
            LIMIT %s
            """,
            (current, limit),
        )
        expired = _rows(cursor)
        if len(expired) > limit:
            raise StateTransitionError("database exceeded bounded recovery limit")
        for row in expired:
            attempt_id, state = _validate_expired_row(row)
            if state in {None, "prepared", "rpc_succeeded"}:
                _release_expired(cursor, row, state, current)
                continue
            if state != "dispatch_marked":
                raise StateTransitionError("expired attempt state is not recoverable")
            cursor.execute(
                """
                UPDATE feedback_odoo_attempts
                SET state = 'ambiguous', outcome_detail = %s, updated_at = %s
                WHERE feedback_id = %s AND attempt_id = %s
                  AND projection_version = %s AND state = %s
                RETURNING attempt_id
                """,
                (
                    "ambiguous_stale_dispatch",
                    current,
                    row["feedback_id"],
                    attempt_id,
                    row["projection_version"],
                    "dispatch_marked",
                ),
            )
            _one_row(cursor, "expired dispatch attempt")
            cursor.execute(
                """
                UPDATE feedback_odoo_sync
                SET state = 'quarantined', claim_owner = NULL, claim_token = NULL,
                    claim_expires_at = NULL,
                    quarantine_reason = 'ambiguous_stale_dispatch',
                    quarantined_at = %s,
                    last_error_class = 'ambiguous_stale_dispatch',
                    last_error_summary = %s,
                    updated_at = %s
                WHERE feedback_id = %s AND state = 'in_flight'
                  AND desired_version = %s AND last_synced_version = %s
                  AND odoo_improvement_id IS NOT DISTINCT FROM %s
                  AND claim_owner = %s AND claim_token = %s
                  AND claim_expires_at = %s
                  AND active_attempt_id = %s AND attempt_count = %s
                  AND EXISTS (
                      SELECT 1 FROM feedback_odoo_attempts a
                      WHERE a.feedback_id = %s AND a.attempt_id = %s
                        AND a.projection_version = %s AND a.state = 'ambiguous'
                  )
                RETURNING feedback_id
                """,
                (
                    current,
                    _QUARANTINE_SUMMARIES["ambiguous_stale_dispatch"],
                    current,
                    row["feedback_id"],
                    row["desired_version"],
                    row["last_synced_version"],
                    row["odoo_improvement_id"],
                    row["claim_owner"],
                    row["claim_token"],
                    row["claim_expires_at"],
                    attempt_id,
                    row["attempt_count"],
                    row["feedback_id"],
                    attempt_id,
                    row["projection_version"],
                ),
            )
            _one_row(cursor, "expired dispatch quarantine")
    return len(expired)


def list_quarantined(*, limit: int = 100) -> tuple[QuarantineItem, ...]:
    """Return at most 100 ordered privacy-safe local quarantine rows."""
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("quarantine list limit must be an integer from 1 through 100")
    with db.cursor() as cursor:
        cursor.execute(
            """
            SELECT feedback_id, active_attempt_id AS attempt_id,
                   quarantine_reason, state AS sync_state,
                   quarantined_at, attempt_count
            FROM feedback_odoo_sync
            WHERE state = 'quarantined'
            ORDER BY quarantined_at, feedback_id
            LIMIT %s
            """,
            (limit,),
        )
        rows = _rows(cursor)
    if len(rows) > limit:
        raise StateTransitionError("database exceeded the quarantine list bound")
    items: list[QuarantineItem] = []
    previous: tuple[datetime, int] | None = None
    for row in rows:
        try:
            item = QuarantineItem(
                feedback_id=row.get("feedback_id"),
                attempt_id=row.get("attempt_id"),
                reason=row.get("quarantine_reason"),
                state=row.get("sync_state"),
                quarantined_at=row.get("quarantined_at"),
                attempt_count=row.get("attempt_count"),
            )
        except (TypeError, ValueError):
            raise StateTransitionError("database returned malformed quarantine state") from None
        order = (item.quarantined_at, item.feedback_id)
        if previous is not None and order <= previous:
            raise StateTransitionError("database returned unordered quarantine state")
        previous = order
        items.append(item)
    return tuple(items)


def _reviewer(value: object) -> str:
    if type(value) is not str:
        raise ValueError("reviewer is malformed")
    cleaned = value.strip()
    if (
        not cleaned
        or len(cleaned) > MAX_REVIEWER_LENGTH
        or not cleaned.isprintable()
        or any(character in cleaned for character in "\r\n\t")
    ):
        raise ValueError("reviewer is malformed")
    return cleaned


def _lock_quarantined_operator_attempt(cursor, attempt_id: UUID) -> dict[str, object]:
    cursor.execute(
        """
        SELECT s.feedback_id, s.desired_version, s.last_synced_version,
               s.odoo_improvement_id, s.due_at, s.attempt_count,
               s.state AS sync_state, s.claim_owner, s.claim_token,
               s.claim_expires_at, s.active_attempt_id,
               s.quarantine_reason, s.quarantined_at,
               a.attempt_id, a.feedback_id AS attempt_feedback_id,
               a.projection_version AS attempt_projection_version,
               a.state AS attempt_state, a.remote_id AS attempt_remote_id,
               f.projection_version AS feedback_projection_version,
               f.status AS feedback_status,
               f.lifecycle_origin AS feedback_lifecycle_origin
        FROM feedback_odoo_sync s
        JOIN feedback_odoo_attempts a
          ON a.feedback_id = s.feedback_id
         AND a.attempt_id = s.active_attempt_id
        JOIN feedback f ON f.id = s.feedback_id
        WHERE s.state = 'quarantined'
          AND s.active_attempt_id = %s
          AND a.attempt_id = %s
        FOR UPDATE OF f, s, a
        """,
        (attempt_id, attempt_id),
    )
    row = _one_row(cursor, "quarantine disposition lock")
    try:
        feedback_id = _positive_signed_64(row.get("feedback_id"), "feedback id")
        desired = _positive_signed_64(row.get("desired_version"), "desired version")
        last_synced = _nonnegative_signed_64(
            row.get("last_synced_version"),
            "last synchronized version",
        )
        if last_synced >= desired:
            raise ValueError("quarantined feedback is not due")
        remote_id = row.get("odoo_improvement_id")
        if remote_id is not None:
            _positive_signed_64(remote_id, "remote id")
        _aware_datetime(row.get("due_at"), "due time")
        count = _attempt_count(row.get("attempt_count"))
        if row.get("sync_state") != "quarantined":
            raise ValueError("sync row is not quarantined")
        if any(
            row.get(name) is not None for name in ("claim_owner", "claim_token", "claim_expires_at")
        ):
            raise ValueError("quarantined sync row retained claim ownership")
        active_attempt = _uuid(row.get("active_attempt_id"), "active attempt id")
        if active_attempt != attempt_id or row.get("attempt_id") != attempt_id:
            raise ValueError("active attempt association changed")
        if row.get("attempt_feedback_id") != feedback_id:
            raise ValueError("attempt belongs to another feedback row")
        reason = row.get("quarantine_reason")
        if type(reason) is not str or reason not in _QUARANTINE_REASONS:
            raise ValueError("quarantine reason is malformed")
        _aware_datetime(row.get("quarantined_at"), "quarantine time")
        attempt_version = _positive_signed_64(
            row.get("attempt_projection_version"),
            "attempt projection version",
        )
        if not last_synced < attempt_version <= desired:
            raise ValueError("attempt version is outside the due window")
        attempt_state = _attempt_state(row.get("attempt_state"))
        if attempt_state not in _OPERATOR_ATTEMPT_STATES:
            raise ValueError("attempt state is not operator-reviewable")
        attempt_remote_id = row.get("attempt_remote_id")
        if attempt_remote_id is not None:
            _positive_signed_64(attempt_remote_id, "attempt remote id")
        if attempt_remote_id != remote_id:
            raise ValueError("attempt remote association changed")
        feedback_version = _positive_signed_64(
            row.get("feedback_projection_version"),
            "feedback projection version",
        )
        if feedback_version != desired:
            raise ValueError("feedback projection and desired version differ")
        if row.get("feedback_status") not in {
            "requested",
            "in_progress",
            "completed",
            "declined",
        } or row.get("feedback_lifecycle_origin") not in {
            "local",
            "legacy_project_task",
        }:
            raise ValueError("feedback lifecycle authority is malformed")
    except (TypeError, ValueError):
        raise StateTransitionError("database returned malformed quarantine authority") from None
    return {
        "feedback_id": feedback_id,
        "desired_version": desired,
        "last_synced_version": last_synced,
        "odoo_improvement_id": remote_id,
        "due_at": row["due_at"],
        "attempt_count": count,
        "attempt_id": attempt_id,
        "attempt_state": attempt_state,
        "attempt_projection_version": attempt_version,
        "quarantine_reason": reason,
        "quarantined_at": row["quarantined_at"],
        "feedback_status": row["feedback_status"],
        "feedback_lifecycle_origin": row["feedback_lifecycle_origin"],
    }


def _append_operator_audit(
    cursor,
    *,
    attempt_id: UUID,
    action: str,
    reviewer: str,
    now: datetime,
) -> None:
    cursor.execute(
        """
        INSERT INTO feedback_odoo_operator_actions
            (attempt_id, action, reviewer, created_at)
        VALUES (%s, %s, %s, %s)
        RETURNING id, attempt_id, action
        """,
        (attempt_id, action, reviewer, now),
    )
    row = _one_row(cursor, "operator audit insert")
    try:
        _positive_signed_64(row.get("id"), "operator action id")
    except ValueError:
        raise StateTransitionError("operator audit returned malformed state") from None
    if row.get("attempt_id") != attempt_id or row.get("action") != action:
        raise StateTransitionError("operator audit returned different authority")


def _validate_disposition_result(
    row: Mapping[str, object],
    *,
    feedback_id: int,
    desired_version: int,
    last_synced_version: int,
    attempt_count: int,
    now: datetime,
) -> None:
    try:
        returned_feedback_id = _positive_signed_64(row.get("feedback_id"), "feedback id")
        returned_desired = _positive_signed_64(
            row.get("desired_version"),
            "desired version",
        )
        last_synced = _nonnegative_signed_64(
            row.get("last_synced_version"),
            "last synchronized version",
        )
        returned_attempt_count = _attempt_count(row.get("attempt_count"))
        due_at = _aware_datetime(row.get("due_at"), "due time")
    except (TypeError, ValueError):
        raise StateTransitionError("disposition returned malformed state") from None
    if (
        returned_feedback_id != feedback_id
        or returned_desired != desired_version
        or last_synced != last_synced_version
        or returned_attempt_count != attempt_count
        or row.get("state") != "idle"
        or row.get("active_attempt_id") is not None
        or due_at != now
    ):
        raise StateTransitionError("disposition returned different state")


def apply_quarantine_disposition(
    *,
    attempt_id: UUID,
    disposition: str,
    reviewer: str,
    human_review_confirmed: bool,
    now: datetime,
) -> QuarantineDispositionResult:
    """Apply one exact local operator disposition in a short transaction."""
    safe_attempt_id = _uuid(attempt_id, "attempt id")
    if type(disposition) is not str or disposition not in _DISPOSITIONS:
        raise ValueError("quarantine disposition is malformed")
    clean_reviewer = _reviewer(reviewer)
    if type(human_review_confirmed) is not bool:
        raise ValueError("human review confirmation must be a boolean")
    if disposition == "supersede-and-retry" and human_review_confirmed is not True:
        raise ValueError("supersede and retry requires human review confirmation")
    current = _aware_datetime(now, "operator action time")
    action = disposition.replace("-", "_")
    with db.cursor() as cursor:
        locked = _lock_quarantined_operator_attempt(cursor, safe_attempt_id)
        attempt_state = locked["attempt_state"]
        if disposition == "supersede-and-retry" and locked["desired_version"] >= MAX_SIGNED_64:
            raise StateTransitionError("feedback projection version cannot advance")
        if disposition == "release-definitive" and attempt_state not in {
            "prepared",
            "definitive_failed",
        }:
            raise StateTransitionError("attempt is not definitively releasable")
        if disposition == "supersede-and-retry" and attempt_state != "ambiguous":
            raise StateTransitionError("attempt is not an ambiguous review candidate")
        _append_operator_audit(
            cursor,
            attempt_id=safe_attempt_id,
            action=action,
            reviewer=clean_reviewer,
            now=current,
        )
        feedback_id = locked["feedback_id"]
        desired = locked["desired_version"]
        if disposition == "keep":
            return QuarantineDispositionResult(
                feedback_id=feedback_id,
                attempt_id=safe_attempt_id,
                disposition=disposition,
                state="quarantined",
                desired_version=desired,
                warning=None,
            )

        if disposition == "supersede-and-retry":
            next_version = desired + 1
            cursor.execute(
                """
                UPDATE feedback
                SET projection_version = projection_version + 1, updated_at = %s
                WHERE id = %s AND projection_version = %s
                  AND status = %s AND lifecycle_origin = %s
                RETURNING id, projection_version
                """,
                (
                    current,
                    feedback_id,
                    desired,
                    locked["feedback_status"],
                    locked["feedback_lifecycle_origin"],
                ),
            )
            feedback_row = _one_row(cursor, "feedback supersede update")
            if (
                feedback_row.get("id") != feedback_id
                or feedback_row.get("projection_version") != next_version
            ):
                raise StateTransitionError("feedback supersede returned different version")
            update_sync_sql = """
                UPDATE feedback_odoo_sync
                SET desired_version = desired_version + 1,
                    state = 'idle', claim_owner = NULL, claim_token = NULL,
                    claim_expires_at = NULL, active_attempt_id = NULL,
                    attempt_count = 0,
                    quarantine_reason = NULL, quarantined_at = NULL,
                    last_error_class = NULL, last_error_summary = NULL,
                    due_at = %s, updated_at = %s
                WHERE feedback_id = %s AND state = 'quarantined'
                  AND desired_version = %s AND last_synced_version = %s
                  AND odoo_improvement_id IS NOT DISTINCT FROM %s
                  AND due_at = %s AND attempt_count = %s
                  AND claim_owner IS NULL AND claim_token IS NULL
                  AND claim_expires_at IS NULL
                  AND active_attempt_id = %s
                  AND quarantine_reason = %s AND quarantined_at = %s
                  AND EXISTS (
                      SELECT 1 FROM feedback_odoo_attempts a
                      WHERE a.feedback_id = feedback_odoo_sync.feedback_id
                        AND a.attempt_id = %s
                        AND a.projection_version = %s
                        AND a.state = %s
                  )
                RETURNING feedback_id, desired_version, last_synced_version,
                          state, active_attempt_id, due_at, attempt_count
            """
        else:
            next_version = desired
            update_sync_sql = """
                UPDATE feedback_odoo_sync
                SET state = 'idle', claim_owner = NULL, claim_token = NULL,
                    claim_expires_at = NULL, active_attempt_id = NULL,
                    attempt_count = 0,
                    quarantine_reason = NULL, quarantined_at = NULL,
                    last_error_class = NULL, last_error_summary = NULL,
                    due_at = %s, updated_at = %s
                WHERE feedback_id = %s AND state = 'quarantined'
                  AND desired_version = %s AND last_synced_version = %s
                  AND odoo_improvement_id IS NOT DISTINCT FROM %s
                  AND due_at = %s AND attempt_count = %s
                  AND claim_owner IS NULL AND claim_token IS NULL
                  AND claim_expires_at IS NULL
                  AND active_attempt_id = %s
                  AND quarantine_reason = %s AND quarantined_at = %s
                  AND EXISTS (
                      SELECT 1 FROM feedback_odoo_attempts a
                      WHERE a.feedback_id = feedback_odoo_sync.feedback_id
                        AND a.attempt_id = %s
                        AND a.projection_version = %s
                        AND a.state = %s
                  )
                RETURNING feedback_id, desired_version, last_synced_version,
                          state, active_attempt_id, due_at, attempt_count
            """

        cursor.execute(
            update_sync_sql,
            (
                current,
                current,
                feedback_id,
                desired,
                locked["last_synced_version"],
                locked["odoo_improvement_id"],
                locked["due_at"],
                locked["attempt_count"],
                safe_attempt_id,
                locked["quarantine_reason"],
                locked["quarantined_at"],
                safe_attempt_id,
                locked["attempt_projection_version"],
                attempt_state,
            ),
        )
        updated = _one_row(cursor, "quarantine disposition update")
        _validate_disposition_result(
            updated,
            feedback_id=feedback_id,
            desired_version=next_version,
            last_synced_version=locked["last_synced_version"],
            attempt_count=0,
            now=current,
        )
    return QuarantineDispositionResult(
        feedback_id=feedback_id,
        attempt_id=safe_attempt_id,
        disposition=disposition,
        state="idle",
        desired_version=next_version,
        warning=(DUPLICATE_RISK_WARNING if disposition == "supersede-and-retry" else None),
    )


def load_verified_canary_evidence(feedback_id: int) -> VerifiedCanaryEvidence:
    """Load one exact synchronized version and its immutable verified attempt."""
    safe_feedback_id = _positive_signed_64(feedback_id, "feedback id")
    with db.cursor() as cursor:
        cursor.execute(
            """
            SELECT a.attempt_id, a.feedback_id, a.projection_version,
                   a.mutation_kind, a.remote_id, a.manifest, a.manifest_digest,
                   a.before_sha256, a.before_byte_length,
                   a.after_sha256, a.after_byte_length, a.state,
                   a.dispatch_marked_at, a.rpc_succeeded_at, a.readback_at,
                   a.settled_at, a.outcome_detail, a.created_at, a.updated_at,
                   s.desired_version AS sync_desired_version,
                   s.last_synced_version AS sync_last_synced_version,
                   s.odoo_improvement_id AS sync_remote_id,
                   s.state AS sync_state,
                   s.claim_owner AS sync_claim_owner,
                   s.claim_token AS sync_claim_token,
                   s.claim_expires_at AS sync_claim_expires_at,
                   s.active_attempt_id AS sync_active_attempt_id,
                   s.attempt_count AS sync_attempt_count,
                   s.quarantine_reason AS sync_quarantine_reason,
                   s.quarantined_at AS sync_quarantined_at,
                   s.last_error_class AS sync_last_error_class,
                   s.last_error_summary AS sync_last_error_summary,
                   f.projection_version AS feedback_projection_version
            FROM feedback_odoo_sync s
            JOIN feedback f ON f.id = s.feedback_id
            JOIN feedback_odoo_attempts a
              ON a.feedback_id = s.feedback_id
             AND a.projection_version = s.last_synced_version
             AND a.remote_id = s.odoo_improvement_id
             AND a.state = 'verified'
            WHERE s.feedback_id = %s
              AND s.state = 'idle'
              AND s.claim_owner IS NULL
              AND s.claim_token IS NULL
              AND s.claim_expires_at IS NULL
              AND s.active_attempt_id IS NULL
              AND s.attempt_count = 0
              AND s.quarantine_reason IS NULL
              AND s.quarantined_at IS NULL
              AND s.last_error_class IS NULL
              AND s.last_error_summary IS NULL
              AND s.desired_version = s.last_synced_version
              AND f.projection_version = s.last_synced_version
              AND a.dispatch_marked_at IS NOT NULL
              AND a.rpc_succeeded_at IS NOT NULL
              AND a.readback_at IS NOT NULL
              AND a.settled_at IS NOT NULL
              AND a.dispatch_marked_at <= a.rpc_succeeded_at
              AND a.rpc_succeeded_at <= a.readback_at
              AND a.readback_at = a.settled_at
            ORDER BY a.settled_at DESC, a.attempt_id
            LIMIT 2
            """,
            (safe_feedback_id,),
        )
        rows = _rows(cursor)
        if len(rows) != 1:
            raise StateTransitionError("verified canary evidence is not exact")
        row = rows[0]
        attempt = _attempt_from_row(row)
        try:
            desired = _positive_signed_64(
                row.get("sync_desired_version"),
                "desired version",
            )
            synchronized = _positive_signed_64(
                row.get("sync_last_synced_version"),
                "last synchronized version",
            )
            remote_id = _positive_signed_64(row.get("sync_remote_id"), "remote id")
            feedback_version = _positive_signed_64(
                row.get("feedback_projection_version"),
                "feedback projection version",
            )
            count = _attempt_count(row.get("sync_attempt_count"))
        except (TypeError, ValueError):
            raise StateTransitionError("verified canary evidence is malformed") from None
        if (
            attempt.feedback_id != safe_feedback_id
            or attempt.state != "verified"
            or not desired == synchronized == feedback_version == attempt.projection_version
            or attempt.remote_id != remote_id
            or row.get("sync_state") != "idle"
            or count != 0
            or any(
                row.get(name) is not None
                for name in (
                    "sync_claim_owner",
                    "sync_claim_token",
                    "sync_claim_expires_at",
                    "sync_active_attempt_id",
                    "sync_quarantine_reason",
                    "sync_quarantined_at",
                    "sync_last_error_class",
                    "sync_last_error_summary",
                )
            )
        ):
            raise StateTransitionError("verified canary evidence changed")
        try:
            return VerifiedCanaryEvidence(
                feedback_id=safe_feedback_id,
                projection_version=synchronized,
                remote_id=remote_id,
                attempt=attempt,
            )
        except (TypeError, ValueError):
            raise StateTransitionError("verified canary evidence is malformed") from None


__all__ = [
    "Attempt",
    "Claim",
    "DUPLICATE_RISK_WARNING",
    "MAX_MUTATION_ATTEMPTS",
    "QuarantineDispositionResult",
    "QuarantineItem",
    "RETRY_DELAYS",
    "StateTransitionError",
    "VerifiedCanaryEvidence",
    "apply_quarantine_disposition",
    "claim_due",
    "defer_unprepared_read_failure",
    "defer_prepared_for_closed_gate",
    "load_active_attempt",
    "load_verified_canary_evidence",
    "list_quarantined",
    "mark_dispatch",
    "mark_rpc_succeeded",
    "prepare_attempt",
    "quarantine",
    "record_definitive_failure",
    "release_stale_unprepared_claim",
    "recover_expired_claims",
    "retry_due",
    "schedule_readback",
    "settle_verified",
]
