"""Database-free safety contract for the shared Odoo feedback mirror."""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import UUID

import pytest

from scripts import feedback_odoo_rollout as rollout_cli
from zira_dashboard import feedback_store
from zira_dashboard import feedback_sync
from zira_dashboard import feedback_sync_store as sync_store
from zira_dashboard import odoo_improvements
from zira_dashboard.feedback_image import (
    MAX_INPUT_BYTES,
    MAX_OUTPUT_BYTES,
    MAX_PIXELS,
    MAX_SIDE,
    OUTPUT_LONG_SIDE,
    ImageRejected,
    normalize_image,
)
from zira_dashboard.feedback_projection import Projection, TYPE_VALUES
from zira_dashboard.feedback_sync import BatchResult
from zira_dashboard.odoo_improvements import (
    ALLOWED,
    SOURCE_VALUE,
    TARGET_FIELDS,
    TARGET_MODEL,
    ContractError,
    ImprovementsClient,
    ImprovementsConfig,
)


SERVICE_ENV_NAMES = (
    "DATABASE_URL",
    "DATABASE_PUBLIC_URL",
    "FEEDBACK_SYNC_TEST_DATABASE",
    "PAYROLL_GUARD_TEST_DATABASE",
    "ODOO_URL",
    "ODOO_DB",
    "ODOO_LOGIN",
    "ODOO_API_KEY",
    "ODOO_ABSENCE_LEAVE_TYPE_NAME",
    "ODOO_KIOSK_DEPARTMENT_FIELD",
    "ODOO_KIOSK_WC_FIELD",
    "ODOO_WORK_CENTER_MAPPING_LOCK_KEY",
    "ODOO_WORK_CENTER_MAP_CACHE",
    "ODOO_IMPROVEMENTS_URL",
    "ODOO_IMPROVEMENTS_DB",
    "ODOO_IMPROVEMENTS_LOGIN",
    "ODOO_IMPROVEMENTS_API_KEY",
    "ODOO_IMPROVEMENTS_EXPECTED_DATABASE_UUID",
    "ODOO_IMPROVEMENTS_EXPECTED_COMPANY",
    "ODOO_IMPROVEMENTS_CANARY_FEEDBACK_ID",
)

DEDICATED_ENV = {
    "ODOO_IMPROVEMENTS_URL": "https://odoo.invalid",
    "ODOO_IMPROVEMENTS_DB": "database",
    "ODOO_IMPROVEMENTS_LOGIN": "service@example.invalid",
    "ODOO_IMPROVEMENTS_API_KEY": "dummy-secret",
    "ODOO_IMPROVEMENTS_EXPECTED_DATABASE_UUID": "expected-uuid",
    "ODOO_IMPROVEMENTS_EXPECTED_COMPANY": "Expected Company",
}


@pytest.fixture(autouse=True)
def disable_real_services(monkeypatch):
    for name in SERVICE_ENV_NAMES:
        monkeypatch.setenv(name, "")
    monkeypatch.setenv("ODOO_SHARED_REPORTING_WRITE_ENABLED", "false")
    monkeypatch.setenv("ODOO_IMPROVEMENTS_WRITE_ENABLED", "false")
    monkeypatch.setenv("ZIRA_API_KEY", "")
    monkeypatch.setenv("ZIRA_BASE_URL", "http://127.0.0.1:9/")


def _now() -> datetime:
    return datetime(2026, 8, 21, 12, 0, tzinfo=UTC)


def _config() -> ImprovementsConfig:
    return ImprovementsConfig(
        url=DEDICATED_ENV["ODOO_IMPROVEMENTS_URL"],
        database=DEDICATED_ENV["ODOO_IMPROVEMENTS_DB"],
        login=DEDICATED_ENV["ODOO_IMPROVEMENTS_LOGIN"],
        api_key=DEDICATED_ENV["ODOO_IMPROVEMENTS_API_KEY"],
        expected_database_uuid=DEDICATED_ENV["ODOO_IMPROVEMENTS_EXPECTED_DATABASE_UUID"],
        expected_company=DEDICATED_ENV["ODOO_IMPROVEMENTS_EXPECTED_COMPANY"],
    )


