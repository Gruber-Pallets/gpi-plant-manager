"""Sequential, fail-closed worker for the local-feedback Odoo mirror."""

from __future__ import annotations

import os
import re
import socket
import xmlrpc.client
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import uuid4

from . import feedback_store
from . import feedback_sync_store as sync_store
from .feedback_projection import (
    BinaryEvidence,
    Projection,
    ReadbackMismatch,
    build_projection_from_snapshot,
    verify_readback,
)
from .odoo_improvements import (
    ContractError,
    GateClosed,
    ImprovementContract,
    ImprovementsClient,
    MalformedMutationResponse,
    TargetIdentityError,
)


_MAX_SIGNED_64 = 9_223_372_036_854_775_807
_DATE_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", re.ASCII)
_DATETIME_RE = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2} [0-9]{2}:[0-9]{2}:[0-9]{2}",
    re.ASCII,
)
_READ_TRANSPORT_ERRORS = (TimeoutError, ConnectionError, OSError, xmlrpc.client.Fault)
_AMBIGUOUS_MUTATION_ERRORS = (
    TimeoutError,
    ConnectionError,
    OSError,
    MalformedMutationResponse,
)
_BATCH_OUTCOMES = frozenset(
    {"verified", "deferred", "retry_scheduled", "quarantined", "isolated_error"}
)


class _SavedBinaryEvidenceChanged(RuntimeError):
    """The current local bytes no longer prove the saved attempt evidence."""


class _SavedProjectionUnavailable(RuntimeError):
    """Saved nonbinary projection evidence is no longer safe to dispatch."""


@dataclass(frozen=True)
class BatchResult:
    attempted: int = 0
    verified: int = 0
    deferred: int = 0
    retry_scheduled: int = 0
    quarantined: int = 0
    isolated_errors: int = 0
    skipped: str | None = None

    @classmethod
    def from_outcomes(cls, outcomes: list[str]) -> BatchResult:
        if type(outcomes) is not list or any(
            type(outcome) is not str or outcome not in _BATCH_OUTCOMES for outcome in outcomes
        ):
            raise ValueError("batch outcomes are malformed")
        return cls(
            attempted=len(outcomes),
            verified=outcomes.count("verified"),
            deferred=outcomes.count("deferred"),
            retry_scheduled=outcomes.count("retry_scheduled"),
            quarantined=outcomes.count("quarantined"),
            isolated_errors=outcomes.count("isolated_error"),
        )


def _positive_remote_id(value: object, *, mutation_acknowledgement: bool = False) -> int:
    if type(value) is not int or not 0 < value <= _MAX_SIGNED_64:
        if mutation_acknowledgement:
            raise MalformedMutationResponse("create response was not a positive integer")
        raise ContractError("remote id must be a positive integer")
    return value


def _quarantine(
    claim: sync_store.Claim,
    reason: str,
    now: datetime,
    *,
    attempt: sync_store.Attempt | None = None,
) -> str:
    if attempt is None:
        sync_store.quarantine(claim, reason, now)
    else:
        sync_store.quarantine(claim, reason, now, attempt=attempt)
    return "quarantined"


def _projection_from_attempt(attempt: sync_store.Attempt) -> Projection:
    manifest = attempt.manifest
    fields = manifest["fields"]
    saved_evidence = attempt.binaries
    binaries: dict[str, BinaryEvidence] = {}
    if saved_evidence:
        try:
            images = feedback_store.attempt_image_snapshot(
                attempt.feedback_id,
                saved_evidence,
            )
        except feedback_store.ProjectionSnapshotUnavailable:
            raise _SavedBinaryEvidenceChanged from None
        if set(images) != set(saved_evidence):
            raise _SavedBinaryEvidenceChanged
        for field_name, evidence in saved_evidence.items():
            image = images.get(field_name)
            if (
                image is None
                or image.sha256 != evidence["sha256"]
                or image.byte_length != evidence["byte_length"]
                or len(image.jpeg_bytes) != evidence["byte_length"]
            ):
                raise _SavedBinaryEvidenceChanged
            binaries[field_name] = BinaryEvidence(
                jpeg_bytes=bytes(image.jpeg_bytes),
                sha256=image.sha256,
                byte_length=image.byte_length,
            )
    try:
        return Projection(
            source_id=fields["x_studio_source_id"],
            fields=fields,
            binaries=binaries,
            manifest=manifest,
            manifest_digest=attempt.manifest_digest,
        )
    except (KeyError, TypeError, ValueError):
        raise _SavedProjectionUnavailable from None


