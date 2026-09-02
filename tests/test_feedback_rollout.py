"""Inert tests for bounded legacy feedback rollout analysis and local backfill."""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import MappingProxyType
from unittest.mock import MagicMock, call
from uuid import UUID, uuid4

import pytest

from scripts import feedback_odoo_rollout as cli
from zira_dashboard import feedback_store
from zira_dashboard import feedback_sync_store as sync_store
from zira_dashboard.feedback_image import MAX_OUTPUT_BYTES, NormalizedImage
from zira_dashboard.feedback_projection import ReadbackMismatch
from zira_dashboard.feedback_rollout import (
    DryRunReport,
    EnqueueReport,
    LegacyApplyReport,
    LegacyMigrationReport,
    PreflightReport,
    apply_legacy_batch,
    dry_run_batch,
    enqueue_history_batch,
    migrate_legacy_batch,
    preflight,
    propose_legacy_status,
    reconciliation_counts,
)
from zira_dashboard.odoo_improvements import (
    ContractError,
    ImprovementContract,
    ImprovementsClient,
    ImprovementsConfig,
    SOURCE_VALUE,
    TARGET_FIELDS,
    TargetInspection,
    TargetIdentityError,
)


MAX_SIGNED_64 = 9_223_372_036_854_775_807
SERVICE_SETTINGS = (
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
    "ODOO_SHARED_REPORTING_WRITE_ENABLED",
    "ODOO_IMPROVEMENTS_WRITE_ENABLED",
    "ODOO_IMPROVEMENTS_CANARY_FEEDBACK_ID",
)


@pytest.fixture(autouse=True)
def disable_real_services(monkeypatch):
    for name in SERVICE_SETTINGS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("DATABASE_URL", "")
    monkeypatch.setenv("FEEDBACK_SYNC_TEST_DATABASE", "")
    monkeypatch.setenv("ODOO_SHARED_REPORTING_WRITE_ENABLED", "false")
    monkeypatch.setenv("ODOO_IMPROVEMENTS_WRITE_ENABLED", "false")


def aware_now() -> datetime:
    return datetime(2026, 8, 20, 18, 0, tzinfo=UTC)


def verified_attempt(
    *,
    manifest: dict[str, object],
    manifest_digest: str,
    binaries: dict[str, object],
    projection_version: int = 3,
    remote_id: int = 901,
    attempt_id: UUID = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"),
) -> sync_store.Attempt:
    settled = aware_now()
    return sync_store.Attempt(
        attempt_id=attempt_id,
        feedback_id=17,
        projection_version=projection_version,
        mutation_kind="update",
        remote_id=remote_id,
        manifest=manifest,
        manifest_digest=manifest_digest,
        binaries=binaries,
        state="verified",
        dispatch_marked_at=settled - timedelta(seconds=3),
        rpc_succeeded_at=settled - timedelta(seconds=2),
        readback_at=settled,
        settled_at=settled,
        created_at=settled - timedelta(seconds=4),
        updated_at=settled,
    )


def normalized(role: str = "before") -> NormalizedImage:
    raw = f"safe-{role}".encode()
    return NormalizedImage(
        jpeg_bytes=raw,
        sha256=hashlib.sha256(raw).hexdigest(),
        byte_length=len(raw),
        width=12,
        height=8,
    )


def local_row(feedback_id: int, **changes) -> dict[str, object]:
    row: dict[str, object] = {
        "id": feedback_id,
        "message": f"private feedback message {feedback_id}",
        "task_type": "bug",
        "created_at": aware_now(),
        "submitter": None,
        "status": "requested",
        "finished_at": None,
        "finished_by": None,
        "resolution_note": None,
        "projection_version": 3,
        "lifecycle_origin": "local",
        "legacy_lifecycle_migrated_at": None,
        "updated_at": aware_now(),
        "odoo_task_id": None,
        "odoo_improvement_id": None,
    }
    row.update(changes)
    return row


def legacy_row(feedback_id: int, task_id: int, **changes) -> dict[str, object]:
    row = local_row(
        feedback_id,
        status=None,
        lifecycle_origin=None,
        projection_version=1,
        odoo_task_id=task_id,
    )
    row.update(changes)
    return row


def good_inspection(**changes) -> TargetInspection:
    values = {
        "database_uuid_matches": True,
        "company_matches": True,
        "missing_fields": (),
        "wrong_types": (),
        "wrong_relations": (),
        "readonly_fields": (),
        "missing_selections": (),
        "wrong_selections": (),
        "source_value_present": True,
        "start_type": "date",
        "stop_type": "datetime",
    }
    values.update(changes)
    return TargetInspection(**values)


class FakeClient:
    def __init__(self, *, inspection: TargetInspection | None = None):
        self.inspection = inspection or good_inspection()
        self.events: list[tuple] = []
        self.stage_rows: list[dict] = []
        self.compound: dict[str, list[dict]] = {}
        self.employees: dict[str, list[dict]] = {}
        self.create_improvement = MagicMock(side_effect=AssertionError("dry-run create attempted"))
        self.write_improvement = MagicMock(side_effect=AssertionError("dry-run write attempted"))

    def inspect_target(self):
        self.events.append(("inspect_target",))
        return self.inspection

    def read_legacy_task_stages(self, task_ids):
        self.events.append(("read_legacy_task_stages", tuple(task_ids)))
        return [dict(row) for row in self.stage_rows]

    def find_employees_by_email(self, email, *, limit):
        self.events.append(("find_employees_by_email", email, limit))
        return [dict(row) for row in self.employees.get(email, [])]

    def find_exact(self, source_id):
        self.events.append(("find_exact", source_id))
        return [dict(row) for row in self.compound.get(source_id, [])]


@pytest.mark.parametrize(
    ("stage", "expected"),
    [
        ("New", "requested"),
        ("Waiting", "requested"),
        ("In Progress", "in_progress"),
        ("Done", "completed"),
        ("Rejected", "declined"),
        ("new", None),
        ("Done ", None),
        ("Canceled", None),
        (None, None),
        (False, None),
        (7, None),
    ],
)
def test_legacy_stage_mapping_is_exact_and_unknown_values_are_omitted(stage, expected):
    assert propose_legacy_status(stage) == expected


def test_preflight_returns_detached_partial_safe_diagnostics():
    client = FakeClient(
        inspection=good_inspection(
            database_uuid_matches=False,
            company_matches=False,
            missing_fields=("x_studio_notes",),
            wrong_types=("x_studio_date_start",),
            wrong_relations=("x_studio_completed_by",),
            readonly_fields=("x_name",),
            missing_selections=("x_studio_status:Declined",),
            wrong_selections=("x_studio_type",),
            source_value_present=False,
            start_type=None,
        )
    )

    report = preflight(client)

    assert report == PreflightReport(
        database_uuid_matches=False,
        company_matches=False,
        fields_ok=False,
        missing_fields=("x_studio_notes",),
        wrong_types=("x_studio_date_start",),
        missing_selections=("x_studio_status:Declined",),
        source_value_present=False,
        wrong_relations=("x_studio_completed_by",),
        readonly_fields=("x_name",),
        wrong_selections=("x_studio_type",),
    )
    assert report.required_source_value == SOURCE_VALUE
    assert dataclasses.is_dataclass(report)
    with pytest.raises(dataclasses.FrozenInstanceError):
        report.fields_ok = True
    serialized = repr(report)
    assert "actual-uuid" not in serialized
    assert "Actual Company" not in serialized


