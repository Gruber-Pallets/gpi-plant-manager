from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta

import pytest

from zira_dashboard import (
    attendance_corrections,
    attendance_exceptions,
    attendance_location_policy,
    attendance_mirror,
    attendance_readiness,
    attendance_timeline,
    attendance_sync,
    db,
    precompute,
    production_history,
    shift_config,
    wc_attributions,
)


NOW = datetime(2026, 8, 31, 14, 0, tzinfo=UTC)
DAY = date(2026, 8, 31)


@contextmanager
def _cursor(value):
    yield value


def _install_shadow_refresh_origin(monkeypatch, *, rollout=None):
    rollout = rollout or attendance_location_policy.RolloutConfig("shadow", None, None)
    epoch = NOW - timedelta(days=2)
    config = attendance_readiness._ShadowConfigSnapshot(
        day=DAY,
        digest="a" * 64,
        day_digest="b" * 64,
        work_center_names={},
        department_requirements={},
        employee_departments={},
        employee_wage_types={},
        attribution_rows=(),
        metered_locations=(),
        workday=True,
        shift_start_utc=NOW - timedelta(hours=2),
        shift_end_utc=NOW + timedelta(hours=2),
        break_windows=(),
    )
    health = attendance_mirror.MirrorHealth(
        last_incremental_completed_at=NOW - timedelta(seconds=1),
        last_full_sweep_completed_at=NOW - timedelta(minutes=1),
        baseline_completed_at=NOW - timedelta(minutes=1),
        oldest_recalc_requested_at=None,
        last_error=None,
        full_sweep_generation=3,
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
        lambda _cur, **_k: rollout,
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
            health.last_incremental_completed_at,
            health.last_full_sweep_completed_at,
            health.full_sweep_generation,
        ),
    )


def test_missing_or_malformed_local_state_never_reports_ready(monkeypatch):
    monkeypatch.setattr(
        attendance_readiness,
        "_read_inputs",
        lambda _now: attendance_readiness._ReadinessInputs.unavailable(),
    )

    report = attendance_readiness.build_report(NOW)

    assert report.ready is False
    assert "rollout_state_unavailable" in report.blockers
    assert "attendance_baseline_incomplete" in report.blockers
    assert "shadow_comparison_unavailable" in report.blockers


def test_shadow_refresh_does_not_replace_saved_aggregate_on_source_failure(monkeypatch):
    writes = []
    _install_shadow_refresh_origin(monkeypatch)
    monkeypatch.setattr(
        attendance_readiness.attendance_location_policy,
        "get_rollout_config",
        lambda: attendance_readiness.attendance_location_policy.RolloutConfig("shadow", None, None),
    )
    monkeypatch.setattr(
        attendance_readiness,
        "_compute_shadow_aggregate",
        lambda *_a, **_k: (_ for _ in ()).throw(
            production_history.ProductionSourceUnavailable(
                "strict production samples do not match the positive total"
            )
        ),
    )
    monkeypatch.setattr(
        attendance_readiness.app_settings,
        "set_setting",
        lambda *args, **kwargs: writes.append((args, kwargs)),
    )

    result = attendance_readiness.refresh_shadow_comparison(NOW, production_client=object())

    assert result.status == "failed"
    assert not [row for row in writes if row[0][0] == attendance_readiness._SHADOW_SETTING]
    assert writes[0][0][0] == attendance_readiness._SHADOW_ERROR_SETTING
    assert "samples do not match" in (result.error or "")


def test_active_live_refresh_failure_preserves_last_good_aggregate_and_sets_fresh_blocker(
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
    old_good = {
        "day": DAY.isoformat(),
        "computed_at": (NOW - timedelta(minutes=1)).isoformat(),
        "changed_worker_units": 2.0,
        "unassigned_units_today": 1.0,
    }
    writes = []
    _install_shadow_refresh_origin(monkeypatch, rollout=active)
    monkeypatch.setattr(
        attendance_readiness.attendance_location_policy,
        "get_rollout_config",
        lambda: active,
    )
    monkeypatch.setattr(
        attendance_readiness,
        "_compute_shadow_aggregate",
        lambda *_a, **_k: (_ for _ in ()).throw(TimeoutError("meter timeout")),
    )
    monkeypatch.setattr(
        attendance_readiness.app_settings,
        "get_setting",
        lambda key: old_good if key == attendance_readiness._SHADOW_SETTING else None,
    )
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
                "reason": "production_source_unavailable",
                "error_type": "TimeoutError",
            },
            None,
        )
    ]
    assert old_good["computed_at"] == (NOW - timedelta(minutes=1)).isoformat()


