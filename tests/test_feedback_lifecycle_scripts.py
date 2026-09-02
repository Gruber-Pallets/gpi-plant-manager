"""Exact, local-only feedback lifecycle command tests."""

from __future__ import annotations

import ast
import json
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from scripts import feedback_lifecycle, process_feedback, resolve_feedback
from zira_dashboard import feedback_store


MAX_SIGNED_64 = 9_223_372_036_854_775_807
NOW = datetime(2026, 9, 2, 15, 30, tzinfo=UTC)
DEFAULT_ACTOR = "dale@gruberpallets.com"


def _install_runtime(monkeypatch, status: str = "requested"):
    lifecycle_state = MagicMock(
        return_value={
            "id": 17,
            "status": status,
            "lifecycle_origin": "local",
            "projection_version": 3,
            "task_sync_state": "delivered",
            "task_desired_version": 3,
            "task_last_synced_version": 3,
        }
    )
    transition = MagicMock(return_value=4)
    init_pool = MagicMock()
    shutdown_pool = MagicMock()
    monkeypatch.setattr(feedback_lifecycle.feedback_store, "lifecycle_state", lifecycle_state)
    monkeypatch.setattr(feedback_lifecycle.feedback_store, "transition", transition)
    monkeypatch.setattr(feedback_lifecycle.db, "init_pool", init_pool)
    monkeypatch.setattr(feedback_lifecycle.db, "shutdown_pool", shutdown_pool)
    monkeypatch.setattr(feedback_lifecycle, "utc_now", MagicMock(return_value=NOW))
    return lifecycle_state, transition, init_pool, shutdown_pool


@pytest.mark.parametrize(
    ("main", "required"),
    [
        (process_feedback.main, []),
        (resolve_feedback.main, ["--note", "Finished safely"]),
    ],
)
def test_commands_require_exactly_one_identifier(monkeypatch, main, required):
    read = MagicMock(side_effect=AssertionError("store read attempted"))
    monkeypatch.setattr(feedback_lifecycle.feedback_store, "lifecycle_state", read)

    with pytest.raises(SystemExit):
        main(required)
    with pytest.raises(SystemExit):
        main(
            [
                "--feedback-id",
                "17",
                "--source-id",
                "GPI-PM-FB-17",
                *required,
            ]
        )

    read.assert_not_called()


@pytest.mark.parametrize(
    "value",
    [
        "0",
        "-1",
        "+1",
        "01",
        " 1",
        "1 ",
        "1.0",
        "true",
        str(MAX_SIGNED_64 + 1),
        "9" * 5000,
    ],
)
def test_feedback_id_rejects_noncanonical_or_out_of_range_values(monkeypatch, value):
    read = MagicMock(side_effect=AssertionError("store read attempted"))
    monkeypatch.setattr(feedback_lifecycle.feedback_store, "lifecycle_state", read)

    with pytest.raises(SystemExit):
        process_feedback.main(["--feedback-id", value])

    read.assert_not_called()


@pytest.mark.parametrize(
    "value",
    [
        "17",
        "TASK-17",
        "GPI-PM-TASK-17",
        "gpi-pm-fb-17",
        "GPI-PM-FB-0",
        "GPI-PM-FB-01",
        "GPI-PM-FB-+1",
        "GPI-PM-FB-17-extra",
        " GPI-PM-FB-17",
        f"GPI-PM-FB-{MAX_SIGNED_64 + 1}",
    ],
)
def test_source_id_rejects_task_ids_loose_strings_and_aliases(monkeypatch, value):
    read = MagicMock(side_effect=AssertionError("store read attempted"))
    monkeypatch.setattr(feedback_lifecycle.feedback_store, "lifecycle_state", read)

    with pytest.raises(SystemExit):
        process_feedback.main(["--source-id", value])

    read.assert_not_called()


@pytest.mark.parametrize(
    "arguments",
    [
        ["--feedback-id", str(MAX_SIGNED_64)],
        ["--source-id", f"GPI-PM-FB-{MAX_SIGNED_64}"],
    ],
)
def test_commands_accept_the_largest_canonical_identifier(monkeypatch, capsys, arguments):
    read, transition, init_pool, shutdown_pool = _install_runtime(monkeypatch)
    read.return_value = {
        "id": MAX_SIGNED_64,
        "status": "requested",
        "lifecycle_origin": "local",
        "projection_version": 3,
    }

    assert process_feedback.main(arguments) == 0

    assert json.loads(capsys.readouterr().out) == {
        "applied": False,
        "current_status": "requested",
        "feedback_id": MAX_SIGNED_64,
        "proposed_status": "in_progress",
        "proposed_task_stage": "In Progress",
        "task_queued": False,
        "task_sync_state": "pending",
    }
    read.assert_called_once_with(MAX_SIGNED_64)
    transition.assert_not_called()
    init_pool.assert_called_once_with()
    shutdown_pool.assert_called_once_with()


