from __future__ import annotations

from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, date, datetime, time, timedelta
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from zira_dashboard import (
    app as app_module,
    attendance_exceptions,
    attendance_location_policy,
    attendance_mirror,
    attendance_readiness,
    db,
    exception_inbox,
    inbox_reconcile,
    precompute,
    production_history,
    shift_config,
    wc_attributions,
)
from zira_dashboard.routes import settings


NOW = datetime(2026, 8, 31, 14, 0, tzinfo=UTC)
DAY = NOW.astimezone(shift_config.SITE_TZ).date()


def _ready_inputs(**changes):
    values = attendance_readiness._ReadinessInputs(
        rollout_mode="shadow",
        rollout_valid=True,
        baseline_completed_at=NOW - timedelta(days=2),
        last_incremental_completed_at=NOW - timedelta(seconds=20),
        last_full_sweep_completed_at=NOW - timedelta(minutes=30),
        last_sweep_deletion_count=0,
        open_rows_not_refreshed=0,
        projection_completed_at=NOW - timedelta(seconds=10),
        recalc_queue_requested_at=None,
        recalc_queue_depth=0,
        open_conflicts=0,
        conflict_seconds_today=0.0,
        open_unmapped=0,
        unmapped_seconds_today=0.0,
        open_missing_required=0,
        missing_seconds_today=0.0,
        unassigned_units_today=0.0,
        oldest_unassigned_at=None,
        shadow_changed_worker_units=0.0,
        failed_corrections=0,
        correction_retries_today=0,
        correction_verification_failures_today=0,
        failed_department_repairs=0,
        shadow_day=DAY,
        shadow_complete_days=1,
        shadow_error=None,
    )
    return replace(values, **changes)


@contextmanager
def _cursor(marker):
    yield marker


def test_build_report_uses_one_frozen_local_database_snapshot(monkeypatch):
    marker = _ActivationCursor()
    seen = []
    monkeypatch.setattr(attendance_readiness.db, "cursor", lambda: _cursor(marker))
    monkeypatch.setattr(
        attendance_readiness,
        "_read_inputs_cur",
        lambda cur, now: seen.append((cur, now)) or _ready_inputs(),
    )
    monkeypatch.setattr(
        attendance_readiness.db,
        "query",
        lambda *_a, **_k: pytest.fail("build_report escaped its coherent cursor"),
    )

    report = attendance_readiness.build_report(NOW)

    assert report.ready is True
    assert report.blockers == ()
    assert seen == [(marker, NOW)]
    assert report.mirror_age_seconds == 20.0
    assert report.last_full_sweep_age_seconds == 1800.0
    assert report.projection_lag_seconds == 10.0
    assert marker.operations == [
        ("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY", None)
    ]


@pytest.mark.parametrize(
    ("changes", "blocker"),
    [
        ({"rollout_valid": False}, "rollout_state_unavailable"),
        ({"baseline_completed_at": None}, "attendance_baseline_incomplete"),
        (
            {"last_incremental_completed_at": NOW - timedelta(seconds=91)},
            "attendance_mirror_stale",
        ),
        (
            {"last_full_sweep_completed_at": NOW - timedelta(hours=2, seconds=1)},
            "attendance_full_sweep_stale",
        ),
        ({"open_rows_not_refreshed": 1}, "attendance_open_rows_not_refreshed"),
        (
            {"projection_completed_at": NOW - timedelta(seconds=91)},
            "attendance_projection_stale",
        ),
        (
            {
                "recalc_queue_depth": 1,
                "recalc_queue_requested_at": NOW - timedelta(minutes=16),
            },
            "attendance_recalculation_stuck",
        ),
        ({"failed_corrections": 1}, "attendance_correction_failed"),
        ({"failed_department_repairs": 1}, "attendance_department_repair_failed"),
        ({"open_conflicts": 1}, "attendance_conflicts_open"),
        ({"open_unmapped": 1}, "attendance_unmapped_location"),
        ({"open_missing_required": 1}, "attendance_required_location_missing"),
        ({"unassigned_units_today": 1.0}, "unassigned_production"),
        ({"shadow_complete_days": 0}, "shadow_comparison_day_incomplete"),
        ({"shadow_error": "source failed"}, "shadow_comparison_unavailable"),
    ],
)
def test_hard_readiness_conditions_fail_closed(monkeypatch, changes, blocker):
    monkeypatch.setattr(attendance_readiness, "_read_inputs", lambda _now: _ready_inputs(**changes))

    report = attendance_readiness.build_report(NOW)

    assert report.ready is False
    assert blocker in report.blockers


def test_shadow_completion_must_follow_the_exact_observation_epoch_even_same_day():
    entered_shadow = datetime(2026, 8, 31, 12, 0, tzinfo=UTC)
    clean_day = date(2026, 8, 31)

    assert (
        attendance_readiness._complete_shadow_day_count(
            [clean_day.isoformat()],
            rollout_mode="shadow",
            rollout_updated_at=entered_shadow,
            completed_at_by_day={clean_day: entered_shadow - timedelta(microseconds=1)},
        )
        == 0
    )
    assert (
        attendance_readiness._complete_shadow_day_count(
            [clean_day.isoformat()],
            rollout_mode="shadow",
            rollout_updated_at=entered_shadow,
            completed_at_by_day={clean_day: entered_shadow},
        )
        == 1
    )
    assert (
        attendance_readiness._complete_shadow_day_count(
            ["2026-08-30"],
            rollout_mode="live",
            rollout_updated_at=entered_shadow + timedelta(days=1),
            completed_at_by_day={},
        )
        == 1
    )


def test_readiness_metrics_use_minutes_and_deterministic_blocker_order(monkeypatch):
    inputs = _ready_inputs(
        open_conflicts=2,
        conflict_seconds_today=150.0,
        open_unmapped=1,
        unmapped_seconds_today=90.0,
        open_missing_required=3,
        missing_seconds_today=210.0,
        unassigned_units_today=12.5,
        oldest_unassigned_at=NOW - timedelta(minutes=9),
        failed_corrections=1,
    )
    monkeypatch.setattr(attendance_readiness, "_read_inputs", lambda _now: inputs)

    first = attendance_readiness.build_report(NOW)
    second = attendance_readiness.build_report(NOW)

    assert first.blockers == second.blockers == tuple(sorted(first.blockers))
    assert first.conflict_minutes_today == 2.5
    assert first.unmapped_minutes_today == 1.5
    assert first.missing_minutes_today == 3.5
    assert first.oldest_unassigned_age_seconds == 540.0


def test_report_digest_binds_exact_report_and_cutover(monkeypatch):
    monkeypatch.setattr(attendance_readiness, "_read_inputs", lambda _now: _ready_inputs())
    report = attendance_readiness.build_report(NOW)
    cutover = NOW + timedelta(days=1)

    digest = attendance_readiness.report_digest(report, cutover)

    assert len(digest) == 64
    assert digest == attendance_readiness.report_digest(report, cutover)
    assert digest != attendance_readiness.report_digest(report, cutover + timedelta(days=1))


class _ShadowConfigCursor:
    def __init__(self, *, meter_id="meter-a", reverse=False):
        work_centers = [
            {
                "name": "WC A",
                "meter_id": meter_id,
                "odoo_work_center_id": 11,
                "odoo_work_center_name": "Luke A",
                "category": "Production",
                "cell": "A",
            },
            {
                "name": "WC B",
                "meter_id": "meter-b",
                "odoo_work_center_id": 22,
                "odoo_work_center_name": "Luke B",
                "category": "Production",
                "cell": "B",
            },
        ]
        departments = [
            {
                "name": "Production",
                "requires_work_center": True,
                "requires_work_center_explicit": True,
            },
            {
                "name": "Maintenance",
                "requires_work_center": False,
                "requires_work_center_explicit": True,
            },
        ]
        if reverse:
            work_centers.reverse()
            departments.reverse()
        self.sources = {
            "work_centers": work_centers,
            "departments": departments,
            "people": [
                {"odoo_id": 101, "department_name": "Maintenance"},
                {"odoo_id": 202, "department_name": "Production"},
            ],
            "global_schedule": [
                {
                    "shift_start": time(7),
                    "shift_end": time(15),
                    "work_weekdays": [0, 1, 2, 3, 4],
                    "breaks": [{"start": "09:00", "end": "09:10"}],
                }
            ],
            "saturday_schedule": [
                {
                    "shift_start": time(6),
                    "shift_end": time(12),
                    "breaks": [],
                }
            ],
            "schedules": [
                {
                    "day": DAY,
                    "published": True,
                    "custom_hours": {"start": "07:00", "end": "15:00"},
                    "published_snapshot": {"version": 2},
                }
            ],
            "company_holidays": [],
            "saturday_recruitments": [],
            "wc_time_attributions": [],
        }
        self.rows = []
        self.statements = []

    def execute(self, sql, _params=None):
        normalized = " ".join(sql.split())
        self.statements.append(normalized)
        source = next(
            (name for name in self.sources if f"FROM {name}" in normalized),
            None,
        )
        self.rows = list(self.sources.get(source, []))

    def fetchall(self):
        return self.rows


def _install_shadow_refresh_origin(
    monkeypatch,
    *,
    rollout=None,
    epoch=NOW - timedelta(days=2),
    mirror_incremental=NOW - timedelta(seconds=1),
    mirror_sweep=NOW - timedelta(minutes=1),
    mirror_generation=7,
    current_mirror_origin=None,
    current_rollout=None,
):
    config = attendance_readiness._shadow_config_snapshot_cur(_ShadowConfigCursor(), DAY)
    rollout = rollout or attendance_location_policy.RolloutConfig("shadow", None, None)
    health = attendance_mirror.MirrorHealth(
        last_incremental_completed_at=mirror_incremental,
        last_full_sweep_completed_at=mirror_sweep,
        baseline_completed_at=mirror_sweep,
        oldest_recalc_requested_at=None,
        last_error=None,
        full_sweep_generation=mirror_generation,
    )
    mirror = attendance_mirror.AttendanceMirrorSnapshot(health=health, rows=())
    monkeypatch.setattr(
        attendance_readiness,
        "_shadow_refresh_origin",
        lambda _day: attendance_readiness._ShadowRefreshOrigin(rollout, epoch, config),
    )
    monkeypatch.setattr(
        attendance_readiness.attendance_mirror,
        "snapshot_overlapping",
        lambda *_a, **_k: mirror,
    )
    monkeypatch.setattr(
        attendance_readiness,
        "_read_rollout_config_cur",
        lambda _cur, **_k: current_rollout or rollout,
    )
    monkeypatch.setattr(attendance_readiness, "_shadow_epoch_cur", lambda _cur: epoch)
    monkeypatch.setattr(
        attendance_readiness,
        "_shadow_config_snapshot_cur",
        lambda _cur, _day: config,
    )
    monkeypatch.setattr(
        attendance_readiness,
        "_mirror_origin_cur",
        lambda _cur: (
            current_mirror_origin
            if current_mirror_origin is not None
            else (mirror_incremental, mirror_sweep, mirror_generation)
        ),
    )
    return config, mirror


def test_shadow_config_digest_is_canonical_curated_and_privacy_safe():
    forward = _ShadowConfigCursor()
    reversed_rows = _ShadowConfigCursor(reverse=True)

    first = attendance_readiness._shadow_config_digest_cur(forward, DAY)
    second = attendance_readiness._shadow_config_digest_cur(reversed_rows, DAY)
    remapped = attendance_readiness._shadow_config_digest_cur(
        _ShadowConfigCursor(meter_id="meter-new"), DAY
    )

    assert first == second
    assert len(first) == 64
    assert remapped != first
    sql = " ".join(forward.statements).lower()
    assert "odoo_attendance" not in sql
    assert "schedule_assignments" not in sql
    assert "production_daily" not in sql
    people_sql = next(statement for statement in forward.statements if "FROM people" in statement)
    assert "odoo_id" in people_sql
    assert "department_name" in people_sql
    assert "full_name" not in people_sql
    assert "birthday" not in people_sql
    assert "published_snapshot" not in sql


def test_shadow_config_digest_changes_when_employee_wage_type_changes():
    before = _ShadowConfigCursor()
    after = _ShadowConfigCursor()
    after.sources["people"][1]["wage_type"] = "monthly"

    assert (
        attendance_readiness._shadow_config_digest_cur(before, DAY)
        != attendance_readiness._shadow_config_digest_cur(after, DAY)
    )


def test_shadow_day_origin_binds_exact_testing_and_breakdown_rows():
    testing = {
        "id": 41,
        "wc_name": "WC A",
        "person_name": "Testing",
        "employee_odoo_id": None,
        "start_utc": NOW - timedelta(minutes=20),
        "end_utc": NOW - timedelta(minutes=10),
        "source": "testing",
        "breakdown_id": None,
    }
    first_cursor = _ShadowConfigCursor()
    first_cursor.sources["wc_time_attributions"] = [testing]
    changed_cursor = _ShadowConfigCursor()
    changed_cursor.sources["wc_time_attributions"] = [
        {**testing, "end_utc": NOW - timedelta(minutes=5)}
    ]

    first = attendance_readiness._shadow_config_snapshot_cur(first_cursor, DAY)
    changed = attendance_readiness._shadow_config_snapshot_cur(changed_cursor, DAY)

    assert first.digest == changed.digest
    assert first.day_digest != changed.day_digest
    assert first.attribution_rows == (testing,)
    assert "person_name" not in first.day_digest


def test_shadow_global_epoch_survives_midnight_while_day_schedule_origin_is_targeted():
    current = attendance_readiness._shadow_config_snapshot_cur(_ShadowConfigCursor(), DAY)
    next_day = attendance_readiness._shadow_config_snapshot_cur(
        _ShadowConfigCursor(), DAY + timedelta(days=1)
    )
    edited_cursor = _ShadowConfigCursor()
    edited_cursor.sources["schedules"][0]["custom_hours"] = {
        "start": "06:00",
        "end": "14:00",
    }
    edited_cursor.sources["schedules"][0]["published_snapshot"] = {
        "assignments": [{"person_name": "Private Person"}]
    }
    edited = attendance_readiness._shadow_config_snapshot_cur(edited_cursor, DAY)

    assert next_day.digest == current.digest
    assert next_day.day_digest != current.day_digest
    assert edited.digest == current.digest
    assert edited.day_digest != current.day_digest