def _valid_saved_time(value: object, field_type: str) -> bool:
    if type(value) is not str:
        return False
    pattern = _DATE_RE if field_type == "date" else _DATETIME_RE
    if pattern.fullmatch(value) is None:
        return False
    try:
        datetime.strptime(
            value,
            "%Y-%m-%d" if field_type == "date" else "%Y-%m-%d %H:%M:%S",
        )
    except ValueError:
        return False
    return True


def _saved_dates_match_contract(projection: Projection, contract: ImprovementContract) -> bool:
    fields = projection.fields
    if not _valid_saved_time(fields.get("x_studio_date_start"), contract.start_type):
        return False
    if "x_studio_date_stop" in fields and not _valid_saved_time(
        fields["x_studio_date_stop"], contract.stop_type
    ):
        return False
    return True


def _read_contract_for_prepared(
    claim: sync_store.Claim,
    attempt: sync_store.Attempt,
    *,
    client: ImprovementsClient,
    now: datetime,
) -> ImprovementContract | str:
    try:
        return client.read_contract()
    except xmlrpc.client.Fault:
        return sync_store.record_definitive_failure(
            claim,
            attempt,
            "odoo_fault",
            now,
        )
    except (TimeoutError, ConnectionError, OSError):
        return sync_store.record_definitive_failure(
            claim,
            attempt,
            "identity_read_failed",
            now,
        )
    except (TargetIdentityError, ContractError):
        return _quarantine(
            claim,
            "target_identity_or_contract_mismatch",
            now,
            attempt=attempt,
        )


def _revalidate_prepared_identity(
    claim: sync_store.Claim,
    attempt: sync_store.Attempt,
    projection: Projection,
    *,
    client: ImprovementsClient,
    now: datetime,
) -> str | None:
    try:
        rows = client.find_exact(projection.source_id)
    except xmlrpc.client.Fault:
        return sync_store.record_definitive_failure(
            claim,
            attempt,
            "odoo_fault",
            now,
        )
    except (TimeoutError, ConnectionError, OSError):
        return sync_store.record_definitive_failure(
            claim,
            attempt,
            "identity_read_failed",
            now,
        )
    except (TargetIdentityError, ContractError):
        return _quarantine(
            claim,
            "target_identity_or_contract_mismatch",
            now,
            attempt=attempt,
        )

    if type(rows) is not list or len(rows) > 3:
        return _quarantine(
            claim,
            "target_identity_or_contract_mismatch",
            now,
            attempt=attempt,
        )
    if len(rows) > 1:
        return _quarantine(
            claim,
            "duplicate_compound_identity",
            now,
            attempt=attempt,
        )
    if not rows:
        if attempt.mutation_kind == "create":
            return None
        return _quarantine(
            claim,
            "compound_identity_changed_before_dispatch",
            now,
            attempt=attempt,
        )

    row = rows[0]
    if not isinstance(row, Mapping):
        return _quarantine(
            claim,
            "target_identity_or_contract_mismatch",
            now,
            attempt=attempt,
        )
    try:
        found_id = _positive_remote_id(row.get("id"))
    except ContractError:
        return _quarantine(
            claim,
            "target_identity_or_contract_mismatch",
            now,
            attempt=attempt,
        )
    if attempt.mutation_kind == "create" or found_id != attempt.remote_id:
        return _quarantine(
            claim,
            "compound_identity_changed_before_dispatch",
            now,
            attempt=attempt,
        )
    return None


def _verify_rpc_succeeded(
    claim: sync_store.Claim,
    attempt: sync_store.Attempt,
    projection: Projection,
    remote_id: int,
    *,
    client: ImprovementsClient,
    now: datetime,
) -> str:
    safe_remote_id = _positive_remote_id(remote_id)
    fields = sorted(projection.dispatch_fields())
    try:
        remote = client.read_improvement(
            safe_remote_id,
            fields,
            full_binary=True,
        )
    except ContractError:
        return _quarantine(claim, "readback_mismatch", now, attempt=attempt)
    except _READ_TRANSPORT_ERRORS:
        sync_store.schedule_readback(claim, attempt, now)
        return "deferred"

    try:
        verify_readback(projection, remote)
    except ReadbackMismatch:
        return _quarantine(claim, "readback_mismatch", now, attempt=attempt)
    sync_store.settle_verified(claim, attempt, safe_remote_id, now)
    return "verified"