def test_readback_diagnostic_uses_saved_binary_and_outputs_only_mismatch_names(
    monkeypatch,
):
    image = normalized("before")
    fields = {
        "x_name": "saved private feedback",
        "x_studio_source_id": "GPI-PM-FB-17",
        "x_studio_source": SOURCE_VALUE,
        "x_studio_date_start": "2026-08-20",
        "x_studio_type": "Digital",
        "x_studio_status": "Requested",
    }
    binaries = {
        "x_studio_image": {
            "sha256": image.sha256,
            "byte_length": image.byte_length,
        }
    }
    manifest = {"fields": fields, "binary_evidence": binaries}
    digest = hashlib.sha256(
        json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    attempt = sync_store.Attempt(
        attempt_id=UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"),
        feedback_id=17,
        projection_version=3,
        mutation_kind="create",
        remote_id=901,
        manifest=manifest,
        manifest_digest=digest,
        binaries=binaries,
        state="ambiguous",
        dispatch_marked_at=aware_now() - timedelta(seconds=2),
        rpc_succeeded_at=aware_now() - timedelta(seconds=1),
        outcome_detail="readback_mismatch",
        created_at=aware_now() - timedelta(seconds=3),
        updated_at=aware_now(),
    )
    evidence = sync_store.QuarantinedReadbackEvidence(
        attempt_id=attempt.attempt_id,
        feedback_id=17,
        projection_version=3,
        remote_id=901,
        state="quarantined",
        reason="readback_mismatch",
        attempt=attempt,
    )
    events = []
    local_read = MagicMock(
        side_effect=lambda value: events.append(("local", value)) or evidence
    )
    monkeypatch.setattr(sync_store, "load_quarantined_readback_evidence", local_read)
    disposition = MagicMock(side_effect=AssertionError("disposition attempted"))
    monkeypatch.setattr(sync_store, "apply_quarantine_disposition", disposition)
    image_read = MagicMock(
        side_effect=lambda feedback_id, saved: events.append(
            ("image", feedback_id, tuple(sorted(saved)))
        )
        or MappingProxyType({"x_studio_image": image})
    )
    monkeypatch.setattr(feedback_store, "attempt_image_snapshot", image_read)

    class DiagnosticClient:
        create_improvement = MagicMock(side_effect=AssertionError("mutation attempted"))
        write_improvement = MagicMock(side_effect=AssertionError("mutation attempted"))

        def inspect_target(self):
            events.append(("preflight",))
            return good_inspection()

        def read_improvement(self, remote_id, read_fields, *, full_binary):
            events.append(("read", remote_id, tuple(read_fields), full_binary))
            return {
                "id": remote_id,
                **fields,
                "x_name": "different private feedback",
                "x_studio_image": "ZGlmZmVyZW50LXByaXZhdGUtaW1hZ2U=",
            }

    client = DiagnosticClient()
    report = cli.rollout.readback_diagnostic(
        attempt_id=attempt.attempt_id,
        client=client,
    )

    assert report == cli.rollout.ReadbackDiagnosticReport(
        attempt_id=attempt.attempt_id,
        feedback_id=17,
        projection_version=3,
        remote_id=901,
        state="quarantined",
        reason="readback_mismatch",
        mismatched_fields=("x_name", "x_studio_image"),
    )
    assert events[0] == ("preflight",)
    assert events[1] == ("local", attempt.attempt_id)
    assert events[2] == ("image", 17, ("x_studio_image",))
    assert events[3] == (
        "read",
        901,
        tuple(sorted({*fields, "x_studio_image"})),
        True,
    )
    assert events[4] == ("local", attempt.attempt_id)
    client.create_improvement.assert_not_called()
    client.write_improvement.assert_not_called()
    disposition.assert_not_called()
    serialized = repr(report)
    for private in (
        "saved private feedback",
        "different private feedback",
        "different-private-image",
        image.sha256,
    ):
        assert private not in serialized


@pytest.mark.parametrize(
    "inspection",
    [
        good_inspection(database_uuid_matches=False),
        good_inspection(company_matches=False),
        good_inspection(missing_fields=("x_studio_image",)),
        good_inspection(source_value_present=False),
    ],
)
def test_readback_diagnostic_requires_fresh_green_preflight_before_local_read(
    monkeypatch, inspection
):
    local_read = MagicMock(side_effect=AssertionError("local evidence read attempted"))
    monkeypatch.setattr(sync_store, "load_quarantined_readback_evidence", local_read)
    client = FakeClient(inspection=inspection)

    with pytest.raises(ContractError, match="preflight"):
        cli.rollout.readback_diagnostic(
            attempt_id=UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"),
            client=client,
        )

    assert client.events == [("inspect_target",)]
    local_read.assert_not_called()


def test_readback_diagnostic_report_rejects_nonallowlisted_or_unordered_fields():
    base = {
        "attempt_id": UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"),
        "feedback_id": 17,
        "projection_version": 3,
        "remote_id": 901,
        "state": "quarantined",
        "reason": "readback_mismatch",
    }

    for mismatched_fields in (
        ("private_field",),
        ("x_studio_type", "x_name"),
        ("x_name", "x_name"),
    ):
        with pytest.raises(ValueError):
            cli.rollout.ReadbackDiagnosticReport(
                **base,
                mismatched_fields=mismatched_fields,
            )


def test_readback_diagnostic_rejects_authority_change_after_remote_read(monkeypatch):
    attempt_id = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
    manifest = {
        "fields": {
            "x_name": "private feedback",
            "x_studio_source_id": "GPI-PM-FB-17",
            "x_studio_source": SOURCE_VALUE,
            "x_studio_date_start": "2026-08-20",
            "x_studio_type": "Digital",
            "x_studio_status": "Requested",
        },
        "binary_evidence": {},
    }
    digest = hashlib.sha256(
        json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    attempt = sync_store.Attempt(
        attempt_id=attempt_id,
        feedback_id=17,
        projection_version=3,
        mutation_kind="update",
        remote_id=901,
        manifest=manifest,
        manifest_digest=digest,
        binaries={},
        state="ambiguous",
        dispatch_marked_at=aware_now() - timedelta(seconds=2),
        rpc_succeeded_at=aware_now() - timedelta(seconds=1),
        outcome_detail="readback_mismatch",
        created_at=aware_now() - timedelta(seconds=3),
        updated_at=aware_now(),
    )
    first = sync_store.QuarantinedReadbackEvidence(
        attempt_id=attempt_id,
        feedback_id=17,
        projection_version=3,
        remote_id=901,
        state="quarantined",
        reason="readback_mismatch",
        attempt=attempt,
        attempt_count=0,
    )
    changed = dataclasses.replace(first, attempt_count=1)
    monkeypatch.setattr(
        sync_store,
        "load_quarantined_readback_evidence",
        MagicMock(side_effect=[first, changed]),
    )
    client = MagicMock()
    client.inspect_target.return_value = good_inspection()
    client.read_improvement.return_value = {"id": 901, **attempt.manifest["fields"]}

    with pytest.raises(sync_store.StateTransitionError, match="changed"):
        cli.rollout.readback_diagnostic(attempt_id=attempt_id, client=client)

    client.read_improvement.assert_called_once()


def _contract_fields() -> dict[str, dict[str, object]]:
    fields = {field_name: {"type": "char", "readonly": False} for field_name in TARGET_FIELDS}
    fields.update(
        {
            "x_studio_date_start": {"type": "date", "readonly": False},
            "x_studio_date_stop": {"type": "datetime", "readonly": False},
            "x_studio_submitted_by": {
                "type": "many2one",
                "readonly": False,
                "relation": "hr.employee",
            },
            "x_studio_completed_by": {
                "type": "many2one",
                "readonly": False,
                "relation": "hr.employee",
            },
            "x_studio_notes": {"type": "html", "readonly": False},
            "x_studio_image": {"type": "binary", "readonly": False},
            "x_studio_after_image": {"type": "binary", "readonly": False},
            "x_studio_source": {
                "type": "selection",
                "readonly": False,
                "selection": [[SOURCE_VALUE, "Plant"]],
            },
            "x_studio_status": {
                "type": "selection",
                "readonly": False,
                "selection": [
                    ["Requested", "Requested"],
                    ["In-Progress", "In-Progress"],
                    ["Completed", "Completed"],
                    ["Declined", "Declined"],
                ],
            },
            "x_studio_type": {
                "type": "selection",
                "readonly": False,
                "selection": [
                    ["Digital", "Bug"],
                    ["Digital - New Feature", "Feature"],
                    ["Physical", "Physical"],
                ],
            },
        }
    )
    return fields


def test_client_inspection_reports_all_safe_mismatches_without_remote_values():
    fields = _contract_fields()
    fields.pop("x_studio_notes")
    fields["x_studio_date_start"]["type"] = "text-from-remote"
    fields["x_studio_completed_by"]["relation"] = "remote.private.model"
    fields["x_name"]["readonly"] = True
    fields["x_studio_source"]["selection"] = [["Another private source", "Other"]]
    fields["x_studio_status"]["selection"] = [["Requested", "Requested"]]
    fields["x_studio_type"]["selection"].append(["Private", "Private"])
    calls = []

    def executor(model, method, *args, **kwargs):
        calls.append((model, method, args, kwargs))
        if (model, method) == ("ir.config_parameter", "get_param"):
            return "actual-uuid-secret"
        if (model, method) == ("res.users", "read"):
            return [{"id": 4, "company_id": [8, "Actual Company Secret"]}]
        if (model, method) == ("res.company", "read"):
            return [{"id": 8, "name": "Actual Company Secret"}]
        return fields

    client = ImprovementsClient(
        ImprovementsConfig(
            url="https://odoo.invalid",
            database="private-db",
            login="private-login",
            api_key="private-key",
            expected_database_uuid="expected-uuid",
            expected_company="Expected Company",
        ),
        executor,
        uid=4,
    )

    inspection = client.inspect_target()

    assert inspection.database_uuid_matches is False
    assert inspection.company_matches is False
    assert inspection.missing_fields == ("x_studio_notes",)
    assert inspection.wrong_types == ("x_studio_date_start",)
    assert inspection.wrong_relations == ("x_studio_completed_by",)
    assert inspection.readonly_fields == ("x_name",)
    assert inspection.source_value_present is False
    assert "x_studio_status:Declined" in inspection.missing_selections
    assert inspection.wrong_selections == ("x_studio_type",)
    assert inspection.start_type is None
    assert inspection.stop_type == "datetime"
    assert all(
        call[0:2] not in {("x_2s_improvements", "create"), ("x_2s_improvements", "write")}
        for call in calls
    )
    safe = repr(inspection)
    for private in (
        "actual-uuid-secret",
        "Actual Company Secret",
        "text-from-remote",
        "remote.private.model",
        "Another private source",
        "Private",
    ):
        assert private not in safe


def test_client_inspection_reports_wrong_selection_types_without_remote_selection_data():
    fields = _contract_fields()
    fields["x_studio_source"] = {"type": "char", "readonly": False}
    fields["x_studio_status"] = {"type": "integer", "readonly": False}
    fields["x_studio_type"] = {"type": "boolean", "readonly": False}

    def executor(model, method, *args, **kwargs):
        if (model, method) == ("ir.config_parameter", "get_param"):
            return "expected-uuid"
        if (model, method) == ("res.users", "read"):
            return [{"id": 4, "company_id": [8, "Expected Company"]}]
        if (model, method) == ("res.company", "read"):
            return [{"id": 8, "name": "Expected Company"}]
        return fields

    client = ImprovementsClient(
        ImprovementsConfig(
            url="https://odoo.invalid",
            database="database",
            login="login",
            api_key="key",
            expected_database_uuid="expected-uuid",
            expected_company="Expected Company",
        ),
        executor,
        uid=4,
    )

    inspection = client.inspect_target()

    assert inspection.wrong_types == (
        "x_studio_source",
        "x_studio_status",
        "x_studio_type",
    )
    assert inspection.source_value_present is False
    assert inspection.missing_selections == ()


@pytest.mark.parametrize(
    ("field_metadata", "expected_wrong_types", "expected_wrong_relations"),
    [
        ({"type": "char", "readonly": False}, ("x_studio_submitted_by",), ()),
        (
            {"type": "many2one", "readonly": False},
            (),
            ("x_studio_submitted_by",),
        ),
        (
            {"type": "many2one", "readonly": False, "relation": 7},
            (),
            ("x_studio_submitted_by",),
        ),
    ],
)
def test_client_inspection_reports_employee_type_or_relation_without_exposing_metadata(
    field_metadata,
    expected_wrong_types,
    expected_wrong_relations,
):
    fields = _contract_fields()
    fields["x_studio_submitted_by"] = field_metadata

    def executor(model, method, *args, **kwargs):
        if (model, method) == ("ir.config_parameter", "get_param"):
            return "expected-uuid"
        if (model, method) == ("res.users", "read"):
            return [{"id": 4, "company_id": [8, "Expected Company"]}]
        if (model, method) == ("res.company", "read"):
            return [{"id": 8, "name": "Expected Company"}]
        return fields

    client = ImprovementsClient(
        ImprovementsConfig(
            url="https://odoo.invalid",
            database="database",
            login="login",
            api_key="key",
            expected_database_uuid="expected-uuid",
            expected_company="Expected Company",
        ),
        executor,
        uid=4,
    )

    inspection = client.inspect_target()

    assert inspection.wrong_types == expected_wrong_types
    assert inspection.wrong_relations == expected_wrong_relations
    assert "7" not in repr(inspection)


@pytest.mark.parametrize(
    "malformed",
    [None, [], {"x_name": []}, {"x_name": {"type": "char", "readonly": 0}}],
)
def test_client_inspection_fails_closed_on_malformed_contract_metadata(malformed):
    responses = iter(
        [
            "expected-uuid",
            [{"id": 4, "company_id": [8, "Expected Company"]}],
            [{"id": 8, "name": "Expected Company"}],
            malformed,
        ]
    )
    client = ImprovementsClient(
        ImprovementsConfig(
            url="https://odoo.invalid",
            database="database",
            login="login",
            api_key="key",
            expected_database_uuid="expected-uuid",
            expected_company="Expected Company",
        ),
        lambda *_args, **_kwargs: next(responses),
        uid=4,
    )

    with pytest.raises(ContractError):
        client.inspect_target()


def test_dry_run_verifies_target_before_any_local_legacy_or_compound_read(monkeypatch):
    client = FakeClient(inspection=good_inspection(database_uuid_matches=False))
    local_read = MagicMock(side_effect=AssertionError("local read attempted"))
    monkeypatch.setattr(feedback_store, "feedback_after", local_read)

    with pytest.raises(TargetIdentityError):
        dry_run_batch(after_id=0, batch_size=10, client=client)

    assert client.events == [("inspect_target",)]
    local_read.assert_not_called()
    client.create_improvement.assert_not_called()
    client.write_improvement.assert_not_called()


def test_dry_run_is_bounded_read_only_and_emits_no_private_or_remote_data(monkeypatch):
    client = FakeClient()
    client.stage_rows = [{"id": 90, "stage_id": [2, "Done"]}]
    client.compound = {
        "GPI-PM-FB-7": [
            {
                "id": 700,
                "x_studio_source": SOURCE_VALUE,
                "x_studio_source_id": "GPI-PM-FB-7",
            }
        ]
    }
    local_mutation = MagicMock(side_effect=AssertionError("local mutation attempted"))
    warning_mutation = MagicMock(side_effect=AssertionError("warning insert attempted"))
    sync_mutation = MagicMock(side_effect=AssertionError("sync insert attempted"))
    row = legacy_row(
        7,
        90,
        message="private employee issue",
        submitter="bad private email",
    )
    feedback_reads = []
    snapshot_reads = []
    monkeypatch.setattr(
        feedback_store,
        "feedback_after",
        lambda after_id, limit: feedback_reads.append((after_id, limit)) or [row],
    )
    monkeypatch.setattr(
        feedback_store,
        "rollout_snapshot",
        lambda **values: (
            snapshot_reads.append(values)
            or feedback_store.RolloutSnapshot(
                feedback=MappingProxyType(dict(row)),
                images=MappingProxyType({"before": normalized()}),
            )
        ),
    )
    monkeypatch.setattr(feedback_store, "apply_legacy_status", local_mutation)
    monkeypatch.setattr(feedback_store, "record_sync_warning", warning_mutation)
    monkeypatch.setattr(feedback_store, "enqueue_history_batch", sync_mutation)

    report = dry_run_batch(after_id=6, batch_size=999, client=client)

    assert report == DryRunReport(
        requested_batch_size=100,
        feedback_ids=(7,),
        projected_ids=(7,),
        skipped_ids=(),
        create_ids=(),
        adopt_ids=(7,),
        update_ids=(),
        duplicate_ids=(),
        ownership_conflict_ids=(),
        employee_missing_count=1,
        employee_ambiguous_count=0,
        before_image_count=1,
        after_image_count=0,
        next_after_id=7,
    )
    assert feedback_reads == [(6, 100)]
    assert snapshot_reads == [
        {
            "feedback_id": 7,
            "expected_projection_version": 1,
            "expected_odoo_task_id": 90,
        }
    ]
    assert client.events[0] == ("inspect_target",)
    assert client.events[1] == ("read_legacy_task_stages", (90,))
    local_mutation.assert_not_called()
    warning_mutation.assert_not_called()
    sync_mutation.assert_not_called()
    client.create_improvement.assert_not_called()
    client.write_improvement.assert_not_called()
    serialized = repr(report)
    for private in (
        "private employee issue",
        "bad private email",
        "safe-before",
        "GPI-PM-FB-7",
        "700",
    ):
        assert private not in serialized


def test_dry_run_classifies_compound_outcomes_and_counts_employees_and_images(monkeypatch):
    client = FakeClient()
    rows = [
        local_row(1, submitter="missing@example.com"),
        local_row(2, submitter="ambiguous@example.com"),
        local_row(3, odoo_improvement_id=300),
        local_row(4),
        local_row(5, odoo_improvement_id=500),
    ]
    client.employees = {
        "ambiguous@example.com": [
            {"id": 20, "work_email": "ambiguous@example.com"},
            {"id": 21, "work_email": "ambiguous@example.com"},
        ]
    }
    client.compound = {
        "GPI-PM-FB-2": [
            {"id": 200, "x_studio_source": SOURCE_VALUE, "x_studio_source_id": "GPI-PM-FB-2"}
        ],
        "GPI-PM-FB-3": [
            {"id": 300, "x_studio_source": SOURCE_VALUE, "x_studio_source_id": "GPI-PM-FB-3"}
        ],
        "GPI-PM-FB-4": [
            {"id": 400, "x_studio_source": SOURCE_VALUE, "x_studio_source_id": "GPI-PM-FB-4"},
            {"id": 401, "x_studio_source": SOURCE_VALUE, "x_studio_source_id": "GPI-PM-FB-4"},
        ],
        "GPI-PM-FB-5": [
            {"id": 501, "x_studio_source": SOURCE_VALUE, "x_studio_source_id": "GPI-PM-FB-5"}
        ],
    }
    monkeypatch.setattr(feedback_store, "feedback_after", lambda *_args: rows)
    monkeypatch.setattr(
        feedback_store,
        "rollout_snapshot",
        lambda **values: feedback_store.RolloutSnapshot(
            feedback=MappingProxyType(
                dict(next(row for row in rows if row["id"] == values["feedback_id"]))
            ),
            images=MappingProxyType(
                {"before": normalized("before"), "after": normalized("after")}
                if values["feedback_id"] == 3
                else {}
            ),
        ),
    )

    report = dry_run_batch(after_id=0, batch_size=5, client=client)

    assert report.feedback_ids == (1, 2, 3, 4, 5)
    assert report.projected_ids == (1, 2, 3, 4, 5)
    assert report.create_ids == (1,)
    assert report.adopt_ids == (2,)
    assert report.update_ids == (3,)
    assert report.duplicate_ids == (4,)
    assert report.ownership_conflict_ids == (5,)
    assert report.employee_missing_count == 1
    assert report.employee_ambiguous_count == 1
    assert report.before_image_count == 1
    assert report.after_image_count == 1


def test_dry_run_projects_legacy_post_migration_version_but_local_stored_version(
    monkeypatch,
):
    client = FakeClient()
    client.stage_rows = [{"id": 90, "stage_id": [3, "In Progress"]}]
    rows = [legacy_row(7, 90, projection_version=4), local_row(8, projection_version=11)]
    seen = []
    from zira_dashboard import feedback_rollout

    real_build = feedback_rollout.build_projection

    def record_build(feedback, **kwargs):
        seen.append(dict(feedback))
        return real_build(feedback, **kwargs)

    monkeypatch.setattr(feedback_store, "feedback_after", lambda *_args: rows)
    monkeypatch.setattr(
        feedback_store,
        "rollout_snapshot",
        lambda **values: feedback_store.RolloutSnapshot(
            feedback=MappingProxyType(
                dict(next(row for row in rows if row["id"] == values["feedback_id"]))
            ),
            images=MappingProxyType({}),
        ),
    )
    monkeypatch.setattr(feedback_rollout, "build_projection", record_build)

    dry_run_batch(after_id=0, batch_size=2, client=client)

    projected_legacy, projected_local = seen
    assert projected_legacy["status"] == "in_progress"
    assert projected_legacy["lifecycle_origin"] == "legacy_project_task"
    assert projected_legacy["projection_version"] == 5
    assert projected_legacy["finished_at"] is None
    assert projected_legacy["finished_by"] is None
    assert projected_legacy["resolution_note"] is None
    assert projected_local["status"] == "requested"
    assert projected_local["projection_version"] == 11


def test_dry_run_skips_missing_unknown_and_malformed_legacy_stages_without_guessing(
    monkeypatch,
):
    client = FakeClient()
    client.stage_rows = [
        {"id": 90, "stage_id": [1, "Unknown"]},
        {"id": 91, "stage_id": False},
    ]
    rows = [
        legacy_row(1, 90),
        legacy_row(2, 91),
        legacy_row(3, 92),
        local_row(4, status=None, lifecycle_origin=None, odoo_task_id=None),
    ]
    snapshot_read = MagicMock(side_effect=AssertionError("skipped snapshot read"))
    monkeypatch.setattr(feedback_store, "feedback_after", lambda *_args: rows)
    monkeypatch.setattr(feedback_store, "rollout_snapshot", snapshot_read)

    report = dry_run_batch(after_id=0, batch_size=4, client=client)

    assert report.feedback_ids == (1, 2, 3, 4)
    assert report.projected_ids == ()
    assert report.skipped_ids == (1, 2, 3, 4)
    assert report.next_after_id == 4
    snapshot_read.assert_not_called()


def test_dry_run_uses_locked_snapshot_feedback_not_stale_page_data(monkeypatch):
    client = FakeClient()
    page_row = local_row(7, odoo_improvement_id=None)
    snapshot_row = local_row(7, odoo_improvement_id=700, message="new locked value")
    client.compound = {
        "GPI-PM-FB-7": [
            {
                "id": 700,
                "x_studio_source": SOURCE_VALUE,
                "x_studio_source_id": "GPI-PM-FB-7",
            }
        ]
    }
    monkeypatch.setattr(feedback_store, "feedback_after", lambda *_args: [page_row])
    monkeypatch.setattr(
        feedback_store,
        "rollout_snapshot",
        lambda **_values: feedback_store.RolloutSnapshot(
            feedback=MappingProxyType(snapshot_row),
            images=MappingProxyType({}),
        ),
    )

    report = dry_run_batch(after_id=0, batch_size=1, client=client)

    assert report.update_ids == (7,)
    assert report.adopt_ids == ()


@pytest.mark.parametrize(
    "snapshot_changes",
    [
        {"status": "completed"},
        {"projection_version": 4},
        {"odoo_task_id": 90},
    ],
)
def test_dry_run_fails_closed_if_locked_selection_authority_changed(monkeypatch, snapshot_changes):
    client = FakeClient()
    page_row = local_row(7)
    snapshot_row = local_row(7, **snapshot_changes)
    monkeypatch.setattr(feedback_store, "feedback_after", lambda *_args: [page_row])
    monkeypatch.setattr(
        feedback_store,
        "rollout_snapshot",
        lambda **_values: feedback_store.RolloutSnapshot(
            feedback=MappingProxyType(snapshot_row),
            images=MappingProxyType({}),
        ),
    )

    with pytest.raises(ValueError, match="selection authority"):
        dry_run_batch(after_id=0, batch_size=1, client=client)

    assert all(event[0] != "find_exact" for event in client.events)


@pytest.mark.parametrize("batch_size", [True, 1.0, "10", None])
def test_dry_run_rejects_non_exact_integer_batch_sizes_before_client_or_database(
    monkeypatch, batch_size
):
    client = FakeClient()
    local_read = MagicMock(side_effect=AssertionError("database read"))
    monkeypatch.setattr(feedback_store, "feedback_after", local_read)

    with pytest.raises(ValueError, match="exact integer"):
        dry_run_batch(after_id=0, batch_size=batch_size, client=client)

    assert client.events == []
    local_read.assert_not_called()


@pytest.mark.parametrize("after_id", [True, -1, 1.0, "0", MAX_SIGNED_64 + 1])
def test_dry_run_rejects_invalid_cursor_before_client_or_database(monkeypatch, after_id):
    client = FakeClient()
    monkeypatch.setattr(
        feedback_store,
        "feedback_after",
        MagicMock(side_effect=AssertionError("database read")),
    )

    with pytest.raises(ValueError, match="signed-64"):
        dry_run_batch(after_id=after_id, batch_size=10, client=client)

    assert client.events == []


def test_report_dataclasses_reject_unbounded_malformed_or_overlapping_ids():
    with pytest.raises(ValueError):
        EnqueueReport(feedback_ids=tuple(range(1, 102)), next_cursor=101)
    with pytest.raises(ValueError):
        LegacyApplyReport(applied_ids=(1,), idempotent_ids=(1,), skipped_ids=())
    with pytest.raises(ValueError):
        DryRunReport(
            requested_batch_size=1,
            feedback_ids=(1,),
            projected_ids=(1,),
            skipped_ids=(),
            create_ids=(1,),
            adopt_ids=(1,),
            update_ids=(),
            duplicate_ids=(),
            ownership_conflict_ids=(),
            employee_missing_count=0,
            employee_ambiguous_count=0,
            before_image_count=0,
            after_image_count=0,
            next_after_id=1,
        )


def test_apply_legacy_batch_uses_only_supplied_exact_map_and_reports_outcomes(monkeypatch):
    calls = []
    outcomes = iter([True, False])
    monkeypatch.setattr(
        feedback_store,
        "apply_legacy_status",
        lambda **values: calls.append(values) or next(outcomes),
    )

    report = apply_legacy_batch(
        rows=[
            {"id": 7, "odoo_task_id": 90, "projection_version": 3},
            {"id": 8, "odoo_task_id": 91, "projection_version": 5},
            {"id": 9, "odoo_task_id": 92, "projection_version": 9},
        ],
        stages={90: "Done", 91: "Waiting", 92: "Unknown"},
        now=aware_now(),
    )

    assert report == LegacyApplyReport(applied_ids=(7,), idempotent_ids=(8,), skipped_ids=(9,))
    assert calls == [
        {
            "feedback_id": 7,
            "expected_odoo_task_id": 90,
            "expected_projection_version": 3,
            "status": "completed",
            "now": aware_now(),
        },
        {
            "feedback_id": 8,
            "expected_odoo_task_id": 91,
            "expected_projection_version": 5,
            "status": "requested",
            "now": aware_now(),
        },
    ]
    assert all("lifecycle_origin" not in call for call in calls)
    assert all("finished_at" not in call for call in calls)


def test_migrate_legacy_batch_inspects_then_reads_only_unmigrated_exact_tasks(monkeypatch):
    client = FakeClient()
    client.stage_rows = [
        {"id": 90, "stage_id": [2, "Done"]},
        {"id": 91, "stage_id": [3, "Unknown"]},
    ]
    rows = [
        legacy_row(7, 90),
        legacy_row(8, 91),
        local_row(9, odoo_task_id=92),
        local_row(10, status=None, lifecycle_origin=None, odoo_task_id=None),
    ]
    store_calls = []
    monkeypatch.setattr(feedback_store, "feedback_after", lambda *_args: rows)
    monkeypatch.setattr(
        feedback_store,
        "apply_legacy_status",
        lambda **values: store_calls.append(values) or True,
    )
    monkeypatch.setattr(
        feedback_store,
        "rollout_snapshot",
        MagicMock(side_effect=AssertionError("migration snapshot attempted")),
    )

    report = migrate_legacy_batch(
        after_id=6,
        batch_size=999,
        client=client,
        now=aware_now(),
    )

    assert report == LegacyMigrationReport(
        selected_ids=(7, 8, 9, 10),
        applied_ids=(7,),
        idempotent_ids=(),
        skipped_ids=(8, 9, 10),
        next_after_id=10,
    )
    assert report.selected_count == 4
    assert report.applied_count == 1
    assert report.idempotent_count == 0
    assert report.skipped_count == 3
    assert client.events == [
        ("inspect_target",),
        ("read_legacy_task_stages", (90, 91)),
    ]
    assert store_calls == [
        {
            "feedback_id": 7,
            "expected_odoo_task_id": 90,
            "expected_projection_version": 1,
            "status": "completed",
            "now": aware_now(),
        }
    ]
    assert "private feedback" not in repr(report)


def test_migrate_legacy_batch_reports_exact_idempotence_and_empty_cursor(monkeypatch):
    client = FakeClient()
    client.stage_rows = [{"id": 90, "stage_id": [2, "Waiting"]}]
    responses = iter([[legacy_row(7, 90)], []])
    monkeypatch.setattr(feedback_store, "feedback_after", lambda *_args: next(responses))
    monkeypatch.setattr(feedback_store, "apply_legacy_status", lambda **_values: False)

    first = migrate_legacy_batch(
        after_id=6,
        batch_size=1,
        client=client,
        now=aware_now(),
    )
    second = migrate_legacy_batch(
        after_id=7,
        batch_size=1,
        client=client,
        now=aware_now(),
    )

    assert first.idempotent_ids == (7,)
    assert first.next_after_id == 7
    assert second.selected_ids == ()
    assert second.next_after_id == 7


def test_migrate_legacy_batch_rejects_target_before_local_or_legacy_reads(monkeypatch):
    client = FakeClient(inspection=good_inspection(company_matches=False))
    local_read = MagicMock(side_effect=AssertionError("local read attempted"))
    monkeypatch.setattr(feedback_store, "feedback_after", local_read)

    with pytest.raises(TargetIdentityError):
        migrate_legacy_batch(
            after_id=0,
            batch_size=10,
            client=client,
            now=aware_now(),
        )

    assert client.events == [("inspect_target",)]
    local_read.assert_not_called()


@pytest.mark.parametrize(
    ("after_id", "batch_size", "now"),
    [
        (True, 1, aware_now()),
        (0, True, aware_now()),
        (0, 1, datetime(2026, 8, 20, 18, 0)),
    ],
)
def test_migrate_legacy_batch_rejects_nonexact_inputs_before_inspection(after_id, batch_size, now):
    client = FakeClient()

    with pytest.raises(ValueError):
        migrate_legacy_batch(
            after_id=after_id,
            batch_size=batch_size,
            client=client,
            now=now,
        )

    assert client.events == []


@pytest.mark.parametrize(
    ("rows", "stages"),
    [
        (tuple(), {}),
        ([{"id": True, "odoo_task_id": 1, "projection_version": 1}], {1: "Done"}),
        ([{"id": 1, "odoo_task_id": 1}], {1: "Done"}),
        ([{"id": 1, "odoo_task_id": 1, "projection_version": True}], {1: "Done"}),
        ([{"id": 1, "odoo_task_id": 1, "projection_version": 0}], {1: "Done"}),
        ([{"id": 1, "odoo_task_id": 1, "projection_version": 1.0}], {1: "Done"}),
        (
            [{"id": 1, "odoo_task_id": 1, "projection_version": MAX_SIGNED_64}],
            {1: "Done"},
        ),
        (
            [
                {"id": 1, "odoo_task_id": 1, "projection_version": 1},
                {"id": 1, "odoo_task_id": 2, "projection_version": 1},
            ],
            {1: "Done"},
        ),
        (
            [
                {"id": 1, "odoo_task_id": 1, "projection_version": 1},
                {"id": 2, "odoo_task_id": 1, "projection_version": 1},
            ],
            {1: "Done"},
        ),
        ([{"id": 1, "odoo_task_id": 1, "projection_version": 1}] * 101, {1: "Done"}),
        ([{"id": 1, "odoo_task_id": 1, "projection_version": 1}], {True: "Done"}),
        ([{"id": 1, "odoo_task_id": 1, "projection_version": 1}], {1: 7}),
    ],
)
def test_apply_legacy_batch_rejects_malformed_or_duplicate_inputs_before_writes(
    monkeypatch, rows, stages
):
    mutation = MagicMock(side_effect=AssertionError("mutation attempted"))
    monkeypatch.setattr(feedback_store, "apply_legacy_status", mutation)

    with pytest.raises(ValueError):
        apply_legacy_batch(rows=rows, stages=stages, now=aware_now())

    mutation.assert_not_called()


class ScriptCursor:
    def __init__(self, script):
        self.script = iter(script)
        self.calls = []
        self.current = None
        self.closed = False

    def execute(self, sql, params):
        self.calls.append((" ".join(sql.split()), params))
        self.current = next(self.script)
        if isinstance(self.current, Exception):
            raise self.current

    def fetchone(self):
        value = self.current
        self.current = None
        return value

    def fetchall(self):
        value = self.current
        self.current = None
        return value

    def close(self):
        self.closed = True


class FakeConnection:
    def __init__(self, cursor):
        self.scripted_cursor = cursor
        self.commits = 0
        self.rollbacks = 0

    def cursor(self, *, cursor_factory=None):
        return self.scripted_cursor

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


class FakePool:
    def __init__(self, connection):
        self.connection = connection
        self.returned = []

    def getconn(self):
        return self.connection

    def putconn(self, connection):
        self.returned.append(connection)


def install_cursor(monkeypatch, module, script):
    cursor = ScriptCursor(script)
    transactions = []

    @contextmanager
    def fake_cursor():
        transactions.append(cursor)
        yield cursor

    monkeypatch.setattr(module.db, "cursor", fake_cursor)
    return cursor, transactions


def rollout_image_row(feedback_id: int, role: str) -> dict[str, object]:
    image = normalized(role)
    return {
        "feedback_id": feedback_id,
        "role": role,
        "jpeg_bytes": image.jpeg_bytes,
        "sha256": image.sha256,
        "byte_length": image.byte_length,
        "width": image.width,
        "height": image.height,
    }


def rollout_image_metadata(role: str, **changes) -> dict[str, object]:
    image = normalized(role)
    metadata: dict[str, object] = {
        "role": role,
        "byte_length": image.byte_length,
        "stored_byte_length": image.byte_length,
        "width": image.width,
        "height": image.height,
    }
    metadata.update(changes)
    return metadata


def test_rollout_snapshot_locks_feedback_then_reads_server_bounded_images_in_one_tx(
    monkeypatch,
):
    source_feedback = local_row(7)
    source_bytes = bytearray(normalized("before").jpeg_bytes)
    image_row = rollout_image_row(7, "before")
    image_row["jpeg_bytes"] = source_bytes
    cursor, transactions = install_cursor(
        monkeypatch,
        feedback_store,
        [source_feedback, [rollout_image_metadata("before")], [image_row]],
    )

    snapshot = feedback_store.rollout_snapshot(
        feedback_id=7,
        expected_projection_version=3,
        expected_odoo_task_id=None,
    )

    assert transactions == [cursor]
    assert len(cursor.calls) == 3
    feedback_sql, feedback_params = cursor.calls[0]
    metadata_sql, metadata_params = cursor.calls[1]
    bytes_sql, bytes_params = cursor.calls[2]
    assert "LEFT JOIN feedback_odoo_sync" in feedback_sql
    assert "f.id = %s" in feedback_sql
    assert "f.projection_version = %s" in feedback_sql
    assert "f.odoo_task_id IS NOT DISTINCT FROM %s" in feedback_sql
    assert "FOR SHARE OF f" in feedback_sql
    assert feedback_params == (7, 3, None)
    assert "SELECT role, byte_length, width, height" in metadata_sql
    assert "octet_length(jpeg_bytes) AS stored_byte_length" in metadata_sql
    assert "ORDER BY role LIMIT 3" in metadata_sql
    assert metadata_params == (7,)
    assert "role = ANY(%s)" in bytes_sql
    assert "byte_length > 0" in bytes_sql
    assert "octet_length(jpeg_bytes) > 0" in bytes_sql
    assert "octet_length(jpeg_bytes) <= %s" in bytes_sql
    assert "octet_length(jpeg_bytes) = byte_length" in bytes_sql
    assert "ORDER BY role LIMIT 2" in bytes_sql
    assert bytes_params == (
        7,
        ["before"],
        MAX_OUTPUT_BYTES,
        MAX_OUTPUT_BYTES,
    )
    source_feedback["message"] = "changed"
    source_bytes[:] = b"changed"
    assert snapshot.feedback["message"] != "changed"
    assert snapshot.images["before"].jpeg_bytes == normalized("before").jpeg_bytes
    with pytest.raises(TypeError):
        snapshot.feedback["message"] = "changed"
    with pytest.raises(TypeError):
        snapshot.images["before"] = normalized("after")


@pytest.mark.parametrize(
    ("stored", "expected_task"),
    [
        (None, None),
        (local_row(8), None),
        (local_row(7, projection_version=4), None),
        (local_row(7, odoo_task_id=90), None),
        (legacy_row(7, 91), 90),
        (local_row(7, status=None, lifecycle_origin="local"), None),
    ],
)
def test_rollout_snapshot_fails_closed_on_feedback_version_task_or_authority_mismatch(
    monkeypatch, stored, expected_task
):
    cursor, _transactions = install_cursor(monkeypatch, feedback_store, [stored])

    with pytest.raises(feedback_store.ProjectionSnapshotUnavailable):
        feedback_store.rollout_snapshot(
            feedback_id=7,
            expected_projection_version=3 if expected_task is None else 1,
            expected_odoo_task_id=expected_task,
        )

    assert len(cursor.calls) == 1


@pytest.mark.parametrize(
    "metadata_rows",
    [
        [rollout_image_metadata("before")] * 3,
        [rollout_image_metadata("middle")],
        [rollout_image_metadata("before", byte_length=0, stored_byte_length=0)],
        [
            rollout_image_metadata(
                "before",
                byte_length=MAX_OUTPUT_BYTES + 1,
                stored_byte_length=MAX_OUTPUT_BYTES + 1,
            )
        ],
        [rollout_image_metadata("before", stored_byte_length=2)],
        [rollout_image_metadata("before", width=0)],
        [rollout_image_metadata("before"), rollout_image_metadata("after")],
    ],
)
def test_rollout_snapshot_detects_corrupt_duplicate_or_unordered_metadata_before_bytes(
    monkeypatch, metadata_rows
):
    cursor, _transactions = install_cursor(
        monkeypatch,
        feedback_store,
        [local_row(7), metadata_rows],
    )

    with pytest.raises(feedback_store.ProjectionSnapshotUnavailable):
        feedback_store.rollout_snapshot(
            feedback_id=7,
            expected_projection_version=3,
            expected_odoo_task_id=None,
        )

    assert len(cursor.calls) == 2


@pytest.mark.parametrize(
    "byte_rows",
    [
        [],
        [rollout_image_row(7, "after")],
        [rollout_image_row(7, "before"), rollout_image_row(7, "before")],
        [rollout_image_row(8, "before")],
    ],
)
def test_rollout_snapshot_rejects_missing_duplicate_or_mismatched_bounded_bytes(
    monkeypatch, byte_rows
):
    cursor, _transactions = install_cursor(
        monkeypatch,
        feedback_store,
        [local_row(7), [rollout_image_metadata("before")], byte_rows],
    )

    with pytest.raises(feedback_store.ProjectionSnapshotUnavailable):
        feedback_store.rollout_snapshot(
            feedback_id=7,
            expected_projection_version=3,
            expected_odoo_task_id=None,
        )

    assert len(cursor.calls) == 3


def test_store_apply_legacy_status_updates_and_enqueues_in_one_short_transaction(monkeypatch):
    cursor, transactions = install_cursor(
        monkeypatch,
        feedback_store,
        [
            {"projection_version": 4},
            [{"feedback_id": 7, "desired_version": 4}],
        ],
    )

    applied = feedback_store.apply_legacy_status(
        feedback_id=7,
        expected_odoo_task_id=90,
        expected_projection_version=3,
        status="completed",
        now=aware_now(),
    )

    assert applied is True
    assert transactions == [cursor]
    assert len(cursor.calls) == 2
    update_sql, update_params = cursor.calls[0]
    sync_sql, sync_params = cursor.calls[1]
    set_clause = update_sql.split("SET", 1)[1].split("WHERE", 1)[0]
    assert "status = %s" in set_clause
    assert "lifecycle_origin = 'legacy_project_task'" in set_clause
    assert "legacy_lifecycle_migrated_at = %s" in set_clause
    assert "projection_version = projection_version + 1" in set_clause
    assert "updated_at = %s" in set_clause
    assert not {"finished_at", "finished_by", "resolution_note"} & set(set_clause.split())
    assert "status IS NULL" in update_sql
    assert "lifecycle_origin IS NULL" in update_sql
    assert "odoo_task_id = %s" in update_sql
    assert "projection_version = %s" in update_sql.split("WHERE", 1)[1]
    assert "RETURNING projection_version" in update_sql
    assert update_params == ("completed", aware_now(), aware_now(), 7, 90, 3)
    assert "INSERT INTO feedback_odoo_sync" in sync_sql
    assert "GREATEST" in sync_sql
    conflict_update = sync_sql.split("DO UPDATE SET", 1)[1]
    for preserved in (
        "state",
        "attempt_count",
        "claim_owner",
        "claim_token",
        "claim_expires_at",
        "active_attempt_id",
        "odoo_improvement_id",
        "last_synced_version",
        "quarantine_reason",
        "quarantined_at",
        "due_at",
    ):
        assert preserved not in conflict_update
    assert "RETURNING feedback_id, desired_version" in sync_sql
    assert sync_params == (7, 4, aware_now())


@pytest.mark.parametrize(
    "sync_rows",
    [
        None,
        [],
        [
            {"feedback_id": 7, "desired_version": 4},
            {"feedback_id": 7, "desired_version": 4},
        ],
        [{"feedback_id": 8, "desired_version": 4}],
        [{"feedback_id": 7, "desired_version": True}],
        [{"feedback_id": 7, "desired_version": 3}],
        [{"feedback_id": 7, "desired_version": 5}],
    ],
)
def test_store_apply_legacy_status_rejects_malformed_or_lagging_sync_upsert(monkeypatch, sync_rows):
    cursor, _transactions = install_cursor(
        monkeypatch,
        feedback_store,
        [{"projection_version": 4}, sync_rows],
    )

    with pytest.raises(feedback_store.InvalidTransition):
        feedback_store.apply_legacy_status(
            feedback_id=7,
            expected_odoo_task_id=90,
            expected_projection_version=3,
            status="completed",
            now=aware_now(),
        )

    assert len(cursor.calls) == 2


@pytest.mark.parametrize("task_id", [None, True, 0, -1, 1.0, MAX_SIGNED_64 + 1])
def test_store_apply_legacy_status_rejects_invalid_expected_task_before_transaction(
    monkeypatch, task_id
):
    monkeypatch.setattr(
        feedback_store.db,
        "cursor",
        MagicMock(side_effect=AssertionError("transaction opened")),
    )

    with pytest.raises(ValueError, match="legacy task id"):
        feedback_store.apply_legacy_status(
            feedback_id=7,
            expected_odoo_task_id=task_id,
            expected_projection_version=3,
            status="completed",
            now=aware_now(),
        )


@pytest.mark.parametrize(
    "version",
    [None, True, 0, -1, 1.0, MAX_SIGNED_64, MAX_SIGNED_64 + 1],
)
def test_store_apply_legacy_status_rejects_invalid_expected_version_before_transaction(
    monkeypatch, version
):
    monkeypatch.setattr(
        feedback_store.db,
        "cursor",
        MagicMock(side_effect=AssertionError("transaction opened")),
    )

    with pytest.raises(ValueError, match="projection version"):
        feedback_store.apply_legacy_status(
            feedback_id=7,
            expected_odoo_task_id=90,
            expected_projection_version=version,
            status="completed",
            now=aware_now(),
        )


@pytest.mark.parametrize("returned_version", [3, 5, True, None])
def test_store_apply_legacy_status_requires_returned_version_to_be_exact_successor(
    monkeypatch, returned_version
):
    cursor, _transactions = install_cursor(
        monkeypatch,
        feedback_store,
        [{"projection_version": returned_version}],
    )

    with pytest.raises(feedback_store.InvalidTransition):
        feedback_store.apply_legacy_status(
            feedback_id=7,
            expected_odoo_task_id=90,
            expected_projection_version=3,
            status="completed",
            now=aware_now(),
        )

    assert len(cursor.calls) == 1


def test_store_apply_legacy_status_returns_explicit_idempotent_without_sync_rewrite(
    monkeypatch,
):
    cursor, _transactions = install_cursor(
        monkeypatch,
        feedback_store,
        [
            None,
            {
                "odoo_task_id": 90,
                "status": "completed",
                "lifecycle_origin": "legacy_project_task",
                "legacy_lifecycle_migrated_at": aware_now(),
                "updated_at": aware_now(),
                "projection_version": 4,
                "sync_feedback_id": 7,
                "desired_version": 4,
                "finished_at": None,
                "finished_by": None,
                "resolution_note": None,
            },
        ],
    )

    applied = feedback_store.apply_legacy_status(
        feedback_id=7,
        expected_odoo_task_id=90,
        expected_projection_version=3,
        status="completed",
        now=aware_now(),
    )

    assert applied is False
    assert len(cursor.calls) == 2
    assert "SELECT f.odoo_task_id, f.status, f.lifecycle_origin" in cursor.calls[1][0]
    assert "LEFT JOIN feedback_odoo_sync" in cursor.calls[1][0]
    assert all(
        not sql.startswith(("INSERT INTO feedback_odoo_sync", "UPDATE feedback_odoo_sync"))
        for sql, _params in cursor.calls
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"odoo_task_id": 91},
        {"status": "requested"},
        {"lifecycle_origin": "local"},
        {"legacy_lifecycle_migrated_at": None},
        {"legacy_lifecycle_migrated_at": datetime(2026, 8, 20, 18, 0)},
        {"updated_at": None},
        {"updated_at": datetime(2026, 8, 20, 18, 0)},
        {"updated_at": datetime(2026, 8, 20, 19, 0, tzinfo=UTC)},
        {"projection_version": True},
        {"projection_version": 0},
        {"projection_version": 3},
        {"projection_version": 5},
        {"sync_feedback_id": None},
        {"sync_feedback_id": 8},
        {"desired_version": None},
        {"desired_version": True},
        {"desired_version": 3},
        {"desired_version": 5},
        {"finished_at": aware_now()},
        {"finished_by": "person@example.com"},
        {"resolution_note": "drift"},
    ],
)
def test_store_apply_legacy_status_rejects_nonexact_idempotent_state(monkeypatch, changes):
    existing = {
        "odoo_task_id": 90,
        "status": "completed",
        "lifecycle_origin": "legacy_project_task",
        "legacy_lifecycle_migrated_at": aware_now(),
        "updated_at": aware_now(),
        "projection_version": 4,
        "sync_feedback_id": 7,
        "desired_version": 4,
        "finished_at": None,
        "finished_by": None,
        "resolution_note": None,
    }
    existing.update(changes)
    cursor, _transactions = install_cursor(
        monkeypatch,
        feedback_store,
        [None, existing],
    )

    with pytest.raises(feedback_store.InvalidTransition):
        feedback_store.apply_legacy_status(
            feedback_id=7,
            expected_odoo_task_id=90,
            expected_projection_version=3,
            status="completed",
            now=aware_now(),
        )

    assert len(cursor.calls) == 2
    assert all(
        not sql.startswith(("INSERT INTO feedback_odoo_sync", "UPDATE feedback_odoo_sync"))
        for sql, _params in cursor.calls
    )


