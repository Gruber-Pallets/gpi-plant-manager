import base64
import hashlib
import json
import xmlrpc.client
from contextlib import AbstractContextManager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import UUID

import pytest

from zira_dashboard import feedback_store
from zira_dashboard import feedback_sync
from zira_dashboard import feedback_sync_store as sync_store
from zira_dashboard.feedback_image import MAX_OUTPUT_BYTES, NormalizedImage
from zira_dashboard.feedback_projection import BinaryEvidence, Projection
from zira_dashboard.feedback_sync import BatchResult, _recover_active, process_claim, run_batch
from zira_dashboard.odoo_improvements import (
    ContractError,
    GateClosed,
    ImprovementContract,
    ImprovementsClient,
    ImprovementsConfig,
    MalformedMutationResponse,
    TargetIdentityError,
)


SERVICE_ENV_NAMES = (
    "DATABASE_URL",
    "FEEDBACK_SYNC_TEST_DATABASE",
    "ODOO_URL",
    "ODOO_DB",
    "ODOO_LOGIN",
    "ODOO_API_KEY",
    "ODOO_IMPROVEMENTS_URL",
    "ODOO_IMPROVEMENTS_DB",
    "ODOO_IMPROVEMENTS_LOGIN",
    "ODOO_IMPROVEMENTS_API_KEY",
    "ODOO_IMPROVEMENTS_EXPECTED_DATABASE_UUID",
    "ODOO_IMPROVEMENTS_EXPECTED_COMPANY",
    "ODOO_IMPROVEMENTS_CANARY_FEEDBACK_ID",
)


@pytest.fixture(autouse=True)
def disable_real_services(monkeypatch):
    for name in SERVICE_ENV_NAMES:
        monkeypatch.setenv(name, "")
    monkeypatch.setenv("ODOO_SHARED_REPORTING_WRITE_ENABLED", "false")
    monkeypatch.setenv("ODOO_IMPROVEMENTS_WRITE_ENABLED", "false")
    monkeypatch.setenv("ZIRA_API_KEY", "test-dummy-zira-key")
    monkeypatch.setenv("ZIRA_BASE_URL", "http://127.0.0.1:9")


def aware_now():
    return datetime(2026, 8, 20, 18, 0, tzinfo=UTC)


def set_or_delete(monkeypatch, name, value):
    if value is None:
        monkeypatch.delenv(name, raising=False)
    else:
        monkeypatch.setenv(name, value)


def claim(
    remote_id=None,
    version=1,
    *,
    active_attempt_id=None,
    last_synced_version=0,
    attempt_count=0,
):
    return sync_store.Claim(
        feedback_id=17,
        desired_version=version,
        last_synced_version=last_synced_version,
        odoo_improvement_id=remote_id,
        claim_owner="worker-a",
        claim_token=UUID("11111111-1111-1111-1111-111111111111"),
        claim_expires_at=aware_now() + timedelta(minutes=5),
        active_attempt_id=active_attempt_id,
        attempt_count=attempt_count,
    )