def test_shadow_projection_uses_the_same_fresh_mapping_and_department_snapshot_as_digest(
    monkeypatch,
):
    cursor = _ShadowConfigCursor()
    snapshot = attendance_readiness._shadow_config_snapshot_cur(cursor, DAY)
    start = NOW - timedelta(minutes=10)
    rows = (
        {
            "odoo_attendance_id": 1,
            "employee_odoo_id": 101,
            "employee_name": "Worker 101",
            "check_in_utc": start,
            "check_out_utc": None,
            "odoo_work_center_id": None,
            "odoo_work_center_name": None,
            "odoo_department_id": None,
            "odoo_department_name": None,
            "odoo_write_date": NOW,
        },
        {
            "odoo_attendance_id": 2,
            "employee_odoo_id": 202,
            "employee_name": "Worker 202",
            "check_in_utc": start,
            "check_out_utc": None,
            "odoo_work_center_id": 11,
            "odoo_work_center_name": "Luke A",
            "odoo_department_id": None,
            "odoo_department_name": "Production",
            "odoo_write_date": NOW,
        },
    )
    mirror = attendance_mirror.AttendanceMirrorSnapshot(
        health=attendance_mirror.MirrorHealth(
            last_incremental_completed_at=NOW,
            last_full_sweep_completed_at=NOW,
            baseline_completed_at=NOW,
            oldest_recalc_requested_at=None,
            last_error=None,
        ),
        rows=rows,
    )
    monkeypatch.setattr(
        attendance_readiness.attendance_timeline.work_centers_store,
        "app_work_center_name_for_odoo_id",
        lambda _wc_id: "STALE CACHE",
    )
    monkeypatch.setattr(
        attendance_location_policy,
        "department_requires_work_center",
        lambda _department: True,
    )

    spans = attendance_readiness._project_shadow_snapshot(snapshot, mirror, NOW)

    worker_101 = [span for span in spans if span.employee_odoo_id == 101]
    worker_202 = [span for span in spans if span.employee_odoo_id == 202]
    assert worker_101[-1].status == "exempt_no_location"
    assert worker_202[-1].status == "valid"
    assert worker_202[-1].app_work_center_name == "WC A"
    assert snapshot.digest == attendance_readiness._shadow_config_digest_cur(
        _ShadowConfigCursor(), DAY
    )


def test_readiness_config_lock_includes_employee_department_fallback_source():
    assert attendance_readiness._READINESS_CONFIG_TABLES[-4:] == (
        "work_centers",
        "departments",
        "people",
        "wc_time_attributions",
    )


def test_schedule_live_requires_future_configured_workday_boundary(monkeypatch):
    state = {"mode": "normal"}

    class BoundaryCursor(_ActivationCursor):
        def __init__(self):
            super().__init__()
            self.row = None

        def execute(self, sql, params=None):
            super().execute(sql, params)
            normalized = " ".join(sql.split())
            if "FROM global_schedule" in normalized:
                if state["mode"] == "unavailable":
                    raise RuntimeError("schedule unavailable")
                weekdays = [0] if state["mode"] == "closed" else [0, 1, 2, 3, 4]
                self.row = {"shift_start": time(7, 0), "work_weekdays": weekdays}
            elif "FROM saturday_schedule" in normalized:
                self.row = {"shift_start": time(6, 0)}
            elif any(
                table in normalized
                for table in (
                    "FROM schedules",
                    "FROM company_holidays",
                    "FROM saturday_recruitments",
                )
            ):
                self.row = None

        def fetchone(self):
            return self.row

    monkeypatch.setattr(
        attendance_readiness.db,
        "cursor",
        lambda: _cursor(BoundaryCursor()),
    )
    local_day = NOW.astimezone(shift_config.SITE_TZ).date() + timedelta(days=1)
    valid = datetime.combine(local_day, time(7, 0), tzinfo=shift_config.SITE_TZ).astimezone(UTC)

    with pytest.raises(ValueError, match="cutover_future_boundary_required"):
        attendance_readiness.schedule_live_cutover(NOW - timedelta(seconds=1), now_utc=NOW)
    with pytest.raises(ValueError, match="cutover_boundary_required"):
        attendance_readiness.schedule_live_cutover(valid + timedelta(minutes=1), now_utc=NOW)
    with pytest.raises(ValueError, match="cutover_timezone_required"):
        attendance_readiness.schedule_live_cutover(valid.replace(tzinfo=None), now_utc=NOW)
    state["mode"] = "closed"
    with pytest.raises(ValueError, match="cutover_workday_required"):
        attendance_readiness.schedule_live_cutover(valid, now_utc=NOW)
    state["mode"] = "unavailable"
    with pytest.raises(ValueError, match="cutover_workday_unavailable"):
        attendance_readiness.schedule_live_cutover(valid, now_utc=NOW)


def test_schedule_live_locks_setting_builds_fresh_report_and_saves_one_gate(monkeypatch):
    marker = _ActivationCursor()
    saved = []
    status_saved = []
    calls = []
    cutover = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
    report = attendance_readiness._report_from_inputs(_ready_inputs(), NOW)
    monkeypatch.setattr(shift_config, "shift_start_for", lambda _day: time(7, 0))
    monkeypatch.setattr(shift_config, "is_workday", lambda _day: True)
    monkeypatch.setattr(attendance_readiness.db, "cursor", lambda: _cursor(marker))
    monkeypatch.setattr(
        attendance_readiness,
        "_lock_rollout_config_cur",
        lambda cur: (
            calls.append(("lock", cur))
            or attendance_location_policy.RolloutConfig("shadow", None, None)
        ),
    )
    monkeypatch.setattr(
        attendance_readiness,
        "_build_report_cur",
        lambda cur, now: calls.append(("report", cur, now)) or report,
    )
    monkeypatch.setattr(
        attendance_location_policy,
        "set_rollout_config",
        lambda config, *, cur=None: saved.append((config, cur)),
    )
    monkeypatch.setattr(
        attendance_readiness.app_settings,
        "set_setting",
        lambda key, value, *, cur=None: status_saved.append((key, value, cur)),
    )
    monkeypatch.setattr(attendance_readiness, "_utc_now", lambda: NOW + timedelta(seconds=1))
    monkeypatch.setattr(
        attendance_readiness,
        "_validate_future_boundary_cur",
        lambda _cur, value, _now: value,
    )

    config = attendance_readiness.schedule_live_cutover(cutover, now_utc=NOW)

    assert calls == [("lock", marker), ("report", marker, NOW)]
    assert saved == [(config, marker)]
    assert status_saved[0][2] is marker
    assert config.mode == "live"
    assert config.cutover_at == cutover
    assert config.live_gate is not None
    assert config.live_gate.checked_at == NOW
    assert config.live_gate.activated_at is None
    assert config.live_gate.report_digest == attendance_readiness.report_digest(report, cutover)
    assert marker.operations == [
        ("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ", None),
        (
            "LOCK TABLE global_schedule, saturday_schedule, schedules, "
            "company_holidays, saturday_recruitments, work_centers, departments, people, "
            "wc_time_attributions IN SHARE MODE",
            None,
        ),
        ("SELECT pg_advisory_xact_lock(%s)", (attendance_readiness._READINESS_LOCK_ID,)),
    ]


def test_schedule_live_rejects_not_ready_stale_and_replayed_state(monkeypatch):
    marker = _ActivationCursor()
    monkeypatch.setattr(shift_config, "shift_start_for", lambda _day: time(7, 0))
    monkeypatch.setattr(shift_config, "is_workday", lambda _day: True)
    monkeypatch.setattr(attendance_readiness.db, "cursor", lambda: _cursor(marker))
    monkeypatch.setattr(
        attendance_readiness,
        "_lock_rollout_config_cur",
        lambda _cur: attendance_location_policy.RolloutConfig("shadow", None, None),
    )
    blocked = attendance_readiness._report_from_inputs(_ready_inputs(open_conflicts=1), NOW)
    cutover = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
    monkeypatch.setattr(attendance_readiness, "_build_report_cur", lambda _cur, _now: blocked)
    with pytest.raises(ValueError, match="live_readiness_blocked"):
        attendance_readiness.schedule_live_cutover(cutover, now_utc=NOW)

    ready = attendance_readiness._report_from_inputs(_ready_inputs(), NOW)
    monkeypatch.setattr(attendance_readiness, "_build_report_cur", lambda _cur, _now: ready)
    monkeypatch.setattr(attendance_readiness, "_utc_now", lambda: NOW + timedelta(minutes=6))
    with pytest.raises(ValueError, match="live_readiness_stale"):
        attendance_readiness.schedule_live_cutover(cutover, now_utc=NOW)

    monkeypatch.setattr(
        attendance_readiness,
        "_lock_rollout_config_cur",
        lambda _cur: attendance_location_policy.RolloutConfig(
            "live",
            NOW + timedelta(days=1),
            attendance_location_policy.LiveGate(NOW, "a" * 64, None),
        ),
    )
    with pytest.raises(ValueError, match="live_cutover_already_scheduled"):
        attendance_readiness.schedule_live_cutover(cutover, now_utc=NOW)


def test_schedule_locks_every_boundary_policy_table_before_validation_and_report(
    monkeypatch,
):
    cursor = _ActivationCursor()
    cutover = NOW + timedelta(days=1)
    ready = attendance_readiness._report_from_inputs(_ready_inputs(), NOW)
    expected_prefix = [
        ("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ", None),
        (
            "LOCK TABLE global_schedule, saturday_schedule, schedules, "
            "company_holidays, saturday_recruitments, work_centers, departments, people, "
            "wc_time_attributions IN SHARE MODE",
            None,
        ),
        ("SELECT pg_advisory_xact_lock(%s)", (attendance_readiness._READINESS_LOCK_ID,)),
    ]
    monkeypatch.setattr(attendance_readiness.db, "cursor", lambda: _cursor(cursor))

    def validate(_cur, value, _now):
        assert cursor.operations == expected_prefix
        return value

    def locked_rollout(_cur):
        assert cursor.operations == expected_prefix
        return attendance_location_policy.RolloutConfig("shadow", None, None)

    def build_report(_cur, _now):
        assert cursor.operations == expected_prefix
        return ready

    monkeypatch.setattr(attendance_readiness, "_validate_future_boundary_cur", validate)
    monkeypatch.setattr(attendance_readiness, "_lock_rollout_config_cur", locked_rollout)
    monkeypatch.setattr(attendance_readiness, "_build_report_cur", build_report)
    monkeypatch.setattr(attendance_readiness, "_utc_now", lambda: NOW + timedelta(seconds=1))
    monkeypatch.setattr(
        attendance_location_policy,
        "set_rollout_config",
        lambda *_a, **_k: None,
    )
    monkeypatch.setattr(
        attendance_readiness.app_settings,
        "set_setting",
        lambda *_a, **_k: None,
    )

    attendance_readiness.schedule_live_cutover(cutover, now_utc=NOW)

    assert cursor.operations == expected_prefix


def test_activation_locks_every_boundary_policy_table_before_gate_and_report_reads(
    monkeypatch,
):
    cursor = _ActivationCursor()
    cutover = NOW - timedelta(minutes=1)
    ready = attendance_readiness._report_from_inputs(_ready_inputs(), NOW)
    expected_prefix = [
        ("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ", None),
        (
            "LOCK TABLE global_schedule, saturday_schedule, schedules, "
            "company_holidays, saturday_recruitments, work_centers, departments, people, "
            "wc_time_attributions IN SHARE MODE",
            None,
        ),
        ("SELECT pg_advisory_xact_lock(%s)", (attendance_readiness._READINESS_LOCK_ID,)),
    ]
    monkeypatch.setattr(attendance_readiness.db, "cursor", lambda: _cursor(cursor))

    def locked_rollout(_cur):
        assert cursor.operations == expected_prefix
        return _pending_live(cutover)

    def build_report(_cur, _now):
        assert cursor.operations == expected_prefix
        return ready

    monkeypatch.setattr(attendance_readiness, "_lock_rollout_config_cur", locked_rollout)
    monkeypatch.setattr(attendance_readiness, "_build_report_cur", build_report)
    monkeypatch.setattr(
        attendance_readiness,
        "_validate_configured_boundary_cur",
        lambda _cur, value: value,
    )
    monkeypatch.setattr(attendance_readiness, "_enqueue_cutover_cur", lambda *_a: None)
    monkeypatch.setattr(attendance_readiness, "_clear_blocked_cur", lambda *_a: None)
    monkeypatch.setattr(
        attendance_location_policy,
        "set_rollout_config",
        lambda *_a, **_k: None,
    )
    monkeypatch.setattr(
        attendance_readiness.app_settings,
        "set_setting",
        lambda *_a, **_k: None,
    )

    result = attendance_readiness.activate_due_cutover(NOW)

    assert result.status == "activated"


def test_schedule_waits_for_config_writer_before_snapshot_and_reads_committed_boundary(
    monkeypatch,
):
    local_day = date(2026, 9, 1)
    cutover = datetime.combine(local_day, time(5, 30), tzinfo=shift_config.SITE_TZ).astimezone(UTC)
    report = attendance_readiness._report_from_inputs(_ready_inputs(), NOW)

    class InterleavingCursor:
        def __init__(self):
            self.operations = []
            self.writer_committed = False
            self.row = None

        def execute(self, sql, params=None):
            normalized = " ".join(sql.split())
            self.operations.append((normalized, params))
            if normalized.startswith("LOCK TABLE"):
                # Simulate a writer that was already in flight and commits
                # before this SHARE lock is granted.
                self.writer_committed = True
                self.row = None
            elif "pg_advisory_xact_lock" in normalized:
                assert self.writer_committed, "RR snapshot was established before writer commit"
                self.row = None
            elif "FROM global_schedule" in normalized:
                assert self.writer_committed
                self.row = {"shift_start": time(5, 30), "work_weekdays": [1]}
            elif "FROM saturday_schedule" in normalized:
                self.row = {"shift_start": time(6, 0)}
            elif "FROM schedules" in normalized:
                self.row = None
            elif "FROM company_holidays" in normalized:
                self.row = None
            elif "FROM saturday_recruitments" in normalized:
                self.row = None
            else:
                self.row = None

        def fetchone(self):
            return self.row

    cursor = InterleavingCursor()
    monkeypatch.setattr(attendance_readiness.db, "cursor", lambda: _cursor(cursor))
    monkeypatch.setattr(
        attendance_readiness,
        "_lock_rollout_config_cur",
        lambda _cur: attendance_location_policy.RolloutConfig("shadow", None, None),
    )
    monkeypatch.setattr(attendance_readiness, "_build_report_cur", lambda *_a: report)
    monkeypatch.setattr(attendance_readiness, "_utc_now", lambda: NOW + timedelta(seconds=1))
    monkeypatch.setattr(attendance_location_policy, "set_rollout_config", lambda *_a, **_k: None)
    monkeypatch.setattr(attendance_readiness.app_settings, "set_setting", lambda *_a, **_k: None)

    saved = attendance_readiness.schedule_live_cutover(cutover, now_utc=NOW)

    assert saved.cutover_at == cutover
    assert cursor.operations[1][0].startswith("LOCK TABLE")
    assert "pg_advisory_xact_lock" in cursor.operations[2][0]


