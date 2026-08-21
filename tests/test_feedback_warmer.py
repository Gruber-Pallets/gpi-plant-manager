import asyncio
import os
import subprocess
import sys
import threading
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from zira_dashboard import app as app_module


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


def test_app_import_does_not_eagerly_import_feedback_worker():
    project_root = Path(__file__).resolve().parents[1]
    safe_env = {
        key: value
        for key, value in os.environ.items()
        if key not in SERVICE_ENV_NAMES
        and key
        not in {
            "ODOO_SHARED_REPORTING_WRITE_ENABLED",
            "ODOO_IMPROVEMENTS_WRITE_ENABLED",
        }
    }
    safe_env.update(
        {
            "DATABASE_URL": "",
            "FEEDBACK_SYNC_TEST_DATABASE": "",
            "ODOO_SHARED_REPORTING_WRITE_ENABLED": "false",
            "ODOO_IMPROVEMENTS_WRITE_ENABLED": "false",
            "PYTHONPATH": str(project_root / "src"),
            "ZIRA_API_KEY": "test-dummy-zira-key",
            "ZIRA_BASE_URL": "http://127.0.0.1:9",
        }
    )
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys; import zira_dashboard.app; "
                "assert 'zira_dashboard.feedback_sync' not in sys.modules"
            ),
        ],
        cwd=project_root,
        env=safe_env,
        check=False,
        capture_output=True,
        text=True,
        timeout=20,
    )

    assert completed.returncode == 0, completed.stderr


def test_feedback_warmer_is_registered_once_at_sixty_seconds():
    matches = [item for item in app_module._WARMERS if item[0] == "feedback Odoo mirror"]

    assert matches == [("feedback Odoo mirror", app_module._tick_feedback_sync, 60)]


def test_feedback_tick_calls_only_run_batch_through_to_thread(monkeypatch):
    from zira_dashboard import feedback_sync

    run = MagicMock()
    to_thread = AsyncMock()
    monkeypatch.setattr(feedback_sync, "run_batch", run)
    monkeypatch.setattr(app_module.asyncio, "to_thread", to_thread)

    asyncio.run(app_module._tick_feedback_sync())

    to_thread.assert_awaited_once_with(run)
    run.assert_not_called()


def test_feedback_tick_runs_batch_off_event_loop(monkeypatch):
    from zira_dashboard import feedback_sync

    event_loop_thread = threading.get_ident()
    called_from = []
    monkeypatch.setattr(
        feedback_sync,
        "run_batch",
        lambda: called_from.append(threading.get_ident()),
    )

    asyncio.run(app_module._tick_feedback_sync())

    assert len(called_from) == 1
    assert called_from[0] != event_loop_thread


def test_feedback_tick_with_closed_gates_does_no_worker_work(monkeypatch):
    from zira_dashboard import feedback_sync
    from zira_dashboard import feedback_sync_store as sync_store

    poison = MagicMock(side_effect=AssertionError("closed tick must do no work"))
    monkeypatch.setattr(feedback_sync.socket, "gethostname", poison)
    monkeypatch.setattr(feedback_sync.os, "getpid", poison)
    monkeypatch.setattr(feedback_sync.ImprovementsClient, "from_env", poison)
    monkeypatch.setattr(feedback_sync.ImprovementsClient, "default_executor", poison)
    monkeypatch.setattr(sync_store, "recover_expired_claims", poison)
    monkeypatch.setattr(sync_store, "claim_due", poison)
    monkeypatch.setattr(feedback_sync, "process_claim", poison)

    asyncio.run(app_module._tick_feedback_sync())

    poison.assert_not_called()