def test_stale_preview_failed_verification_and_partial_sweep_are_explicit_blockers(
    monkeypatch,
):
    base = attendance_readiness._ReadinessInputs.unavailable()
    inputs = base.__class__(
        **{
            **base.__dict__,
            "rollout_mode": "shadow",
            "rollout_valid": True,
            "baseline_completed_at": NOW - timedelta(days=1),
            "last_incremental_completed_at": NOW - timedelta(seconds=20),
            "last_full_sweep_completed_at": NOW - timedelta(hours=3),
            "projection_completed_at": NOW - timedelta(minutes=5),
            "failed_corrections": 1,
            "correction_verification_failures_today": 1,
            "shadow_error": "partial production source",
        }
    )
    monkeypatch.setattr(attendance_readiness, "_read_inputs", lambda _now: inputs)

    report = attendance_readiness.build_report(NOW)

    assert {
        "attendance_full_sweep_stale",
        "attendance_projection_stale",
        "attendance_correction_failed",
        "shadow_comparison_unavailable",
    } <= set(report.blockers)


def test_shadow_refresh_writes_only_aggregate_health_and_never_production_or_strict_state(
    monkeypatch,
):
    writes = []

    class Cursor:
        def execute(self, sql, params=None):
            writes.append((" ".join(sql.split()), params))

        def fetchone(self):
            return None

    monkeypatch.setattr(
        attendance_location_policy,
        "get_rollout_config",
        lambda: attendance_location_policy.RolloutConfig("shadow", None, None),
    )
    _install_shadow_refresh_origin(monkeypatch)
    monkeypatch.setattr(
        attendance_readiness,
        "_compute_shadow_aggregate",
        lambda *_a, **_k: {
            "changed_worker_units": 3.0,
            "unassigned_units_today": 2.0,
            "oldest_unassigned_at": NOW - timedelta(minutes=1),
        },
    )
    monkeypatch.setattr(attendance_readiness.app_settings, "get_setting", lambda _key: None)
    monkeypatch.setattr(shift_config, "shift_end_for", lambda _day: datetime.min.time())
    monkeypatch.setattr(shift_config, "is_workday", lambda _day: True)
    monkeypatch.setattr(
        attendance_readiness.attendance_timeline,
        "timeline_for_range",
        lambda *_a, **_k: (),
    )
    monkeypatch.setattr(
        attendance_readiness.app_settings,
        "set_setting",
        lambda key, value, *, cur=None: writes.append((f"setting:{key}", value, cur)),
    )
    monkeypatch.setattr(attendance_readiness.db, "cursor", lambda: _cursor(Cursor()))

    result = attendance_readiness.refresh_shadow_comparison(NOW, production_client=object())

    assert result.status == "stored"
    assert [row for row in writes if row[0] == f"setting:{attendance_readiness._SHADOW_SETTING}"]
    serialized = repr(writes).lower()
    assert "insert into production_daily" not in serialized
    assert "delete from production_daily" not in serialized
    assert "attendance_strict_days" not in serialized
    assert "attendance_recalc_queue" not in serialized


def test_positive_total_without_samples_preserves_snapshot_and_surfaces_source_exception(
    monkeypatch,
):
    prior_snapshot = [{"day": DAY, "emp_id": "101", "units": 7.0}]
    store_calls = []
    enqueued = []

    def invalid_strict_source(_day, _client):
        production_history._validate_strict_sample_totals(
            {"WC A": (10.0, 0.0)},
            {"WC A": ()},
        )

    monkeypatch.setattr(production_history, "attribution_for", invalid_strict_source)
    monkeypatch.setattr(
        precompute,
        "store_prepared_day",
        lambda prepared: store_calls.append(prepared),
    )
    monkeypatch.setattr(
        attendance_mirror,
        "enqueue_recalc",
        lambda days, reason: enqueued.append((tuple(days), reason)),
    )

    with pytest.raises(
        production_history.ProductionSourceUnavailable,
        match="samples.*do not match",
    ) as failed:
        precompute.precompute_day(DAY, object())

    assert prior_snapshot == [{"day": DAY, "emp_id": "101", "units": 7.0}]
    assert store_calls == []
    assert enqueued == [((DAY,), "production_source_unavailable")]

    monkeypatch.setattr(
        attendance_location_policy,
        "get_rollout_config",
        lambda: attendance_location_policy.RolloutConfig("shadow", None, None),
    )
    monkeypatch.setattr(attendance_location_policy, "strict_days", lambda: set())
    monkeypatch.setattr(
        attendance_mirror,
        "health_snapshot",
        lambda: attendance_mirror.MirrorHealth(
            last_incremental_completed_at=NOW,
            last_full_sweep_completed_at=NOW - timedelta(minutes=20),
            baseline_completed_at=NOW - timedelta(days=1),
            oldest_recalc_requested_at=None,
            last_error=None,
        ),
    )
    monkeypatch.setattr(attendance_mirror, "rows_overlapping", lambda *_a: ())
    monkeypatch.setattr(attendance_timeline, "timeline_for_range", lambda *_a, **_k: ())
    monkeypatch.setattr(attendance_exceptions, "_failed_department_repairs", lambda *_a: ())
    monkeypatch.setattr(
        wc_attributions,
        "shadow_unassigned_runs_for_day",
        lambda *_a, **_k: (_ for _ in ()).throw(failed.value),
    )
    monkeypatch.setattr(db, "query", lambda *_a, **_k: [])

    exception_snapshot = attendance_exceptions.build_snapshot(
        DAY,
        now_utc=NOW,
        production_client=object(),
    )

    issues = exception_snapshot.issues_for("production_source_unavailable")
    assert len(issues) == 1
    assert issues[0].priority == "urgent"
    assert "samples" in issues[0].reason
    assert exception_snapshot.complete is False


