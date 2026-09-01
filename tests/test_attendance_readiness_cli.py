"""Safety contract for the read-only attendance readiness command."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
import importlib

import pytest

from zira_dashboard import (
    _zira_persist,
    app_settings,
    attendance_location_policy,
    attendance_readiness,
    odoo_client,
    leaderboard,
    production_history,
)


def _report(*, ready: bool) -> attendance_readiness.ReadinessReport:
    return attendance_readiness.ReadinessReport(
        ready=ready,
        mirror_age_seconds=12.0,
        last_full_sweep_age_seconds=30.0,
        open_rows_not_refreshed=0,
        last_sweep_deletion_count=0,
        projection_lag_seconds=1.0,
        recalc_queue_age_seconds=None,
        recalc_queue_depth=0,
        open_conflicts=0,
        conflict_minutes_today=0.0,
        open_unmapped=0,
        unmapped_minutes_today=0.0,
        open_missing_required=0,
        missing_minutes_today=0.0,
        unassigned_units_today=0.0,
        oldest_unassigned_age_seconds=None,
        shadow_changed_worker_units=0.0,
        failed_corrections=0,
        correction_retries_today=0,
        correction_verification_failures_today=0,
        failed_department_repairs=0,
        blockers=() if ready else ("baseline_incomplete",),
    )


def test_readiness_command_initializes_config_and_pool_then_prints_json(monkeypatch, capsys):
    try:
        command = importlib.import_module("scripts.check_attendance_location_readiness")
    except ImportError:
        command = None
    assert command is not None, "the planned readiness command must exist"

    events: list[object] = []
    report = _report(ready=True)
    now = datetime(2026, 8, 31, 15, 0, tzinfo=UTC)

    monkeypatch.setattr(
        command,
        "load_dotenv",
        lambda *, dotenv_path, override: events.append(("config", dotenv_path, override)),
    )
    monkeypatch.setattr(command.db, "init_pool", lambda: events.append("pool"))
    monkeypatch.setattr(
        command.attendance_readiness,
        "build_report",
        lambda observed_at: events.append(("report", observed_at)) or report,
    )
    monkeypatch.setattr(command, "_utc_now", lambda: now)

    assert command.main() == 0

    assert events == [
        ("config", command.ROOT / ".env", False),
        "pool",
        ("report", now),
    ]
    assert capsys.readouterr().out == attendance_readiness.report_json(report) + "\n"


def test_blocked_readiness_exits_nonzero_and_never_calls_write_apis(monkeypatch, capsys):
    command = importlib.import_module("scripts.check_attendance_location_readiness")
    report = _report(ready=False)

    monkeypatch.setattr(command, "load_dotenv", lambda **_kwargs: None)
    monkeypatch.setattr(command.db, "init_pool", lambda: None)
    monkeypatch.setattr(command.attendance_readiness, "build_report", lambda _now: report)

    def unexpected_write(*_args, **_kwargs):
        pytest.fail("the readiness command attempted a write API")

    monkeypatch.setattr(command.db, "bootstrap_schema", unexpected_write)
    monkeypatch.setattr(command.db, "execute", unexpected_write)
    monkeypatch.setattr(app_settings, "set_setting", unexpected_write)
    monkeypatch.setattr(attendance_location_policy, "set_rollout_config", unexpected_write)
    monkeypatch.setattr(attendance_readiness, "schedule_live_cutover", unexpected_write)
    monkeypatch.setattr(attendance_readiness, "activate_due_cutover", unexpected_write)
    monkeypatch.setattr(odoo_client, "execute", unexpected_write)

    assert command.main() == 1

    output = capsys.readouterr().out
    assert '"ready":false' in output
    assert '"blockers":["baseline_incomplete"]' in output


def test_readiness_meter_collection_bypasses_all_persistent_caches(monkeypatch):
    from zira_dashboard.stations import Station

    day = date(2026, 8, 31)
    start = datetime(2026, 8, 31, 12, tzinfo=UTC)
    frozen = production_history.StrictSourceSnapshot(
        day=day,
        shift_start_utc=start,
        shift_end_utc=start + timedelta(hours=8),
        break_windows=(),
        shift_by_day={day: (True, datetime.min.time(), datetime.max.time(), ())},
        stations=(Station("meter-1", "Repair 4", "Repair", "R4"),),
        work_center_by_odoo_id={44: "Repair 4"},
        source_fingerprint="canonical-read-only-source",
    )
    raw_calls = []
    monkeypatch.setattr(
        leaderboard,
        "leaderboard",
        lambda client, stations, requested_day, now_utc, **kwargs: (
            raw_calls.append((client, tuple(stations), requested_day, now_utc, kwargs))
            or []
        ),
    )

    def unexpected_write(*_args, **_kwargs):
        pytest.fail("readiness meter collection touched a persistent cache/write")

    monkeypatch.setattr(leaderboard, "cached_leaderboard", unexpected_write)
    monkeypatch.setattr(_zira_persist, "save_day", unexpected_write)
    monkeypatch.setattr(app_settings, "set_setting", unexpected_write)
    monkeypatch.setattr(odoo_client, "execute", unexpected_write)

    client, rows = attendance_readiness._freeze_leaderboard_rows(  # noqa: SLF001
        day,
        object(),
        now_utc=start,
        frozen_production_day=frozen,
    )

    assert client is not None
    assert rows == ()
    assert len(raw_calls) == 1
    assert raw_calls[0][2:] == (day, start, {"shift_by_day": frozen.shift_by_day})