@pytest.mark.parametrize(
    (
        "day",
        "schedule",
        "holiday",
        "recruitment",
        "expected_start",
        "expected_workday",
    ),
    [
        (date(2026, 9, 1), None, None, None, time(7, 0), True),
        (
            date(2026, 9, 1),
            {"published": True, "custom_hours": {"start": "05:30"}},
            None,
            None,
            time(5, 30),
            True,
        ),
        (
            date(2026, 9, 5),
            {"published": False, "custom_hours": None},
            None,
            {"day_kind": "saturday", "holiday_odoo_id": None, "status": "closed"},
            time(7, 0),
            False,
        ),
        (
            date(2026, 9, 5),
            {"published": True, "custom_hours": None},
            None,
            {"day_kind": "saturday", "holiday_odoo_id": None, "status": "published"},
            time(6, 0),
            True,
        ),
        (
            date(2026, 9, 2),
            {"published": True, "custom_hours": None},
            {"odoo_id": 42},
            {"day_kind": "holiday", "holiday_odoo_id": 42, "status": "published"},
            time(6, 0),
            True,
        ),
        (
            date(2026, 9, 2),
            {"published": True, "custom_hours": None},
            {"odoo_id": 42},
            {"day_kind": "holiday", "holiday_odoo_id": 99, "status": "published"},
            time(7, 0),
            False,
        ),
    ],
    ids=[
        "normal-weekday",
        "published-custom-hours",
        "closed-saturday",
        "published-saturday",
        "dual-published-holiday",
        "mismatched-holiday-publication",
    ],
)
def test_locked_boundary_resolution_matches_operational_workday_semantics(
    day,
    schedule,
    holiday,
    recruitment,
    expected_start,
    expected_workday,
):
    class BoundaryCursor:
        def __init__(self):
            self.row = None

        def execute(self, sql, params=None):
            normalized = " ".join(sql.split())
            if "FROM global_schedule" in normalized:
                self.row = {"shift_start": time(7, 0), "work_weekdays": [0, 1, 2, 3, 4]}
            elif "FROM saturday_schedule" in normalized:
                self.row = {"shift_start": time(6, 0)}
            elif "FROM schedules" in normalized:
                assert params == (day,)
                self.row = schedule
            elif "FROM company_holidays" in normalized:
                assert params == (day, day)
                self.row = holiday
            elif "FROM saturday_recruitments" in normalized:
                assert params == (day,)
                self.row = recruitment
            else:
                raise AssertionError(normalized)

        def fetchone(self):
            return self.row

    assert attendance_readiness._configured_boundary_cur(BoundaryCursor(), day) == (
        expected_start,
        expected_workday,
    )


def test_locked_boundary_validation_ignores_stale_process_cache(monkeypatch):
    day = date(2026, 9, 1)

    class FreshCursor:
        def __init__(self):
            self.row = None

        def execute(self, sql, params=None):
            normalized = " ".join(sql.split())
            if "FROM global_schedule" in normalized:
                self.row = {"shift_start": time(5, 30), "work_weekdays": [1]}
            elif "FROM saturday_schedule" in normalized:
                self.row = {"shift_start": time(6, 0)}
            elif "FROM schedules" in normalized:
                self.row = None
            elif "FROM company_holidays" in normalized:
                self.row = None
            elif "FROM saturday_recruitments" in normalized:
                self.row = None
            else:
                raise AssertionError(normalized)

        def fetchone(self):
            return self.row

    monkeypatch.setattr(shift_config, "shift_start_for", lambda _day: time(7, 0))
    monkeypatch.setattr(shift_config, "is_workday", lambda _day: False)
    cutover = datetime.combine(day, time(5, 30), tzinfo=shift_config.SITE_TZ).astimezone(UTC)

    assert attendance_readiness._validate_configured_boundary_cur(FreshCursor(), cutover) == cutover


def test_recalculation_health_rejects_impossible_rows_without_hiding_a_zero_depth():
    completed = NOW - timedelta(minutes=2)
    rows = [
        {
            "attempt_count": 0,
            "requested_at": NOW - timedelta(minutes=4),
            "started_at": None,
            "completed_at": completed,
            "cache_started_at": None,
            "cache_ready_at": completed + timedelta(seconds=1),
            "last_error": None,
        },
        {
            "attempt_count": -1,
            "requested_at": NOW - timedelta(minutes=3),
            "started_at": None,
            "completed_at": completed,
            "cache_started_at": None,
            "cache_ready_at": completed + timedelta(seconds=1),
            "last_error": None,
        },
        {
            "attempt_count": 1,
            "requested_at": NOW - timedelta(minutes=2),
            "started_at": NOW - timedelta(minutes=1),
            "completed_at": completed,
            "cache_started_at": None,
            "cache_ready_at": completed + timedelta(seconds=1),
            "last_error": None,
        },
        {
            "attempt_count": 1,
            "requested_at": NOW - timedelta(minutes=1),
            "started_at": None,
            "completed_at": completed,
            "cache_started_at": NOW,
            "cache_ready_at": completed - timedelta(seconds=1),
            "last_error": "old failure",
        },
    ]

    class RecalcCursor:
        def __init__(self):
            self.result = None

        def execute(self, sql, params=None):
            normalized = " ".join(sql.split())
            assert "attempt_count < 0" in normalized
            assert "completed_at IS NULL" in normalized
            assert "cache_ready_at < completed_at" in normalized
            invalid = 0
            for row in rows:
                impossible = (
                    row["attempt_count"] < 0
                    or (
                        row["completed_at"] is None
                        and (
                            row["cache_started_at"] is not None or row["cache_ready_at"] is not None
                        )
                    )
                    or (
                        row["completed_at"] is not None
                        and (row["started_at"] is not None or row["last_error"] is not None)
                    )
                    or (
                        row["cache_ready_at"] is not None
                        and (
                            row["cache_started_at"] is not None
                            or row["completed_at"] is None
                            or row["cache_ready_at"] < row["completed_at"]
                        )
                    )
                )
                invalid += impossible
            pending = [
                row for row in rows if row["completed_at"] is None or row["cache_ready_at"] is None
            ]
            self.result = {
                "depth": len(pending),
                "oldest": min((row["requested_at"] for row in pending), default=None),
                "invalid": invalid,
            }

        def fetchone(self):
            return self.result

    assert attendance_readiness._recalculation_health_cur(RecalcCursor()) == {
        "depth": 0,
        "oldest": None,
        "invalid": 3,
    }


def test_invalid_recalculation_state_is_an_explicit_readiness_blocker():
    report = attendance_readiness._report_from_inputs(
        _ready_inputs(recalc_queue_depth=0, recalc_queue_invalid=1),
        NOW,
    )

    assert report.ready is False
    assert "attendance_recalculation_invalid" in report.blockers


def _valid_shadow_health_setting() -> dict:
    complete_day = DAY - timedelta(days=1)
    return {
        "day": DAY.isoformat(),
        "computed_at": NOW.isoformat(),
        "config_digest": "a" * 64,
        "day_config_digest": "b" * 64,
        "mirror_verified_through": (NOW - timedelta(seconds=1)).isoformat(),
        "mirror_full_sweep_completed_at": (NOW - timedelta(minutes=1)).isoformat(),
        "mirror_full_sweep_generation": 3,
        "shadow_epoch_at": (NOW - timedelta(days=2)).isoformat(),
        "complete_days": [complete_day.isoformat()],
        "complete_day_health": [
            {
                "day": complete_day.isoformat(),
                "completed_at": datetime.combine(
                    complete_day,
                    time(15),
                    tzinfo=shift_config.SITE_TZ,
                )
                .astimezone(UTC)
                .isoformat(),
                "schedule_digest": "c" * 64,
                "workday": True,
                "conflict_minutes": 0.0,
                "unmapped_minutes": 0.0,
                "missing_minutes": 0.0,
                "unassigned_units": 0.0,
                "clean": True,
            }
        ],
        "changed_worker_units": 0.0,
        "unassigned_units_today": 0.0,
        "oldest_unassigned_at": None,
    }


@pytest.mark.parametrize(
    "mutate",
    [
        lambda value: value.pop("day"),
        lambda value: value.pop("config_digest"),
        lambda value: value.update(config_digest="not-a-digest"),
        lambda value: value.pop("day_config_digest"),
        lambda value: value.pop("mirror_verified_through"),
        lambda value: value.update(computed_at="2026-08-31T14:00:00"),
        lambda value: value.update(computed_at=(NOW + timedelta(days=1)).isoformat()),
        lambda value: value.update(complete_days=[DAY.isoformat(), DAY.isoformat()]),
        lambda value: value.update(complete_days=[(DAY + timedelta(days=1)).isoformat()]),
        lambda value: value.update(
            complete_days=[(DAY - timedelta(days=offset)).isoformat() for offset in range(31)]
        ),
        lambda value: value.update(changed_worker_units=float("nan")),
        lambda value: value.update(unassigned_units_today=-1.0),
        lambda value: value.update(oldest_unassigned_at=(NOW - timedelta(minutes=1)).isoformat()),
        lambda value: value.update(unassigned_units_today=1.0, oldest_unassigned_at=None),
        lambda value: value.update(
            unassigned_units_today=1.0,
            oldest_unassigned_at=(NOW + timedelta(minutes=1)).isoformat(),
        ),
        lambda value: value.update(
            complete_day_health=value["complete_day_health"] * 2,
        ),
        lambda value: value["complete_day_health"][0].pop("missing_minutes"),
        lambda value: value["complete_day_health"][0].pop("completed_at"),
        lambda value: value["complete_day_health"][0].pop("schedule_digest"),
        lambda value: value.update(complete_days=[]),
        lambda value: value["complete_day_health"][0].update(
            day=(DAY + timedelta(days=1)).isoformat()
        ),
    ],
    ids=[
        "missing-day",
        "missing-config-digest",
        "malformed-config-digest",
        "missing-day-config-digest",
        "missing-mirror-origin",
        "naive-computed-at",
        "cross-day-computed-at",
        "duplicate-complete-day",
        "future-complete-day",
        "unbounded-complete-days",
        "non-finite-changed-units",
        "negative-unassigned-units",
        "oldest-present-with-zero-units",
        "oldest-missing-with-units",
        "oldest-after-computation",
        "duplicate-health-day",
        "missing-health-metric",
        "missing-health-completion",
        "missing-health-schedule-origin",
        "clean-list-without-matching-evidence",
        "health-after-stored-day",
    ],
)
def test_shadow_health_setting_rejects_cross_field_impossible_shapes(mutate):
    value = _valid_shadow_health_setting()
    mutate(value)

    with pytest.raises(ValueError, match="shadow comparison is malformed"):
        attendance_readiness._validate_shadow_health(value, NOW)


def test_shadow_health_setting_accepts_one_coherent_bounded_aggregate():
    validated = attendance_readiness._validate_shadow_health(
        _valid_shadow_health_setting(),
        NOW,
    )

    assert validated.day == DAY
    assert validated.computed_at == NOW
    assert validated.clean_days == (DAY - timedelta(days=1),)


def test_non_live_save_treats_only_a_missing_locked_rollout_as_initial_off(monkeypatch):
    cursor = _ActivationCursor()
    calls = []
    requested = attendance_location_policy.RolloutConfig("shadow", None, None)
    monkeypatch.setattr(attendance_readiness.db, "cursor", lambda: _cursor(cursor))
    monkeypatch.setattr(
        attendance_location_policy,
        "set_rollout_config",
        lambda config, *, cur=None: calls.append(("rollout", config, cur)),
    )
    monkeypatch.setattr(
        attendance_location_policy,
        "set_department_requirement",
        lambda name, required, *, cur=None: calls.append(("department", name, required, cur)),
    )

    saved = attendance_readiness.save_non_live_rollout(
        requested,
        {"Assembly": True},
        now_utc=NOW,
    )

    assert saved == requested
    assert calls == [
        ("rollout", requested, cursor),
        ("department", "Assembly", True, cursor),
    ]


def test_non_live_save_rejects_a_present_malformed_locked_rollout(monkeypatch):
    class PresentMalformedCursor(_ActivationCursor):
        def fetchone(self):
            return {"value": {"mode": "broken"}}

    cursor = PresentMalformedCursor()
    writes = []
    monkeypatch.setattr(attendance_readiness.db, "cursor", lambda: _cursor(cursor))
    monkeypatch.setattr(
        attendance_location_policy,
        "set_rollout_config",
        lambda *_a, **_k: writes.append("rollout"),
    )
    monkeypatch.setattr(
        attendance_location_policy,
        "set_department_requirement",
        lambda *_a, **_k: writes.append("department"),
    )

    with pytest.raises(ValueError, match="^rollout_state_unavailable$"):
        attendance_readiness.save_non_live_rollout(
            attendance_location_policy.RolloutConfig("shadow", None, None),
            {"Assembly": True},
            now_utc=NOW,
        )

    assert writes == []


def test_non_live_save_rejects_due_pending_cutover_before_rollout_or_department_write(
    monkeypatch,
):
    cursor = _ActivationCursor()
    writes = []
    monkeypatch.setattr(attendance_readiness.db, "cursor", lambda: _cursor(cursor))
    monkeypatch.setattr(
        attendance_readiness,
        "_lock_rollout_config_cur",
        lambda _cur: _pending_live(NOW),
    )
    monkeypatch.setattr(
        attendance_location_policy,
        "set_rollout_config",
        lambda *_a, **_k: writes.append("rollout"),
    )
    monkeypatch.setattr(
        attendance_location_policy,
        "set_department_requirement",
        lambda *_a, **_k: writes.append("department"),
    )

    with pytest.raises(ValueError, match="^cutover_decision_pending$"):
        attendance_readiness.save_non_live_rollout(
            attendance_location_policy.RolloutConfig("shadow", None, None),
            {"Assembly": True},
            now_utc=NOW,
        )

    assert writes == []
    assert cursor.operations[:3] == [
        ("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ", None),
        (
            "LOCK TABLE global_schedule, saturday_schedule, schedules, "
            "company_holidays, saturday_recruitments, work_centers, departments, people, "
            "wc_time_attributions IN SHARE MODE",
            None,
        ),
        ("SELECT pg_advisory_xact_lock(%s)", (attendance_readiness._READINESS_LOCK_ID,)),
    ]


def test_non_live_save_serializes_activation_winner_and_cannot_disable_active_live(
    monkeypatch,
):
    cursor = _ActivationCursor()
    cutover = NOW - timedelta(minutes=1)
    active = attendance_location_policy.RolloutConfig(
        "live",
        cutover,
        attendance_location_policy.LiveGate(
            checked_at=NOW - timedelta(minutes=2),
            report_digest="d" * 64,
            activated_at=NOW,
        ),
    )
    writes = []
    monkeypatch.setattr(attendance_readiness.db, "cursor", lambda: _cursor(cursor))
    monkeypatch.setattr(
        attendance_readiness,
        "_lock_rollout_config_cur",
        lambda _cur: active,
    )
    monkeypatch.setattr(
        attendance_location_policy,
        "set_rollout_config",
        lambda *_a, **_k: writes.append("rollout"),
    )
    monkeypatch.setattr(
        attendance_location_policy,
        "set_department_requirement",
        lambda *_a, **_k: writes.append("department"),
    )

    with pytest.raises(ValueError, match="^rollback_boundary_required$"):
        attendance_readiness.save_non_live_rollout(
            attendance_location_policy.RolloutConfig("off", None, None),
            {"Assembly": False},
            now_utc=NOW,
        )

    assert writes == []