def test_store_apply_legacy_status_rejects_stale_unmigrated_projection_without_writes(
    monkeypatch,
):
    cursor, _transactions = install_cursor(
        monkeypatch,
        feedback_store,
        [
            None,
            {
                "odoo_task_id": 90,
                "status": None,
                "lifecycle_origin": None,
                "legacy_lifecycle_migrated_at": None,
                "updated_at": aware_now(),
                "projection_version": 3,
                "sync_feedback_id": None,
                "desired_version": None,
                "finished_at": None,
                "finished_by": None,
                "resolution_note": None,
            },
        ],
    )

    with pytest.raises(feedback_store.InvalidTransition):
        feedback_store.apply_legacy_status(
            feedback_id=7,
            expected_odoo_task_id=90,
            expected_projection_version=1,
            status="completed",
            now=aware_now(),
        )

    assert len(cursor.calls) == 2
    assert all("INSERT INTO feedback_odoo_sync" not in sql for sql, _params in cursor.calls)


def test_store_apply_legacy_status_rejects_stale_migrated_projection_as_nonidempotent(
    monkeypatch,
):
    cursor, _transactions = install_cursor(
        monkeypatch,
        feedback_store,
        [
            None,
            {
                "odoo_task_id": 90,
                "status": "completed",
                "lifecycle_origin": "legacy_project_task",
                "legacy_lifecycle_migrated_at": aware_now(),
                "updated_at": aware_now(),
                "projection_version": 4,
                "sync_feedback_id": 7,
                "desired_version": 4,
                "finished_at": None,
                "finished_by": None,
                "resolution_note": None,
            },
        ],
    )

    with pytest.raises(feedback_store.InvalidTransition):
        feedback_store.apply_legacy_status(
            feedback_id=7,
            expected_odoo_task_id=90,
            expected_projection_version=1,
            status="completed",
            now=aware_now(),
        )

    assert len(cursor.calls) == 2


