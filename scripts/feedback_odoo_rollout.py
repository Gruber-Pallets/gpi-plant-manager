"""Explicit inert-by-default operator CLI for the feedback Odoo rollout."""

from __future__ import annotations

import argparse
import dataclasses
import json
import sys
from datetime import UTC, datetime
from uuid import UUID

from zira_dashboard import feedback_rollout as rollout
from zira_dashboard import feedback_sync_store as sync_store
from zira_dashboard.odoo_improvements import ImprovementsClient


MAX_SIGNED_64 = 9_223_372_036_854_775_807
_SAFE_FAILURE = "feedback rollout command failed safely"
_RECONCILIATION_KEYS = frozenset(
    {"synchronized", "due", "deferred", "in_flight", "quarantined", "version_lag"}
)


class _SafeArgumentParser(argparse.ArgumentParser):
    """Argparse surface whose failures never echo caller-controlled values."""

    def error(self, message: str) -> None:
        del message
        self.exit(2, f"{self.prog}: invalid arguments\n")


def require_flag(value: bool, message: str) -> None:
    if value is not True:
        raise SystemExit(message)


def utc_now() -> datetime:
    return datetime.now(UTC)


def _unsigned_text(value: str, *, positive: bool, maximum: int, label: str) -> int:
    if (
        type(value) is not str
        or not value
        or len(value) > len(str(MAX_SIGNED_64))
        or not value.isascii()
        or not value.isdigit()
    ):
        raise argparse.ArgumentTypeError(f"{label} must be an exact integer")
    parsed = int(value)
    minimum = 1 if positive else 0
    if not minimum <= parsed <= maximum:
        raise argparse.ArgumentTypeError(f"{label} is outside its allowed range")
    return parsed


def _nonnegative_id(value: str) -> int:
    return _unsigned_text(
        value,
        positive=False,
        maximum=MAX_SIGNED_64,
        label="after id",
    )


def _positive_id(value: str) -> int:
    return _unsigned_text(
        value,
        positive=True,
        maximum=MAX_SIGNED_64,
        label="feedback id",
    )


def _batch_size(value: str) -> int:
    return _unsigned_text(value, positive=True, maximum=100, label="batch size")


def _canonical_uuid(value: str) -> UUID:
    if type(value) is not str or len(value) != 36:
        raise argparse.ArgumentTypeError("attempt id must be a canonical UUID")
    try:
        parsed = UUID(value)
    except (AttributeError, ValueError):
        raise argparse.ArgumentTypeError("attempt id must be a canonical UUID") from None
    if str(parsed) != value:
        raise argparse.ArgumentTypeError("attempt id must be a canonical UUID")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = _SafeArgumentParser(
        prog="feedback-odoo-rollout",
        description="Guarded Plant Manager feedback rollout operations.",
        allow_abbrev=False,
    )
    commands = parser.add_subparsers(
        dest="command",
        required=True,
        parser_class=_SafeArgumentParser,
    )

    preflight = commands.add_parser("preflight", allow_abbrev=False)
    preflight.add_argument("--confirm-read-only", action="store_true")

    dry_run = commands.add_parser("dry-run", allow_abbrev=False)
    dry_run.add_argument("--confirm-read-only", action="store_true")
    dry_run.add_argument("--after-id", required=True, type=_nonnegative_id)
    dry_run.add_argument("--batch-size", required=True, type=_batch_size)

    migrate = commands.add_parser("migrate-legacy", allow_abbrev=False)
    migrate.add_argument("--confirm-read-only", action="store_true")
    migrate.add_argument("--confirm-local-migration", action="store_true")
    migrate.add_argument("--after-id", required=True, type=_nonnegative_id)
    migrate.add_argument("--batch-size", required=True, type=_batch_size)

    enqueue = commands.add_parser("enqueue-history", allow_abbrev=False)
    enqueue.add_argument("--confirm-local-backfill", action="store_true")
    enqueue.add_argument("--batch-size", required=True, type=_batch_size)

    commands.add_parser("reconcile", allow_abbrev=False)

    canary = commands.add_parser("canary-report", allow_abbrev=False)
    canary.add_argument("--confirm-read-only", action="store_true")
    canary.add_argument("--feedback-id", required=True, type=_positive_id)

    commands.add_parser("quarantine-list", allow_abbrev=False)

    disposition = commands.add_parser("quarantine-disposition", allow_abbrev=False)
    disposition.add_argument("--attempt-id", required=True, type=_canonical_uuid)
    disposition.add_argument(
        "--disposition",
        required=True,
        choices=("keep", "release-definitive", "supersede-and-retry"),
    )
    disposition.add_argument("--reviewer", required=True)
    disposition.add_argument("--confirm-human-review", action="store_true")
    return parser