def test_non_live_save_cancels_future_pending_gate_with_departments_in_one_transaction(
    monkeypatch,
):
    cursor = _ActivationCursor()
    calls = []
    requested = attendance_location_policy.RolloutConfig("shadow", None, None)
    monkeypatch.setattr(attendance_readiness.db, "cursor", lambda: _cursor(cursor))
    monkeypatch.setattr(
        attendance_readiness,
        "_lock_rollout_config_cur",
        lambda _cur: _pending_live(NOW + timedelta(days=1)),
    )
    monkeypatch.setattr(
        attendance_location_policy,
        "set_rollout_config",
        lambda config, *, cur=None: calls.append(("rollout", config, cur)),
    )
    monkeypatch.setattr(
        attendance_location_policy,
        "set_department_requirement",
        lambda name, required, *, cur=None: calls.append(("department", name, required, cur)),
    )

    saved = attendance_readiness.save_non_live_rollout(
        requested,
        {"Assembly": True, "Maintenance": False},
        now_utc=NOW,
    )

    assert saved == requested
    assert calls == [
        ("rollout", requested, cursor),
        ("department", "Assembly", True, cursor),
        ("department", "Maintenance", False, cursor),
    ]


def test_latest_correction_intent_supersedes_old_failure_with_id_tie_breaker():
    created = NOW - timedelta(hours=1)
    rows = [
        {
            "id": 1,
            "item_key": "run:a",
            "status": "failed",
            "attempt_count": 3,
            "verification_failure_count": 1,
            "created_at": created - timedelta(minutes=1),
            "updated_at": NOW,
        },
        {
            "id": 2,
            "item_key": "run:a",
            "status": "complete",
            "attempt_count": 1,
            "verification_failure_count": 0,
            "created_at": created,
            "updated_at": NOW,
        },
        {
            "id": 3,
            "item_key": "run:b",
            "status": "complete",
            "attempt_count": 1,
            "verification_failure_count": 0,
            "created_at": created - timedelta(minutes=1),
            "updated_at": NOW,
        },
        {
            "id": 4,
            "item_key": "run:b",
            "status": "failed",
            "attempt_count": 2,
            "verification_failure_count": 1,
            "created_at": created,
            "updated_at": NOW,
        },
        {
            "id": 5,
            "item_key": "run:c",
            "status": "failed",
            "attempt_count": 4,
            "verification_failure_count": 2,
            "created_at": created,
            "updated_at": NOW,
        },
        {
            "id": 6,
            "item_key": "run:c",
            "status": "complete",
            "attempt_count": 1,
            "verification_failure_count": 0,
            "created_at": created,
            "updated_at": NOW,
        },
    ]

    class CorrectionCursor:
        def __init__(self):
            self.result = None

        def execute(self, sql, params=None):
            normalized = " ".join(sql.split())
            assert "DISTINCT ON (item_key)" in normalized
            assert "ORDER BY item_key, created_at DESC, id DESC" in normalized
            assert params is not None
            latest = {}
            for row in sorted(
                rows,
                key=lambda value: (
                    value["item_key"],
                    value["created_at"],
                    value["id"],
                ),
                reverse=True,
            ):
                latest.setdefault(row["item_key"], row)
            day_start, day_end, _again_start, _again_end = params
            selected = tuple(latest.values())
            self.result = {
                "failed": sum(row["status"] == "failed" for row in selected),
                "retries": sum(
                    max(row["attempt_count"] - 1, 0)
                    for row in selected
                    if day_start <= row["updated_at"] < day_end
                ),
                "verification_failures": sum(
                    row["verification_failure_count"]
                    for row in selected
                    if day_start <= row["updated_at"] < day_end
                ),
            }

        def fetchone(self):
            return self.result

    cursor = CorrectionCursor()
    health = attendance_readiness._correction_health_cur(
        cursor,
        NOW - timedelta(hours=2),
        NOW + timedelta(hours=2),
    )

    assert health == {"failed": 1, "retries": 1, "verification_failures": 1}
    assert len(rows) == 6  # prior attempts remain immutable audit history


def test_read_only_json_payload_contains_every_public_metric(monkeypatch):
    monkeypatch.setattr(attendance_readiness, "_read_inputs", lambda _now: _ready_inputs())

    payload = attendance_readiness.report_json(attendance_readiness.build_report(NOW))
    decoded = json.loads(payload)

    assert decoded["ready"] is True
    assert decoded["checked_at"] == NOW.isoformat()
    assert set(attendance_readiness.ReadinessReport.__dataclass_fields__) <= set(decoded)


class _ActivationCursor:
    def __init__(self):
        self.operations = []

    def execute(self, sql, params=None):
        self.operations.append((" ".join(sql.split()), params))

    def fetchone(self):
        return None


def _rollout_value(config):
    gate = config.live_gate
    return {
        "mode": config.mode,
        "cutover_at": config.cutover_at.isoformat() if config.cutover_at else None,
        "live_gate": (
            {
                "checked_at": gate.checked_at.isoformat(),
                "report_digest": gate.report_digest,
                "activated_at": gate.activated_at.isoformat() if gate.activated_at else None,
            }
            if gate is not None
            else None
        ),
    }


class _OrdinaryFenceCursor(_ActivationCursor):
    def __init__(self, rollout, status=None, queue=None):
        super().__init__()
        self.rollout = rollout
        self.status = status
        self.queue = queue
        self.row = None

    def execute(self, sql, params=None):
        super().execute(sql, params)
        normalized = " ".join(sql.split())
        if "FROM app_settings WHERE key" in normalized:
            key = params[0]
            value = self.rollout if key == attendance_readiness._ROLLOUT_SETTING else self.status
            self.row = {"value": value} if value is not None else None
        elif "FROM attendance_recalc_queue WHERE day" in normalized:
            self.row = self.queue
        else:
            self.row = None

    def fetchone(self):
        return self.row


def _pending_live(cutover):
    return attendance_location_policy.RolloutConfig(
        "live",
        cutover,
        attendance_location_policy.LiveGate(NOW - timedelta(minutes=1), "b" * 64, None),
    )


def test_due_activation_is_serialized_marks_strict_and_enqueues_before_gate(monkeypatch):
    cursor = _ActivationCursor()
    cutover = NOW - timedelta(minutes=1)
    calls = []
    monkeypatch.setattr(attendance_readiness.db, "cursor", lambda: _cursor(cursor))
    monkeypatch.setattr(
        attendance_readiness, "_lock_rollout_config_cur", lambda _cur: _pending_live(cutover)
    )
    monkeypatch.setattr(
        attendance_readiness,
        "_validate_configured_boundary_cur",
        lambda _cur, value: value,
    )
    monkeypatch.setattr(
        attendance_readiness,
        "_build_report_cur",
        lambda _cur, _now: attendance_readiness._report_from_inputs(_ready_inputs(), NOW),
    )
    monkeypatch.setattr(
        attendance_readiness,
        "_enqueue_cutover_cur",
        lambda cur, day, now: calls.append(("enqueue", cur, day, now)),
    )
    monkeypatch.setattr(
        attendance_readiness, "_clear_blocked_cur", lambda cur: calls.append(("clear", cur))
    )
    monkeypatch.setattr(
        attendance_location_policy,
        "set_rollout_config",
        lambda config, *, cur=None: calls.append(("config", cur, config)),
    )
    monkeypatch.setattr(
        attendance_readiness.app_settings,
        "set_setting",
        lambda key, value, *, cur=None: calls.append(("status", cur, key, value)),
    )

    result = attendance_readiness.activate_due_cutover(NOW)

    assert result.status == "activated"
    assert cursor.operations[0] == ("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ", None)
    assert cursor.operations[1][0].startswith("LOCK TABLE")
    assert "pg_advisory_xact_lock" in cursor.operations[2][0]
    assert [call[0] for call in calls] == ["enqueue", "config", "clear", "status"]
    assert calls[0][2] == cutover.astimezone(shift_config.SITE_TZ).date()
    assert calls[1][2].live_gate.activated_at == NOW


def test_activated_cutover_stays_strict_but_reports_recalc_cache_pending(monkeypatch):
    cursor = _ActivationCursor()
    cutover = NOW - timedelta(minutes=1)
    queue_ready = {"value": False}
    statuses = []
    activated = replace(
        _pending_live(cutover),
        live_gate=attendance_location_policy.LiveGate(
            NOW - timedelta(minutes=2), "b" * 64, NOW - timedelta(minutes=1)
        ),
    )
    monkeypatch.setattr(attendance_readiness.db, "cursor", lambda: _cursor(cursor))
    monkeypatch.setattr(attendance_readiness, "_lock_rollout_config_cur", lambda _cur: activated)
    monkeypatch.setattr(
        attendance_readiness,
        "_cutover_queue_state_cur",
        lambda *_a: "ready" if queue_ready["value"] else "pending",
    )
    monkeypatch.setattr(
        attendance_readiness.app_settings,
        "set_setting",
        lambda key, value, *, cur=None: statuses.append((key, value, cur)),
    )

    result = attendance_readiness.activate_due_cutover(NOW)

    assert result.status == "recalculation_pending"
    assert (
        attendance_location_policy._match_state_from_config(
            cutover.astimezone(shift_config.SITE_TZ).date(),
            config=activated,
            now_utc=NOW,
        )
        == "strict"
    )
    assert statuses[-1][1]["status"] == "recalculation_pending"

    queue_ready["value"] = True
    assert attendance_readiness.activate_due_cutover(NOW).status == "active"
    assert statuses[-1][1] == {"status": "active", "cutover_at": cutover.isoformat()}


def test_cutover_queue_ready_requires_one_legal_terminal_row():
    completed = NOW - timedelta(minutes=1)
    rows = iter(
        [
            {
                "attempt_count": 1,
                "started_at": None,
                "completed_at": completed,
                "cache_started_at": NOW,
                "cache_ready_at": completed - timedelta(seconds=1),
                "last_error": "stale",
            },
            {
                "attempt_count": 0,
                "started_at": None,
                "completed_at": completed,
                "cache_started_at": None,
                "cache_ready_at": completed + timedelta(seconds=1),
                "last_error": None,
            },
        ]
    )

    class QueueCursor:
        def execute(self, _sql, _params):
            pass

        def fetchone(self):
            return next(rows)

    cursor = QueueCursor()
    assert attendance_readiness._cutover_queue_ready_cur(cursor, DAY) is False
    assert attendance_readiness._cutover_queue_ready_cur(cursor, DAY) is True


def test_activated_cutover_invalid_terminal_queue_never_releases_fence(monkeypatch):
    cutover = NOW - timedelta(minutes=1)
    completed = NOW - timedelta(minutes=2)
    activated = replace(
        _pending_live(cutover),
        live_gate=attendance_location_policy.LiveGate(
            NOW - timedelta(minutes=3),
            "b" * 64,
            NOW - timedelta(minutes=1),
        ),
    )
    statuses = []

    class QueueCursor(_ActivationCursor):
        def __init__(self):
            super().__init__()
            self.row = None

        def execute(self, sql, params=None):
            super().execute(sql, params)
            if "FROM attendance_recalc_queue WHERE day" in " ".join(sql.split()):
                self.row = {
                    "attempt_count": 1,
                    "started_at": None,
                    "completed_at": completed,
                    "cache_started_at": NOW,
                    "cache_ready_at": completed - timedelta(seconds=1),
                    "last_error": "stale",
                }
            else:
                self.row = None

        def fetchone(self):
            return self.row

    cursor = QueueCursor()
    monkeypatch.setattr(attendance_readiness.db, "cursor", lambda: _cursor(cursor))
    monkeypatch.setattr(attendance_readiness, "_lock_rollout_config_cur", lambda _cur: activated)
    monkeypatch.setattr(
        attendance_readiness.app_settings,
        "set_setting",
        lambda key, value, *, cur=None: statuses.append((key, value, cur)),
    )

    result = attendance_readiness.activate_due_cutover(NOW)

    assert result.status == "recalculation_pending"
    assert result.blockers == ("attendance_recalculation_invalid",)
    assert statuses[-1][1] == {
        "status": "recalculation_pending",
        "cutover_at": cutover.isoformat(),
        "blockers": ["attendance_recalculation_invalid"],
    }


def test_cutover_recalculation_fences_ordinary_precompute_until_cache_ready(monkeypatch):
    cutover = NOW - timedelta(minutes=1)
    config = replace(
        _pending_live(cutover),
        live_gate=attendance_location_policy.LiveGate(
            NOW - timedelta(minutes=2), "b" * 64, NOW - timedelta(minutes=1)
        ),
    )
    status = {
        "value": {
            "status": "recalculation_pending",
            "cutover_at": cutover.isoformat(),
        }
    }
    prepared = precompute.PreparedProductionDay(DAY, (), DAY, "strict")
    calls = []
    queue = {
        "attempt_count": 1,
        "started_at": None,
        "completed_at": NOW - timedelta(seconds=2),
        "cache_started_at": None,
        "cache_ready_at": NOW - timedelta(seconds=1),
        "last_error": None,
    }
    monkeypatch.setattr(precompute, "prepare_day", lambda *_a: calls.append("prepare") or prepared)
    monkeypatch.setattr(
        precompute,
        "_validate_prepared_match_state_cur",
        lambda *_a: calls.append("validate"),
    )
    monkeypatch.setattr(
        precompute,
        "_upsert_production_daily_cur",
        lambda *_a, **_k: calls.append("write") or 1,
    )
    monkeypatch.setattr(
        db,
        "cursor",
        lambda: _cursor(_OrdinaryFenceCursor(_rollout_value(config), status["value"], queue)),
    )

    pending_result = precompute.precompute_day(DAY, object())

    assert pending_result == {
        "day": DAY.isoformat(),
        "rows_written": 0,
        "skipped": "cutover_recalculation_pending",
    }
    assert calls == []

    later_day = DAY + timedelta(days=1)
    later_result = precompute.precompute_day(later_day, object())
    assert later_result == {
        "day": later_day.isoformat(),
        "rows_written": 0,
        "skipped": "cutover_recalculation_pending",
    }
    assert calls == []

    # The dedicated attendance-recalculation worker calls prepare/store
    # directly, so it remains the sole writer while ordinary refresh is fenced.
    assert precompute.store_prepared_day(prepared, cur=object()) == 1
    assert calls == ["validate", "write"]

    status["value"] = {"status": "active", "cutover_at": cutover.isoformat()}
    assert precompute.precompute_day(DAY, object())["rows_written"] == 1
    assert precompute.precompute_day(later_day, object())["rows_written"] == 1
    assert calls == [
        "validate",
        "write",
        "prepare",
        "validate",
        "write",
        "prepare",
        "validate",
        "write",
    ]