def test_store_apply_legacy_status_rejects_missing_row_as_conflict(monkeypatch):
    cursor, _transactions = install_cursor(monkeypatch, feedback_store, [None, None])

    with pytest.raises(feedback_store.InvalidTransition):
        feedback_store.apply_legacy_status(
            feedback_id=7,
            expected_odoo_task_id=90,
            expected_projection_version=3,
            status="completed",
            now=aware_now(),
        )

    assert len(cursor.calls) == 2


def test_store_apply_legacy_status_rolls_back_if_sync_upsert_fails(monkeypatch):
    cursor, transactions = install_cursor(
        monkeypatch,
        feedback_store,
        [{"projection_version": 4}, RuntimeError("sync failure")],
    )

    with pytest.raises(RuntimeError, match="sync failure"):
        feedback_store.apply_legacy_status(
            feedback_id=7,
            expected_odoo_task_id=90,
            expected_projection_version=3,
            status="completed",
            now=aware_now(),
        )

    assert transactions == [cursor]
    assert len(cursor.calls) == 2


def test_store_apply_legacy_status_second_write_failure_rolls_back_real_db_cursor(
    monkeypatch,
):
    cursor = ScriptCursor([{"projection_version": 4}, RuntimeError("sync failure")])
    connection = FakeConnection(cursor)
    pool = FakePool(connection)
    monkeypatch.setattr(feedback_store.db, "_pool", pool)

    with pytest.raises(RuntimeError, match="sync failure"):
        feedback_store.apply_legacy_status(
            feedback_id=7,
            expected_odoo_task_id=90,
            expected_projection_version=3,
            status="completed",
            now=aware_now(),
        )

    assert connection.commits == 0
    assert connection.rollbacks == 1
    assert cursor.closed is True
    assert pool.returned == [connection]