def test_timeout_partial_sweep_stale_preview_and_failed_reread_preserve_last_good_state(
    monkeypatch,
):
    prior_mirror = [{"odoo_attendance_id": 901, "odoo_work_center_id": 11}]
    prior_credit = [{"day": DAY, "emp_id": "101", "units": 7.0}]

    class RunBackend:
        def __init__(self):
            self.store_calls = []

        def sync_state(self):
            return attendance_sync.SyncState(
                cursor_write_date=NOW - timedelta(minutes=1),
                cursor_id=901,
                last_incremental_completed_at=NOW - timedelta(minutes=1),
                last_full_sweep_completed_at=NOW - timedelta(minutes=20),
                full_sweep_generation=2,
                baseline_completed_at=NOW - timedelta(days=1),
            )

        def record_incremental_started(self, _started_at):
            return None

        def store_incremental_cycle(self, *args, **kwargs):
            self.store_calls.append((args, kwargs))
            return set()

        def active_attendance_ids(self):
            return {901}

        def tombstoned_attendance_ids(self, _ids):
            return set()

        def store_full_sweep(self, *args, **kwargs):
            self.store_calls.append((args, kwargs))
            return attendance_sync.SweepStoreResult(frozenset(), 0)

    class Backend:
        def __init__(self):
            self.run = RunBackend()
            self.failures = []

        @contextmanager
        def logical_run(self):
            yield self.run

        def record_failure(self, owner, error):
            self.failures.append((owner, str(error)))

    class TimeoutSource:
        def fetch_attendance_changes(self, **_kwargs):
            return []

        def fetch_open_attendance_rows(self):
            raise TimeoutError("Odoo timeout")

    backend = Backend()
    monkeypatch.setattr(attendance_sync, "_backend", backend)
    monkeypatch.setattr(attendance_sync, "_source", TimeoutSource())

    timeout_result = attendance_sync.run_incremental_sync(now_utc=NOW)

    assert timeout_result.success is False
    assert backend.run.store_calls == []
    assert prior_mirror == [{"odoo_attendance_id": 901, "odoo_work_center_id": 11}]

    class PartialSweepSource:
        def fetch_complete_attendance_id_sweep(self):
            return attendance_sync.AttendanceIdSweepSnapshot((901,), complete=False)

    monkeypatch.setattr(attendance_sync, "_source", PartialSweepSource())
    partial_result = attendance_sync.run_full_sweep(now_utc=NOW)

    assert partial_result.success is False
    assert backend.run.store_calls == []
    assert prior_mirror == [{"odoo_attendance_id": 901, "odoo_work_center_id": 11}]

    source = {
        "odoo_attendance_id": 901,
        "employee_odoo_id": 101,
        "check_in_utc": NOW - timedelta(hours=1),
        "check_out_utc": None,
        "odoo_work_center_id": 11,
        "odoo_department_id": 7,
        "odoo_write_date": NOW - timedelta(minutes=1),
    }
    plan = attendance_corrections.plan_correction(
        rows=[source],
        employee_odoo_id=101,
        start_utc=NOW - timedelta(minutes=30),
        end_utc=None,
        odoo_work_center_id=22,
        odoo_department_id=7,
    )
    writes = []

    class StaleFacade:
        def fetch_attendance_rows_by_ids(self, _ids):
            return [{**source, "odoo_work_center_id": 33, "odoo_write_date": NOW}]

        def fetch_employee_attendance_rows(self, *_args):
            raise TimeoutError("verification reread failed")

        def update_attendance(self, *args, **kwargs):
            writes.append((args, kwargs))

    facade = StaleFacade()
    with pytest.raises(attendance_corrections._SourceChanged):
        attendance_corrections._preflight_operations(
            facade,
            plan.operations,
            plan.source_intervals,
        )
    assert writes == []

    completed = []
    created_id = 1000
    for operation in plan.operations:
        record = {
            "operation_key": operation.key,
            "kind": operation.kind,
            "attendance_id": operation.attendance_id,
        }
        if operation.kind == "create":
            created_id += 1
            record["attendance_id"] = created_id
        completed.append(record)
    with pytest.raises(TimeoutError, match="verification reread failed"):
        attendance_corrections._verification_rows(
            facade,
            {101: plan},
            completed,
            NOW - timedelta(minutes=30),
            None,
        )

    credit = production_history.attribute_for_segments(
        (),
        wc_totals={"WC B": (10, 0)},
        samples_by_wc={"WC B": [(NOW, 10)]},
        productive_minutes=lambda *_a: 0,
        strict=True,
    )
    assert credit == {}
    assert prior_credit == [{"day": DAY, "emp_id": "101", "units": 7.0}]