def test_ordinary_precompute_rechecks_cutover_queue_inside_store_transaction(
    monkeypatch,
):
    cutover = NOW - timedelta(minutes=1)
    config = replace(
        _pending_live(cutover),
        live_gate=attendance_location_policy.LiveGate(
            NOW - timedelta(minutes=2), "b" * 64, NOW - timedelta(minutes=1)
        ),
    )
    ready_queue = {
        "attempt_count": 1,
        "started_at": None,
        "completed_at": NOW - timedelta(seconds=2),
        "cache_started_at": None,
        "cache_ready_at": NOW - timedelta(seconds=1),
        "last_error": None,
    }
    pending_queue = {
        "attempt_count": 0,
        "started_at": None,
        "completed_at": None,
        "cache_started_at": None,
        "cache_ready_at": None,
        "last_error": None,
    }
    cursor = _OrdinaryFenceCursor(
        _rollout_value(config),
        {"status": "active", "cutover_at": cutover.isoformat()},
        ready_queue,
    )
    prepared = precompute.PreparedProductionDay(DAY, (), DAY, "strict")
    writes = []
    monkeypatch.setattr(attendance_readiness, "ordinary_refresh_ready", lambda _day: True)

    def prepare(*_args):
        # The exact cutover is requeued after meter/source computation but
        # before the ordinary transaction is allowed to publish its snapshot.
        cursor.queue = pending_queue
        return prepared

    monkeypatch.setattr(precompute, "prepare_day", prepare)
    monkeypatch.setattr(precompute, "_validate_prepared_match_state_cur", lambda *_a: None)
    monkeypatch.setattr(
        precompute,
        "_upsert_production_daily_cur",
        lambda *_a, **_k: writes.append("production") or 1,
    )
    monkeypatch.setattr(db, "cursor", lambda: _cursor(cursor))
    monkeypatch.setattr(
        attendance_mirror,
        "enqueue_recalc",
        lambda *_a, **_k: pytest.fail("fenced ordinary store mutated the queue"),
    )

    result = precompute.precompute_day(DAY, object())

    assert result == {
        "day": DAY.isoformat(),
        "rows_written": 0,
        "skipped": "cutover_recalculation_pending",
    }
    assert writes == []
    queue_reads = [
        operation
        for operation in cursor.operations
        if "FROM attendance_recalc_queue WHERE day" in operation[0]
    ]
    assert len(queue_reads) == 1
    assert queue_reads[0][0].endswith("FOR SHARE")


def test_ordinary_store_uses_one_deadlock_safe_lock_order(monkeypatch):
    cutover = NOW - timedelta(minutes=1)
    config = replace(
        _pending_live(cutover),
        live_gate=attendance_location_policy.LiveGate(
            NOW - timedelta(minutes=2), "b" * 64, NOW - timedelta(minutes=1)
        ),
    )
    cursor = _OrdinaryFenceCursor(
        _rollout_value(config),
        {"status": "active", "cutover_at": cutover.isoformat()},
        {
            "attempt_count": 1,
            "started_at": None,
            "completed_at": NOW - timedelta(seconds=2),
            "cache_started_at": None,
            "cache_ready_at": NOW - timedelta(seconds=1),
            "last_error": None,
        },
    )
    prepared = precompute.PreparedProductionDay(DAY, (), DAY, "strict")
    monkeypatch.setattr(db, "cursor", lambda: _cursor(cursor))
    monkeypatch.setattr(
        precompute,
        "_validate_prepared_match_state_cur",
        lambda cur, _prepared: cur.execute(
            "LOCK TABLE app_settings, attendance_strict_days IN SHARE ROW EXCLUSIVE MODE"
        ),
    )
    monkeypatch.setattr(precompute, "_upsert_production_daily_cur", lambda *_a, **_k: 1)

    assert precompute.store_prepared_day(prepared) == 1

    sql = [operation[0] for operation in cursor.operations]
    config_lock = next(
        index for index, value in enumerate(sql) if value.startswith("LOCK TABLE global_schedule")
    )
    readiness_lock = next(
        index
        for index, operation in enumerate(cursor.operations)
        if operation[1] == (attendance_readiness._READINESS_LOCK_ID,)
    )
    queue_lock = next(
        index
        for index, value in enumerate(sql)
        if "FROM attendance_recalc_queue WHERE day" in value and value.endswith("FOR SHARE")
    )
    matcher_lock = sql.index(
        "LOCK TABLE app_settings, attendance_strict_days IN SHARE ROW EXCLUSIVE MODE"
    )
    assert config_lock < readiness_lock < queue_lock < matcher_lock


def test_malformed_rollout_fences_public_ordinary_precompute_before_source_work(
    monkeypatch,
):
    cursor = _OrdinaryFenceCursor({"mode": "live", "cutover_at": "not-a-time", "live_gate": None})
    monkeypatch.setattr(attendance_readiness.db, "cursor", lambda: _cursor(cursor))
    monkeypatch.setattr(
        precompute,
        "prepare_day",
        lambda *_a, **_k: pytest.fail("malformed rollout reached production source work"),
    )

    result = precompute.precompute_day(DAY, object())

    assert result == {
        "day": DAY.isoformat(),
        "rows_written": 0,
        "skipped": "cutover_recalculation_pending",
    }


def test_due_pending_live_fences_before_source_work_or_queue_mutation(monkeypatch):
    cutover = NOW - timedelta(minutes=1)
    cursor = _OrdinaryFenceCursor(_rollout_value(_pending_live(cutover)))
    monkeypatch.setattr(attendance_readiness.db, "cursor", lambda: _cursor(cursor))
    monkeypatch.setattr(
        precompute,
        "prepare_day",
        lambda *_a, **_k: pytest.fail("pending live reached production source work"),
    )
    monkeypatch.setattr(
        attendance_mirror,
        "enqueue_recalc",
        lambda *_a, **_k: pytest.fail("pending live mutated recalculation queue"),
    )

    result = precompute.precompute_day(DAY, object())

    assert result == {
        "day": DAY.isoformat(),
        "rows_written": 0,
        "skipped": "cutover_recalculation_pending",
    }


@pytest.mark.parametrize("stored_status", [None, {}, {"status": "broken"}])
def test_activated_cutover_fails_closed_when_local_status_is_missing_or_malformed(
    monkeypatch,
    stored_status,
):
    cutover = NOW - timedelta(minutes=1)
    config = replace(
        _pending_live(cutover),
        live_gate=attendance_location_policy.LiveGate(NOW, "b" * 64, NOW),
    )
    cursor = _OrdinaryFenceCursor(_rollout_value(config), stored_status)
    monkeypatch.setattr(attendance_readiness.db, "cursor", lambda: _cursor(cursor))

    assert attendance_readiness.ordinary_refresh_ready(DAY) is False


@pytest.mark.parametrize(
    "queue_row",
    [
        None,
        {
            "attempt_count": 0,
            "started_at": None,
            "completed_at": None,
            "cache_started_at": None,
            "cache_ready_at": None,
            "last_error": None,
        },
        {
            "attempt_count": 1,
            "started_at": None,
            "completed_at": NOW - timedelta(minutes=2),
            "cache_started_at": NOW,
            "cache_ready_at": NOW - timedelta(minutes=3),
            "last_error": "stale",
        },
    ],
    ids=["missing", "pending", "invalid"],
)
def test_active_cutover_status_does_not_release_fence_without_ready_queue(
    monkeypatch,
    queue_row,
):
    cutover = NOW - timedelta(minutes=1)
    config = replace(
        _pending_live(cutover),
        live_gate=attendance_location_policy.LiveGate(
            NOW - timedelta(minutes=3), "b" * 64, NOW - timedelta(minutes=1)
        ),
    )
    cursor = _OrdinaryFenceCursor(
        _rollout_value(config),
        {"status": "active", "cutover_at": cutover.isoformat()},
        queue_row,
    )
    monkeypatch.setattr(attendance_readiness.db, "cursor", lambda: _cursor(cursor))

    assert attendance_readiness.ordinary_refresh_ready(DAY) is False


def test_failed_boundary_recheck_rolls_back_once_with_stable_blocked_identity(monkeypatch):
    cursor = _ActivationCursor()
    cutover = NOW - timedelta(minutes=1)
    calls = []
    blocked = attendance_readiness._report_from_inputs(_ready_inputs(open_conflicts=1), NOW)
    monkeypatch.setattr(attendance_readiness.db, "cursor", lambda: _cursor(cursor))
    monkeypatch.setattr(
        attendance_readiness, "_lock_rollout_config_cur", lambda _cur: _pending_live(cutover)
    )
    monkeypatch.setattr(
        attendance_readiness,
        "_validate_configured_boundary_cur",
        lambda _cur, value: value,
    )
    monkeypatch.setattr(attendance_readiness, "_build_report_cur", lambda *_a: blocked)
    monkeypatch.setattr(
        attendance_location_policy,
        "set_rollout_config",
        lambda config, *, cur=None: calls.append(("config", config, cur)),
    )
    monkeypatch.setattr(
        attendance_readiness,
        "_store_blocked_cur",
        lambda cur, *, cutover_at, report: calls.append(
            ("blocked", cur, cutover_at, report.blockers)
        ),
    )
    monkeypatch.setattr(
        attendance_readiness.app_settings,
        "set_setting",
        lambda key, value, *, cur=None: calls.append(("status", key, value, cur)),
    )

    result = attendance_readiness.activate_due_cutover(NOW)

    assert result.status == "blocked"
    assert calls[0][1] == attendance_location_policy.RolloutConfig("shadow", None, None)
    assert [call[0] for call in calls].count("blocked") == 1
    assert result.blockers == tuple(sorted(result.blockers))


def test_due_activation_revalidates_a_changed_workday_boundary_before_marking_strict(
    monkeypatch,
):
    cursor = _ActivationCursor()
    cutover = NOW - timedelta(minutes=1)
    calls = []
    monkeypatch.setattr(attendance_readiness.db, "cursor", lambda: _cursor(cursor))
    monkeypatch.setattr(
        attendance_readiness,
        "_lock_rollout_config_cur",
        lambda _cur: _pending_live(cutover),
    )
    monkeypatch.setattr(shift_config, "shift_start_for", lambda _day: time(8, 0))
    monkeypatch.setattr(shift_config, "is_workday", lambda _day: True)
    monkeypatch.setattr(
        attendance_readiness,
        "_build_report_cur",
        lambda *_a: attendance_readiness._report_from_inputs(_ready_inputs(), NOW),
    )
    monkeypatch.setattr(
        attendance_readiness,
        "_enqueue_cutover_cur",
        lambda *_a: pytest.fail("changed boundary was marked strict"),
    )
    monkeypatch.setattr(
        attendance_location_policy,
        "set_rollout_config",
        lambda config, *, cur=None: calls.append((config, cur)),
    )
    monkeypatch.setattr(
        attendance_readiness.app_settings,
        "set_setting",
        lambda *_a, **_k: None,
    )
    monkeypatch.setattr(
        attendance_readiness,
        "_store_blocked_cur",
        lambda _cur, *, cutover_at, report: calls.append((cutover_at, report.blockers)),
    )

    result = attendance_readiness.activate_due_cutover(NOW)

    assert result.status == "blocked"
    assert "cutover_boundary_required" in result.blockers
    assert calls[0][0] == attendance_location_policy.RolloutConfig("shadow", None, None)


@pytest.mark.parametrize("display_status_day_offset", [-10, 0, 10])
def test_due_rollback_with_changed_boundary_restores_active_live_and_opens_one_blocker(
    monkeypatch,
    display_status_day_offset,
):
    cursor = _ActivationCursor()
    original_cutover = datetime(2026, 8, 28, 12, tzinfo=UTC)
    rollback_at = datetime(2026, 8, 31, 12, tzinfo=UTC)
    gate = attendance_location_policy.LiveGate(
        checked_at=original_cutover,
        report_digest="d" * 64,
        activated_at=original_cutover,
    )
    rollback = attendance_location_policy.RolloutConfig("shadow", rollback_at, gate)
    report = attendance_readiness._report_from_inputs(_ready_inputs(), NOW)
    calls = []
    monkeypatch.setattr(attendance_readiness.db, "cursor", lambda: _cursor(cursor))
    monkeypatch.setattr(attendance_readiness, "_lock_rollout_config_cur", lambda _cur: rollback)
    monkeypatch.setattr(
        attendance_readiness,
        "_cutover_status_cur",
        lambda _cur: {
            "status": "active",
            "cutover_at": (
                original_cutover + timedelta(days=display_status_day_offset)
            ).isoformat(),
        },
        raising=False,
    )
    monkeypatch.setattr(attendance_readiness, "_build_report_cur", lambda *_a: report)
    monkeypatch.setattr(
        attendance_readiness,
        "_validate_configured_boundary_cur",
        lambda *_a: (_ for _ in ()).throw(ValueError("cutover_boundary_required")),
    )
    monkeypatch.setattr(
        attendance_location_policy,
        "restore_active_after_rejected_rollback",
        lambda config, *, cur=None: calls.append(("restore", config, cur)),
        raising=False,
    )
    monkeypatch.setattr(
        attendance_readiness,
        "_store_blocked_cur",
        lambda cur, *, cutover_at, report: calls.append(
            ("blocked", cur, cutover_at, report.blockers)
        ),
    )
    monkeypatch.setattr(
        attendance_readiness.app_settings,
        "set_setting",
        lambda key, value, *, cur=None: calls.append(("status", key, value, cur)),
    )
    monkeypatch.setattr(
        attendance_readiness,
        "_enqueue_cutover_cur",
        lambda *_a, **_k: pytest.fail("rejected rollback marked a new strict day"),
    )

    result = attendance_readiness.activate_due_cutover(NOW)

    assert result.status == "blocked"
    assert result.blockers == ("cutover_boundary_required",)
    assert [call[0] for call in calls] == ["restore", "blocked", "status"]
    restored = calls[0][1]
    assert restored.mode == "live"
    assert restored.cutover_at == original_cutover
    assert restored.live_gate.activated_at == original_cutover
    assert restored.live_gate.checked_at == NOW
    assert restored.live_gate.report_digest == attendance_readiness.report_digest(
        replace(report, ready=False, blockers=result.blockers),
        original_cutover,
    )
    attendance_location_policy._validate_config_shape(restored)