def test_store_apply_legacy_status_rejects_conflict_instead_of_partial_success(monkeypatch):
    cursor, _transactions = install_cursor(
        monkeypatch,
        feedback_store,
        [
            None,
            {
                "odoo_task_id": 90,
                "status": "completed",
                "lifecycle_origin": "local",
                "legacy_lifecycle_migrated_at": aware_now(),
                "projection_version": 4,
                "finished_at": None,
                "finished_by": None,
                "resolution_note": None,
            },
        ],
    )

    with pytest.raises(feedback_store.InvalidTransition):
        feedback_store.apply_legacy_status(
            feedback_id=7,
            expected_odoo_task_id=90,
            expected_projection_version=3,
            status="completed",
            now=aware_now(),
        )

    assert all(
        not sql.startswith(("INSERT INTO feedback_odoo_sync", "UPDATE feedback_odoo_sync"))
        for sql, _params in cursor.calls
    )


def test_historical_enqueue_locks_cursor_upserts_eligible_rows_without_state_loss(
    monkeypatch,
):
    cursor, transactions = install_cursor(
        monkeypatch,
        feedback_store,
        [
            {"last_feedback_id": 2},
            [
                {"id": 3, "projection_version": 2},
                {"id": 4, "projection_version": 5},
            ],
            None,
            None,
            {"last_feedback_id": 4},
        ],
    )

    stored = feedback_store.enqueue_history_batch(batch_size=100, now=aware_now())

    assert stored == {"feedback_ids": (3, 4), "next_cursor": 4}
    assert transactions == [cursor]
    assert "FOR UPDATE" in cursor.calls[0][0]
    select_sql, select_params = cursor.calls[1]
    assert "lifecycle_origin IN ('local', 'legacy_project_task')" in select_sql
    assert "status IS NOT NULL" in select_sql
    assert "ORDER BY id" in select_sql
    assert "LIMIT %s" in select_sql
    assert select_params == (2, 100)
    for sync_sql, sync_params in cursor.calls[2:4]:
        assert "INSERT INTO feedback_odoo_sync" in sync_sql
        assert "GREATEST" in sync_sql
        conflict_update = sync_sql.split("DO UPDATE SET", 1)[1]
        assert "desired_version" in conflict_update
        assert "state" not in conflict_update
        assert "quarant" not in conflict_update
        assert "attempt" not in conflict_update
        assert "claim" not in conflict_update
        assert "last_synced_version" not in conflict_update
        assert sync_params[-1] == aware_now()
    cursor_sql, cursor_params = cursor.calls[4]
    assert "last_feedback_id = %s" in cursor_sql
    assert "last_feedback_id = %s" in cursor_sql
    assert cursor_params == (4, aware_now(), 2)


def test_historical_enqueue_empty_batch_preserves_cursor_without_update(monkeypatch):
    cursor, _transactions = install_cursor(
        monkeypatch,
        feedback_store,
        [{"last_feedback_id": 9}, []],
    )

    stored = feedback_store.enqueue_history_batch(batch_size=10, now=aware_now())

    assert stored == {"feedback_ids": (), "next_cursor": 9}
    assert len(cursor.calls) == 2


def test_historical_enqueue_upsert_failure_rolls_back_before_cursor_advance(monkeypatch):
    cursor = ScriptCursor(
        [
            {"last_feedback_id": 2},
            [{"id": 3, "projection_version": 2}],
            RuntimeError("upsert failure"),
        ]
    )
    connection = FakeConnection(cursor)
    pool = FakePool(connection)
    monkeypatch.setattr(feedback_store.db, "_pool", pool)

    with pytest.raises(RuntimeError, match="upsert failure"):
        feedback_store.enqueue_history_batch(batch_size=100, now=aware_now())

    assert len(cursor.calls) == 3
    assert all("feedback_odoo_backfill_state SET" not in sql for sql, _ in cursor.calls)
    assert connection.commits == 0
    assert connection.rollbacks == 1
    assert cursor.closed is True


def test_historical_enqueue_wrapper_validates_and_detaches_store_report(monkeypatch):
    response = {"feedback_ids": (1, 2), "next_cursor": 2}
    monkeypatch.setattr(feedback_store, "enqueue_history_batch", lambda **_kwargs: response)

    report = enqueue_history_batch(batch_size=999, now=aware_now())
    response["feedback_ids"] = (99,)

    assert report == EnqueueReport(feedback_ids=(1, 2), next_cursor=2)


def test_historical_enqueue_wrapper_preserves_public_report_compatibility(monkeypatch):
    stored = EnqueueReport(feedback_ids=(3, 4), next_cursor=4)
    monkeypatch.setattr(feedback_store, "enqueue_history_batch", lambda **_kwargs: stored)

    report = enqueue_history_batch(batch_size=100, now=aware_now())

    assert report == stored
    assert report is not stored


@pytest.mark.parametrize(
    "stored",
    [
        None,
        [],
        {"feedback_ids": [1], "next_cursor": 1},
        {"feedback_ids": (2, 1), "next_cursor": 2},
        {"feedback_ids": (1, 1), "next_cursor": 1},
        {"feedback_ids": (1,), "next_cursor": 0},
        {"feedback_ids": (True,), "next_cursor": 1},
    ],
)
def test_historical_enqueue_wrapper_fails_closed_on_malformed_store_report(monkeypatch, stored):
    monkeypatch.setattr(feedback_store, "enqueue_history_batch", lambda **_kwargs: stored)

    with pytest.raises(ValueError):
        enqueue_history_batch(batch_size=10, now=aware_now())


def test_store_reconciliation_uses_one_local_aggregate_query_and_validates_counts(
    monkeypatch,
):
    adapter_row = {
        "synchronized": 3,
        "due": 2,
        "deferred": 4,
        "in_flight": 1,
        "quarantined": 1,
        "version_lag": Decimal("7"),
    }
    cursor, transactions = install_cursor(monkeypatch, feedback_store, [adapter_row])

    result = feedback_store.reconciliation_counts(gates_open=True)

    assert result == {**adapter_row, "version_lag": 7}
    assert type(result["version_lag"]) is int
    assert result is not adapter_row
    assert transactions == [cursor]
    assert len(cursor.calls) == 1
    sql, params = cursor.calls[0]
    assert "FROM feedback_odoo_sync" in sql
    assert "FILTER" in sql
    assert "GREATEST(desired_version - last_synced_version, 0)" in sql
    assert "due_at <= now()" in sql
    assert "state = 'quarantined'" in sql
    assert "state = 'in_flight'" in sql
    assert sql.count("state = 'idle'") == 2
    assert "NOT (gate.gates_open AND due_at <= now())" in sql
    assert params == (True,)


@pytest.mark.parametrize(
    "value",
    [
        Decimal("-1"),
        Decimal("1.5"),
        Decimal(MAX_SIGNED_64 + 1),
        Decimal("NaN"),
        Decimal("Infinity"),
    ],
)
def test_store_reconciliation_rejects_nonintegral_or_unbounded_numeric_lag(monkeypatch, value):
    row = {
        "synchronized": 0,
        "due": 0,
        "deferred": 0,
        "in_flight": 0,
        "quarantined": 0,
        "version_lag": value,
    }
    install_cursor(monkeypatch, feedback_store, [row])

    with pytest.raises(ValueError):
        feedback_store.reconciliation_counts(gates_open=False)


