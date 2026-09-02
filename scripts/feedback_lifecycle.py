"""Shared, local-only feedback lifecycle command safeguards."""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import UTC, datetime
from typing import Literal

from zira_dashboard import db, feedback_store, feedback_task_delivery


MAX_SIGNED_64 = 9_223_372_036_854_775_807
DEFAULT_ACTOR = "dale@gruberpallets.com"
SAFE_FAILURE = "feedback lifecycle command failed safely"
_SOURCE_ID_RE = re.compile(r"GPI-PM-FB-([1-9][0-9]*)", re.ASCII)
_EMAIL_RE = re.compile(r"[^@\s]+@[^@\s]+", re.ASCII)
_LifecycleCommand = Literal["start", "finish"]


class SafeArgumentParser(argparse.ArgumentParser):
    """Argument parser whose failures never echo caller-controlled values."""

    def error(self, message: str) -> None:
        del message
        self.exit(2, f"{self.prog}: invalid arguments\n")


def utc_now() -> datetime:
    return datetime.now(UTC)


def _positive_id(value: str) -> int:
    if (
        type(value) is not str
        or not value
        or len(value) > len(str(MAX_SIGNED_64))
        or not value.isascii()
        or not value.isdigit()
        or value.startswith("0")
    ):
        raise argparse.ArgumentTypeError("feedback id must be canonical")
    parsed = int(value)
    if parsed > MAX_SIGNED_64:
        raise argparse.ArgumentTypeError("feedback id is outside its allowed range")
    return parsed


def _source_id(value: str) -> int:
    if type(value) is not str:
        raise argparse.ArgumentTypeError("source id must be canonical")
    matched = _SOURCE_ID_RE.fullmatch(value)
    if matched is None:
        raise argparse.ArgumentTypeError("source id must be canonical")
    return _positive_id(matched.group(1))


def _nonblank(value: str) -> str:
    if type(value) is not str or not value.strip():
        raise argparse.ArgumentTypeError("value must not be blank")
    return value.strip()


def _email(value: str) -> str:
    clean = _nonblank(value).lower()
    if not clean.isascii() or _EMAIL_RE.fullmatch(clean) is None:
        raise argparse.ArgumentTypeError("closer must be an email address")
    return clean


def build_parser(*, prog: str, command: _LifecycleCommand) -> argparse.ArgumentParser:
    parser = SafeArgumentParser(prog=prog, allow_abbrev=False)
    identifiers = parser.add_mutually_exclusive_group(required=True)
    identifiers.add_argument("--feedback-id", type=_positive_id)
    identifiers.add_argument("--source-id", type=_source_id)
    if command == "finish":
        parser.add_argument("--note", required=True, type=_nonblank)
        parser.add_argument("--by", default=DEFAULT_ACTOR, type=_email)
    parser.add_argument("--yes", action="store_true")
    return parser


def _feedback_id(args: argparse.Namespace) -> int:
    feedback_id = args.feedback_id if args.feedback_id is not None else args.source_id
    if type(feedback_id) is not int:
        raise ValueError("feedback identifier is unavailable")
    return feedback_id


def _result(
    *,
    feedback_id: int,
    current_status: str,
    proposed_status: str,
    applied: bool,
    task_sync_state: str,
    task_queued: bool,
) -> dict[str, object]:
    if (
        type(feedback_id) is not int
        or not 0 < feedback_id <= MAX_SIGNED_64
        or current_status not in {"requested", "in_progress", "completed", "declined"}
        or proposed_status not in {"requested", "in_progress", "completed", "declined"}
        or type(applied) is not bool
        or task_sync_state not in {"pending", "synced", "attention"}
        or type(task_queued) is not bool
    ):
        raise ValueError("feedback lifecycle result is unsafe")
    return {
        "feedback_id": feedback_id,
        "current_status": current_status,
        "proposed_status": proposed_status,
        "proposed_task_stage": {
            "requested": "New",
            "in_progress": "In Progress",
            "completed": "Done",
            "declined": "Done",
        }[proposed_status],
        "task_sync_state": task_sync_state,
        "task_queued": task_queued,
        "applied": applied,
    }


def _run_command(args: argparse.Namespace, command: _LifecycleCommand) -> tuple[int, dict[str, object]]:
    feedback_id = _feedback_id(args)
    state = feedback_store.lifecycle_state(feedback_id)
    current_status = state["status"]
    raw_task_state = state.get("task_sync_state")
    task_sync_state = (
        "attention"
        if raw_task_state in {"attention", "blocked"}
        else "synced"
        if raw_task_state == "delivered"
        and state.get("task_last_synced_version") == state.get("task_desired_version")
        and state.get("task_desired_contract_version")
        == feedback_task_delivery.TASK_SYNC_CONTRACT_VERSION
        and state.get("task_last_synced_contract_version")
        == state.get("task_desired_contract_version")
        else "pending"
    )

    def result(proposed_status: str, *, applied: bool, queued: bool = False):
        return _result(
            feedback_id=feedback_id,
            current_status=current_status,
            proposed_status=proposed_status,
            applied=applied,
            task_sync_state=task_sync_state,
            task_queued=queued,
        )

    if command == "start":
        if current_status == "in_progress":
            return 0, result(current_status, applied=False)
        if current_status != "requested":
            return 1, result(current_status, applied=False)
        proposed_status = "in_progress"
        actor = DEFAULT_ACTOR
        note = None
    else:
        if current_status != "in_progress":
            return 1, result(current_status, applied=False)
        proposed_status = "completed"
        actor = args.by
        note = args.note

    if not args.yes:
        return 0, result(proposed_status, applied=False)

    feedback_store.transition(
        feedback_id=feedback_id,
        status=proposed_status,
        actor=actor,
        resolution_note=note,
        after_image=None,
        now=utc_now(),
    )
    return 0, result(proposed_status, applied=True, queued=True)


def main(
    argv: list[str] | None = None,
    *,
    command: _LifecycleCommand,
    prog: str,
) -> int:
    args = build_parser(prog=prog, command=command).parse_args(argv)
    failed = False
    try:
        try:
            db.init_pool()
            exit_code, payload = _run_command(args, command)
        finally:
            db.shutdown_pool()
    except Exception:
        failed = True
        exit_code = 1
        payload = None
    if failed:
        raise SystemExit(SAFE_FAILURE) from None
    sys.stdout.write(json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n")
    return exit_code