def test_due_rollback_with_invalid_status_keeps_live_ownership_and_one_stable_blocker(
    monkeypatch,
):
    cursor = _ActivationCursor()
    original_cutover = datetime(2026, 8, 28, 12, tzinfo=UTC)
    rollback_at = datetime(2026, 8, 31, 12, tzinfo=UTC)
    gate = attendance_location_policy.LiveGate(
        checked_at=original_cutover,
        report_digest="d" * 64,
        activated_at=original_cutover,
    )
    rollback = attendance_location_policy.RolloutConfig("shadow", rollback_at, gate)
    report = attendance_readiness._report_from_inputs(_ready_inputs(), NOW)
    calls = []
    monkeypatch.setattr(attendance_readiness.db, "cursor", lambda: _cursor(cursor))
    monkeypatch.setattr(attendance_readiness, "_lock_rollout_config_cur", lambda _cur: rollback)
    monkeypatch.setattr(
        attendance_readiness,
        "_cutover_status_cur",
        lambda _cur: {"status": "broken", "cutover_at": "not-a-time"},
    )
    monkeypatch.setattr(attendance_readiness, "_build_report_cur", lambda *_a: report)
    monkeypatch.setattr(
        attendance_readiness,
        "_validate_configured_boundary_cur",
        lambda *_a: (_ for _ in ()).throw(ValueError("cutover_boundary_required")),
    )
    monkeypatch.setattr(
        attendance_location_policy,
        "restore_active_after_rejected_rollback",
        lambda config, *, cur=None: calls.append(("restore", config, cur)),
    )
    monkeypatch.setattr(
        attendance_readiness,
        "_store_blocked_cur",
        lambda cur, *, cutover_at, report: calls.append(
            ("blocked", cur, cutover_at, report.blockers)
        ),
    )
    monkeypatch.setattr(
        attendance_readiness.app_settings,
        "set_setting",
        lambda key, value, *, cur=None: calls.append(("status", key, value, cur)),
    )
    monkeypatch.setattr(
        attendance_readiness,
        "_enqueue_cutover_cur",
        lambda *_a, **_k: pytest.fail("rejected rollback changed strict ownership"),
    )

    first = attendance_readiness.activate_due_cutover(NOW)
    second = attendance_readiness.activate_due_cutover(NOW)

    assert first == second
    assert first.status == "blocked"
    assert first.blockers == ("cutover_boundary_required",)
    assert [call[0] for call in calls] == [
        "restore",
        "blocked",
        "status",
        "restore",
        "blocked",
        "status",
    ]
    restored = [call[1] for call in calls if call[0] == "restore"]
    assert {config.mode for config in restored} == {"live"}
    assert {config.cutover_at for config in restored} == {original_cutover}
    assert {config.live_gate.activated_at for config in restored} == {original_cutover}
    blocked = [call for call in calls if call[0] == "blocked"]
    assert {(call[2], call[3]) for call in blocked} == {
        (rollback_at, ("cutover_boundary_required",))
    }


def test_cutover_blocked_exception_has_stable_boundary_key_and_urgent_inbox_section(
    monkeypatch,
):
    blocked = {
        "scheduled_at": NOW,
        "checked_at": NOW,
        "report_digest": "c" * 64,
        "blockers": ("attendance_conflicts_open", "unassigned_production"),
    }
    monkeypatch.setattr(attendance_readiness, "blocked_cutover_snapshot", lambda: blocked)

    issue = attendance_exceptions._blocked_cutover_issue(NOW)

    assert issue.kind == "attendance_cutover_blocked"
    assert issue.item_key == f"attendance_cutover_blocked:{NOW.isoformat()}"
    assert issue.priority == "urgent"
    assert "attendance_cutover_blocked" in exception_inbox._ATTENDANCE_SECTION_META
    assert (
        inbox_reconcile._SECTION_KIND["attendance_cutover_blocked"] == "attendance_cutover_blocked"
    )
    assert inbox_reconcile._KIND_SOURCE["attendance_cutover_blocked"] == "Attendance Timeline"


def test_malformed_blocked_cutover_state_keeps_its_source_incomplete(monkeypatch):
    monkeypatch.setattr(
        attendance_readiness.app_settings,
        "get_setting",
        lambda key: (
            {"scheduled_at": "not-a-timestamp"}
            if key == attendance_readiness._BLOCKED_SETTING
            else None
        ),
    )
    monkeypatch.setattr(
        attendance_exceptions,
        "_policy_snapshot_for_day",
        lambda *_a, **_k: (
            attendance_location_policy.RolloutConfig("off", None, None),
            "legacy",
            None,
        ),
    )

    snapshot = attendance_exceptions.build_snapshot(DAY, now_utc=NOW)

    assert snapshot.complete is False
    assert snapshot.issues == ()
    assert snapshot.source_errors == ("Attendance Timeline",)
    reconciliation_snapshot = {
        "source_errors": [{"source": "Attendance Timeline"}],
        "sections": [
            {
                "id": "attendance_cutover_blocked",
                "count": 0,
                "rows": [],
                "complete": snapshot.complete,
            }
        ],
    }
    assert "attendance_cutover_blocked" not in inbox_reconcile._complete_kinds(
        reconciliation_snapshot
    )


def test_failed_cutover_retries_reconcile_one_stable_item_without_touching_other_issues(
    monkeypatch,
):
    cutover = NOW - timedelta(minutes=1)
    stored = {}

    def set_setting(key, value, *, cur=None):
        stored[key] = value

    monkeypatch.setattr(attendance_readiness.app_settings, "set_setting", set_setting)
    monkeypatch.setattr(
        attendance_readiness.app_settings,
        "get_setting",
        lambda key: stored.get(key),
    )
    first_report = attendance_readiness._report_from_inputs(_ready_inputs(open_conflicts=1), NOW)
    retry_report = attendance_readiness._report_from_inputs(
        _ready_inputs(open_conflicts=1), NOW + timedelta(seconds=30)
    )

    attendance_readiness._store_blocked_cur(object(), cutover_at=cutover, report=first_report)
    attendance_readiness._store_blocked_cur(object(), cutover_at=cutover, report=retry_report)
    first_issue = attendance_exceptions._blocked_cutover_issue(NOW)
    retry_issue = attendance_exceptions._blocked_cutover_issue(NOW + timedelta(seconds=30))

    blocked_key = f"attendance_cutover_blocked:{cutover.isoformat()}"
    unrelated_key = "attendance_unmapped_location:11"
    assert list(stored) == [attendance_readiness._BLOCKED_SETTING]
    assert first_issue.item_key == retry_issue.item_key == blocked_key
    assert first_issue.priority == retry_issue.priority == "urgent"

    failed_snapshot = {
        "source_errors": [],
        "sections": [
            {
                "id": "attendance_cutover_blocked",
                "count": 1,
                "complete": True,
                "rows": [{"item_key": blocked_key, "priority": "urgent"}],
            },
            {
                "id": "attendance_unmapped_location",
                "count": 1,
                "complete": True,
                "rows": [{"item_key": unrelated_key, "priority": "urgent"}],
            },
        ],
        "queue": [
            {
                "section_id": "attendance_cutover_blocked",
                "item_key": blocked_key,
                "priority": "urgent",
            },
            {
                "section_id": "attendance_unmapped_location",
                "item_key": unrelated_key,
                "priority": "urgent",
            },
        ],
    }
    first_open = inbox_reconcile._open_now_from_snapshot(failed_snapshot)
    retry_actions = inbox_reconcile.plan_reconcile(
        first_open,
        first_open,
        inbox_reconcile._complete_kinds(failed_snapshot),
    )

    assert set(first_open) == {blocked_key, unrelated_key}
    assert retry_actions == {
        "arrivals": [],
        "still_open": [blocked_key, unrelated_key],
        "departed": [],
    }

    recovered_snapshot = {
        **failed_snapshot,
        "sections": [
            {
                "id": "attendance_cutover_blocked",
                "count": 0,
                "complete": True,
                "rows": [],
            },
            failed_snapshot["sections"][1],
        ],
        "queue": [failed_snapshot["queue"][1]],
    }
    recovered_open = inbox_reconcile._open_now_from_snapshot(recovered_snapshot)
    recovered_actions = inbox_reconcile.plan_reconcile(
        recovered_open,
        first_open,
        inbox_reconcile._complete_kinds(recovered_snapshot),
    )

    assert recovered_actions["departed"] == [blocked_key]
    assert recovered_actions["still_open"] == [unrelated_key]


def test_blocked_cutover_source_failure_is_not_mistaken_for_resolution(monkeypatch):
    monkeypatch.setattr(
        attendance_exceptions,
        "_policy_snapshot_for_day",
        lambda *_a, **_k: (
            attendance_location_policy.RolloutConfig("off", None, None),
            "legacy",
            None,
        ),
    )
    monkeypatch.setattr(
        attendance_readiness,
        "blocked_cutover_snapshot",
        lambda: (_ for _ in ()).throw(RuntimeError("local blocked state unavailable")),
    )

    snapshot = attendance_exceptions.build_snapshot(DAY, now_utc=NOW)

    assert snapshot.complete is False
    assert snapshot.source_errors == ("Attendance Timeline",)


def test_exactly_one_nonblocking_readiness_warmer_runs_every_30_seconds(monkeypatch):
    matches = [
        item for item in app_module._WARMERS if item[1] is app_module._tick_attendance_readiness
    ]
    calls = []
    monkeypatch.setattr(attendance_readiness, "tick", lambda: calls.append("tick"))

    import asyncio

    asyncio.run(app_module._tick_attendance_readiness())

    assert matches == [("attendance readiness", app_module._tick_attendance_readiness, 30)]
    assert calls == ["tick"]


class _FormValues(dict):
    def getlist(self, key):
        value = self.get(key, [])
        return value if isinstance(value, list) else [value]


class _FormRequest:
    def __init__(self, values):
        self._values = _FormValues(values)
        self.headers = {"accept": "application/json"}
        self.state = type("State", (), {})()

    async def form(self):
        return self._values


def test_super_admin_live_save_runs_fresh_scheduler_with_posted_department_policy(
    monkeypatch,
):
    request = _FormRequest(
        {
            "rollout_mode": "live",
            "cutover_at": "2026-09-01T07:00",
            "departments_present": "1",
            "department_requires_work_center": ["Production"],
        }
    )
    calls = []
    monkeypatch.setattr(settings.auth, "request_is_super_admin", lambda _request: True)
    monkeypatch.setattr(
        settings.work_centers_store,
        "synced_departments",
        lambda: ["Production", "Maintenance"],
    )
    monkeypatch.setattr(
        settings.attendance_readiness,
        "schedule_live_cutover",
        lambda cutover, **kwargs: calls.append((cutover, kwargs)) or _pending_live(cutover),
    )

    import asyncio

    response = asyncio.run(settings.settings_save_attendance_location(request))

    assert response.status_code == 200
    assert calls[0][1]["department_requirements"] == {
        "Maintenance": False,
        "Production": True,
    }


def test_settings_context_and_template_show_plant_local_health_to_all_managers(
    monkeypatch,
):
    report = attendance_readiness._report_from_inputs(
        _ready_inputs(
            open_rows_not_refreshed=2,
            last_sweep_deletion_count=3,
            open_conflicts=1,
            conflict_seconds_today=120,
            open_unmapped=1,
            unmapped_seconds_today=60,
            open_missing_required=1,
            missing_seconds_today=180,
            unassigned_units_today=4,
            oldest_unassigned_at=NOW - timedelta(minutes=5),
            shadow_changed_worker_units=2,
            correction_retries_today=1,
        ),
        NOW,
    )
    monkeypatch.setattr(settings.attendance_readiness, "build_report", lambda _now: report)
    monkeypatch.setattr(settings.attendance_readiness, "cutover_status_snapshot", lambda: None)
    monkeypatch.setattr(settings.db, "query", lambda *_a, **_k: [])
    monkeypatch.setattr(
        settings.attendance_location_policy,
        "get_rollout_config",
        lambda: attendance_location_policy.RolloutConfig("shadow", None, None),
    )
    monkeypatch.setattr(settings.attendance_location_policy, "live_is_active", lambda: False)

    context = settings._attendance_location_context()
    html = Path("src/zira_dashboard/templates/settings.html").read_text()

    assert context["readiness"] is report
    assert context["readiness_checked_at"].tzinfo == shift_config.SITE_TZ
    assert context["mirror_freshness"].tzinfo == shift_config.SITE_TZ
    assert context["last_full_sweep"].tzinfo == shift_config.SITE_TZ
    for label in (
        "Readiness",
        "Cutover status",
        "Open rows not refreshed",
        "Last sweep deletions",
        "Projection lag",
        "Recalculation queue",
        "Conflict time",
        "Unknown location time",
        "Missing location time",
        "Unassigned production",
        "Oldest unassigned age",
        "Shadow worker-unit changes",
        "Correction failures",
        "Correction retries",
        "Department repair failures",
        "Why live is blocked",
    ):
        assert label in html
    assert "replace('_', ' ')" in html
    assert "Live — readiness check required" in html
    assert 'value="live"' in html and 'value="live"' in html.replace(" disabled", "")


def test_settings_health_remains_blocked_and_visible_when_local_state_is_unavailable(
    monkeypatch,
):
    unavailable = attendance_readiness._unavailable_report(NOW)
    monkeypatch.setattr(settings.attendance_readiness, "build_report", lambda _now: unavailable)
    monkeypatch.setattr(
        settings.attendance_readiness.app_settings,
        "get_setting",
        lambda _key: (_ for _ in ()).throw(RuntimeError("database unavailable")),
    )
    monkeypatch.setattr(
        settings.attendance_location_policy,
        "get_rollout_config",
        lambda: (_ for _ in ()).throw(RuntimeError("database unavailable")),
    )
    monkeypatch.setattr(
        settings.attendance_location_policy,
        "live_is_active",
        lambda: pytest.fail("unavailable rollout must not get a second optimistic read"),
    )
    monkeypatch.setattr(
        settings.db,
        "query",
        lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("database unavailable")),
    )

    context = settings._attendance_location_context()

    assert context["mode"] == "off"
    assert context["live_active"] is False
    assert context["readiness"].ready is False
    assert context["cutover_status"] == {"status": "unavailable"}


def test_readiness_cli_and_operator_runbook_are_present_and_read_only():
    script = Path("scripts/check_attendance_location_readiness.py")
    assert script.exists()
    source = script.read_text()
    assert "build_report" in source
    assert "report_json" in source
    assert "set_rollout_config" not in source
    assert "odoo_client" not in source

    design = Path(
        "docs/superpowers/specs/2026-08-28-odoo-attendance-live-location-truth-design.md"
    ).read_text()
    assert "check_attendance_location_readiness.py" in design
    assert "schedule rollback to `shadow` at the next clean workday boundary" in design