def _saved_projection() -> Projection:
    fields = {
        "x_name": "Saved version one",
        "x_studio_source_id": "GPI-PM-FB-17",
        "x_studio_date_start": "2026-08-21",
        "x_studio_type": "Digital",
        "x_studio_status": "Requested",
        "x_studio_source": SOURCE_VALUE,
    }
    manifest = {"fields": fields, "binary_evidence": {}}
    canonical = json.dumps(
        manifest,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode()
    return Projection(
        source_id="GPI-PM-FB-17",
        fields=fields,
        binaries={},
        manifest=manifest,
        manifest_digest=hashlib.sha256(canonical).hexdigest(),
    )


def test_shared_identity_and_target_are_fixed():
    assert SOURCE_VALUE == "GPI Plant Manager"
    assert TARGET_MODEL == "x_2s_improvements"


class _PoisonBatchArgument:
    def __bool__(self):
        raise AssertionError("batch arguments must not be read")


@pytest.mark.parametrize(
    ("shared_gate", "improvements_gate"),
    [("false", "true"), ("true", "false")],
)
def test_closing_either_write_gate_blocks_every_downstream_boundary(
    monkeypatch,
    shared_gate,
    improvements_gate,
):
    monkeypatch.setenv("ODOO_SHARED_REPORTING_WRITE_ENABLED", shared_gate)
    monkeypatch.setenv("ODOO_IMPROVEMENTS_WRITE_ENABLED", improvements_gate)
    poison = MagicMock(side_effect=AssertionError("downstream boundary was reached"))

    monkeypatch.setattr(feedback_sync.socket, "gethostname", poison)
    monkeypatch.setattr(feedback_sync.os, "getpid", poison)
    monkeypatch.setattr(feedback_sync.ImprovementsClient, "from_env", poison)
    monkeypatch.setattr(feedback_sync.ImprovementsClient, "default_executor", poison)
    monkeypatch.setattr(odoo_improvements.ImprovementsConfig, "from_env", poison)
    monkeypatch.setattr(odoo_improvements, "_server_proxy", poison)
    monkeypatch.setattr(odoo_improvements.xmlrpc.client, "ServerProxy", poison)
    monkeypatch.setattr(sync_store.db, "cursor", poison)
    monkeypatch.setattr(sync_store, "recover_expired_claims", poison)
    monkeypatch.setattr(sync_store, "claim_due", poison)
    monkeypatch.setattr(feedback_store, "projection_snapshot", poison)
    monkeypatch.setattr(feedback_sync, "process_claim", poison)
    monkeypatch.setattr(ImprovementsClient, "create_improvement", poison)
    monkeypatch.setattr(ImprovementsClient, "write_improvement", poison)
    argument = _PoisonBatchArgument()

    result = feedback_sync.run_batch(now=argument, worker_id=argument, limit=argument)

    assert result == BatchResult(skipped="write_gates_closed")
    poison.assert_not_called()


def test_target_and_payload_surfaces_cannot_token_archive_delete_or_emit_physical():
    target_methods = {method for model, method in ALLOWED if model == TARGET_MODEL}
    surface = set(TARGET_FIELDS) | target_methods

    assert target_methods == {"fields_get", "search_read", "read", "create", "write"}
    assert not any("token" in item.casefold() for item in surface)
    assert not any(
        forbidden in item.casefold()
        for item in surface
        for forbidden in ("active", "archive", "delete", "unlink")
    )
    assert "Physical" not in TYPE_VALUES.values()
    assert "odoo_client" not in vars(odoo_improvements)


def test_credentials_are_environment_only_and_raw_target_mutation_is_not_exposed(
    monkeypatch,
):
    monkeypatch.setenv("ODOO_URL", "https://generic.invalid")
    monkeypatch.setenv("ODOO_DB", "generic-database")
    monkeypatch.setenv("ODOO_LOGIN", "generic@example.invalid")
    monkeypatch.setenv("ODOO_API_KEY", "generic-secret")
    for name, value in DEDICATED_ENV.items():
        monkeypatch.setenv(name, value)

    config = ImprovementsConfig.from_env()
    executor = MagicMock(side_effect=AssertionError("raw mutation reached executor"))
    client = ImprovementsClient(config, executor, uid=7)

    assert config == _config()
    assert "_executor" not in vars(client)
    with pytest.raises(ContractError, match="internal consumer"):
        client._execute(TARGET_MODEL, "create", {"x_name": "unsafe"})
    executor.assert_not_called()


def test_compound_lookup_and_binary_read_are_exact_and_bounded():
    calls = []

    def executor(model, method, *args, **kwargs):
        calls.append((model, method, args, kwargs))
        if method == "search_read":
            return []
        if method == "read":
            return [{"id": 901, "x_studio_image": "c2FmZQ=="}]
        raise AssertionError("unexpected dedicated client operation")

    client = ImprovementsClient(_config(), executor, uid=7)

    assert client.find_exact("GPI-PM-FB-17") == []
    assert client.read_improvement(901, ["x_studio_image"], full_binary=True) == {
        "id": 901,
        "x_studio_image": "c2FmZQ==",
    }
    assert calls == [
        (
            TARGET_MODEL,
            "search_read",
            (
                [
                    ("x_studio_source", "=", SOURCE_VALUE),
                    ("x_studio_source_id", "=", "GPI-PM-FB-17"),
                ],
            ),
            {
                "fields": ["id", "x_studio_source", "x_studio_source_id"],
                "limit": 3,
            },
        ),
        (
            TARGET_MODEL,
            "read",
            ([901],),
            {"fields": ["x_studio_image"], "context": {"bin_size": False}},
        ),
    ]


def test_worker_batch_and_image_limits_remain_bounded(monkeypatch):
    monkeypatch.setenv("ODOO_SHARED_REPORTING_WRITE_ENABLED", "true")
    monkeypatch.setenv("ODOO_IMPROVEMENTS_WRITE_ENABLED", "true")
    client = SimpleNamespace(
        assert_worker_enabled=MagicMock(),
        canary_feedback_id=MagicMock(return_value=17),
    )
    recover = MagicMock(return_value=0)
    claim_due = MagicMock(return_value=[])
    monkeypatch.setattr(
        feedback_sync.ImprovementsClient,
        "from_env",
        MagicMock(return_value=client),
    )
    monkeypatch.setattr(sync_store, "recover_expired_claims", recover)
    monkeypatch.setattr(sync_store, "claim_due", claim_due)

    assert feedback_sync.run_batch(now=_now(), worker_id="worker-a", limit=10_000) == BatchResult()
    claim_due.assert_called_once_with(
        now=_now(),
        worker_id="worker-a",
        limit=10,
        canary_feedback_id=17,
    )
    assert (MAX_INPUT_BYTES, MAX_OUTPUT_BYTES, MAX_SIDE, MAX_PIXELS, OUTPUT_LONG_SIDE) == (
        10 * 1024 * 1024,
        5 * 1024 * 1024,
        8192,
        25_000_000,
        2048,
    )
    with pytest.raises(ImageRejected, match="10 MiB"):
        normalize_image(b"x" * (MAX_INPUT_BYTES + 1))


def test_saved_attempt_is_immutable_authority_when_local_truth_is_newer(monkeypatch):
    selected = _saved_projection()
    attempt_id = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
    saved = sync_store.Attempt(
        attempt_id=attempt_id,
        feedback_id=17,
        projection_version=1,
        mutation_kind="create",
        remote_id=901,
        manifest=selected.manifest,
        manifest_digest=selected.manifest_digest,
        binaries={},
        state="rpc_succeeded",
    )
    claim = sync_store.Claim(
        feedback_id=17,
        desired_version=2,
        last_synced_version=0,
        odoo_improvement_id=901,
        claim_owner="worker-a",
        claim_token=UUID("11111111-1111-1111-1111-111111111111"),
        claim_expires_at=_now() + timedelta(minutes=5),
        active_attempt_id=attempt_id,
        attempt_count=1,
    )
    read = MagicMock(return_value={"id": 901, **selected.dispatch_fields()})
    client = SimpleNamespace(read_improvement=read)
    poison = MagicMock(side_effect=AssertionError("current local truth was read"))
    settle = MagicMock(return_value=True)
    monkeypatch.setattr(sync_store, "load_active_attempt", MagicMock(return_value=saved))
    monkeypatch.setattr(feedback_store, "projection_snapshot", poison)
    monkeypatch.setattr(feedback_sync, "build_projection_from_snapshot", poison)
    monkeypatch.setattr(sync_store, "settle_verified", settle)

    exposed_manifest = saved.manifest
    exposed_manifest["fields"]["x_name"] = "caller changed the saved evidence"
    result = feedback_sync.process_claim(claim, client=client, now=_now())

    assert result == "verified"
    assert saved.manifest["fields"]["x_name"] == "Saved version one"
    read.assert_called_once_with(
        901,
        sorted(selected.dispatch_fields()),
        full_binary=True,
    )
    settle.assert_called_once_with(claim, saved, 901, _now())
    poison.assert_not_called()


def test_rollout_cli_has_exact_commands_and_no_alias_or_config_overrides():
    parser = rollout_cli.build_parser()
    subparsers_action = next(
        action for action in parser._actions if isinstance(action, argparse._SubParsersAction)
    )
    command_parsers = tuple(subparsers_action.choices.values())
    option_strings = {
        option
        for selected_parser in (parser, *command_parsers)
        for action in selected_parser._actions
        for option in action.option_strings
    }

    assert set(subparsers_action.choices) == {
        "preflight",
        "dry-run",
        "migrate-legacy",
        "enqueue-history",
        "reconcile",
        "canary-report",
        "quarantine-list",
        "quarantine-disposition",
        "quarantine-release-pre-attempt",
    }
    assert all(
        selected_parser.allow_abbrev is False for selected_parser in (parser, *command_parsers)
    )
    assert option_strings == {
        "-h",
        "--help",
        "--confirm-read-only",
        "--confirm-local-migration",
        "--confirm-local-backfill",
        "--after-id",
        "--batch-size",
        "--feedback-id",
        "--attempt-id",
        "--disposition",
        "--reviewer",
        "--confirm-human-review",
        "--confirm-local-release",
    }