def projection(
    *,
    raw: bytes | None = None,
    start_value: str = "2026-08-20",
    include_optionals: bool = False,
) -> Projection:
    fields = {
        "x_name": "Safe",
        "x_studio_source_id": "GPI-PM-FB-17",
        "x_studio_date_start": start_value,
        "x_studio_type": "Digital",
        "x_studio_status": "Requested",
        "x_studio_source": "GPI Plant Manager",
    }
    if include_optionals:
        fields.update(
            {
                "x_studio_submitted_by": 42,
                "x_studio_date_stop": "2026-08-21",
                "x_studio_completed_by": 43,
                "x_studio_notes": "<p>Done</p>",
            }
        )
    binaries = {}
    if raw is not None:
        binaries["x_studio_image"] = BinaryEvidence(
            jpeg_bytes=raw,
            sha256=hashlib.sha256(raw).hexdigest(),
            byte_length=len(raw),
        )
    manifest = {
        "fields": fields,
        "binary_evidence": {
            field_name: {
                "sha256": evidence.sha256,
                "byte_length": evidence.byte_length,
            }
            for field_name, evidence in binaries.items()
        },
    }
    encoded = json.dumps(
        manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode()
    return Projection(
        source_id="GPI-PM-FB-17",
        fields=fields,
        binaries=binaries,
        manifest=manifest,
        manifest_digest=hashlib.sha256(encoded).hexdigest(),
    )


def attempt(
    state: str,
    *,
    selected: Projection | None = None,
    mutation_kind: str = "create",
    remote_id: int | None = None,
    projection_version: int = 1,
) -> sync_store.Attempt:
    value = selected or projection()
    if state == "rpc_succeeded" and remote_id is None:
        remote_id = 901
    if mutation_kind == "update" and remote_id is None:
        remote_id = 901
    return sync_store.Attempt(
        attempt_id=UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"),
        feedback_id=17,
        projection_version=projection_version,
        mutation_kind=mutation_kind,
        remote_id=remote_id,
        manifest=value.manifest,
        manifest_digest=value.manifest_digest,
        binaries=value.binaries,
        state=state,
    )


def active_claim(saved: sync_store.Attempt, *, desired_version=None):
    return claim(
        remote_id=saved.remote_id if saved.state == "rpc_succeeded" else None,
        version=desired_version or saved.projection_version,
        active_attempt_id=saved.attempt_id,
    )


def normalized(raw: bytes) -> NormalizedImage:
    return NormalizedImage(
        jpeg_bytes=raw,
        sha256=hashlib.sha256(raw).hexdigest(),
        byte_length=len(raw),
        width=8,
        height=8,
    )


class FakeClient:
    def __init__(
        self,
        *,
        exact_rows=None,
        contract=None,
        create_result=901,
        create_error=None,
        write_error=None,
        gate_error=None,
        read_error=None,
        remote_matches=True,
    ):
        self.contract = contract or ImprovementContract(start_type="date", stop_type="date")
        self.exact_rows = [] if exact_rows is None else exact_rows
        self.create_result = create_result
        self.create_error = create_error
        self.write_error = write_error
        self.gate_error = gate_error
        self.read_error = read_error
        self.remote_matches = remote_matches
        self.calls = []
        self.mutation_calls = []
        self.create_fields = {}
        self.write_fields = {}
        self.write_id = None
        self.expected_contracts = []
        self.read_fields = []
        self.read_full_binary = []
        self.read_count = 0
        self.remote_values = None
        self.worker_enabled_calls = 0
        self.canary = None

    def read_contract(self):
        return self.contract

    def find_exact(self, source_id):
        self.calls.append(("find_exact", source_id))
        return self.exact_rows

    def assert_mutation_allowed(self, feedback_id):
        self.calls.append(("assert_mutation_allowed", feedback_id))
        if self.gate_error is not None:
            raise self.gate_error

    def assert_worker_enabled(self):
        self.worker_enabled_calls += 1

    def canary_feedback_id(self):
        return self.canary

    def create_improvement(self, fields, *, feedback_id, expected_contract):
        self.calls.append(("create_improvement", feedback_id))
        if isinstance(self.create_error, GateClosed):
            raise self.create_error
        self.mutation_calls.append("create")
        self.create_fields = dict(fields)
        self.expected_contracts.append(expected_contract)
        if self.create_error is not None:
            raise self.create_error
        return self.create_result

    def write_improvement(self, remote_id, fields, *, feedback_id, expected_contract):
        self.calls.append(("write_improvement", feedback_id, remote_id))
        if isinstance(self.write_error, GateClosed):
            raise self.write_error
        self.mutation_calls.append("write")
        self.write_id = remote_id
        self.write_fields = dict(fields)
        self.expected_contracts.append(expected_contract)
        if self.write_error is not None:
            raise self.write_error

    def read_improvement(self, remote_id, fields, *, full_binary):
        self.calls.append(("read_improvement", remote_id))
        self.read_count += 1
        self.read_fields = list(fields)
        self.read_full_binary.append(full_binary)
        if self.read_error is not None:
            raise self.read_error
        values = self.remote_values
        if values is None:
            values = self.create_fields or self.write_fields or projection().dispatch_fields()
        remote = {"id": remote_id, **values}
        for field_name in {"x_studio_submitted_by", "x_studio_completed_by"} & remote.keys():
            remote[field_name] = [remote[field_name], "Safe Employee"]
        if not self.remote_matches:
            remote.pop("x_name", None)
        return remote


@pytest.fixture
def flow(monkeypatch):
    selected = projection()
    prepared = attempt("prepared", selected=selected)
    dispatched = attempt("dispatch_marked", selected=selected)
    succeeded = attempt("rpc_succeeded", selected=selected, remote_id=901)
    snapshot = object()

    load_active = MagicMock(return_value=None)
    snapshot_read = MagicMock(return_value=snapshot)
    build = MagicMock(return_value=selected)
    prepare = MagicMock(return_value=prepared)
    gate_defer = MagicMock(return_value=True)
    mark_dispatch = MagicMock(return_value=dispatched)
    mark_rpc = MagicMock(return_value=succeeded)
    settle = MagicMock(return_value=True)
    schedule = MagicMock(return_value=True)
    definitive = MagicMock(return_value="retry_scheduled")
    quarantine = MagicMock(return_value=True)
    release_stale = MagicMock(return_value=False)
    image_snapshot = MagicMock(return_value={})

    monkeypatch.setattr(sync_store, "load_active_attempt", load_active)
    monkeypatch.setattr(feedback_store, "projection_snapshot", snapshot_read)
    monkeypatch.setattr(feedback_sync, "build_projection_from_snapshot", build)
    monkeypatch.setattr(sync_store, "prepare_attempt", prepare)
    monkeypatch.setattr(sync_store, "defer_prepared_for_closed_gate", gate_defer)
    monkeypatch.setattr(sync_store, "mark_dispatch", mark_dispatch)
    monkeypatch.setattr(sync_store, "mark_rpc_succeeded", mark_rpc)
    monkeypatch.setattr(sync_store, "settle_verified", settle)
    monkeypatch.setattr(sync_store, "schedule_readback", schedule)
    monkeypatch.setattr(sync_store, "record_definitive_failure", definitive)
    monkeypatch.setattr(sync_store, "quarantine", quarantine)
    monkeypatch.setattr(sync_store, "release_stale_unprepared_claim", release_stale)
    monkeypatch.setattr(feedback_store, "attempt_image_snapshot", image_snapshot)
    return SimpleNamespace(
        selected=selected,
        prepared=prepared,
        dispatched=dispatched,
        succeeded=succeeded,
        snapshot=snapshot,
        load_active=load_active,
        snapshot_read=snapshot_read,
        build=build,
        prepare=prepare,
        gate_defer=gate_defer,
        mark_dispatch=mark_dispatch,
        mark_rpc=mark_rpc,
        settle=settle,
        schedule=schedule,
        definitive=definitive,
        quarantine=quarantine,
        release_stale=release_stale,
        image_snapshot=image_snapshot,
    )


def test_zero_match_creates_then_reads_back_every_written_field(monkeypatch):
    selected = projection(raw=b"safe-image")
    prepared = sync_store.Attempt(
        attempt_id=UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"),
        feedback_id=17,
        projection_version=1,
        mutation_kind="create",
        remote_id=None,
        manifest=selected.manifest,
        manifest_digest=selected.manifest_digest,
        binaries=selected.binaries,
        state="prepared",
    )
    dispatched = sync_store.Attempt(
        attempt_id=prepared.attempt_id,
        feedback_id=17,
        projection_version=1,
        mutation_kind="create",
        remote_id=None,
        manifest=selected.manifest,
        manifest_digest=selected.manifest_digest,
        binaries=selected.binaries,
        state="dispatch_marked",
    )
    succeeded = sync_store.Attempt(
        attempt_id=prepared.attempt_id,
        feedback_id=17,
        projection_version=1,
        mutation_kind="create",
        remote_id=901,
        manifest=selected.manifest,
        manifest_digest=selected.manifest_digest,
        binaries=selected.binaries,
        state="rpc_succeeded",
    )
    monkeypatch.setattr(sync_store, "load_active_attempt", lambda _claim: None)
    monkeypatch.setattr(feedback_store, "projection_snapshot", lambda *_args: object())
    monkeypatch.setattr(
        "zira_dashboard.feedback_sync.build_projection_from_snapshot",
        lambda *_args, **_kwargs: selected,
    )
    monkeypatch.setattr(sync_store, "prepare_attempt", lambda **_kwargs: prepared)
    monkeypatch.setattr(sync_store, "mark_dispatch", lambda *_args: dispatched)
    monkeypatch.setattr(sync_store, "mark_rpc_succeeded", lambda *_args: succeeded)
    settled = []
    monkeypatch.setattr(sync_store, "settle_verified", lambda *args: settled.append(args))

    client = FakeClient()
    result = process_claim(claim(), client=client, now=aware_now())

    assert result == "verified"
    assert client.calls[0] == ("find_exact", "GPI-PM-FB-17")
    assert client.create_fields["x_studio_source"] == "GPI Plant Manager"
    assert set(client.read_fields) == set(client.create_fields)
    assert base64.b64decode(client.create_fields["x_studio_image"]) == b"safe-image"
    assert len(settled) == 1


def test_one_match_adopts_and_updates_without_clearing_absent_optionals(flow):
    row = {
        "id": 901,
        "x_studio_source": "GPI Plant Manager",
        "x_studio_source_id": "GPI-PM-FB-17",
    }
    flow.prepared = attempt(
        "prepared", selected=flow.selected, mutation_kind="update", remote_id=901
    )
    flow.dispatched = attempt(
        "dispatch_marked", selected=flow.selected, mutation_kind="update", remote_id=901
    )
    flow.succeeded = attempt(
        "rpc_succeeded", selected=flow.selected, mutation_kind="update", remote_id=901
    )
    flow.prepare.return_value = flow.prepared
    flow.mark_dispatch.return_value = flow.dispatched
    flow.mark_rpc.return_value = flow.succeeded
    client = FakeClient(exact_rows=[row])

    result = process_claim(claim(), client=client, now=aware_now())

    assert result == "verified"
    assert client.write_id == 901
    assert "x_studio_image" not in client.write_fields
    assert "x_studio_submitted_by" not in client.write_fields
    assert client.expected_contracts == [client.contract]
    assert client.expected_contracts[0] is client.contract
    flow.prepare.assert_called_once_with(
        claim=claim(),
        attempt_id=flow.prepare.call_args.kwargs["attempt_id"],
        mutation_kind="update",
        remote_id=901,
        manifest=flow.selected.manifest,
        manifest_digest=flow.selected.manifest_digest,
        binaries=flow.selected.binaries,
        now=aware_now(),
    )


def test_saved_id_exact_match_updates_only_owned_record(flow):
    row = {
        "id": 901,
        "x_studio_source": "GPI Plant Manager",
        "x_studio_source_id": "GPI-PM-FB-17",
    }
    saved_claim = claim(remote_id=901)
    flow.prepared = attempt(
        "prepared", selected=flow.selected, mutation_kind="update", remote_id=901
    )
    flow.dispatched = attempt(
        "dispatch_marked", selected=flow.selected, mutation_kind="update", remote_id=901
    )
    flow.succeeded = attempt(
        "rpc_succeeded", selected=flow.selected, mutation_kind="update", remote_id=901
    )
    flow.prepare.return_value = flow.prepared
    flow.mark_dispatch.return_value = flow.dispatched
    flow.mark_rpc.return_value = flow.succeeded
    client = FakeClient(exact_rows=[row])

    assert process_claim(saved_claim, client=client, now=aware_now()) == "verified"
    assert client.write_id == 901
    assert client.mutation_calls == ["write"]


def test_fresh_create_revalidates_compound_identity_after_prepare(flow):
    appeared = {
        "id": 901,
        "x_studio_source": "GPI Plant Manager",
        "x_studio_source_id": "GPI-PM-FB-17",
    }
    client = FakeClient()
    client.find_exact = MagicMock(side_effect=[[], [appeared]])

    result = process_claim(claim(), client=client, now=aware_now())

    assert result == "quarantined"
    assert client.mutation_calls == []
    flow.mark_dispatch.assert_not_called()
    flow.quarantine.assert_called_once_with(
        claim(),
        "compound_identity_changed_before_dispatch",
        aware_now(),
        attempt=flow.prepared,
    )


def test_recovered_deferred_create_stops_when_exact_row_appears(flow):
    saved = attempt("prepared")
    owned = active_claim(saved)
    client = FakeClient(exact_rows=[{"id": 901}])

    result = _recover_active(owned, saved, client=client, now=aware_now())

    assert result == "quarantined"
    assert client.mutation_calls == []
    flow.mark_dispatch.assert_not_called()
    flow.quarantine.assert_called_once_with(
        owned,
        "compound_identity_changed_before_dispatch",
        aware_now(),
        attempt=saved,
    )


@pytest.mark.parametrize("fresh_rows", [[], [{"id": 902}]])
def test_recovered_update_stops_when_exact_owned_row_is_missing_or_wrong(flow, fresh_rows):
    saved_projection = projection()
    saved = attempt(
        "prepared",
        selected=saved_projection,
        mutation_kind="update",
        remote_id=901,
    )
    owned = active_claim(saved)
    client = FakeClient(exact_rows=fresh_rows)

    result = _recover_active(owned, saved, client=client, now=aware_now())

    assert result == "quarantined"
    assert client.mutation_calls == []
    flow.mark_dispatch.assert_not_called()
    flow.quarantine.assert_called_once_with(
        owned,
        "compound_identity_changed_before_dispatch",
        aware_now(),
        attempt=saved,
    )


def test_prepared_dispatch_duplicate_lookup_fails_closed_before_mark(flow):
    saved = attempt("prepared")
    owned = active_claim(saved)
    client = FakeClient(exact_rows=[{"id": 901}, {"id": 902}])

    result = _recover_active(owned, saved, client=client, now=aware_now())

    assert result == "quarantined"
    assert client.mutation_calls == []
    flow.mark_dispatch.assert_not_called()
    flow.quarantine.assert_called_once_with(
        owned, "duplicate_compound_identity", aware_now(), attempt=saved
    )


@pytest.mark.parametrize(
    ("error", "safe_class"),
    [
        (TimeoutError("lookup payload token@example.invalid"), "identity_read_failed"),
        (ConnectionError("lookup URL"), "identity_read_failed"),
        (OSError("lookup secret"), "identity_read_failed"),
        (xmlrpc.client.Fault(9, "lookup remote text"), "odoo_fault"),
    ],
)
def test_prepared_dispatch_lookup_transport_failure_is_safe_and_definitive(flow, error, safe_class):
    saved = attempt("prepared")
    owned = active_claim(saved)
    client = FakeClient()
    client.find_exact = MagicMock(side_effect=error)

    result = _recover_active(owned, saved, client=client, now=aware_now())

    assert result == "retry_scheduled"
    assert client.mutation_calls == []
    flow.mark_dispatch.assert_not_called()
    flow.definitive.assert_called_once_with(owned, saved, safe_class, aware_now())
    assert str(error) not in repr(flow.definitive.call_args_list)


def test_prepared_dispatch_malformed_lookup_quarantines_before_mark(flow):
    saved = attempt("prepared")
    owned = active_claim(saved)
    error = ContractError("remote payload token@example.invalid")
    client = FakeClient()
    client.find_exact = MagicMock(side_effect=error)

    result = _recover_active(owned, saved, client=client, now=aware_now())

    assert result == "quarantined"
    assert client.mutation_calls == []
    flow.mark_dispatch.assert_not_called()
    flow.quarantine.assert_called_once_with(
        owned,
        "target_identity_or_contract_mismatch",
        aware_now(),
        attempt=saved,
    )
    assert str(error) not in repr(flow.quarantine.call_args_list)


def test_duplicate_exact_matches_quarantine_without_attempt_or_mutation(flow):
    client = FakeClient(exact_rows=[{"id": 1}, {"id": 2}])

    result = process_claim(claim(), client=client, now=aware_now())

    assert result == "quarantined"
    assert client.mutation_calls == []
    flow.prepare.assert_not_called()
    flow.quarantine.assert_called_once_with(claim(), "duplicate_compound_identity", aware_now())


@pytest.mark.parametrize("rows", [[], [{"id": 902}]])
def test_saved_id_lookup_conflict_quarantines_without_mutation(flow, rows):
    client = FakeClient(exact_rows=rows)
    saved_claim = claim(remote_id=901)

    result = process_claim(saved_claim, client=client, now=aware_now())

    assert result == "quarantined"
    assert client.mutation_calls == []
    flow.prepare.assert_not_called()
    flow.quarantine.assert_called_once_with(saved_claim, "saved_id_ownership_conflict", aware_now())


@pytest.mark.parametrize("invalid_remote_id", [True, 0, -1, "901", 2**63])
def test_invalid_adopted_remote_id_fails_closed_before_mutation(flow, invalid_remote_id):
    client = FakeClient(exact_rows=[{"id": invalid_remote_id}])

    result = process_claim(claim(), client=client, now=aware_now())

    assert result == "quarantined"
    assert client.mutation_calls == []
    flow.prepare.assert_not_called()
    flow.quarantine.assert_called_once_with(
        claim(), "target_identity_or_contract_mismatch", aware_now()
    )


def test_gate_closed_before_dispatch_defers_prepared_attempt(flow):
    gate_error = GateClosed("attacker@example.invalid secret-token")
    client = FakeClient(gate_error=gate_error)

    result = process_claim(claim(), client=client, now=aware_now())

    assert result == "deferred"
    flow.gate_defer.assert_called_once_with(claim(), flow.prepared, aware_now())
    flow.mark_dispatch.assert_not_called()
    flow.definitive.assert_not_called()
    flow.quarantine.assert_not_called()
    assert client.mutation_calls == []


def test_gate_closes_inside_wrapper_after_dispatch_and_uses_safe_store_class(flow):
    secret = "attacker@example.invalid token=do-not-store"
    client = FakeClient(create_error=GateClosed(secret))

    result = process_claim(claim(), client=client, now=aware_now())

    assert result == "retry_scheduled"
    flow.mark_dispatch.assert_called_once_with(claim(), flow.prepared, aware_now())
    flow.definitive.assert_called_once_with(
        claim(), flow.dispatched, "gate_closed_before_rpc", aware_now()
    )
    flow.quarantine.assert_not_called()
    assert secret not in repr(flow.definitive.call_args_list)
    assert client.mutation_calls == []


def test_real_wrapper_gate_close_occurs_before_executor_call(flow, monkeypatch):
    saved = attempt("prepared")
    owned = active_claim(saved)
    executor = MagicMock(side_effect=AssertionError("executor must not be called"))
    client = dedicated_client(executor)
    contract = ImprovementContract(start_type="date", stop_type="date")
    monkeypatch.setattr(client, "read_contract", MagicMock(return_value=contract))
    monkeypatch.setattr(client, "find_exact", MagicMock(return_value=[]))
    gate = MagicMock(side_effect=[None, GateClosed("closed before authorization")])
    monkeypatch.setattr(client, "assert_mutation_allowed", gate)

    result = _recover_active(owned, saved, client=client, now=aware_now())

    assert result == "retry_scheduled"
    executor.assert_not_called()
    assert gate.call_count == 2
    flow.definitive.assert_called_once_with(
        owned, flow.dispatched, "gate_closed_before_rpc", aware_now()
    )


def test_definitive_store_outcome_can_quarantine_at_retry_limit(flow):
    flow.definitive.return_value = "quarantined"
    client = FakeClient(create_error=GateClosed("closed"))

    assert process_claim(claim(), client=client, now=aware_now()) == "quarantined"


def test_odoo_fault_is_the_only_remote_rejection_that_schedules_mutation_retry(flow):
    secret = "fault payload token=do-not-store@example.invalid"
    client = FakeClient(create_error=xmlrpc.client.Fault(70017, secret))

    result = process_claim(claim(), client=client, now=aware_now())

    assert result == "retry_scheduled"
    flow.definitive.assert_called_once_with(claim(), flow.dispatched, "odoo_fault", aware_now())
    flow.quarantine.assert_not_called()
    assert secret not in repr(flow.definitive.call_args_list)


@pytest.mark.parametrize(
    "error",
    [
        TimeoutError("payload-timeout@example.invalid"),
        ConnectionError("payload-connection@example.invalid"),
        OSError("payload-os@example.invalid"),
        MalformedMutationResponse("payload-malformed@example.invalid"),
    ],
)
def test_ambiguous_mutation_classes_quarantine_without_retry(flow, error):
    client = FakeClient(create_error=error)

    result = process_claim(claim(), client=client, now=aware_now())

    assert result == "quarantined"
    flow.definitive.assert_not_called()
    flow.quarantine.assert_called_once_with(
        claim(), "ambiguous_mutation", aware_now(), attempt=flow.dispatched
    )
    assert str(error) not in repr(flow.quarantine.call_args_list)


@pytest.mark.parametrize(
    "error",
    [
        TargetIdentityError("target payload@example.invalid"),
        ContractError("contract payload@example.invalid"),
    ],
)
def test_deterministic_wrapper_mismatch_quarantines_without_retry(flow, error):
    client = FakeClient(create_error=error)

    result = process_claim(claim(), client=client, now=aware_now())

    assert result == "quarantined"
    flow.definitive.assert_not_called()
    flow.quarantine.assert_called_once_with(
        claim(),
        "target_identity_or_contract_mismatch",
        aware_now(),
        attempt=flow.dispatched,
    )
    assert str(error) not in repr(flow.quarantine.call_args_list)


@pytest.mark.parametrize("invalid_result", [True, False, 0, -1, "901", 2**63])
def test_invalid_create_acknowledgement_is_ambiguous_after_dispatch(flow, invalid_result):
    client = FakeClient(create_result=invalid_result)

    result = process_claim(claim(), client=client, now=aware_now())

    assert result == "quarantined"
    flow.mark_rpc.assert_not_called()
    flow.quarantine.assert_called_once_with(
        claim(), "ambiguous_mutation", aware_now(), attempt=flow.dispatched
    )


def test_rpc_success_persistence_failure_best_effort_quarantines_and_reraises(flow):
    failure = RuntimeError("database-url token@example.invalid")
    flow.mark_rpc.side_effect = failure
    client = FakeClient()

    with pytest.raises(RuntimeError) as raised:
        process_claim(claim(), client=client, now=aware_now())

    assert raised.value is failure
    flow.quarantine.assert_called_once_with(
        claim(), "ambiguous_mutation", aware_now(), attempt=flow.dispatched
    )
    assert str(failure) not in repr(flow.quarantine.call_args_list)


def test_rpc_success_persistence_failure_preserves_original_if_quarantine_fails(flow):
    failure = RuntimeError("first secret")
    flow.mark_rpc.side_effect = failure
    flow.quarantine.side_effect = RuntimeError("second secret")

    with pytest.raises(RuntimeError) as raised:
        process_claim(claim(), client=FakeClient(), now=aware_now())

    assert raised.value is failure


def test_matching_values_do_not_clear_dispatch_marked_ambiguity(flow):
    saved = attempt("dispatch_marked")
    owned = active_claim(saved)
    client = FakeClient(remote_matches=True)

    result = _recover_active(owned, saved, client=client, now=aware_now())

    assert result == "quarantined"
    assert client.read_count == 0
    assert client.mutation_calls == []
    flow.quarantine.assert_called_once_with(
        owned, "ambiguous_stale_dispatch", aware_now(), attempt=saved
    )


def test_rpc_succeeded_recovery_uses_saved_v1_manifest_under_v2_claim(flow):
    saved_projection = projection(raw=b"saved-v1-image")
    saved = attempt("rpc_succeeded", selected=saved_projection, projection_version=1)
    newer_claim = active_claim(saved, desired_version=2)
    flow.image_snapshot.return_value = {"x_studio_image": normalized(b"saved-v1-image")}
    client = FakeClient()
    client.remote_values = saved_projection.dispatch_fields()

    result = _recover_active(newer_claim, saved, client=client, now=aware_now())

    assert result == "verified"
    assert client.mutation_calls == []
    assert client.read_count == 1
    assert set(client.read_fields) == set(saved_projection.dispatch_fields())
    assert client.read_full_binary == [True]
    flow.snapshot_read.assert_not_called()
    flow.build.assert_not_called()
    flow.image_snapshot.assert_called_once_with(17, saved.binaries)
    flow.settle.assert_called_once_with(newer_claim, saved, 901, aware_now())


def test_prepared_v1_recovery_dispatches_saved_manifest_not_current_v2(flow):
    saved_projection = projection(raw=b"saved-v1-image")
    saved = attempt("prepared", selected=saved_projection, projection_version=1)
    dispatched = attempt("dispatch_marked", selected=saved_projection, projection_version=1)
    succeeded = attempt(
        "rpc_succeeded",
        selected=saved_projection,
        projection_version=1,
        remote_id=901,
    )
    newer_claim = active_claim(saved, desired_version=2)
    flow.mark_dispatch.return_value = dispatched
    flow.mark_rpc.return_value = succeeded
    flow.image_snapshot.return_value = {"x_studio_image": normalized(b"saved-v1-image")}
    client = FakeClient()

    result = _recover_active(newer_claim, saved, client=client, now=aware_now())

    assert result == "verified"
    assert client.create_fields == saved_projection.dispatch_fields()
    assert client.expected_contracts[0] is client.contract
    assert client.calls.index(("assert_mutation_allowed", 17)) < client.calls.index(
        ("create_improvement", 17)
    )
    flow.snapshot_read.assert_not_called()
    flow.build.assert_not_called()


def test_prepared_recovery_contract_drift_quarantines_before_dispatch(flow):
    saved_projection = projection(start_value="2026-08-20")
    saved = attempt("prepared", selected=saved_projection)
    owned = active_claim(saved)
    client = FakeClient(contract=ImprovementContract(start_type="datetime", stop_type="date"))

    result = _recover_active(owned, saved, client=client, now=aware_now())

    assert result == "quarantined"
    assert client.mutation_calls == []
    flow.mark_dispatch.assert_not_called()
    flow.quarantine.assert_called_once_with(
        owned, "contract_changed_before_dispatch", aware_now(), attempt=saved
    )


@pytest.mark.parametrize(
    ("contract", "selected"),
    [
        (
            ImprovementContract(start_type="date", stop_type="date"),
            projection(start_value="2026-08-20 18:00:00"),
        ),
        (
            ImprovementContract(start_type="datetime", stop_type="datetime"),
            projection(
                start_value="2026-08-20 18:00:00",
                include_optionals=True,
            ),
        ),
    ],
)
def test_prepared_recovery_validates_every_saved_date_shape(flow, contract, selected):
    saved = attempt("prepared", selected=selected)
    owned = active_claim(saved)
    client = FakeClient(contract=contract)

    result = _recover_active(owned, saved, client=client, now=aware_now())

    if contract.start_type == "date":
        assert result == "quarantined"
        assert client.mutation_calls == []
    else:
        # include_optionals keeps a date-only stop value, so the datetime stop
        # contract is also a drift and must fail closed.
        assert result == "quarantined"
        assert client.mutation_calls == []
    flow.quarantine.assert_called_once_with(
        owned, "contract_changed_before_dispatch", aware_now(), attempt=saved
    )


def test_prepared_recovery_gate_closed_before_marking_dispatch(flow):
    saved = attempt("prepared")
    owned = active_claim(saved)
    client = FakeClient(gate_error=GateClosed("secret"))

    result = _recover_active(owned, saved, client=client, now=aware_now())

    assert result == "deferred"
    flow.gate_defer.assert_called_once_with(owned, saved, aware_now())
    flow.mark_dispatch.assert_not_called()
    assert client.mutation_calls == []


@pytest.mark.parametrize(
    ("error", "safe_class"),
    [
        (TimeoutError("remote token"), "identity_read_failed"),
        (ConnectionError("remote email@example.invalid"), "identity_read_failed"),
        (OSError("remote URL"), "identity_read_failed"),
        (xmlrpc.client.Fault(99, "remote payload"), "odoo_fault"),
    ],
)
def test_prepared_contract_read_safe_failures_schedule_definitive_retry(flow, error, safe_class):
    saved = attempt("prepared")
    owned = active_claim(saved)
    client = FakeClient()
    client.read_contract = MagicMock(side_effect=error)

    result = _recover_active(owned, saved, client=client, now=aware_now())

    assert result == "retry_scheduled"
    flow.definitive.assert_called_once_with(owned, saved, safe_class, aware_now())
    assert str(error) not in repr(flow.definitive.call_args_list)
    flow.mark_dispatch.assert_not_called()


def test_prepared_contract_validation_failure_quarantines_without_mutation(flow):
    saved = attempt("prepared")
    owned = active_claim(saved)
    client = FakeClient()
    error = ContractError("remote payload@example.invalid")
    client.read_contract = MagicMock(side_effect=error)

    result = _recover_active(owned, saved, client=client, now=aware_now())

    assert result == "quarantined"
    flow.quarantine.assert_called_once_with(
        owned,
        "target_identity_or_contract_mismatch",
        aware_now(),
        attempt=saved,
    )
    assert str(error) not in repr(flow.quarantine.call_args_list)


@pytest.mark.parametrize(
    "read_error",
    [
        TimeoutError("read payload"),
        ConnectionError("read URL"),
        OSError("read token"),
        xmlrpc.client.Fault(8, "read email@example.invalid"),
    ],
)
def test_rpc_succeeded_read_transport_failure_schedules_readback_only(flow, read_error):
    saved = attempt("rpc_succeeded")
    owned = active_claim(saved)
    client = FakeClient(read_error=read_error)

    result = _recover_active(owned, saved, client=client, now=aware_now())

    assert result == "deferred"
    assert client.mutation_calls == []
    flow.schedule.assert_called_once_with(owned, saved, aware_now())
    flow.definitive.assert_not_called()
    flow.quarantine.assert_not_called()
    assert str(read_error) not in repr(flow.schedule.call_args_list)


def test_readback_value_mismatch_quarantines_without_another_mutation(flow):
    saved = attempt("rpc_succeeded")
    owned = active_claim(saved)
    client = FakeClient(remote_matches=False)

    result = _recover_active(owned, saved, client=client, now=aware_now())

    assert result == "quarantined"
    assert client.mutation_calls == []
    flow.settle.assert_not_called()
    flow.quarantine.assert_called_once_with(owned, "readback_mismatch", aware_now(), attempt=saved)


def test_malformed_read_response_quarantines_as_readback_mismatch(flow):
    saved = attempt("rpc_succeeded")
    owned = active_claim(saved)
    error = ContractError("payload token@example.invalid")
    client = FakeClient(read_error=error)

    result = _recover_active(owned, saved, client=client, now=aware_now())

    assert result == "quarantined"
    flow.schedule.assert_not_called()
    flow.quarantine.assert_called_once_with(owned, "readback_mismatch", aware_now(), attempt=saved)
    assert str(error) not in repr(flow.quarantine.call_args_list)


@pytest.mark.parametrize("image_result", [{}, {"x_studio_image": normalized(b"changed")}])
def test_saved_binary_evidence_drift_quarantines_before_read_or_mutation(flow, image_result):
    saved_projection = projection(raw=b"saved")
    saved = attempt("rpc_succeeded", selected=saved_projection)
    owned = active_claim(saved)
    flow.image_snapshot.return_value = image_result
    client = FakeClient()

    result = _recover_active(owned, saved, client=client, now=aware_now())

    assert result == "quarantined"
    assert client.read_count == 0
    assert client.mutation_calls == []
    flow.quarantine.assert_called_once_with(
        owned, "local_binary_evidence_changed", aware_now(), attempt=saved
    )


def test_malformed_saved_nonbinary_manifest_uses_local_projection_quarantine(flow):
    manifest = projection().manifest
    manifest["fields"]["x_studio_type"] = "Physical"
    encoded = json.dumps(
        manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode()
    saved = sync_store.Attempt(
        attempt_id=UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"),
        feedback_id=17,
        projection_version=1,
        mutation_kind="create",
        remote_id=901,
        manifest=manifest,
        manifest_digest=hashlib.sha256(encoded).hexdigest(),
        binaries={},
        state="rpc_succeeded",
    )
    owned = active_claim(saved)
    client = FakeClient()

    result = _recover_active(owned, saved, client=client, now=aware_now())

    assert result == "quarantined"
    assert client.read_count == 0
    flow.quarantine.assert_called_once_with(
        owned, "local_projection_unavailable", aware_now(), attempt=saved
    )


def test_stale_unprepared_v1_claim_is_released_immediately_when_local_row_advances(flow):
    flow.snapshot_read.side_effect = feedback_store.ProjectionSnapshotUnavailable(
        "current row is v2"
    )
    flow.release_stale.return_value = True
    client = FakeClient()

    result = process_claim(claim(version=1), client=client, now=aware_now())

    assert result == "deferred"
    flow.release_stale.assert_called_once_with(claim(version=1), aware_now())
    flow.quarantine.assert_not_called()
    flow.prepare.assert_not_called()
    assert client.calls == []
    assert client.mutation_calls == []


def test_claim_that_turns_stale_between_snapshot_and_prepare_is_released(flow):
    stale_error = sync_store.StateTransitionError("desired version advanced")
    flow.prepare.side_effect = stale_error
    flow.release_stale.return_value = True
    client = FakeClient()

    result = process_claim(claim(version=1), client=client, now=aware_now())

    assert result == "deferred"
    flow.release_stale.assert_called_once_with(claim(version=1), aware_now())
    flow.mark_dispatch.assert_not_called()
    assert client.mutation_calls == []


def test_unavailable_projection_without_proven_version_race_quarantines(flow):
    secret = "broken row token@example.invalid"
    flow.snapshot_read.side_effect = feedback_store.ProjectionSnapshotUnavailable(secret)
    flow.release_stale.return_value = False

    result = process_claim(claim(), client=FakeClient(), now=aware_now())

    assert result == "quarantined"
    flow.quarantine.assert_called_once_with(claim(), "local_projection_unavailable", aware_now())
    assert secret not in repr(flow.quarantine.call_args_list)


def test_initial_contract_mismatch_quarantines_before_attempt(flow):
    error = ContractError("target contract remote@example.invalid")
    client = FakeClient()
    client.read_contract = MagicMock(side_effect=error)

    result = process_claim(claim(), client=client, now=aware_now())

    assert result == "quarantined"
    flow.prepare.assert_not_called()
    flow.quarantine.assert_called_once_with(
        claim(), "target_identity_or_contract_mismatch", aware_now()
    )
    assert str(error) not in repr(flow.quarantine.call_args_list)


@pytest.mark.parametrize("phase", ["contract", "employee_projection", "exact_lookup"])
@pytest.mark.parametrize(
    ("error", "safe_class"),
    [
        (TimeoutError("remote URL token@example.invalid"), "identity_read_failed"),
        (xmlrpc.client.Fault(77, "remote payload secret"), "odoo_fault"),
    ],
)
def test_initial_remote_read_failure_defers_unprepared_claim_without_text(
    flow, monkeypatch, phase, error, safe_class
):
    client = FakeClient()
    if phase == "contract":
        client.read_contract = MagicMock(side_effect=error)
    elif phase == "employee_projection":
        flow.build.side_effect = error
    else:
        client.find_exact = MagicMock(side_effect=error)
    defer = MagicMock(return_value=True)
    monkeypatch.setattr(
        sync_store,
        "defer_unprepared_read_failure",
        defer,
        raising=False,
    )

    result = process_claim(claim(), client=client, now=aware_now())

    assert result == "deferred"
    flow.prepare.assert_not_called()
    flow.definitive.assert_not_called()
    flow.quarantine.assert_not_called()
    defer.assert_called_once_with(claim(), safe_class, aware_now())
    assert str(error) not in repr(defer.call_args_list)


def test_batch_result_counts_only_allowlisted_worker_outcomes():
    outcomes = [
        "verified",
        "deferred",
        "retry_scheduled",
        "quarantined",
        "verified",
        "isolated_error",
    ]

    assert BatchResult.from_outcomes(outcomes) == BatchResult(
        attempted=6,
        verified=2,
        deferred=1,
        retry_scheduled=1,
        quarantined=1,
        isolated_errors=1,
    )


@pytest.mark.parametrize(
    "outcomes",
    [
        ["verified", "unknown"],
        ["verified", ""],
        ["verified", None],
        ("verified",),
    ],
)
def test_batch_result_rejects_unknown_or_nonlist_outcomes(outcomes):
    with pytest.raises(ValueError):
        BatchResult.from_outcomes(outcomes)


@pytest.mark.parametrize(("requested", "expected"), [(0, 1), (1, 1), (9, 9), (11, 10)])
def test_run_batch_is_bounded_sequential_and_uses_canary(monkeypatch, requested, expected):
    monkeypatch.setenv("ODOO_SHARED_REPORTING_WRITE_ENABLED", "true")
    monkeypatch.setenv("ODOO_IMPROVEMENTS_WRITE_ENABLED", "true")
    client = FakeClient()
    client.canary = 17
    claims = [claim(), claim(remote_id=901)]
    recover = MagicMock(return_value=0)
    claim_due = MagicMock(return_value=claims)
    order = []

    monkeypatch.setattr(
        feedback_sync.ImprovementsClient, "from_env", MagicMock(return_value=client)
    )
    monkeypatch.setattr(sync_store, "recover_expired_claims", recover)
    monkeypatch.setattr(sync_store, "claim_due", claim_due)
    monkeypatch.setattr(
        feedback_sync,
        "process_claim",
        lambda item, *, client, now: order.append(item.feedback_id) or "verified",
    )

    result = run_batch(now=aware_now(), worker_id="worker-a", limit=requested)

    assert result == BatchResult(attempted=2, verified=2)
    assert client.worker_enabled_calls == 1
    recover.assert_called_once_with(aware_now())
    claim_due.assert_called_once_with(
        now=aware_now(),
        worker_id="worker-a",
        limit=expected,
        canary_feedback_id=17,
    )
    assert order == [17, 17]


def test_run_batch_isolates_one_claim_exception_and_continues_sequentially(monkeypatch):
    monkeypatch.setenv("ODOO_SHARED_REPORTING_WRITE_ENABLED", "true")
    monkeypatch.setenv("ODOO_IMPROVEMENTS_WRITE_ENABLED", "true")
    client = FakeClient()
    claims = [claim(), claim(remote_id=901)]
    calls = []

    monkeypatch.setattr(
        feedback_sync.ImprovementsClient, "from_env", MagicMock(return_value=client)
    )
    monkeypatch.setattr(sync_store, "recover_expired_claims", MagicMock(return_value=0))
    monkeypatch.setattr(sync_store, "claim_due", MagicMock(return_value=claims))

    def run(item, *, client, now):
        calls.append(item.odoo_improvement_id)
        if item.odoo_improvement_id is None:
            raise RuntimeError("payload token@example.invalid")
        return "verified"

    monkeypatch.setattr(feedback_sync, "process_claim", run)

    result = run_batch(now=aware_now(), worker_id="worker-a", limit=2)

    assert calls == [None, 901]
    assert result == BatchResult(attempted=2, verified=1, isolated_errors=1)
    assert (
        result.verified
        + result.deferred
        + result.retry_scheduled
        + result.quarantined
        + result.isolated_errors
        == result.attempted
    )


@pytest.mark.parametrize(
    "invalid_limit",
    [True, False, 1.0, 2.5, "1", None, float("nan"), float("inf")],
)
def test_run_batch_rejects_noninteger_limit_before_client_creation(monkeypatch, invalid_limit):
    monkeypatch.setenv("ODOO_SHARED_REPORTING_WRITE_ENABLED", "true")
    monkeypatch.setenv("ODOO_IMPROVEMENTS_WRITE_ENABLED", "true")
    from_env = MagicMock(side_effect=AssertionError("client must not be created"))
    monkeypatch.setattr(feedback_sync.ImprovementsClient, "from_env", from_env)

    with pytest.raises(ValueError):
        run_batch(now=aware_now(), worker_id="worker-a", limit=invalid_limit)

    from_env.assert_not_called()


class PoisonBatchArgument:
    def __bool__(self):
        raise AssertionError("batch arguments must not be read")


@pytest.mark.parametrize(
    ("master", "improvements"),
    [
        (None, None),
        (None, "true"),
        ("", "true"),
        ("false", "true"),
        ("0", "true"),
        ("TRUE", "true"),
        ("true ", "true"),
        ("true", None),
        ("true", ""),
        ("true", "false"),
        ("true", "0"),
        ("true", "TRUE"),
        ("true", "true "),
    ],
)
def test_closed_gates_stop_before_arguments_config_database_or_odoo(
    monkeypatch, master, improvements
):
    set_or_delete(monkeypatch, "ODOO_SHARED_REPORTING_WRITE_ENABLED", master)
    set_or_delete(monkeypatch, "ODOO_IMPROVEMENTS_WRITE_ENABLED", improvements)
    poison = MagicMock(side_effect=AssertionError("downstream boundary must not run"))
    monkeypatch.setattr(feedback_sync.socket, "gethostname", poison)
    monkeypatch.setattr(feedback_sync.os, "getpid", poison)
    monkeypatch.setattr(feedback_sync.ImprovementsClient, "from_env", poison)
    monkeypatch.setattr(feedback_sync.ImprovementsClient, "default_executor", poison)
    monkeypatch.setattr(sync_store, "recover_expired_claims", poison)
    monkeypatch.setattr(sync_store, "claim_due", poison)
    monkeypatch.setattr(feedback_sync, "process_claim", poison)
    argument = PoisonBatchArgument()

    result = run_batch(now=argument, worker_id=argument, limit=argument)

    assert result == BatchResult(skipped="write_gates_closed")
    poison.assert_not_called()


def dedicated_client(executor=None):
    config = ImprovementsConfig(
        url="https://odoo.invalid",
        database="database",
        login="service@example.invalid",
        api_key="dummy-secret",
        expected_database_uuid="expected-uuid",
        expected_company="Expected Company",
    )
    return ImprovementsClient(config, executor or MagicMock(), uid=7)


def test_worker_gate_check_and_canary_parse_are_environment_only(monkeypatch):
    executor = MagicMock(side_effect=AssertionError("must not call Odoo"))
    client = dedicated_client(executor)

    with pytest.raises(GateClosed):
        client.assert_worker_enabled()
    executor.assert_not_called()

    monkeypatch.setenv("ODOO_SHARED_REPORTING_WRITE_ENABLED", "true")
    monkeypatch.setenv("ODOO_IMPROVEMENTS_WRITE_ENABLED", "true")
    monkeypatch.setenv("ODOO_IMPROVEMENTS_CANARY_FEEDBACK_ID", "17")
    client.assert_worker_enabled()
    assert client.canary_feedback_id() == 17
    executor.assert_not_called()


@pytest.mark.parametrize("value", ["0", "-1", "+1", " 1", "1 ", "1.0", "true", str(2**63)])
def test_canary_feedback_id_rejects_every_noncanonical_value(monkeypatch, value):
    client = dedicated_client()
    monkeypatch.setenv("ODOO_IMPROVEMENTS_CANARY_FEEDBACK_ID", value)

    with pytest.raises(GateClosed):
        client.canary_feedback_id()


def test_canary_feedback_id_omission_returns_none(monkeypatch):
    client = dedicated_client()
    monkeypatch.setenv("ODOO_IMPROVEMENTS_CANARY_FEEDBACK_ID", "")
    assert client.canary_feedback_id() is None


def test_worker_never_logs_remote_exception_or_payload_text(flow, caplog):
    secret = "https://remote.invalid token=s3cret person@example.invalid"
    client = FakeClient(create_error=TimeoutError(secret))

    assert process_claim(claim(), client=client, now=aware_now()) == "quarantined"

    assert caplog.records == []
    assert secret not in repr(flow.quarantine.call_args_list)
    assert secret not in repr(flow.definitive.call_args_list)


class RecordingCursor(AbstractContextManager):
    def __init__(self, *results):
        self.results = list(results)
        self.current = []
        self.executions = []
        self.exit_exception = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        self.exit_exception = exc_type
        return False

    def execute(self, statement, params=None):
        self.executions.append((str(statement), params))
        index = len(self.executions) - 1
        self.current = self.results[index] if index < len(self.results) else []

    def fetchone(self):
        return self.current[0] if self.current else None

    def fetchall(self):
        return list(self.current)


def image_row(role: str, raw: bytes):
    return {
        "feedback_id": 17,
        "role": role,
        "jpeg_bytes": memoryview(raw),
        "sha256": hashlib.sha256(raw).hexdigest(),
        "byte_length": len(raw),
        "width": 8,
        "height": 8,
    }


def claimed_row(**changes):
    row = {
        "feedback_id": 17,
        "desired_version": 1,
        "last_synced_version": 0,
        "odoo_improvement_id": None,
        "claim_owner": "worker-a",
        "claim_token": UUID("11111111-1111-1111-1111-111111111111"),
        "claim_expires_at": aware_now() + timedelta(minutes=5),
        "active_attempt_id": None,
        "attempt_count": 0,
    }
    row.update(changes)
    return row


def normalized_sql(cursor, index):
    return " ".join(cursor.executions[index][0].split())


def test_released_retry_budget_survives_worker_post_dispatch_gate_failure(monkeypatch, flow):
    attempt_id = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
    cursor = RecordingCursor(
        [
            {
                "feedback_id": 17,
                "desired_version": 3,
                "last_synced_version": 2,
                "odoo_improvement_id": 77,
                "due_at": aware_now() + timedelta(minutes=10),
                "attempt_count": sync_store.MAX_MUTATION_ATTEMPTS,
                "sync_state": "quarantined",
                "claim_owner": None,
                "claim_token": None,
                "claim_expires_at": None,
                "active_attempt_id": attempt_id,
                "quarantine_reason": "retry_exhausted",
                "quarantined_at": aware_now(),
                "attempt_id": attempt_id,
                "attempt_feedback_id": 17,
                "attempt_projection_version": 3,
                "attempt_state": "definitive_failed",
                "attempt_remote_id": 77,
                "feedback_projection_version": 3,
                "feedback_status": "requested",
                "feedback_lifecycle_origin": "local",
            }
        ],
        [{"id": 1, "attempt_id": attempt_id, "action": "release_definitive"}],
        [
            {
                "feedback_id": 17,
                "desired_version": 3,
                "last_synced_version": 2,
                "state": "idle",
                "active_attempt_id": None,
                "due_at": aware_now(),
                "attempt_count": 0,
            }
        ],
    )
    monkeypatch.setattr(sync_store.db, "cursor", lambda: cursor)

    released = sync_store.apply_quarantine_disposition(
        attempt_id=attempt_id,
        disposition="release-definitive",
        reviewer="Human Operator",
        human_review_confirmed=False,
        now=aware_now(),
    )

    update_set = normalized_sql(cursor, 2).split("SET", 1)[1].split("WHERE", 1)[0]
    assert "attempt_count = 0" in update_set
    assert released.state == "idle"
    released_claim = claim(remote_id=77, version=3, last_synced_version=2, attempt_count=0)
    update_prepared = attempt(
        "prepared", mutation_kind="update", remote_id=77, projection_version=3
    )
    update_dispatched = attempt(
        "dispatch_marked", mutation_kind="update", remote_id=77, projection_version=3
    )
    flow.prepare.return_value = update_prepared
    flow.mark_dispatch.return_value = update_dispatched

    def record_fresh_failure(owned, dispatched, error_class, now):
        assert owned.attempt_count == 0
        assert error_class == "gate_closed_before_rpc"
        assert dispatched.state == "dispatch_marked"
        assert now == aware_now()
        return "retry_scheduled"

    flow.definitive.side_effect = record_fresh_failure
    client = FakeClient(write_error=GateClosed("closed after dispatch"), exact_rows=[{"id": 77}])

    assert process_claim(released_claim, client=client, now=aware_now()) == "retry_scheduled"
    flow.quarantine.assert_not_called()


def test_attempt_image_snapshot_is_bounded_detached_and_returns_only_saved_evidence(
    monkeypatch,
):
    saved = attempt("prepared", selected=projection(raw=b"saved-before"))
    cursor = RecordingCursor([image_row("before", b"saved-before")])
    monkeypatch.setattr(feedback_store.db, "cursor", lambda: cursor)

    result = feedback_store.attempt_image_snapshot(17, saved.binaries)

    assert set(result) == {"x_studio_image"}
    assert result["x_studio_image"].jpeg_bytes == b"saved-before"
    assert type(result["x_studio_image"].jpeg_bytes) is bytes
    assert len(cursor.executions) == 1
    sql = normalized_sql(cursor, 0)
    assert "role = ANY(%s)" in sql
    assert "byte_length > 0" in sql
    assert "byte_length <= %s" in sql
    assert "octet_length(jpeg_bytes) > 0" in sql
    assert "octet_length(jpeg_bytes) <= %s" in sql
    assert "octet_length(jpeg_bytes) = byte_length" in sql
    assert "ORDER BY role LIMIT 2" in sql
    assert cursor.executions[0][1] == (
        17,
        ["before"],
        MAX_OUTPUT_BYTES,
        MAX_OUTPUT_BYTES,
    )
    assert cursor.exit_exception is None
    with pytest.raises(TypeError):
        result["x_studio_after_image"] = normalized(b"changed")


def test_attempt_image_snapshot_queries_only_sorted_exact_required_roles(monkeypatch):
    selected = projection(raw=b"before")
    fields = selected.fields
    binaries = selected.binaries
    binaries["x_studio_after_image"] = BinaryEvidence(
        jpeg_bytes=b"after",
        sha256=hashlib.sha256(b"after").hexdigest(),
        byte_length=len(b"after"),
    )
    manifest = {
        "fields": fields,
        "binary_evidence": {
            name: {"sha256": evidence.sha256, "byte_length": evidence.byte_length}
            for name, evidence in binaries.items()
        },
    }
    encoded = json.dumps(
        manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode()
    saved = sync_store.Attempt(
        attempt_id=UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"),
        feedback_id=17,
        projection_version=1,
        mutation_kind="create",
        remote_id=None,
        manifest=manifest,
        manifest_digest=hashlib.sha256(encoded).hexdigest(),
        binaries=binaries,
        state="prepared",
    )
    cursor = RecordingCursor([image_row("after", b"after"), image_row("before", b"before")])
    monkeypatch.setattr(feedback_store.db, "cursor", lambda: cursor)

    result = feedback_store.attempt_image_snapshot(17, saved.binaries)

    assert set(result) == {"x_studio_after_image", "x_studio_image"}
    assert cursor.executions[0][1] == (
        17,
        ["after", "before"],
        MAX_OUTPUT_BYTES,
        MAX_OUTPUT_BYTES,
    )


@pytest.mark.parametrize(
    "rows",
    [
        [],
        [image_row("before", b"changed")],
        [image_row("before", b"saved"), image_row("before", b"saved")],
        [image_row("unexpected", b"saved")],
    ],
)
def test_attempt_image_snapshot_rejects_missing_changed_duplicate_or_unknown_rows(
    monkeypatch, rows
):
    saved = attempt("prepared", selected=projection(raw=b"saved"))
    cursor = RecordingCursor(rows)
    monkeypatch.setattr(feedback_store.db, "cursor", lambda: cursor)

    with pytest.raises(feedback_store.ProjectionSnapshotUnavailable):
        feedback_store.attempt_image_snapshot(17, saved.binaries)


def test_release_stale_unprepared_claim_requires_newer_desired_version_and_exact_owner(
    monkeypatch,
):
    cursor = RecordingCursor(
        [claimed_row(desired_version=2)],
        [{"feedback_id": 17, "desired_version": 2}],
    )
    monkeypatch.setattr(sync_store.db, "cursor", lambda: cursor)

    assert sync_store.release_stale_unprepared_claim(claim(version=1), aware_now()) is True

    select_sql = normalized_sql(cursor, 0)
    update_sql = normalized_sql(cursor, 1)
    assert "FOR UPDATE" in select_sql
    assert "claim_owner = %s" in select_sql
    assert "claim_token = %s" in select_sql
    assert "active_attempt_id IS NULL" in select_sql
    assert "desired_version > %s" in update_sql
    assert "state = 'idle'" in update_sql
    assert "claim_owner = NULL" in update_sql
    assert "claim_token = NULL" in update_sql
    assert "claim_expires_at = NULL" in update_sql
    assert "attempt_count" not in update_sql.split("SET", 1)[1].split("WHERE", 1)[0]
    assert cursor.exit_exception is None


def test_release_stale_unprepared_claim_returns_false_when_claim_version_is_current(
    monkeypatch,
):
    cursor = RecordingCursor([claimed_row(desired_version=1)])
    monkeypatch.setattr(sync_store.db, "cursor", lambda: cursor)

    assert sync_store.release_stale_unprepared_claim(claim(version=1), aware_now()) is False
    assert len(cursor.executions) == 1


@pytest.mark.parametrize("selected", [[], [claimed_row(), claimed_row()]])
def test_release_stale_unprepared_claim_fails_closed_on_lost_authority(monkeypatch, selected):
    cursor = RecordingCursor(selected)
    monkeypatch.setattr(sync_store.db, "cursor", lambda: cursor)

    with pytest.raises(sync_store.StateTransitionError):
        sync_store.release_stale_unprepared_claim(claim(), aware_now())


@pytest.mark.parametrize("safe_class", ["identity_read_failed", "odoo_fault"])
def test_defer_unprepared_read_failure_uses_fixed_safe_summary_and_one_minute_due(
    monkeypatch, safe_class
):
    due = aware_now() + timedelta(minutes=1)
    cursor = RecordingCursor(
        [claimed_row()],
        [{"feedback_id": 17, "desired_version": 1, "due_at": due}],
    )
    monkeypatch.setattr(sync_store.db, "cursor", lambda: cursor)

    assert sync_store.defer_unprepared_read_failure(claim(), safe_class, aware_now()) is True

    select_sql = normalized_sql(cursor, 0)
    update_sql = normalized_sql(cursor, 1)
    assert "FOR UPDATE" in select_sql
    assert "claim_owner = %s" in select_sql
    assert "claim_token = %s" in select_sql
    assert "claim_expires_at = %s" in select_sql
    assert "active_attempt_id IS NULL" in select_sql
    assert "state = 'idle'" in update_sql
    assert "last_error_class = %s" in update_sql
    assert "last_error_summary = %s" in update_sql
    assert "attempt_count" not in update_sql.split("SET", 1)[1].split("WHERE", 1)[0]
    params = cursor.executions[1][1]
    assert due in params
    assert safe_class in params
    assert not any("token@example.invalid" in str(value) for value in params)
    assert cursor.exit_exception is None


def test_defer_unprepared_read_failure_is_due_now_if_desired_version_advanced(
    monkeypatch,
):
    cursor = RecordingCursor(
        [claimed_row(desired_version=2)],
        [{"feedback_id": 17, "desired_version": 2, "due_at": aware_now()}],
    )
    monkeypatch.setattr(sync_store.db, "cursor", lambda: cursor)

    assert (
        sync_store.defer_unprepared_read_failure(
            claim(version=1), "identity_read_failed", aware_now()
        )
        is True
    )

    assert aware_now() in cursor.executions[1][1]
    assert aware_now() + timedelta(minutes=1) not in cursor.executions[1][1]


@pytest.mark.parametrize("selected", [[], [claimed_row(), claimed_row()]])
def test_defer_unprepared_read_failure_lost_authority_rolls_back(monkeypatch, selected):
    cursor = RecordingCursor(selected)
    monkeypatch.setattr(sync_store.db, "cursor", lambda: cursor)

    with pytest.raises(sync_store.StateTransitionError):
        sync_store.defer_unprepared_read_failure(claim(), "identity_read_failed", aware_now())

    assert cursor.exit_exception is sync_store.StateTransitionError


@pytest.mark.parametrize(
    "unsafe_class",
    ["", "timeout", "ambiguous_mutation", "identity_read_failed\nsecret"],
)
def test_defer_unprepared_read_failure_rejects_nonallowlisted_class_before_database(
    monkeypatch, unsafe_class
):
    database = MagicMock(side_effect=AssertionError("database must not be touched"))
    monkeypatch.setattr(sync_store.db, "cursor", database)

    with pytest.raises(ValueError):
        sync_store.defer_unprepared_read_failure(claim(), unsafe_class, aware_now())

    database.assert_not_called()


def test_local_projection_unavailable_is_an_exact_store_allowlisted_reason(monkeypatch):
    cursor = RecordingCursor([claimed_row()], [{"feedback_id": 17}])
    monkeypatch.setattr(sync_store.db, "cursor", lambda: cursor)

    assert sync_store.quarantine(claim(), "local_projection_unavailable", aware_now()) is True
    assert "local_projection_unavailable" in cursor.executions[1][1]


def test_compound_identity_change_is_an_exact_store_allowlisted_reason(monkeypatch):
    cursor = RecordingCursor([claimed_row()], [{"feedback_id": 17}])
    monkeypatch.setattr(sync_store.db, "cursor", lambda: cursor)

    assert (
        sync_store.quarantine(claim(), "compound_identity_changed_before_dispatch", aware_now())
        is True
    )
    assert "compound_identity_changed_before_dispatch" in cursor.executions[1][1]