def test_reconciliation_reads_only_exact_gate_names_and_never_builds_a_client(
    monkeypatch,
):
    seen = []
    monkeypatch.setenv("ODOO_SHARED_REPORTING_WRITE_ENABLED", "true")
    monkeypatch.setenv("ODOO_IMPROVEMENTS_WRITE_ENABLED", "TRUE")
    monkeypatch.setenv("SOME_OTHER_GATE", "true")
    monkeypatch.setattr(
        feedback_store,
        "reconciliation_counts",
        lambda gates_open: (
            seen.append(gates_open)
            or {
                "synchronized": 3,
                "due": 0,
                "deferred": 2,
                "in_flight": 1,
                "quarantined": 1,
                "version_lag": 4,
            }
        ),
    )
    monkeypatch.setattr(
        ImprovementsClient,
        "from_env",
        MagicMock(side_effect=AssertionError("client constructed")),
    )

    counts = reconciliation_counts()

    assert seen == [False]
    assert counts == {
        "synchronized": 3,
        "due": 0,
        "deferred": 2,
        "in_flight": 1,
        "quarantined": 1,
        "version_lag": 4,
    }


def test_reconciliation_opens_due_bucket_only_when_both_exact_gates_are_true(
    monkeypatch,
):
    seen = []
    monkeypatch.setenv("ODOO_SHARED_REPORTING_WRITE_ENABLED", "true")
    monkeypatch.setenv("ODOO_IMPROVEMENTS_WRITE_ENABLED", "true")
    monkeypatch.setattr(
        feedback_store,
        "reconciliation_counts",
        lambda gates_open: (
            seen.append(gates_open)
            or {
                "synchronized": 0,
                "due": 1,
                "deferred": 0,
                "in_flight": 0,
                "quarantined": 0,
                "version_lag": 1,
            }
        ),
    )

    assert reconciliation_counts()["due"] == 1
    assert seen == [True]


@pytest.mark.parametrize("value", [-1, True, 1.0, MAX_SIGNED_64 + 1])
def test_reconciliation_rejects_malformed_or_unbounded_counts(monkeypatch, value):
    result = {
        "synchronized": 0,
        "due": 0,
        "deferred": 0,
        "in_flight": 0,
        "quarantined": 0,
        "version_lag": 0,
    }
    result["version_lag"] = value
    monkeypatch.setattr(feedback_store, "reconciliation_counts", lambda _gates: result)

    with pytest.raises(ValueError):
        reconciliation_counts()


def test_feedback_after_uses_exact_bounded_cursor_query_and_detaches_rows(monkeypatch):
    source = [local_row(3), local_row(4)]
    seen = {}
    monkeypatch.setattr(
        feedback_store.db,
        "query",
        lambda sql, params: seen.update(sql=" ".join(sql.split()), params=params) or source,
    )

    rows = feedback_store.feedback_after(after_id=2, limit=100)
    source[0]["message"] = "changed"

    assert rows[0]["message"] != "changed"
    assert rows[0] is not source[0]
    assert "FROM feedback WHERE id > %s ORDER BY id LIMIT %s" in seen["sql"]
    selected = seen["sql"].split("FROM feedback", 1)[0]
    for column in (
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
    ):
        assert column in selected
    assert seen["params"] == (2, 100)


@pytest.mark.parametrize(
    "rows",
    [
        None,
        {},
        [local_row(3), local_row(2)],
        [local_row(3), local_row(3)],
        [local_row(2)],
        [local_row(3, id=True)],
        [local_row(3)] * 101,
    ],
)
def test_feedback_after_fails_closed_on_malformed_unordered_or_duplicate_rows(monkeypatch, rows):
    monkeypatch.setattr(feedback_store.db, "query", lambda _sql, _params: rows)

    with pytest.raises((ValueError, feedback_store.ProjectionSnapshotUnavailable)):
        feedback_store.feedback_after(after_id=2, limit=100)


def test_feedback_after_allows_missing_legacy_task_for_safe_dry_run_omission(monkeypatch):
    row = local_row(3, status=None, lifecycle_origin=None, odoo_task_id=None)
    monkeypatch.setattr(feedback_store.db, "query", lambda _sql, _params: [row])

    assert feedback_store.feedback_after(after_id=2, limit=100) == [row]


def test_no_rollout_test_accidentally_inherits_a_live_service_setting():
    assert os.environ.get("DATABASE_URL") == ""
    assert os.environ.get("FEEDBACK_SYNC_TEST_DATABASE") == ""
    assert os.environ.get("ODOO_SHARED_REPORTING_WRITE_ENABLED") == "false"
    assert os.environ.get("ODOO_IMPROVEMENTS_WRITE_ENABLED") == "false"


def test_rollout_cli_exposes_only_the_exact_planned_subcommands():
    parser = cli.build_parser()
    subparser_action = next(
        action for action in parser._actions if getattr(action, "choices", None)
    )

    assert set(subparser_action.choices) == {
        "preflight",
        "dry-run",
        "migrate-legacy",
        "enqueue-history",
        "reconcile",
        "canary-report",
        "quarantine-list",
        "quarantine-readback-diagnostic",
        "quarantine-disposition",
        "quarantine-release-pre-attempt",
    }

    help_text = parser.format_help().casefold()
    for forbidden in (
        "api-key",
        "password",
        "write-enabled",
        "database-url",
        "expected-company",
        "source-value",
        "remote-id",
    ):
        assert forbidden not in help_text


@pytest.mark.parametrize(
    "argv",
    [
        ["preflight"],
        ["dry-run", "--after-id", "0", "--batch-size", "10"],
        [
            "migrate-legacy",
            "--confirm-local-migration",
            "--after-id",
            "0",
            "--batch-size",
            "10",
        ],
        [
            "migrate-legacy",
            "--confirm-read-only",
            "--after-id",
            "0",
            "--batch-size",
            "10",
        ],
        ["enqueue-history", "--batch-size", "10"],
        ["canary-report", "--feedback-id", "17"],
        [
            "quarantine-release-pre-attempt",
            "--feedback-id",
            "17",
            "--reviewer",
            "Dale Gruber",
            "--confirm-local-release",
        ],
        [
            "quarantine-release-pre-attempt",
            "--feedback-id",
            "17",
            "--reviewer",
            "Dale Gruber",
            "--confirm-read-only",
        ],
    ],
)
def test_cli_acknowledgements_fail_before_time_client_database_or_helper(monkeypatch, argv):
    bomb = MagicMock(side_effect=AssertionError("dependency reached before guard"))
    monkeypatch.setattr(cli, "utc_now", bomb)
    monkeypatch.setattr(cli.ImprovementsClient, "from_env", bomb)
    monkeypatch.setattr(cli.rollout, "preflight", bomb)
    monkeypatch.setattr(cli.rollout, "dry_run_batch", bomb)
    monkeypatch.setattr(cli.rollout, "migrate_legacy_batch", bomb)
    monkeypatch.setattr(cli.rollout, "enqueue_history_batch", bomb)
    monkeypatch.setattr(cli.rollout, "canary_report", bomb)
    monkeypatch.setattr(cli.sync_store, "release_pre_attempt_quarantine", bomb)

    with pytest.raises(SystemExit):
        cli.main(argv)

    bomb.assert_not_called()


@pytest.mark.parametrize(
    ("subcommand", "option", "values"),
    [
        (
            "dry-run",
            "--after-id",
            ["-1", "+1", " 1", "1 ", "1.0", "true", str(MAX_SIGNED_64 + 1)],
        ),
        (
            "dry-run",
            "--batch-size",
            ["0", "101", "-1", "+1", " 1", "1.0", "true", "9" * 5000],
        ),
        (
            "canary-report",
            "--feedback-id",
            ["0", "-1", "+1", " 1", "1.0", "true", str(MAX_SIGNED_64 + 1)],
        ),
    ],
)
def test_cli_rejects_nonexact_or_out_of_range_numbers_before_dependencies(
    monkeypatch, subcommand, option, values
):
    bomb = MagicMock(side_effect=AssertionError("dependency called"))
    monkeypatch.setattr(cli.ImprovementsClient, "from_env", bomb)
    monkeypatch.setattr(cli.rollout, "dry_run_batch", bomb)
    monkeypatch.setattr(cli.rollout, "canary_report", bomb)
    fixed = {
        "dry-run": ["--confirm-read-only", "--after-id", "0", "--batch-size", "10"],
        "canary-report": ["--confirm-read-only", "--feedback-id", "17"],
    }

    for value in values:
        argv = [subcommand, *fixed[subcommand]]
        option_index = argv.index(option)
        argv[option_index + 1] = value
        with pytest.raises(SystemExit):
            cli.main(argv)

    bomb.assert_not_called()


def test_cli_rejects_unplanned_aliases_overrides_and_auto_clear_paths():
    parser = cli.build_parser()
    rejected = (
        ["quarantine", "clear-if-matching", "--attempt-id", str(uuid4())],
        ["preflight", "--confirm-read-only", "--url", "https://example.invalid"],
        ["reconcile", "--database", "other"],
        ["dryrun", "--confirm-read-only", "--after-id", "0", "--batch-size", "10"],
        ["preflight", "--confirm-read"],
        [
            "quarantine-disposition",
            "--attempt-id",
            "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
            "--disposition",
            "keep",
            "--reviewer",
            "Human Operator",
            "--confirm-human",
        ],
    )

    for argv in rejected:
        with pytest.raises(SystemExit):
            parser.parse_args(argv)


def test_cli_parser_failures_never_echo_attacker_controlled_argument_values(capsys):
    attacker_value = "https://private.invalid/api-key-value"

    with pytest.raises(SystemExit):
        cli.main(
            [
                "preflight",
                "--confirm-read-only",
                "--api-key",
                attacker_value,
            ]
        )

    captured = capsys.readouterr()
    assert attacker_value not in captured.out
    assert attacker_value not in captured.err
    assert "api-key-value" not in captured.out
    assert "api-key-value" not in captured.err


def _install_cli_fakes(monkeypatch):
    client = MagicMock(name="dedicated_client")
    monkeypatch.setattr(cli.ImprovementsClient, "from_env", MagicMock(return_value=client))
    monkeypatch.setattr(cli, "utc_now", MagicMock(return_value=aware_now()))
    monkeypatch.setattr(cli.rollout, "preflight", MagicMock())
    monkeypatch.setattr(cli.rollout, "dry_run_batch", MagicMock())
    monkeypatch.setattr(cli.rollout, "migrate_legacy_batch", MagicMock())
    monkeypatch.setattr(cli.rollout, "enqueue_history_batch", MagicMock())
    monkeypatch.setattr(cli.rollout, "reconciliation_counts", MagicMock())
    monkeypatch.setattr(cli.rollout, "canary_report", MagicMock())
    monkeypatch.setattr(cli.rollout, "readback_diagnostic", MagicMock())
    monkeypatch.setattr(cli.sync_store, "list_quarantined", MagicMock())
    monkeypatch.setattr(cli.sync_store, "apply_quarantine_disposition", MagicMock())
    monkeypatch.setattr(cli.sync_store, "release_pre_attempt_quarantine", MagicMock())
    return client


def test_readback_diagnostic_parser_requires_exact_uuid_and_read_only_confirmation():
    attempt_id = "50bf66d9-c8d0-44f0-8e7f-10906330046b"
    args = cli.build_parser().parse_args(
        [
            "quarantine-readback-diagnostic",
            "--attempt-id",
            attempt_id,
            "--confirm-read-only",
        ]
    )

    assert vars(args) == {
        "command": "quarantine-readback-diagnostic",
        "attempt_id": UUID(attempt_id),
        "confirm_read_only": True,
    }
    for malformed in (
        attempt_id.upper(),
        "50bf66d9c8d044f08e7f10906330046b",
        "{50bf66d9-c8d0-44f0-8e7f-10906330046b}",
        "not-a-private-attempt-value",
    ):
        with pytest.raises(SystemExit):
            cli.build_parser().parse_args(
                [
                    "quarantine-readback-diagnostic",
                    "--attempt-id",
                    malformed,
                    "--confirm-read-only",
                ]
            )


def test_readback_diagnostic_cli_requires_confirmation_before_dependencies(monkeypatch):
    bomb = MagicMock(side_effect=AssertionError("dependency reached before guard"))
    monkeypatch.setattr(cli.ImprovementsClient, "from_env", bomb)
    monkeypatch.setattr(cli.rollout, "readback_diagnostic", bomb)

    with pytest.raises(SystemExit):
        cli.main(
            [
                "quarantine-readback-diagnostic",
                "--attempt-id",
                "50bf66d9-c8d0-44f0-8e7f-10906330046b",
            ]
        )

    bomb.assert_not_called()