def test_final_child_readable_patch_note_is_exact():
    changelog = Path("CHANGELOG.md").read_text()
    note = "### Odoo live locations are ready for a safe start"
    assert note in changelog
    assert changelog.index("## 2026-09-01") < changelog.index(note)
    assert changelog.index(note) < changelog.index("## 2026-08-31")
    assert (
        "**Plant Manager can now check that Odoo locations are fresh and complete before "
        "they control production.** The app can compare the new answer first, start on a "
        "clean workday, and show clear reasons when it is not safe to start."
    ) in changelog


@pytest.mark.parametrize(
    ("day_health", "expected_clean", "expected_complete_days"),
    [
        (
            {
                "workday": False,
                "conflict_minutes": 0.0,
                "unmapped_minutes": 0.0,
                "missing_minutes": 0.0,
            },
            False,
            [],
        ),
        (
            {
                "workday": True,
                "conflict_minutes": 5.0,
                "unmapped_minutes": 0.0,
                "missing_minutes": 0.0,
            },
            False,
            [],
        ),
        (
            {
                "workday": True,
                "conflict_minutes": 0.0,
                "unmapped_minutes": 0.0,
                "missing_minutes": 0.0,
            },
            True,
            [DAY.isoformat()],
        ),
    ],
    ids=["closed-day", "dirty-workday", "clean-workday"],
)
def test_shadow_complete_day_requires_same_day_workday_and_clean_aggregate(
    monkeypatch,
    day_health,
    expected_clean,
    expected_complete_days,
):
    stored = {}
    config, _mirror = _install_shadow_refresh_origin(monkeypatch)
    monkeypatch.setattr(
        attendance_readiness.attendance_location_policy,
        "get_rollout_config",
        lambda: attendance_location_policy.RolloutConfig("shadow", None, None),
    )
    monkeypatch.setattr(
        attendance_readiness,
        "_compute_shadow_aggregate",
        lambda *_a, **_k: {
            "changed_worker_units": 0.0,
            "unassigned_units_today": 0.0,
            "oldest_unassigned_at": None,
        },
    )
    monkeypatch.setattr(
        attendance_readiness,
        "_shadow_day_health",
        lambda *_a, **_k: {**day_health, "clean": expected_clean},
    )
    monkeypatch.setattr(attendance_readiness.app_settings, "get_setting", lambda _key: None)
    monkeypatch.setattr(
        attendance_readiness.app_settings,
        "set_setting",
        lambda key, value, *, cur=None: stored.update({key: value}),
    )
    cursor = _ActivationCursor()
    monkeypatch.setattr(attendance_readiness.db, "cursor", lambda: _cursor(cursor))
    monkeypatch.setattr(shift_config, "shift_end_for", lambda _day: time(15, 0))
    after_shift = datetime.combine(
        DAY,
        time(23, 59),
        tzinfo=shift_config.SITE_TZ,
    ).astimezone(UTC)

    result = attendance_readiness.refresh_shadow_comparison(
        after_shift,
        production_client=object(),
    )

    value = stored[attendance_readiness._SHADOW_SETTING]
    assert result.status == "stored"
    assert value["complete_days"] == expected_complete_days
    assert value["complete_day_health"] == [
        {
            "day": DAY.isoformat(),
            "completed_at": after_shift.isoformat(),
            "schedule_digest": config.day_digest,
            **day_health,
            "unassigned_units": 0.0,
            "clean": expected_clean,
        }
    ]


def test_entering_shadow_after_shift_start_cannot_retroactively_certify_that_day(
    monkeypatch,
):
    base_config = attendance_readiness._shadow_config_snapshot_cur(_ShadowConfigCursor(), DAY)
    entered_shadow = base_config.shift_end_utc + timedelta(minutes=1)
    after_shift = entered_shadow + timedelta(minutes=1)
    stored = {}
    _install_shadow_refresh_origin(monkeypatch, epoch=entered_shadow)
    monkeypatch.setattr(
        attendance_readiness,
        "_compute_shadow_aggregate",
        lambda *_a, **_k: {
            "changed_worker_units": 0.0,
            "unassigned_units_today": 0.0,
            "oldest_unassigned_at": None,
        },
    )
    monkeypatch.setattr(
        attendance_readiness,
        "_shadow_day_health",
        lambda *_a, **_k: {
            "workday": True,
            "conflict_minutes": 0.0,
            "unmapped_minutes": 0.0,
            "missing_minutes": 0.0,
            "clean": True,
        },
    )
    monkeypatch.setattr(attendance_readiness.db, "cursor", lambda: _cursor(_ActivationCursor()))
    monkeypatch.setattr(
        attendance_readiness.app_settings,
        "set_setting",
        lambda key, value, *, cur=None: stored.update({key: value}),
    )

    result = attendance_readiness.refresh_shadow_comparison(
        after_shift,
        production_client=object(),
    )

    assert result.status == "stored"
    value = stored[attendance_readiness._SHADOW_SETTING]
    assert value["complete_days"] == []
    assert value["complete_day_health"] == []


def test_shadow_refresh_rejects_mid_compute_config_change_without_replacing_last_good(
    monkeypatch,
):
    cursor = _ActivationCursor()
    writes = []
    config, _mirror = _install_shadow_refresh_origin(monkeypatch)
    monkeypatch.setattr(
        attendance_readiness.attendance_location_policy,
        "get_rollout_config",
        lambda: attendance_location_policy.RolloutConfig("shadow", None, None),
    )
    monkeypatch.setattr(
        attendance_readiness,
        "_shadow_config_snapshot_cur",
        lambda _cur, _day: replace(config, digest="b" * 64),
    )
    monkeypatch.setattr(
        attendance_readiness,
        "_compute_shadow_aggregate",
        lambda *_a, **_k: {
            "changed_worker_units": 0.0,
            "unassigned_units_today": 0.0,
            "oldest_unassigned_at": None,
        },
    )
    monkeypatch.setattr(
        attendance_readiness,
        "_shadow_day_health",
        lambda *_a, **_k: {
            "workday": True,
            "conflict_minutes": 0.0,
            "unmapped_minutes": 0.0,
            "missing_minutes": 0.0,
            "clean": True,
        },
    )
    monkeypatch.setattr(attendance_readiness.db, "cursor", lambda: _cursor(cursor))
    monkeypatch.setattr(
        attendance_readiness.app_settings,
        "set_setting",
        lambda key, value, *, cur=None: writes.append((key, value, cur)),
    )

    result = attendance_readiness.refresh_shadow_comparison(NOW, production_client=object())

    assert result.status == "failed"
    assert not [row for row in writes if row[0] == attendance_readiness._SHADOW_SETTING]
    assert writes == [
        (
            attendance_readiness._SHADOW_ERROR_SETTING,
            {
                "failed_at": NOW.isoformat(),
                "reason": "configuration_changed",
                "error_type": "ShadowConfigurationChanged",
            },
            cursor,
        )
    ]


@pytest.mark.parametrize(
    "current_mirror_origin",
    [
        (
            NOW,
            NOW - timedelta(minutes=1),
            7,
        ),
        (
            NOW - timedelta(seconds=1),
            NOW,
            8,
        ),
    ],
    ids=["incremental-transfer", "full-sweep-generation"],
)
def test_shadow_refresh_rejects_mirror_generation_change_after_detached_snapshot(
    monkeypatch,
    current_mirror_origin,
):
    cursor = _ActivationCursor()
    writes = []
    _install_shadow_refresh_origin(
        monkeypatch,
        current_mirror_origin=current_mirror_origin,
    )
    monkeypatch.setattr(
        attendance_readiness,
        "_compute_shadow_aggregate",
        lambda *_a, **_k: {
            "changed_worker_units": 0.0,
            "unassigned_units_today": 0.0,
            "oldest_unassigned_at": None,
        },
    )
    monkeypatch.setattr(
        attendance_readiness,
        "_shadow_day_health",
        lambda *_a, **_k: {
            "workday": True,
            "conflict_minutes": 0.0,
            "unmapped_minutes": 0.0,
            "missing_minutes": 0.0,
            "clean": True,
        },
    )
    monkeypatch.setattr(attendance_readiness.db, "cursor", lambda: _cursor(cursor))
    monkeypatch.setattr(
        attendance_readiness.app_settings,
        "set_setting",
        lambda key, value, *, cur=None: writes.append((key, value, cur)),
    )

    result = attendance_readiness.refresh_shadow_comparison(NOW, production_client=object())

    assert result.status == "failed"
    assert not [row for row in writes if row[0] == attendance_readiness._SHADOW_SETTING]
    assert writes[0][1]["reason"] == "mirror_changed"
    assert writes[0][2] is cursor


def test_shadow_final_store_serializes_with_mirror_sync_before_snapshot_reads(
    monkeypatch,
):
    original = (NOW - timedelta(seconds=1), NOW - timedelta(minutes=1), 7)
    changed = (NOW, NOW - timedelta(minutes=1), 7)
    state = {"origin": original}

    class InterleavingCursor(_ActivationCursor):
        read_committed = False

        def execute(self, sql, params=None):
            super().execute(sql, params)
            if "SET TRANSACTION ISOLATION LEVEL READ COMMITTED" in sql:
                self.read_committed = True
            if params == (attendance_mirror._SYNC_ADVISORY_LOCK_KEY,):
                # Model a sync that held the common lock first and committed
                # generation B before this final transaction could proceed.
                state["origin"] = changed

    cursor = InterleavingCursor()
    writes = []
    _install_shadow_refresh_origin(monkeypatch)
    monkeypatch.setattr(
        attendance_readiness,
        "_mirror_origin_cur",
        lambda _cur: state["origin"] if cursor.read_committed else original,
    )
    monkeypatch.setattr(
        attendance_readiness,
        "_compute_shadow_aggregate",
        lambda *_a, **_k: {
            "changed_worker_units": 0.0,
            "unassigned_units_today": 0.0,
            "oldest_unassigned_at": None,
        },
    )
    monkeypatch.setattr(
        attendance_readiness,
        "_shadow_day_health",
        lambda *_a, **_k: {
            "workday": True,
            "conflict_minutes": 0.0,
            "unmapped_minutes": 0.0,
            "missing_minutes": 0.0,
            "clean": True,
        },
    )
    monkeypatch.setattr(attendance_readiness.db, "cursor", lambda: _cursor(cursor))
    monkeypatch.setattr(
        attendance_readiness.app_settings,
        "set_setting",
        lambda key, value, *, cur=None: writes.append((key, value, cur)),
    )

    result = attendance_readiness.refresh_shadow_comparison(NOW, production_client=object())

    assert result.status == "failed"
    assert not [row for row in writes if row[0] == attendance_readiness._SHADOW_SETTING]
    assert writes[0][1]["reason"] == "mirror_changed"
    assert cursor.operations[:4] == [
        ("SET TRANSACTION ISOLATION LEVEL READ COMMITTED", None),
        (
            "LOCK TABLE global_schedule, saturday_schedule, schedules, company_holidays, "
            "saturday_recruitments, work_centers, departments, people, "
            "wc_time_attributions IN SHARE MODE",
            None,
        ),
        ("SELECT pg_advisory_xact_lock(%s)", (attendance_readiness._READINESS_LOCK_ID,)),
        (
            "SELECT pg_advisory_xact_lock(%s)",
            (attendance_mirror._SYNC_ADVISORY_LOCK_KEY,),
        ),
    ]


@pytest.mark.parametrize(
    ("mirror_incremental", "mirror_sweep"),
    [
        (NOW + timedelta(microseconds=1), NOW - timedelta(minutes=1)),
        (NOW - timedelta(seconds=1), NOW + timedelta(microseconds=1)),
    ],
    ids=["future-incremental", "future-full-sweep"],
)
def test_shadow_refresh_rejects_future_mirror_origin_before_replacing_last_good(
    monkeypatch,
    mirror_incremental,
    mirror_sweep,
):
    writes = []
    _install_shadow_refresh_origin(
        monkeypatch,
        mirror_incremental=mirror_incremental,
        mirror_sweep=mirror_sweep,
    )
    monkeypatch.setattr(
        attendance_readiness,
        "_compute_shadow_aggregate",
        lambda *_a, **_k: pytest.fail("future mirror origin reached meter work"),
    )
    monkeypatch.setattr(
        attendance_readiness.app_settings,
        "set_setting",
        lambda key, value, *, cur=None: writes.append((key, value, cur)),
    )

    result = attendance_readiness.refresh_shadow_comparison(NOW, production_client=object())

    assert result.status == "failed"
    assert not [row for row in writes if row[0] == attendance_readiness._SHADOW_SETTING]
    assert writes[0][0] == attendance_readiness._SHADOW_ERROR_SETTING
    assert writes[0][1]["reason"] == "production_source_unavailable"


def test_shadow_refresh_fails_closed_when_strict_attribution_rows_are_unavailable(
    monkeypatch,
):
    writes = []

    class UnavailableAttributionCursor(_ShadowConfigCursor):
        def __init__(self):
            super().__init__()
            self.row = None

        def execute(self, sql, params=None):
            normalized = " ".join(sql.split())
            if "FROM app_settings WHERE key" in normalized:
                key = params[0]
                value = (
                    {"mode": "shadow", "cutover_at": None, "live_gate": None}
                    if key == attendance_readiness._ROLLOUT_SETTING
                    else {"entered_at": (NOW - timedelta(days=2)).isoformat()}
                )
                self.row = {"value": value}
                return
            if "FROM wc_time_attributions" in normalized:
                raise RuntimeError("attribution rows unavailable")
            self.row = None
            super().execute(sql, params)

        def fetchone(self):
            return self.row

    cursor = UnavailableAttributionCursor()
    monkeypatch.setattr(attendance_readiness.db, "cursor", lambda: _cursor(cursor))
    monkeypatch.setattr(
        attendance_readiness.app_settings,
        "set_setting",
        lambda key, value, *, cur=None: writes.append((key, value, cur)),
    )

    result = attendance_readiness.refresh_shadow_comparison(NOW, production_client=object())

    assert result.status == "failed"
    assert not [row for row in writes if row[0] == attendance_readiness._SHADOW_SETTING]
    assert writes[0][0] == attendance_readiness._SHADOW_ERROR_SETTING
    assert writes[0][1]["reason"] == "configuration_unavailable"
    assert "attribution rows unavailable" in (result.error or "")