def test_parser_failures_do_not_echo_caller_controlled_values(monkeypatch, capsys):
    private_value = "GPI-PM-FB-17-private-secret"
    read = MagicMock(side_effect=AssertionError("store read attempted"))
    monkeypatch.setattr(feedback_lifecycle.feedback_store, "lifecycle_state", read)

    with pytest.raises(SystemExit):
        process_feedback.main(["--source-id", private_value])

    captured = capsys.readouterr()
    assert private_value not in captured.out
    assert private_value not in captured.err
    assert "private-secret" not in captured.out
    assert "private-secret" not in captured.err
    read.assert_not_called()


def test_start_preview_is_read_only_and_reports_only_safe_fields(monkeypatch, capsys):
    read, transition, init_pool, shutdown_pool = _install_runtime(monkeypatch)

    assert process_feedback.main(["--source-id", "GPI-PM-FB-17"]) == 0

    assert json.loads(capsys.readouterr().out) == {
        "applied": False,
        "current_status": "requested",
        "feedback_id": 17,
        "proposed_status": "in_progress",
        "proposed_task_stage": "In Progress",
        "task_queued": False,
        "task_sync_state": "synced",
    }
    read.assert_called_once_with(17)
    transition.assert_not_called()
    init_pool.assert_called_once_with()
    shutdown_pool.assert_called_once_with()