def _dispatch_prepared_once(
    claim: sync_store.Claim,
    attempt: sync_store.Attempt,
    *,
    client: ImprovementsClient,
    now: datetime,
    projection: Projection | None = None,
    contract: ImprovementContract | None = None,
) -> str:
    if projection is None:
        try:
            projection = _projection_from_attempt(attempt)
        except _SavedBinaryEvidenceChanged:
            return _quarantine(
                claim,
                "local_binary_evidence_changed",
                now,
                attempt=attempt,
            )
        except _SavedProjectionUnavailable:
            return _quarantine(
                claim,
                "local_projection_unavailable",
                now,
                attempt=attempt,
            )
    if contract is None:
        contract_or_outcome = _read_contract_for_prepared(
            claim,
            attempt,
            client=client,
            now=now,
        )
        if type(contract_or_outcome) is str:
            return contract_or_outcome
        contract = contract_or_outcome
        if not _saved_dates_match_contract(projection, contract):
            return _quarantine(
                claim,
                "contract_changed_before_dispatch",
                now,
                attempt=attempt,
            )

    try:
        client.assert_mutation_allowed(claim.feedback_id)
    except GateClosed:
        sync_store.defer_prepared_for_closed_gate(claim, attempt, now)
        return "deferred"

    identity_outcome = _revalidate_prepared_identity(
        claim,
        attempt,
        projection,
        client=client,
        now=now,
    )
    if identity_outcome is not None:
        return identity_outcome
    dispatched = sync_store.mark_dispatch(claim, attempt, now)
    payload = projection.dispatch_fields()
    try:
        if attempt.mutation_kind == "create":
            remote_id = client.create_improvement(
                payload,
                feedback_id=claim.feedback_id,
                expected_contract=contract,
            )
            remote_id = _positive_remote_id(remote_id, mutation_acknowledgement=True)
        else:
            remote_id = _positive_remote_id(attempt.remote_id)
            client.write_improvement(
                remote_id,
                payload,
                feedback_id=claim.feedback_id,
                expected_contract=contract,
            )
    except GateClosed:
        return sync_store.record_definitive_failure(
            claim,
            dispatched,
            "gate_closed_before_rpc",
            now,
        )
    except xmlrpc.client.Fault:
        return sync_store.record_definitive_failure(
            claim,
            dispatched,
            "odoo_fault",
            now,
        )
    except (TargetIdentityError, ContractError):
        return _quarantine(
            claim,
            "target_identity_or_contract_mismatch",
            now,
            attempt=dispatched,
        )
    except _AMBIGUOUS_MUTATION_ERRORS:
        return _quarantine(
            claim,
            "ambiguous_mutation",
            now,
            attempt=dispatched,
        )

    try:
        succeeded = sync_store.mark_rpc_succeeded(
            claim,
            dispatched,
            remote_id,
            now,
        )
    except Exception:
        try:
            sync_store.quarantine(
                claim,
                "ambiguous_mutation",
                now,
                attempt=dispatched,
            )
        except Exception:
            pass
        raise
    return _verify_rpc_succeeded(
        claim,
        succeeded,
        projection,
        remote_id,
        client=client,
        now=now,
    )


def _recover_active(
    claim: sync_store.Claim,
    attempt: sync_store.Attempt,
    *,
    client: ImprovementsClient,
    now: datetime,
) -> str:
    if attempt.state == "prepared":
        return _dispatch_prepared_once(claim, attempt, client=client, now=now)
    if attempt.state == "dispatch_marked":
        return _quarantine(
            claim,
            "ambiguous_stale_dispatch",
            now,
            attempt=attempt,
        )
    if attempt.state == "rpc_succeeded":
        try:
            projection = _projection_from_attempt(attempt)
        except _SavedBinaryEvidenceChanged:
            return _quarantine(
                claim,
                "local_binary_evidence_changed",
                now,
                attempt=attempt,
            )
        except _SavedProjectionUnavailable:
            return _quarantine(
                claim,
                "local_projection_unavailable",
                now,
                attempt=attempt,
            )
        return _verify_rpc_succeeded(
            claim,
            attempt,
            projection,
            _positive_remote_id(attempt.remote_id),
            client=client,
            now=now,
        )
    raise RuntimeError("active attempt state is not recoverable")