def test_readback_diagnostic_cli_emits_only_bounded_authority_and_field_names(
    monkeypatch, capsys
):
    client = _install_cli_fakes(monkeypatch)
    attempt_id = UUID("50bf66d9-c8d0-44f0-8e7f-10906330046b")
    cli.rollout.readback_diagnostic.return_value = cli.rollout.ReadbackDiagnosticReport(
        attempt_id=attempt_id,
        feedback_id=44,
        projection_version=2,
        remote_id=234,
        state="quarantined",
        reason="readback_mismatch",
        mismatched_fields=("x_studio_image",),
    )

    assert (
        cli.main(
            [
                "quarantine-readback-diagnostic",
                "--attempt-id",
                str(attempt_id),
                "--confirm-read-only",
            ]
        )
        == 0
    )

    assert json.loads(capsys.readouterr().out) == {
        "command": "quarantine-readback-diagnostic",
        "report": {
            "attempt_id": str(attempt_id),
            "feedback_id": 44,
            "projection_version": 2,
            "remote_id": 234,
            "state": "quarantined",
            "reason": "readback_mismatch",
            "mismatched_fields": ["x_studio_image"],
        },
    }
    cli.rollout.readback_diagnostic.assert_called_once_with(
        attempt_id=attempt_id,
        client=client,
    )


def test_readback_diagnostic_cli_hides_malformed_remote_values(monkeypatch, capsys):
    _install_cli_fakes(monkeypatch)
    cli.rollout.readback_diagnostic.side_effect = ReadbackMismatch(
        "private note email@example.com ZGlmZmVyZW50LXByaXZhdGUtaW1hZ2U="
    )

    with pytest.raises(SystemExit) as caught:
        cli.main(
            [
                "quarantine-readback-diagnostic",
                "--attempt-id",
                "50bf66d9-c8d0-44f0-8e7f-10906330046b",
                "--confirm-read-only",
            ]
        )

    assert str(caught.value) == "feedback rollout command failed safely"
    captured = capsys.readouterr()
    exposed = captured.out + captured.err + repr(caught.value)
    for forbidden in (
        "private note",
        "email@example.com",
        "ZGlmZmVyZW50LXByaXZhdGUtaW1hZ2U=",
    ):
        assert forbidden not in exposed


def test_cli_dispatches_all_bounded_commands_through_fakes_and_emits_safe_json(monkeypatch, capsys):
    client = _install_cli_fakes(monkeypatch)
    cli.rollout.preflight.return_value = PreflightReport(
        database_uuid_matches=True,
        company_matches=True,
        fields_ok=True,
        missing_fields=(),
        wrong_types=(),
        missing_selections=(),
        source_value_present=True,
    )
    cli.rollout.dry_run_batch.return_value = DryRunReport(
        requested_batch_size=10,
        feedback_ids=(17,),
        projected_ids=(17,),
        skipped_ids=(),
        create_ids=(17,),
        adopt_ids=(),
        update_ids=(),
        duplicate_ids=(),
        ownership_conflict_ids=(),
        employee_missing_count=0,
        employee_ambiguous_count=0,
        before_image_count=0,
        after_image_count=0,
        next_after_id=17,
    )
    cli.rollout.migrate_legacy_batch.return_value = LegacyMigrationReport(
        selected_ids=(17,),
        applied_ids=(17,),
        idempotent_ids=(),
        skipped_ids=(),
        next_after_id=17,
    )
    cli.rollout.enqueue_history_batch.return_value = EnqueueReport(
        feedback_ids=(17,), next_cursor=17
    )
    cli.rollout.reconciliation_counts.return_value = {
        "synchronized": 1,
        "due": 0,
        "deferred": 0,
        "in_flight": 0,
        "quarantined": 0,
        "version_lag": 0,
    }
    cli.rollout.canary_report.return_value = cli.rollout.CanaryReport(
        feedback_id=17,
        projection_version=3,
        target_identity_ok=True,
        synchronized=True,
        verified_attempt=True,
        compound_match_count=1,
        compound_matches_saved=True,
        readback_matches=True,
    )
    client.canary_feedback_id.return_value = 17
    cli.sync_store.list_quarantined.return_value = ()
    cli.sync_store.apply_quarantine_disposition.return_value = (
        sync_store.QuarantineDispositionResult(
            feedback_id=17,
            attempt_id=UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"),
            disposition="keep",
            state="quarantined",
            desired_version=3,
            warning=None,
        )
    )
    cli.sync_store.release_pre_attempt_quarantine.return_value = (
        sync_store.PreAttemptReleaseResult(
            feedback_id=17,
            desired_version=3,
            state="idle",
        )
    )

    commands = (
        ["preflight", "--confirm-read-only"],
        ["dry-run", "--confirm-read-only", "--after-id", "0", "--batch-size", "10"],
        [
            "migrate-legacy",
            "--confirm-read-only",
            "--confirm-local-migration",
            "--after-id",
            "0",
            "--batch-size",
            "10",
        ],
        ["enqueue-history", "--confirm-local-backfill", "--batch-size", "10"],
        ["reconcile"],
        ["canary-report", "--confirm-read-only", "--feedback-id", "17"],
        ["quarantine-list"],
        [
            "quarantine-disposition",
            "--attempt-id",
            "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
            "--disposition",
            "keep",
            "--reviewer",
            "  Human Operator  ",
        ],
        [
            "quarantine-release-pre-attempt",
            "--feedback-id",
            "17",
            "--reviewer",
            "  Dale Gruber  ",
            "--confirm-read-only",
            "--confirm-local-release",
        ],
    )
    payloads = []
    for argv in commands:
        assert cli.main(argv) == 0
        payloads.append(json.loads(capsys.readouterr().out))

    assert [payload["command"] for payload in payloads] == [item[0] for item in commands]
    cli.rollout.dry_run_batch.assert_called_once_with(after_id=0, batch_size=10, client=client)
    cli.rollout.migrate_legacy_batch.assert_called_once_with(
        after_id=0, batch_size=10, client=client, now=aware_now()
    )
    cli.rollout.enqueue_history_batch.assert_called_once_with(batch_size=10, now=aware_now())
    cli.sync_store.apply_quarantine_disposition.assert_called_once_with(
        attempt_id=UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"),
        disposition="keep",
        reviewer="  Human Operator  ",
        human_review_confirmed=False,
        now=aware_now(),
    )
    cli.sync_store.release_pre_attempt_quarantine.assert_called_once_with(
        feedback_id=17,
        reviewer="  Dale Gruber  ",
        now=aware_now(),
    )
    serialized = "\n".join(json.dumps(payload, sort_keys=True) for payload in payloads)
    for forbidden in (
        "Human Operator",
        "Dale Gruber",
        "private feedback",
        "https://",
        "api_key",
        "remote_id",
        "manifest",
    ):
        assert forbidden not in serialized


def test_pre_attempt_release_parser_requires_exact_bounded_payload():
    args = cli.build_parser().parse_args(
        [
            "quarantine-release-pre-attempt",
            "--feedback-id",
            "44",
            "--reviewer",
            "Dale Gruber",
            "--confirm-read-only",
            "--confirm-local-release",
        ]
    )

    assert vars(args) == {
        "command": "quarantine-release-pre-attempt",
        "feedback_id": 44,
        "reviewer": "Dale Gruber",
        "confirm_read_only": True,
        "confirm_local_release": True,
    }


@pytest.mark.parametrize(
    "report",
    [
        PreflightReport(False, True, True, (), (), (), True),
        PreflightReport(True, False, True, (), (), (), True),
        PreflightReport(True, True, False, ("x_studio_type",), (), (), True),
        PreflightReport(True, True, False, (), (), (), False),
    ],
)
def test_pre_attempt_release_requires_fully_green_fresh_preflight(
    monkeypatch, report
):
    client = _install_cli_fakes(monkeypatch)
    cli.rollout.preflight.return_value = report

    with pytest.raises(SystemExit, match="feedback rollout command failed safely"):
        cli.main(
            [
                "quarantine-release-pre-attempt",
                "--feedback-id",
                "44",
                "--reviewer",
                "Dale Gruber",
                "--confirm-read-only",
                "--confirm-local-release",
            ]
        )

    cli.rollout.preflight.assert_called_once_with(client)
    cli.sync_store.release_pre_attempt_quarantine.assert_not_called()


def test_pre_attempt_release_calls_store_once_only_after_fresh_green_preflight(monkeypatch):
    client = _install_cli_fakes(monkeypatch)
    events = []
    green = PreflightReport(True, True, True, (), (), (), True)
    cli.rollout.preflight.side_effect = lambda supplied: events.append(
        ("preflight", supplied)
    ) or green
    result = sync_store.PreAttemptReleaseResult(44, 3, "idle")
    cli.sync_store.release_pre_attempt_quarantine.side_effect = lambda **values: events.append(
        ("release", values)
    ) or result

    payload = cli._command_payload(
        cli.build_parser().parse_args(
            [
                "quarantine-release-pre-attempt",
                "--feedback-id",
                "44",
                "--reviewer",
                "Dale Gruber",
                "--confirm-read-only",
                "--confirm-local-release",
            ]
        )
    )

    assert events == [
        ("preflight", client),
        (
            "release",
            {
                "feedback_id": 44,
                "reviewer": "Dale Gruber",
                "now": aware_now(),
            },
        ),
    ]
    assert payload == {
        "command": "quarantine-release-pre-attempt",
        "report": {"feedback_id": 44, "desired_version": 3, "state": "idle"},
    }


def test_pre_attempt_release_failure_hides_reviewer_metadata_and_credentials(
    monkeypatch, capsys
):
    _install_cli_fakes(monkeypatch)
    cli.rollout.preflight.return_value = PreflightReport(
        True, True, True, (), (), (), True
    )
    cli.sync_store.release_pre_attempt_quarantine.side_effect = RuntimeError(
        "Dale Gruber metadata https://secret.invalid api-key-value"
    )

    with pytest.raises(SystemExit) as caught:
        cli.main(
            [
                "quarantine-release-pre-attempt",
                "--feedback-id",
                "44",
                "--reviewer",
                "Dale Gruber",
                "--confirm-read-only",
                "--confirm-local-release",
            ]
        )

    assert str(caught.value) == "feedback rollout command failed safely"
    captured = capsys.readouterr()
    exposed = captured.out + captured.err + repr(caught.value)
    for forbidden in ("Dale Gruber", "metadata", "secret.invalid", "api-key-value"):
        assert forbidden not in exposed


def test_canary_cli_rejects_absent_malformed_or_mismatched_fence_before_reads(monkeypatch):
    client = _install_cli_fakes(monkeypatch)
    cli.rollout.canary_report.side_effect = AssertionError("canary read attempted")

    for configured in (None, 18):
        client.canary_feedback_id.return_value = configured
        with pytest.raises(SystemExit):
            cli.main(["canary-report", "--confirm-read-only", "--feedback-id", "17"])

    cli.rollout.canary_report.assert_not_called()


def test_supersede_confirmation_is_checked_before_time_or_store(monkeypatch):
    _install_cli_fakes(monkeypatch)
    cli.utc_now.side_effect = AssertionError("time read before confirmation")
    cli.sync_store.apply_quarantine_disposition.side_effect = AssertionError(
        "store called before confirmation"
    )

    with pytest.raises(SystemExit):
        cli.main(
            [
                "quarantine-disposition",
                "--attempt-id",
                "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
                "--disposition",
                "supersede-and-retry",
                "--reviewer",
                "Human Operator",
            ]
        )


def test_cli_outer_boundary_never_prints_operational_exception_text(monkeypatch, capsys):
    client = _install_cli_fakes(monkeypatch)
    client.inspect_target.side_effect = RuntimeError(
        "https://secret.invalid private-message api-key-value"
    )
    cli.rollout.preflight.side_effect = client.inspect_target.side_effect

    with pytest.raises(SystemExit) as caught:
        cli.main(["preflight", "--confirm-read-only"])

    assert str(caught.value) == "feedback rollout command failed safely"
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    captured = capsys.readouterr()
    assert "secret" not in captured.out
    assert "secret" not in captured.err