def test_shadow_refresh_cannot_store_after_rollout_leaves_shadow_mid_compute(monkeypatch):
    cursor = _ActivationCursor()
    writes = []
    _install_shadow_refresh_origin(
        monkeypatch,
        current_rollout=attendance_location_policy.RolloutConfig("off", None, None),
    )
    monkeypatch.setattr(
        attendance_readiness,
        "_compute_shadow_aggregate",
        lambda *_a, **_k: {
            "changed_worker_units": 0.0,
            "unassigned_units_today": 0.0,
            "oldest_unassigned_at": None,
        },
    )
    monkeypatch.setattr(
        attendance_readiness,
        "_shadow_day_health",
        lambda *_a, **_k: {
            "workday": True,
            "conflict_minutes": 0.0,
            "unmapped_minutes": 0.0,
            "missing_minutes": 0.0,
            "clean": True,
        },
    )
    monkeypatch.setattr(attendance_readiness.db, "cursor", lambda: _cursor(cursor))
    monkeypatch.setattr(
        attendance_readiness.app_settings,
        "set_setting",
        lambda key, value, *, cur=None: writes.append((key, value, cur)),
    )

    result = attendance_readiness.refresh_shadow_comparison(NOW, production_client=object())

    assert result.status == "failed"
    assert not [row for row in writes if row[0] == attendance_readiness._SHADOW_SETTING]
    assert writes[0][1]["reason"] == "configuration_changed"


def test_shadow_refresh_resets_clean_evidence_when_config_digest_changes(monkeypatch):
    old_digest = "a" * 64
    new_digest = "b" * 64
    old_day = DAY - timedelta(days=1)
    existing = {
        **_valid_shadow_health_setting(),
        "day": old_day.isoformat(),
        "computed_at": (NOW - timedelta(days=1)).isoformat(),
        "config_digest": old_digest,
        "complete_days": [old_day.isoformat()],
        "complete_day_health": [
            {
                "day": old_day.isoformat(),
                "completed_at": datetime.combine(
                    old_day,
                    time(15),
                    tzinfo=shift_config.SITE_TZ,
                )
                .astimezone(UTC)
                .isoformat(),
                "schedule_digest": "c" * 64,
                "workday": True,
                "conflict_minutes": 0.0,
                "unmapped_minutes": 0.0,
                "missing_minutes": 0.0,
                "unassigned_units": 0.0,
                "clean": True,
            }
        ],
    }

    class ExistingCursor(_ActivationCursor):
        def __init__(self):
            super().__init__()
            self.row = None

        def execute(self, sql, params=None):
            super().execute(sql, params)
            self.row = {"value": existing} if "FROM app_settings" in " ".join(sql.split()) else None

        def fetchone(self):
            return self.row

    cursor = ExistingCursor()
    writes = []
    config, mirror = _install_shadow_refresh_origin(monkeypatch)
    changed_config = replace(config, digest=new_digest)
    epoch = NOW - timedelta(days=2)
    monkeypatch.setattr(
        attendance_readiness,
        "_shadow_refresh_origin",
        lambda _day: attendance_readiness._ShadowRefreshOrigin(
            attendance_location_policy.RolloutConfig("shadow", None, None),
            epoch,
            changed_config,
        ),
    )
    monkeypatch.setattr(
        attendance_readiness,
        "_shadow_config_snapshot_cur",
        lambda _cur, _day: changed_config,
    )
    monkeypatch.setattr(
        attendance_readiness.attendance_location_policy,
        "get_rollout_config",
        lambda: attendance_location_policy.RolloutConfig("shadow", None, None),
    )
    monkeypatch.setattr(
        attendance_readiness,
        "_compute_shadow_aggregate",
        lambda *_a, **_k: {
            "changed_worker_units": 0.0,
            "unassigned_units_today": 0.0,
            "oldest_unassigned_at": None,
        },
    )
    monkeypatch.setattr(
        attendance_readiness,
        "_shadow_day_health",
        lambda *_a, **_k: {
            "workday": True,
            "conflict_minutes": 0.0,
            "unmapped_minutes": 0.0,
            "missing_minutes": 0.0,
            "clean": True,
        },
    )
    monkeypatch.setattr(shift_config, "shift_end_for", lambda _day: time(23, 59))
    monkeypatch.setattr(attendance_readiness.db, "cursor", lambda: _cursor(cursor))
    monkeypatch.setattr(
        attendance_readiness.app_settings,
        "set_setting",
        lambda key, value, *, cur=None: writes.append((key, value, cur)),
    )

    result = attendance_readiness.refresh_shadow_comparison(NOW, production_client=object())

    assert result.status == "stored"
    stored = next(
        value for key, value, _cur in writes if key == attendance_readiness._SHADOW_SETTING
    )
    assert stored["config_digest"] == new_digest
    assert stored["complete_days"] == []
    assert stored["complete_day_health"] == []


def test_pending_live_keeps_shadow_comparison_fresh_until_boundary(monkeypatch):
    cutover = NOW + timedelta(days=1)
    cursor = _ActivationCursor()
    _install_shadow_refresh_origin(monkeypatch, rollout=_pending_live(cutover))
    monkeypatch.setattr(
        attendance_readiness.attendance_location_policy,
        "get_rollout_config",
        lambda: _pending_live(cutover),
    )
    monkeypatch.setattr(
        attendance_readiness,
        "_compute_shadow_aggregate",
        lambda *_a, **_k: {
            "changed_worker_units": 0.0,
            "unassigned_units_today": 0.0,
            "oldest_unassigned_at": None,
        },
    )
    monkeypatch.setattr(
        attendance_readiness,
        "_shadow_day_health",
        lambda *_a, **_k: {
            "workday": True,
            "conflict_minutes": 0.0,
            "unmapped_minutes": 0.0,
            "missing_minutes": 0.0,
            "unassigned_units": 0.0,
            "clean": True,
        },
    )
    monkeypatch.setattr(attendance_readiness.app_settings, "get_setting", lambda _key: None)
    writes = []
    monkeypatch.setattr(
        attendance_readiness.app_settings,
        "set_setting",
        lambda key, value, *, cur=None: writes.append((key, value, cur)),
    )
    monkeypatch.setattr(shift_config, "shift_end_for", lambda _day: time(15, 0))
    monkeypatch.setattr(attendance_readiness.db, "cursor", lambda: _cursor(cursor))

    result = attendance_readiness.refresh_shadow_comparison(NOW, production_client=object())

    assert result.status == "stored"
    assert writes and writes[0][1]["computed_at"] == NOW.isoformat()
    assert writes[0][2] is cursor
    assert attendance_readiness._SHADOW_ERROR_SETTING in cursor.operations[-1][1]


def test_active_live_keeps_aggregate_health_current_without_production_mutation(
    monkeypatch,
):
    cutover = NOW - timedelta(days=1)
    active = attendance_location_policy.RolloutConfig(
        "live",
        cutover,
        attendance_location_policy.LiveGate(
            checked_at=cutover - timedelta(minutes=5),
            report_digest="a" * 64,
            activated_at=cutover,
        ),
    )
    cursor = _ActivationCursor()
    writes = []
    config, mirror = _install_shadow_refresh_origin(monkeypatch, rollout=active)
    monkeypatch.setattr(
        attendance_readiness.attendance_location_policy,
        "get_rollout_config",
        lambda: active,
    )
    monkeypatch.setattr(
        attendance_readiness,
        "_compute_shadow_aggregate",
        lambda *_a, **_k: {
            "changed_worker_units": 1.5,
            "unassigned_units_today": 0.5,
            "oldest_unassigned_at": NOW - timedelta(minutes=2),
        },
    )
    monkeypatch.setattr(
        attendance_readiness,
        "_shadow_day_health",
        lambda *_a, **_k: {
            "workday": True,
            "conflict_minutes": 0.0,
            "unmapped_minutes": 0.0,
            "missing_minutes": 0.0,
            "unassigned_units": 0.5,
            "clean": False,
        },
    )
    monkeypatch.setattr(attendance_readiness.app_settings, "get_setting", lambda _key: None)
    monkeypatch.setattr(
        attendance_readiness.app_settings,
        "set_setting",
        lambda key, value, *, cur=None: writes.append((key, value, cur)),
    )
    monkeypatch.setattr(shift_config, "shift_end_for", lambda _day: time(15, 0))
    monkeypatch.setattr(attendance_readiness.db, "cursor", lambda: _cursor(cursor))

    result = attendance_readiness.refresh_shadow_comparison(NOW, production_client=object())

    assert result.status == "stored"
    assert len(writes) == 1
    key, value, write_cursor = writes[0]
    assert key == attendance_readiness._SHADOW_SETTING
    assert write_cursor is cursor
    assert value["config_digest"] == config.digest
    assert value["day_config_digest"] == config.day_digest
    assert value["mirror_verified_through"] == (
        mirror.health.last_incremental_completed_at.isoformat()
    )
    assert value["changed_worker_units"] == 1.5
    assert value["unassigned_units_today"] == 0.5
    serialized = repr([*writes, *cursor.operations]).lower()
    assert "production_daily" not in serialized
    assert "attendance_strict_days" not in serialized
    assert "attendance_recalc_queue" not in serialized
    assert "insert into odoo_attendance" not in serialized
    assert "update odoo_attendance" not in serialized
    assert "delete from odoo_attendance" not in serialized


def test_latest_shadow_failure_is_durable_and_blocks_an_old_good_aggregate(monkeypatch):
    writes = []
    _install_shadow_refresh_origin(monkeypatch)
    monkeypatch.setattr(
        attendance_readiness.attendance_location_policy,
        "get_rollout_config",
        lambda: attendance_location_policy.RolloutConfig("shadow", None, None),
    )
    monkeypatch.setattr(
        attendance_readiness,
        "_compute_shadow_aggregate",
        lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("meter timed out")),
    )
    monkeypatch.setattr(
        attendance_readiness.app_settings,
        "set_setting",
        lambda key, value, *, cur=None: writes.append((key, value, cur)),
    )

    result = attendance_readiness.refresh_shadow_comparison(NOW, production_client=object())

    assert result.status == "failed"
    assert writes == [
        (
            attendance_readiness._SHADOW_ERROR_SETTING,
            {
                "failed_at": NOW.isoformat(),
                "reason": "production_source_unavailable",
                "error_type": "RuntimeError",
            },
            None,
        )
    ]


def test_due_activation_rebinds_gate_to_exact_boundary_report(monkeypatch):
    cursor = _ActivationCursor()
    cutover = NOW - timedelta(minutes=1)
    report = attendance_readiness._report_from_inputs(_ready_inputs(), NOW)
    saved = []
    monkeypatch.setattr(attendance_readiness.db, "cursor", lambda: _cursor(cursor))
    monkeypatch.setattr(
        attendance_readiness, "_lock_rollout_config_cur", lambda _cur: _pending_live(cutover)
    )
    monkeypatch.setattr(
        attendance_readiness,
        "_validate_configured_boundary_cur",
        lambda _cur, value: value,
    )
    monkeypatch.setattr(attendance_readiness, "_build_report_cur", lambda *_a: report)
    monkeypatch.setattr(attendance_readiness, "_enqueue_cutover_cur", lambda *_a: None)
    monkeypatch.setattr(attendance_readiness, "_clear_blocked_cur", lambda *_a: None)
    monkeypatch.setattr(
        attendance_location_policy,
        "set_rollout_config",
        lambda config, *, cur=None: saved.append(config),
    )
    monkeypatch.setattr(attendance_readiness.app_settings, "set_setting", lambda *_a, **_k: None)

    attendance_readiness.activate_due_cutover(NOW)

    assert saved[0].live_gate.checked_at == NOW
    assert saved[0].live_gate.report_digest == attendance_readiness.report_digest(report, cutover)


def test_shadow_comparison_uses_one_strict_source_snapshot(monkeypatch):
    calls = []
    cursor = _ShadowConfigCursor()
    cursor.sources["wc_time_attributions"] = [
        {
            "id": 41,
            "wc_name": "WC A",
            "person_name": "Testing",
            "employee_odoo_id": None,
            "start_utc": NOW - timedelta(minutes=20),
            "end_utc": NOW - timedelta(minutes=10),
            "source": "testing",
            "breakdown_id": None,
        }
    ]
    config = attendance_readiness._shadow_config_snapshot_cur(cursor, DAY)
    mirror = attendance_mirror.AttendanceMirrorSnapshot(
        health=attendance_mirror.MirrorHealth(
            last_incremental_completed_at=NOW,
            last_full_sweep_completed_at=NOW - timedelta(minutes=1),
            baseline_completed_at=NOW - timedelta(days=1),
            oldest_recalc_requested_at=None,
            last_error=None,
        ),
        rows=(),
    )
    inputs = SimpleNamespace(
        segments=(),
        wc_totals={},
        samples_by_wc={},
        active_intervals_by_wc={},
        excluded_minutes={},
        break_windows=(),
        testing_windows={},
        breakdown_windows={},
    )
    monkeypatch.setattr(attendance_readiness.db, "query", lambda *_a, **_k: [])
    monkeypatch.setattr(
        production_history,
        "_strict_inputs_for_day",
        lambda *_a, **kwargs: calls.append(kwargs) or inputs,
    )
    monkeypatch.setattr(
        wc_attributions,
        "for_day",
        lambda *_a, **_k: pytest.fail("shadow compute re-read attribution rows"),
    )
    monkeypatch.setattr(
        production_history,
        "_strict_attribution_for",
        lambda *_a, **_k: pytest.fail("shadow attribution re-read its source"),
    )
    monkeypatch.setattr(
        wc_attributions,
        "shadow_unassigned_runs_for_day",
        lambda *_a, **_k: pytest.fail("unassigned runs re-read their source"),
    )

    result = attendance_readiness._compute_shadow_aggregate(
        DAY,
        NOW,
        object(),
        config_snapshot=config,
        mirror_snapshot=mirror,
        location_spans=(),
    )

    assert len(calls) == 1
    assert calls[0]["attribution_rows"] == config.attribution_rows
    assert result["changed_worker_units"] == 0.0


def test_future_health_timestamp_fails_closed(monkeypatch):
    monkeypatch.setattr(
        attendance_readiness,
        "_read_inputs",
        lambda _now: _ready_inputs(last_incremental_completed_at=NOW + timedelta(seconds=1)),
    )

    report = attendance_readiness.build_report(NOW)

    assert report.ready is False
    assert "attendance_mirror_invalid" in report.blockers


def test_schedule_rejects_a_clock_regression_as_a_stale_report(monkeypatch):
    marker = _ActivationCursor()
    cutover = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
    monkeypatch.setattr(shift_config, "shift_start_for", lambda _day: time(7, 0))
    monkeypatch.setattr(shift_config, "is_workday", lambda _day: True)
    monkeypatch.setattr(attendance_readiness.db, "cursor", lambda: _cursor(marker))
    monkeypatch.setattr(
        attendance_readiness,
        "_lock_rollout_config_cur",
        lambda _cur: attendance_location_policy.RolloutConfig("shadow", None, None),
    )
    monkeypatch.setattr(
        attendance_readiness,
        "_build_report_cur",
        lambda *_a: attendance_readiness._report_from_inputs(_ready_inputs(), NOW),
    )
    monkeypatch.setattr(attendance_readiness, "_utc_now", lambda: NOW - timedelta(seconds=1))

    with pytest.raises(ValueError, match="live_readiness_stale"):
        attendance_readiness.schedule_live_cutover(cutover, now_utc=NOW)