def test_start_yes_uses_the_authoritative_local_transition(monkeypatch, capsys):
    _read, transition, _init_pool, _shutdown_pool = _install_runtime(monkeypatch)

    assert process_feedback.main(["--feedback-id", "17", "--yes"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["applied"] is True
    assert payload["task_queued"] is True
    assert payload["proposed_task_stage"] == "In Progress"
    transition.assert_called_once_with(
        feedback_id=17,
        status="in_progress",
        actor=DEFAULT_ACTOR,
        resolution_note=None,
        after_image=None,
        now=NOW,
    )


@pytest.mark.parametrize("yes", [False, True])
def test_start_is_a_successful_noop_when_already_in_progress(
    monkeypatch, capsys, yes
):
    _read, transition, _init_pool, _shutdown_pool = _install_runtime(
        monkeypatch, status="in_progress"
    )
    argv = ["--feedback-id", "17", *(["--yes"] if yes else [])]

    assert process_feedback.main(argv) == 0

    assert json.loads(capsys.readouterr().out) == {
        "applied": False,
        "current_status": "in_progress",
        "feedback_id": 17,
        "proposed_status": "in_progress",
        "proposed_task_stage": "In Progress",
        "task_queued": False,
        "task_sync_state": "synced",
    }
    transition.assert_not_called()


@pytest.mark.parametrize("status", ["completed", "declined"])
def test_start_does_not_reopen_terminal_feedback(monkeypatch, capsys, status):
    _read, transition, _init_pool, _shutdown_pool = _install_runtime(
        monkeypatch, status=status
    )

    assert process_feedback.main(["--feedback-id", "17", "--yes"]) == 1

    assert json.loads(capsys.readouterr().out) == {
        "applied": False,
        "current_status": status,
        "feedback_id": 17,
        "proposed_status": status,
        "proposed_task_stage": "Done",
        "task_queued": False,
        "task_sync_state": "synced",
    }
    transition.assert_not_called()


@pytest.mark.parametrize("note", ["", " ", "\t\n"])
def test_finish_requires_a_nonblank_note_before_store_access(monkeypatch, note):
    read = MagicMock(side_effect=AssertionError("store read attempted"))
    monkeypatch.setattr(feedback_lifecycle.feedback_store, "lifecycle_state", read)

    with pytest.raises(SystemExit):
        resolve_feedback.main(["--feedback-id", "17", "--note", note])

    read.assert_not_called()


def test_finish_preview_is_read_only_and_defaults_the_closer(monkeypatch, capsys):
    _read, transition, _init_pool, _shutdown_pool = _install_runtime(
        monkeypatch, status="in_progress"
    )

    assert resolve_feedback.main(
        ["--source-id", "GPI-PM-FB-17", "--note", "Fixed safely"]
    ) == 0

    assert json.loads(capsys.readouterr().out) == {
        "applied": False,
        "current_status": "in_progress",
        "feedback_id": 17,
        "proposed_status": "completed",
        "proposed_task_stage": "Done",
        "task_queued": False,
        "task_sync_state": "synced",
    }
    transition.assert_not_called()


def test_finish_yes_passes_the_real_closer_and_required_note(monkeypatch, capsys):
    _read, transition, _init_pool, _shutdown_pool = _install_runtime(
        monkeypatch, status="in_progress"
    )

    assert resolve_feedback.main(
        [
            "--feedback-id",
            "17",
            "--note",
            "  Fixed safely  ",
            "--by",
            "closer@gruberpallets.com",
            "--yes",
        ]
    ) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["applied"] is True
    assert payload["task_queued"] is True
    assert payload["proposed_task_stage"] == "Done"
    transition.assert_called_once_with(
        feedback_id=17,
        status="completed",
        actor="closer@gruberpallets.com",
        resolution_note="Fixed safely",
        after_image=None,
        now=NOW,
    )


def test_finish_yes_defaults_the_closer(monkeypatch):
    _read, transition, _init_pool, _shutdown_pool = _install_runtime(
        monkeypatch, status="in_progress"
    )

    assert resolve_feedback.main(
        ["--feedback-id", "17", "--note", "Done", "--yes"]
    ) == 0

    assert transition.call_args.kwargs["actor"] == DEFAULT_ACTOR


@pytest.mark.parametrize("status", ["requested", "completed", "declined"])
def test_finish_cannot_skip_start_or_change_terminal_feedback(
    monkeypatch, capsys, status
):
    _read, transition, _init_pool, _shutdown_pool = _install_runtime(
        monkeypatch, status=status
    )

    assert resolve_feedback.main(
        ["--feedback-id", "17", "--note", "Done", "--yes"]
    ) == 1

    assert json.loads(capsys.readouterr().out) == {
        "applied": False,
        "current_status": status,
        "feedback_id": 17,
        "proposed_status": status,
        "proposed_task_stage": {
            "requested": "New",
            "completed": "Done",
            "declined": "Done",
        }[status],
        "task_queued": False,
        "task_sync_state": "synced",
    }
    transition.assert_not_called()


def test_local_authority_failure_is_private_and_always_closes_the_pool(
    monkeypatch, capsys
):
    read, transition, init_pool, shutdown_pool = _install_runtime(monkeypatch)
    read.side_effect = feedback_store.InvalidTransition(
        "legacy row contains private feedback text"
    )

    with pytest.raises(SystemExit) as caught:
        process_feedback.main(["--feedback-id", "17", "--yes"])

    assert str(caught.value) == "feedback lifecycle command failed safely"
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    captured = capsys.readouterr()
    assert "private feedback" not in captured.out
    assert "private feedback" not in captured.err
    transition.assert_not_called()
    init_pool.assert_called_once_with()
    shutdown_pool.assert_called_once_with()


def test_pool_cleanup_failure_stays_inside_the_private_error_boundary(
    monkeypatch, capsys
):
    _read, transition, init_pool, shutdown_pool = _install_runtime(monkeypatch)
    shutdown_pool.side_effect = RuntimeError("private database address")

    with pytest.raises(SystemExit) as caught:
        process_feedback.main(["--feedback-id", "17"])

    assert str(caught.value) == "feedback lifecycle command failed safely"
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    captured = capsys.readouterr()
    assert "private database address" not in captured.out
    assert "private database address" not in captured.err
    transition.assert_not_called()
    init_pool.assert_called_once_with()
    shutdown_pool.assert_called_once_with()


def test_lifecycle_scripts_do_not_import_or_construct_an_odoo_client():
    script_paths = (
        Path("scripts/feedback_lifecycle.py"),
        Path("scripts/process_feedback.py"),
        Path("scripts/resolve_feedback.py"),
    )
    imported_modules = []
    loaded_names = []
    for path in script_paths:
        tree = ast.parse(path.read_text())
        imported_modules.extend(
            node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
        )
        imported_modules.extend(
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        )
        loaded_names.extend(
            node.id for node in ast.walk(tree) if isinstance(node, ast.Name)
        )

    assert all("odoo" not in module.lower() for module in imported_modules)
    assert "ImprovementsClient" not in loaded_names