def test_cli_serializer_rejects_unapproved_dataclasses_and_mapping_keys():
    manifest = {
        "fields": {
            "x_name": "private feedback",
            "x_studio_source_id": "GPI-PM-FB-17",
            "x_studio_source": SOURCE_VALUE,
            "x_studio_date_start": "2026-08-20",
            "x_studio_type": "Digital",
            "x_studio_status": "Requested",
        },
        "binary_evidence": {},
    }
    digest = hashlib.sha256(
        json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    attempt = verified_attempt(
        manifest=manifest,
        manifest_digest=digest,
        binaries={},
    )
    evidence = sync_store.VerifiedCanaryEvidence(
        feedback_id=17,
        projection_version=3,
        remote_id=901,
        attempt=attempt,
    )

    for unsafe in (attempt, evidence, {"private_key": "private feedback"}):
        with pytest.raises(ValueError, match="unsafe"):
            cli._json_value(unsafe)

    assert cli._json_value(sync_store.PreAttemptReleaseResult(17, 3, "idle")) == {
        "feedback_id": 17,
        "desired_version": 3,
        "state": "idle",
    }


def test_canary_report_uses_only_exact_verified_saved_projection_evidence(monkeypatch):
    manifest, digest, binaries = (
        {
            "fields": {
                "x_name": "private feedback",
                "x_studio_source_id": "GPI-PM-FB-17",
                "x_studio_source": SOURCE_VALUE,
                "x_studio_date_start": "2026-08-20",
                "x_studio_type": "Digital",
                "x_studio_status": "Requested",
            },
            "binary_evidence": {},
        },
        None,
        {},
    )
    encoded = json.dumps(manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    digest = hashlib.sha256(encoded.encode()).hexdigest()
    saved_attempt = verified_attempt(
        manifest=manifest,
        manifest_digest=digest,
        binaries=binaries,
    )
    evidence = sync_store.VerifiedCanaryEvidence(
        feedback_id=17,
        projection_version=3,
        remote_id=901,
        attempt=saved_attempt,
    )
    events = []

    class CanaryClient:
        def verify_target_identity(self):
            events.append("target")
            return ImprovementContract(start_type="date", stop_type="date")

        def find_exact(self, source_id):
            events.append(("compound", source_id))
            return [
                {
                    "id": 901,
                    "x_studio_source": SOURCE_VALUE,
                    "x_studio_source_id": "GPI-PM-FB-17",
                }
            ]

        def read_improvement(self, remote_id, fields, *, full_binary):
            events.append(("readback", remote_id, tuple(fields), full_binary))
            return {"id": 901, **manifest["fields"]}

    monkeypatch.setattr(
        sync_store,
        "load_verified_canary_evidence",
        lambda feedback_id: events.append(("local", feedback_id)) or evidence,
    )
    image_read = MagicMock(return_value=MappingProxyType({}))
    monkeypatch.setattr(feedback_store, "attempt_image_snapshot", image_read)

    report = cli.rollout.canary_report(feedback_id=17, client=CanaryClient())

    assert report == cli.rollout.CanaryReport(
        feedback_id=17,
        projection_version=3,
        target_identity_ok=True,
        synchronized=True,
        verified_attempt=True,
        compound_match_count=1,
        compound_matches_saved=True,
        readback_matches=True,
    )
    assert events[0] == "target"
    assert events[1] == ("local", 17)
    assert events[2] == ("compound", "GPI-PM-FB-17")
    assert events[3][0] == "readback"
    assert events[3][1] == 901
    assert set(events[3][2]) == set(manifest["fields"])
    assert events[3][3] is True
    assert events[4] == ("local", 17)
    image_read.assert_not_called()
    assert "private feedback" not in repr(report)
    assert "901" not in repr(report)


@pytest.mark.parametrize(
    "invalid_contract",
    [
        object(),
        ImprovementContract(start_type="text", stop_type="date"),
        ImprovementContract(start_type="date", stop_type="text"),
    ],
)
def test_canary_report_requires_exact_fresh_improvement_contract_before_local_read(
    monkeypatch, invalid_contract
):
    local_read = MagicMock(side_effect=AssertionError("local evidence read attempted"))
    monkeypatch.setattr(sync_store, "load_verified_canary_evidence", local_read)
    client = MagicMock()
    client.verify_target_identity.return_value = invalid_contract

    with pytest.raises(ContractError, match="contract"):
        cli.rollout.canary_report(feedback_id=17, client=client)

    local_read.assert_not_called()


def test_canary_report_validates_saved_dates_against_fresh_contract_before_remote_reads(
    monkeypatch,
):
    manifest = {
        "fields": {
            "x_name": "private feedback",
            "x_studio_source_id": "GPI-PM-FB-17",
            "x_studio_source": SOURCE_VALUE,
            "x_studio_date_start": "2026-08-20",
            "x_studio_type": "Digital",
            "x_studio_status": "Requested",
        },
        "binary_evidence": {},
    }
    digest = hashlib.sha256(
        json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    evidence = sync_store.VerifiedCanaryEvidence(
        feedback_id=17,
        projection_version=3,
        remote_id=901,
        attempt=verified_attempt(manifest=manifest, manifest_digest=digest, binaries={}),
    )
    monkeypatch.setattr(sync_store, "load_verified_canary_evidence", lambda _value: evidence)
    client = MagicMock()
    client.verify_target_identity.return_value = ImprovementContract(
        start_type="datetime", stop_type="date"
    )
    client.find_exact.side_effect = AssertionError("compound read attempted")

    with pytest.raises(ContractError, match="date"):
        cli.rollout.canary_report(feedback_id=17, client=client)

    client.find_exact.assert_not_called()
    client.read_improvement.assert_not_called()


def test_canary_report_rechecks_exact_local_authority_after_remote_readback(monkeypatch):
    manifest = {
        "fields": {
            "x_name": "private feedback",
            "x_studio_source_id": "GPI-PM-FB-17",
            "x_studio_source": SOURCE_VALUE,
            "x_studio_date_start": "2026-08-20",
            "x_studio_type": "Digital",
            "x_studio_status": "Requested",
        },
        "binary_evidence": {},
    }
    digest = hashlib.sha256(
        json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    first = sync_store.VerifiedCanaryEvidence(
        feedback_id=17,
        projection_version=3,
        remote_id=901,
        attempt=verified_attempt(manifest=manifest, manifest_digest=digest, binaries={}),
    )
    advanced = sync_store.VerifiedCanaryEvidence(
        feedback_id=17,
        projection_version=4,
        remote_id=901,
        attempt=verified_attempt(
            manifest=manifest,
            manifest_digest=digest,
            binaries={},
            projection_version=4,
            attempt_id=UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"),
        ),
    )
    local_read = MagicMock(side_effect=[first, advanced])
    monkeypatch.setattr(sync_store, "load_verified_canary_evidence", local_read)
    client = MagicMock()
    client.verify_target_identity.return_value = ImprovementContract(
        start_type="date", stop_type="date"
    )
    client.find_exact.return_value = [
        {
            "id": 901,
            "x_studio_source": SOURCE_VALUE,
            "x_studio_source_id": "GPI-PM-FB-17",
        }
    ]
    client.read_improvement.return_value = {"id": 901, **manifest["fields"]}

    with pytest.raises(sync_store.StateTransitionError, match="changed"):
        cli.rollout.canary_report(feedback_id=17, client=client)

    assert local_read.call_args_list == [call(17), call(17)]


def test_canary_report_rejects_boolean_compound_count():
    with pytest.raises(ValueError, match="compound match count"):
        cli.rollout.CanaryReport(
            feedback_id=17,
            projection_version=3,
            target_identity_ok=True,
            synchronized=True,
            verified_attempt=True,
            compound_match_count=True,
            compound_matches_saved=True,
            readback_matches=True,
        )


@pytest.mark.parametrize(
    "matches",
    [
        [],
        [
            {
                "id": 901,
                "x_studio_source": SOURCE_VALUE,
                "x_studio_source_id": "GPI-PM-FB-17",
            },
            {
                "id": 902,
                "x_studio_source": SOURCE_VALUE,
                "x_studio_source_id": "GPI-PM-FB-17",
            },
        ],
        [
            {
                "id": 902,
                "x_studio_source": SOURCE_VALUE,
                "x_studio_source_id": "GPI-PM-FB-17",
            }
        ],
    ],
)
def test_canary_report_fails_closed_on_missing_duplicate_or_wrong_saved_association(
    monkeypatch, matches
):
    manifest, digest, binaries = (
        {
            "fields": {
                "x_name": "private",
                "x_studio_source_id": "GPI-PM-FB-17",
                "x_studio_source": SOURCE_VALUE,
                "x_studio_date_start": "2026-08-20",
                "x_studio_type": "Digital",
                "x_studio_status": "Requested",
            },
            "binary_evidence": {},
        },
        "",
        {},
    )
    digest = hashlib.sha256(
        json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    attempt = verified_attempt(
        manifest=manifest,
        manifest_digest=digest,
        binaries=binaries,
    )
    monkeypatch.setattr(
        sync_store,
        "load_verified_canary_evidence",
        lambda _feedback_id: sync_store.VerifiedCanaryEvidence(
            feedback_id=17,
            projection_version=3,
            remote_id=901,
            attempt=attempt,
        ),
    )
    readback = MagicMock(side_effect=AssertionError("readback attempted"))
    client = MagicMock()
    client.verify_target_identity.return_value = ImprovementContract(
        start_type="date", stop_type="date"
    )
    client.find_exact.return_value = matches
    client.read_improvement = readback

    with pytest.raises((ContractError, TargetIdentityError)):
        cli.rollout.canary_report(feedback_id=17, client=client)

    readback.assert_not_called()


def test_canary_report_fails_before_local_read_when_fresh_target_identity_fails(
    monkeypatch,
):
    local_read = MagicMock(side_effect=AssertionError("local evidence read attempted"))
    monkeypatch.setattr(sync_store, "load_verified_canary_evidence", local_read)
    client = MagicMock()
    client.verify_target_identity.side_effect = TargetIdentityError("fixed mismatch")

    with pytest.raises(TargetIdentityError):
        cli.rollout.canary_report(feedback_id=17, client=client)

    local_read.assert_not_called()


def test_canary_report_rejects_fresh_readback_mismatch_from_saved_manifest(monkeypatch):
    manifest = {
        "fields": {
            "x_name": "saved private value",
            "x_studio_source_id": "GPI-PM-FB-17",
            "x_studio_source": SOURCE_VALUE,
            "x_studio_date_start": "2026-08-20",
            "x_studio_type": "Digital",
            "x_studio_status": "Requested",
        },
        "binary_evidence": {},
    }
    digest = hashlib.sha256(
        json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    attempt = verified_attempt(
        manifest=manifest,
        manifest_digest=digest,
        binaries={},
    )
    monkeypatch.setattr(
        sync_store,
        "load_verified_canary_evidence",
        lambda _feedback_id: sync_store.VerifiedCanaryEvidence(
            feedback_id=17,
            projection_version=3,
            remote_id=901,
            attempt=attempt,
        ),
    )
    client = MagicMock()
    client.verify_target_identity.return_value = ImprovementContract(
        start_type="date", stop_type="date"
    )
    client.find_exact.return_value = [
        {
            "id": 901,
            "x_studio_source": SOURCE_VALUE,
            "x_studio_source_id": "GPI-PM-FB-17",
        }
    ]
    client.read_improvement.return_value = {
        "id": 901,
        **manifest["fields"],
        "x_name": "different private value",
    }

    with pytest.raises(ReadbackMismatch):
        cli.rollout.canary_report(feedback_id=17, client=client)


def test_canary_report_rejects_changed_saved_binary_before_remote_reads(monkeypatch):
    raw = b"saved-image"
    digest_value = hashlib.sha256(raw).hexdigest()
    manifest = {
        "fields": {
            "x_name": "saved private value",
            "x_studio_source_id": "GPI-PM-FB-17",
            "x_studio_source": SOURCE_VALUE,
            "x_studio_date_start": "2026-08-20",
            "x_studio_type": "Digital",
            "x_studio_status": "Requested",
        },
        "binary_evidence": {
            "x_studio_image": {
                "sha256": digest_value,
                "byte_length": len(raw),
            }
        },
    }
    digest = hashlib.sha256(
        json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    attempt = verified_attempt(
        manifest=manifest,
        manifest_digest=digest,
        binaries=manifest["binary_evidence"],
    )
    monkeypatch.setattr(
        sync_store,
        "load_verified_canary_evidence",
        lambda _feedback_id: sync_store.VerifiedCanaryEvidence(
            feedback_id=17,
            projection_version=3,
            remote_id=901,
            attempt=attempt,
        ),
    )
    monkeypatch.setattr(
        feedback_store,
        "attempt_image_snapshot",
        MagicMock(side_effect=feedback_store.ProjectionSnapshotUnavailable("changed")),
    )
    client = MagicMock()
    client.verify_target_identity.return_value = ImprovementContract(
        start_type="date", stop_type="date"
    )

    with pytest.raises(ContractError, match="binary evidence"):
        cli.rollout.canary_report(feedback_id=17, client=client)

    client.find_exact.assert_not_called()
    client.read_improvement.assert_not_called()


def test_task_11_runbook_environment_readme_and_patch_note_are_complete_and_dark():
    root = Path(__file__).resolve().parents[1]
    runbook = (root / "docs/odoo-2s-feedback-operations.md").read_text()
    env_example = (root / ".env.example").read_text()
    readme = (root / "README.md").read_text()
    changelog = (root / "CHANGELOG.md").read_text()

    numbered_sections = [line for line in runbook.splitlines() if line.startswith("## ")]
    assert len(numbered_sections) == 16
    assert "fresh retry budget" in runbook
    assert "`keep` does not reset that budget" in runbook
    required_phrases = (
        "GPI Plant Manager",
        "preflight --confirm-read-only",
        "dry-run --confirm-read-only",
        "ODOO_IMPROVEMENTS_CANARY_FEEDBACK_ID",
        "migrate-legacy",
        "enqueue-history",
        "version lag",
        "matching values",
        "never delete or archive",
        "unproven",
    )
    for phrase in required_phrases:
        assert phrase.casefold() in runbook.casefold()
    for name in (
        "ODOO_IMPROVEMENTS_URL",
        "ODOO_IMPROVEMENTS_DB",
        "ODOO_IMPROVEMENTS_LOGIN",
        "ODOO_IMPROVEMENTS_API_KEY",
        "ODOO_IMPROVEMENTS_EXPECTED_DATABASE_UUID",
        "ODOO_IMPROVEMENTS_EXPECTED_COMPANY",
        "ODOO_SHARED_REPORTING_WRITE_ENABLED",
        "ODOO_IMPROVEMENTS_WRITE_ENABLED",
        "ODOO_IMPROVEMENTS_CANARY_FEEDBACK_ID",
    ):
        assert f"{name}=" in env_example
    assert "docs/odoo-2s-feedback-operations.md" in readme
    assert "### Shared improvements connection is safely off" in changelog
    assert (
        "Plant Manager is ready to share feedback with the improvements list, "
        "but the connection starts off."
    ) in changelog
    assert "ODOO_SHARED_REPORTING_WRITE_ENABLED=true" not in runbook
    assert "ODOO_IMPROVEMENTS_WRITE_ENABLED=true" not in runbook