def _approved_report_types() -> tuple[type, ...]:
    return (
        rollout.PreflightReport,
        rollout.DryRunReport,
        rollout.LegacyMigrationReport,
        rollout.EnqueueReport,
        rollout.CanaryReport,
        sync_store.QuarantineItem,
        sync_store.QuarantineDispositionResult,
    )


def _json_value(value, *, _nested: bool = False):
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        if type(value) not in _approved_report_types():
            raise ValueError("command report contained an unsafe dataclass")
        return {
            field.name: _json_value(getattr(value, field.name), _nested=True)
            for field in dataclasses.fields(value)
        }
    if isinstance(value, tuple):
        if not _nested and any(type(item) is not sync_store.QuarantineItem for item in value):
            raise ValueError("command report contained an unsafe tuple")
        return [_json_value(item, _nested=True) for item in value]
    if type(value) is dict:
        if (
            _nested
            or set(value) != _RECONCILIATION_KEYS
            or any(
                type(item) is not int or not 0 <= item <= MAX_SIGNED_64 for item in value.values()
            )
        ):
            raise ValueError("command report contained an unsafe mapping")
        return {key: value[key] for key in sorted(_RECONCILIATION_KEYS)}
    if type(value) is datetime:
        if not _nested:
            raise ValueError("command report contained an unsafe value")
        return value.isoformat()
    if type(value) is UUID:
        if not _nested:
            raise ValueError("command report contained an unsafe value")
        return str(value)
    if _nested and (type(value) in {str, int, bool} or value is None):
        return value
    raise ValueError("command report contained an unsafe value")


def _command_payload(args: argparse.Namespace) -> dict[str, object]:
    command = args.command
    if command == "preflight":
        require_flag(args.confirm_read_only, "preflight requires --confirm-read-only")
        client = ImprovementsClient.from_env()
        report = rollout.preflight(client)
    elif command == "dry-run":
        require_flag(args.confirm_read_only, "dry-run requires --confirm-read-only")
        client = ImprovementsClient.from_env()
        report = rollout.dry_run_batch(
            after_id=args.after_id,
            batch_size=args.batch_size,
            client=client,
        )
    elif command == "migrate-legacy":
        require_flag(
            args.confirm_read_only,
            "migrate-legacy requires --confirm-read-only",
        )
        require_flag(
            args.confirm_local_migration,
            "migrate-legacy requires --confirm-local-migration",
        )
        client = ImprovementsClient.from_env()
        report = rollout.migrate_legacy_batch(
            after_id=args.after_id,
            batch_size=args.batch_size,
            client=client,
            now=utc_now(),
        )
    elif command == "enqueue-history":
        require_flag(
            args.confirm_local_backfill,
            "enqueue-history requires --confirm-local-backfill",
        )
        report = rollout.enqueue_history_batch(
            batch_size=args.batch_size,
            now=utc_now(),
        )
    elif command == "reconcile":
        report = rollout.reconciliation_counts()
    elif command == "canary-report":
        require_flag(
            args.confirm_read_only,
            "canary-report requires --confirm-read-only",
        )
        client = ImprovementsClient.from_env()
        configured_canary = client.canary_feedback_id()
        if configured_canary is None or configured_canary != args.feedback_id:
            raise SystemExit("canary feedback id does not match the exact configured fence")
        report = rollout.canary_report(feedback_id=args.feedback_id, client=client)
    elif command == "quarantine-list":
        report = sync_store.list_quarantined(limit=100)
    elif command == "quarantine-disposition":
        if args.disposition == "supersede-and-retry":
            require_flag(
                args.confirm_human_review,
                "supersede-and-retry requires --confirm-human-review",
            )
        report = sync_store.apply_quarantine_disposition(
            attempt_id=args.attempt_id,
            disposition=args.disposition,
            reviewer=args.reviewer,
            human_review_confirmed=args.confirm_human_review,
            now=utc_now(),
        )
    else:
        raise ValueError("unsupported rollout command")
    return {"command": command, "report": _json_value(report)}


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    failed = False
    try:
        payload = _command_payload(args)
    except SystemExit:
        raise
    except Exception:
        failed = True
        payload = None
    if failed:
        raise SystemExit(_SAFE_FAILURE) from None
    sys.stdout.write(json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