def process_claim(
    claim: sync_store.Claim,
    *,
    client: ImprovementsClient,
    now: datetime,
) -> str:
    active = sync_store.load_active_attempt(claim)
    if active is not None:
        return _recover_active(claim, active, client=client, now=now)

    try:
        snapshot = feedback_store.projection_snapshot(
            claim.feedback_id,
            claim.desired_version,
        )
    except feedback_store.ProjectionSnapshotUnavailable:
        if sync_store.release_stale_unprepared_claim(claim, now):
            return "deferred"
        return _quarantine(claim, "local_projection_unavailable", now)

    try:
        contract = client.read_contract()
        projection = build_projection_from_snapshot(
            snapshot,
            client=client,
            contract=contract,
        )
        rows = client.find_exact(projection.source_id)
    except xmlrpc.client.Fault:
        sync_store.defer_unprepared_read_failure(claim, "odoo_fault", now)
        return "deferred"
    except (TimeoutError, ConnectionError, OSError):
        sync_store.defer_unprepared_read_failure(claim, "identity_read_failed", now)
        return "deferred"
    except (TargetIdentityError, ContractError):
        return _quarantine(
            claim,
            "target_identity_or_contract_mismatch",
            now,
        )
    except ValueError:
        return _quarantine(claim, "local_projection_unavailable", now)

    if type(rows) is not list or len(rows) > 3:
        return _quarantine(
            claim,
            "target_identity_or_contract_mismatch",
            now,
        )
    if len(rows) > 1:
        return _quarantine(claim, "duplicate_compound_identity", now)

    if rows:
        try:
            found_id = _positive_remote_id(rows[0].get("id"))
        except (AttributeError, ContractError):
            return _quarantine(
                claim,
                "target_identity_or_contract_mismatch",
                now,
            )
    else:
        found_id = None

    if claim.odoo_improvement_id is not None:
        if found_id != claim.odoo_improvement_id:
            return _quarantine(claim, "saved_id_ownership_conflict", now)
        remote_id = claim.odoo_improvement_id
        mutation_kind = "update"
    elif found_id is not None:
        remote_id = found_id
        mutation_kind = "update"
    else:
        remote_id = None
        mutation_kind = "create"

    try:
        prepared = sync_store.prepare_attempt(
            claim=claim,
            attempt_id=uuid4(),
            mutation_kind=mutation_kind,
            remote_id=remote_id,
            manifest=projection.manifest,
            manifest_digest=projection.manifest_digest,
            binaries=projection.binaries,
            now=now,
        )
    except sync_store.StateTransitionError:
        if sync_store.release_stale_unprepared_claim(claim, now):
            return "deferred"
        raise
    return _dispatch_prepared_once(
        claim,
        prepared,
        client=client,
        now=now,
        projection=projection,
        contract=contract,
    )


def worker_write_enabled() -> bool:
    return (
        os.environ.get("ODOO_SHARED_REPORTING_WRITE_ENABLED") == "true"
        and os.environ.get("ODOO_IMPROVEMENTS_WRITE_ENABLED") == "true"
    )


def run_batch(
    now: datetime | None = None,
    worker_id: str | None = None,
    limit: int = 10,
) -> BatchResult:
    if not worker_write_enabled():
        return BatchResult(skipped="write_gates_closed")
    current = now or datetime.now(UTC)
    identity = worker_id or f"{socket.gethostname()}:{os.getpid()}"
    if type(limit) is not int:
        raise ValueError("batch limit must be an integer")
    capped = max(1, min(limit, 10))
    client = ImprovementsClient.from_env()
    client.assert_worker_enabled()
    canary = client.canary_feedback_id()
    sync_store.recover_expired_claims(current)
    claims = sync_store.claim_due(
        now=current,
        worker_id=identity,
        limit=capped,
        canary_feedback_id=canary,
    )
    outcomes: list[str] = []
    for item in claims:
        try:
            outcomes.append(process_claim(item, client=client, now=current))
        except Exception:
            outcomes.append("isolated_error")
    return BatchResult.from_outcomes(outcomes)


__all__ = ["BatchResult", "process_claim", "run_batch", "worker_write_enabled"]
