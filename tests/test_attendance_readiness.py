"""Readiness and atomic cutover contracts for Odoo attendance locations."""

from __future__ import annotations

from dataclasses import asdict, replace
from datetime import UTC, date, datetime, time, timedelta
from contextlib import contextmanager
import json
import math
import os
from types import SimpleNamespace
from threading import Event, Lock, Thread
from unittest.mock import MagicMock

import pytest

from zira_dashboard import (
    _schema,
    attendance_exceptions,
    attendance_location_policy,
    attendance_readiness,
    attendance_recalc,
    attendance_timeline,
    precompute,
    production_history,
)


NOW = datetime(2026, 9, 1, 15, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _canonical_shift_fixture(monkeypatch):
    monkeypatch.setattr(
        attendance_readiness.shift_config,
        "snapshot_for",
        lambda day, **_kwargs: SimpleNamespace(
            day=day,
            shift_start=datetime.min.time().replace(hour=7),
            shift_end=datetime.min.time().replace(hour=15),
            breaks=(),
            is_workday=True,
        ),
    )


@contextmanager
def _source_fence():
    yield object()


def _set_persisted_due_boundary(db):
    """Make the current DB clock an exact canonical start-boundary fixture."""
    original_rows = db.query(
        "SELECT shift_start, shift_end, work_weekdays, breaks, updated_at "
        "FROM global_schedule WHERE id = 1"
    )
    original = dict(original_rows[0]) if original_rows else None
    now = db.query("SELECT clock_timestamp() AS now")[0]["now"]
    local_cutover = (now - timedelta(seconds=1)).astimezone(
        attendance_readiness.shift_config.SITE_TZ
    )
    shift_start = local_cutover.time().replace(tzinfo=None)
    candidate_end = local_cutover + timedelta(hours=1)
    shift_end = (
        candidate_end.time().replace(tzinfo=None)
        if candidate_end.date() == local_cutover.date()
        else time.max
    )
    db.execute(
        "INSERT INTO global_schedule "
        "(id, shift_start, shift_end, work_weekdays, breaks, updated_at) "
        "VALUES (1, %s, %s, %s, '[]'::jsonb, now()) "
        "ON CONFLICT (id) DO UPDATE SET shift_start = EXCLUDED.shift_start, "
        "shift_end = EXCLUDED.shift_end, work_weekdays = EXCLUDED.work_weekdays, "
        "breaks = EXCLUDED.breaks, updated_at = EXCLUDED.updated_at",
        (shift_start, shift_end, [local_cutover.weekday()]),
    )
    snapshot = attendance_readiness.shift_config.snapshot_for(local_cutover.date())
    assert snapshot.is_workday
    cutover = datetime.combine(
        local_cutover.date(),
        snapshot.shift_start,
        tzinfo=attendance_readiness.shift_config.SITE_TZ,
    )
    assert cutover.astimezone(UTC) == local_cutover.astimezone(UTC)
    return now, cutover, original


def _restore_persisted_boundary(db, original) -> None:
    if original is None:
        db.execute("DELETE FROM global_schedule WHERE id = 1")
        return
    db.execute(
        "UPDATE global_schedule SET shift_start = %s, shift_end = %s, "
        "work_weekdays = %s, breaks = %s, updated_at = %s WHERE id = 1",
        (
            original["shift_start"],
            original["shift_end"],
            original["work_weekdays"],
            original["breaks"],
            original["updated_at"],
        ),
    )


def _frozen_day(day: date, source: str = "frozen-day"):
    return production_history.StrictSourceSnapshot(
        day=day,
        shift_start_utc=datetime.combine(day, datetime.min.time(), tzinfo=UTC)
        + timedelta(hours=12),
        shift_end_utc=datetime.combine(day, datetime.min.time(), tzinfo=UTC)
        + timedelta(hours=20),
        break_windows=(),
        shift_by_day={},
        stations=(),
        work_center_by_odoo_id={},
        source_fingerprint=source,
    )


def _bound_decision(
    report,
    local_source="local-source",
    production_source="production-source",
    frozen_source="meter-source",
    checked_at=NOW,
    valid_for=timedelta(minutes=5),
):
    digest = attendance_readiness.report_digest(report)
    valid_until = checked_at + valid_for
    return attendance_readiness.DecisionSnapshot(
        report=report,
        report_digest=digest,
        local_source_fingerprint=local_source,
        production_fingerprint=production_source,
        frozen_production_fingerprint=frozen_source,
        source_binding_digest=attendance_readiness._decision_binding_digest(
            digest,
            local_source,
            production_source,
            frozen_source,
            checked_at=checked_at,
            valid_until=valid_until,
        ),
        checked_at=checked_at,
        valid_until=valid_until,
    )


def _decision(report):
    return _bound_decision(report)


def _inputs(**overrides):
    values = {
        "baseline_complete": True,
        "mirror_error": None,
        "projection_complete": True,
        "mirror_age_seconds": 15.0,
        "last_full_sweep_age_seconds": 600.0,
        "open_rows_not_refreshed": 0,
        "last_sweep_deletion_count": 2,
        "projection_lag_seconds": 0.0,
        "recalc_queue_age_seconds": None,
        "recalc_queue_depth": 0,
        "open_conflicts": 0,
        "conflict_minutes_today": 0.0,
        "open_unmapped": 0,
        "unmapped_minutes_today": 0.0,
        "open_missing_required": 0,
        "missing_minutes_today": 0.0,
        "unassigned_units_today": 0.0,
        "oldest_unassigned_age_seconds": None,
        "shadow_changed_worker_units": 0.0,
        "failed_corrections": 0,
        "correction_retries_today": 0,
        "correction_verification_failures_today": 0,
        "failed_department_repairs": 0,
        "unmapped_affects_production": False,
        "missing_affects_production": False,
        "comparison_identity_available": True,
        "shadow_day_complete": True,
    }
    values.update(overrides)
    return attendance_readiness._ReadinessInputs(**values)


def test_readiness_report_has_the_exact_public_fields():
    assert tuple(attendance_readiness.ReadinessReport.__dataclass_fields__) == (
        "ready",
        "mirror_age_seconds",
        "last_full_sweep_age_seconds",
        "open_rows_not_refreshed",
        "last_sweep_deletion_count",
        "projection_lag_seconds",
        "recalc_queue_age_seconds",
        "recalc_queue_depth",
        "open_conflicts",
        "conflict_minutes_today",
        "open_unmapped",
        "unmapped_minutes_today",
        "open_missing_required",
        "missing_minutes_today",
        "unassigned_units_today",
        "oldest_unassigned_age_seconds",
        "shadow_changed_worker_units",
        "failed_corrections",
        "correction_retries_today",
        "correction_verification_failures_today",
        "failed_department_repairs",
        "blockers",
    )


def test_rollout_audit_schema_is_append_only_and_identifier_safe():
    ddl = _schema.SCHEMA_DDL
    assert "CREATE TABLE IF NOT EXISTS attendance_rollout_audit" in ddl
    assert "event_kind TEXT NOT NULL" in ddl
    assert "blocker_codes JSONB" in ddl
    assert "employee_name" not in ddl.split(
        "CREATE TABLE IF NOT EXISTS attendance_rollout_audit", 1
    )[1].split(";", 1)[0]


def test_ready_report_uses_one_collected_snapshot(monkeypatch):
    calls = []
    monkeypatch.setattr(
        attendance_readiness,
        "_collect_inputs",
        lambda now_utc, production_client=None: calls.append((now_utc, production_client))
        or _inputs(),
    )

    report = attendance_readiness.build_report(NOW, production_client="meter")

    assert report.ready is True
    assert report.blockers == ()
    assert report.last_sweep_deletion_count == 2
    assert calls == [(NOW, "meter")]


def test_report_converts_source_failure_to_bounded_fail_closed_metrics(monkeypatch):
    monkeypatch.setattr(
        attendance_readiness,
        "_collect_inputs",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(TimeoutError("person name")),
    )

    report = attendance_readiness.build_report(NOW)

    assert report.ready is False
    assert "projection_incomplete" in report.blockers
    assert "person name" not in attendance_readiness.report_json(report)


@pytest.mark.parametrize(
    ("overrides", "blocker"),
    [
        ({"baseline_complete": False}, "baseline_incomplete"),
        ({"mirror_age_seconds": 90.001}, "mirror_stale"),
        ({"mirror_error": "timeout"}, "mirror_sync_failed"),
        ({"open_rows_not_refreshed": 1}, "open_rows_not_refreshed"),
        ({"last_full_sweep_age_seconds": 7200.001}, "full_sweep_stale"),
        (
            {"recalc_queue_age_seconds": 900.001, "recalc_queue_depth": 1},
            "recalculation_stuck",
        ),
        ({"open_conflicts": 1}, "unresolved_conflicts"),
        ({"failed_corrections": 1}, "failed_corrections"),
        ({"failed_department_repairs": 1}, "failed_department_repairs"),
        ({"projection_complete": False}, "projection_incomplete"),
    ],
)
def test_hard_readiness_blockers_are_fail_closed(monkeypatch, overrides, blocker):
    monkeypatch.setattr(
        attendance_readiness,
        "_collect_inputs",
        lambda *_args, **_kwargs: _inputs(**overrides),
    )

    report = attendance_readiness.build_report(NOW)

    assert report.ready is False
    assert blocker in report.blockers


def test_unmapped_and_missing_only_block_when_they_affect_production(monkeypatch):
    monkeypatch.setattr(
        attendance_readiness,
        "_collect_inputs",
        lambda *_args, **_kwargs: _inputs(
            open_unmapped=2,
            unmapped_minutes_today=18.0,
            open_missing_required=3,
            missing_minutes_today=22.0,
        ),
    )

    visible_only = attendance_readiness.build_report(NOW)

    assert visible_only.ready is True
    assert visible_only.open_unmapped == 2
    assert visible_only.open_missing_required == 3

    monkeypatch.setattr(
        attendance_readiness,
        "_collect_inputs",
        lambda *_args, **_kwargs: _inputs(
            open_unmapped=2,
            open_missing_required=3,
            unassigned_units_today=12.0,
            unmapped_affects_production=True,
            missing_affects_production=True,
        ),
    )

    affected = attendance_readiness.build_report(NOW)

    assert affected.ready is False
    assert affected.blockers[-2:] == (
        "unmapped_location_affects_production",
        "missing_location_affects_production",
    )


def test_report_digest_is_stable_and_contains_no_personal_fields(monkeypatch):
    monkeypatch.setattr(
        attendance_readiness,
        "_collect_inputs",
        lambda *_args, **_kwargs: _inputs(open_conflicts=1),
    )
    report = attendance_readiness.build_report(NOW)

    first = attendance_readiness.report_digest(report)
    second = attendance_readiness.report_digest(
        attendance_readiness.ReadinessReport(**asdict(report))
    )

    assert first == second
    assert len(first) == 64
    assert "employee" not in attendance_readiness.report_json(report).lower()


def test_report_digest_ignores_moving_ages_but_rejects_nonfinite_decision_values():
    first = attendance_readiness.ReadinessReport(
        True, 1.0, 2.0, 0, 0, 3.0, None, 0, 0, 4.0, 0, 5.0, 0, 6.0,
        0.0, None, 0.0, 0, 0, 0, 0, (),
    )
    later = attendance_readiness.ReadinessReport(
        True, 31.0, 32.0, 0, 0, 33.0, None, 0, 0, 34.0, 0, 35.0, 0, 36.0,
        0.0, None, 0.0, 0, 0, 0, 0, (),
    )

    assert attendance_readiness.report_digest(first) == attendance_readiness.report_digest(later)
    invalid = attendance_readiness.ReadinessReport(
        True, 1.0, 2.0, 0, 0, 3.0, None, 0, 0, 0.0, 0, 0.0, 0, 0.0,
        math.inf, None, 0.0, 0, 0, 0, 0, (),
    )
    with pytest.raises(ValueError, match="nonfinite_readiness_value"):
        attendance_readiness.report_digest(invalid)


def test_decision_snapshot_reads_one_source_digest_from_its_repeatable_snapshot(
    monkeypatch,
):
    ready = attendance_readiness.ReadinessReport(
        True, 1.0, 2.0, 0, 0, 0.0, None, 0, 0, 0.0, 0, 0.0, 0, 0.0,
        0.0, None, 0.0, 0, 0, 0, 0, (),
    )
    fingerprint_calls = []
    monkeypatch.setattr(
        attendance_readiness,
        "_source_fingerprints",
        lambda **kwargs: fingerprint_calls.append(kwargs)
        or ("local-a", "production-a"),
    )
    monkeypatch.setattr(
        attendance_readiness,
        "_collect_or_failed",
        lambda *_args, **_kwargs: _inputs(frozen_production_fingerprint="meter-a"),
    )
    monkeypatch.setattr(
        attendance_readiness, "_report_from_inputs", lambda *_args, **_kwargs: ready
    )
    monkeypatch.setattr(
        attendance_readiness.production_history,
        "_metered_leaderboard",
        lambda *_args, **_kwargs: (),
    )
    monkeypatch.setattr(attendance_readiness, "_saved_shadow_day", lambda: None)
    monkeypatch.setattr(
        attendance_readiness,
        "_frozen_local_sources",
        _source_fence,
        raising=False,
    )

    decision = attendance_readiness.build_decision_snapshot(NOW)

    assert decision.local_source_fingerprint == "local-a"
    assert decision.production_fingerprint == "production-a"
    assert len(fingerprint_calls) == 1


def test_decision_snapshot_binds_semantic_report_and_both_source_fingerprints(monkeypatch):
    ready = attendance_readiness.ReadinessReport(
        True, 1.0, 2.0, 0, 0, 0.0, None, 0, 0, 0.0, 0, 0.0, 0, 0.0,
        0.0, None, 0.0, 0, 0, 0, 0, (),
    )
    monkeypatch.setattr(
        attendance_readiness,
        "_source_fingerprints",
        lambda **_kwargs: ("local-a", "production-a"),
    )
    monkeypatch.setattr(
        attendance_readiness,
        "_collect_or_failed",
        lambda *_args, **_kwargs: _inputs(frozen_production_fingerprint="meter-a"),
    )
    monkeypatch.setattr(
        attendance_readiness, "_report_from_inputs", lambda *_args, **_kwargs: ready
    )
    monkeypatch.setattr(
        attendance_readiness.production_history,
        "_metered_leaderboard",
        lambda *_args, **_kwargs: (),
    )
    monkeypatch.setattr(attendance_readiness, "_saved_shadow_day", lambda: None)
    monkeypatch.setattr(
        attendance_readiness,
        "_frozen_local_sources",
        _source_fence,
        raising=False,
    )
    frozen_day = _frozen_day(NOW.astimezone(attendance_readiness.shift_config.SITE_TZ).date())
    monkeypatch.setattr(
        attendance_readiness,
        "_freeze_readiness_production_sources",
        lambda *_args, **_kwargs: ("meter", (), None, None, frozen_day, None),
    )
    monkeypatch.setattr(
        attendance_readiness,
        "_snapshot_production_day",
        lambda _day: frozen_day,
    )

    decision = attendance_readiness.build_decision_snapshot(NOW)

    assert decision.report is ready
    assert decision.report_digest == attendance_readiness.report_digest(ready)
    assert decision.local_source_fingerprint == "local-a"
    assert decision.production_fingerprint == "production-a"
    assert decision.frozen_production_fingerprint == "meter-a"
    assert decision.source_binding_digest == attendance_readiness._decision_binding_digest(
        decision.report_digest,
        "local-a",
        "production-a",
        "meter-a",
        checked_at=decision.checked_at,
        valid_until=decision.valid_until,
    )


def test_decision_snapshot_holds_one_local_source_fence_across_collection(monkeypatch):
    state = {"locked": False, "collected": False}
    cursor = object()

    @contextmanager
    def frozen_sources():
        state["locked"] = True
        try:
            yield cursor
        finally:
            state["locked"] = False

    def fingerprints(*, cur=None, **_kwargs):
        assert state["locked"] is True
        assert cur is cursor
        return ("local-a", "production-a")

    def collect(*_args, **_kwargs):
        assert state["locked"] is True
        state["collected"] = True
        return _inputs(frozen_production_fingerprint="meter-a")

    monkeypatch.setattr(
        attendance_readiness,
        "_frozen_local_sources",
        frozen_sources,
        raising=False,
    )
    monkeypatch.setattr(attendance_readiness, "_source_fingerprints", fingerprints)
    monkeypatch.setattr(attendance_readiness, "_collect_or_failed", collect)
    monkeypatch.setattr(
        attendance_readiness.production_history,
        "_metered_leaderboard",
        lambda *_args, **_kwargs: (),
    )
    monkeypatch.setattr(attendance_readiness, "_saved_shadow_day", lambda: None)
    frozen_day = _frozen_day(NOW.astimezone(attendance_readiness.shift_config.SITE_TZ).date())
    monkeypatch.setattr(
        attendance_readiness,
        "_freeze_readiness_production_sources",
        lambda *_args, **_kwargs: ("meter", (), None, None, frozen_day, None),
    )
    monkeypatch.setattr(
        attendance_readiness,
        "_snapshot_production_day",
        lambda _day: frozen_day,
    )

    attendance_readiness.build_decision_snapshot(NOW)

    assert state == {"locked": False, "collected": True}


def test_local_source_fingerprint_covers_shadow_proof_and_department_fallback():
    sql = attendance_readiness._SOURCE_FINGERPRINT_SQL
    strict_sql = production_history._STRICT_LOCAL_SOURCE_SQL
    assert "odoo_attendance_shadow_health" in sql
    assert "odoo_attendance_shadow_epoch" in sql
    assert "department_name" in strict_sql and "FROM people" in strict_sql
    assert "odoo_department_name" in strict_sql
    assert "odoo_department_name" in attendance_readiness._SHADOW_SOURCE_FINGERPRINT_SQL


def test_readiness_source_digest_is_day_scoped_and_semantic():
    sql = attendance_readiness._SOURCE_FINGERPRINT_SQL

    assert "jsonb_agg(to_jsonb(s)" not in sql
    assert "JOIN odoo_attendance_mirror" in sql
    assert "COUNT(*) FILTER" in sql
    assert "last_seen_at < sync.mirror_observed_at" in sql
    assert "WHERE completed_at IS NULL OR cache_ready_at IS NULL" in sql
    assert "SELECT DISTINCT ON (item_key)" in sql
    assert "FROM attendance_correction_job_events" in sql
    assert "WHERE created_at >= %s AND created_at < %s" in sql
    assert "FROM attendance_department_repairs WHERE status = 'failed'" in sql
    assert "WHERE p.day = %s" in sql


@pytest.mark.parametrize(
    ("overrides", "blocker"),
    [
        ({"unassigned_units_today": 0.001}, "unassigned_production"),
        ({"comparison_identity_available": False}, "comparison_identity_unavailable"),
        ({"shadow_day_complete": False}, "shadow_day_incomplete"),
    ],
)
def test_shadow_and_comparison_preconditions_fail_closed(monkeypatch, overrides, blocker):
    monkeypatch.setattr(
        attendance_readiness,
        "_collect_inputs",
        lambda *_args, **_kwargs: _inputs(**overrides),
    )

    report = attendance_readiness.build_report(NOW)

    assert report.ready is False
    assert blocker in report.blockers


def test_cutover_must_be_future_and_exact_local_boundary_including_dst(monkeypatch):
    monkeypatch.setattr(attendance_readiness.shift_config, "shift_start_for", lambda _day: datetime.min.time().replace(hour=6))
    monkeypatch.setattr(attendance_readiness.shift_config, "is_workday", lambda _day: True)
    monkeypatch.setattr(
        attendance_readiness.shift_config,
        "snapshot_for",
        lambda day, **_kwargs: SimpleNamespace(
            day=day,
            shift_start=datetime.min.time().replace(hour=6),
            shift_end=datetime.min.time().replace(hour=15),
            breaks=(),
            is_workday=True,
        ),
    )
    future_boundary = datetime(2026, 11, 2, 6, 0, tzinfo=attendance_readiness.shift_config.SITE_TZ)

    assert attendance_readiness.validate_cutover(future_boundary, now_utc=NOW) == future_boundary
    with pytest.raises(ValueError, match="cutover_boundary_required"):
        attendance_readiness.validate_cutover(
            future_boundary + timedelta(minutes=1), now_utc=NOW
        )
    with pytest.raises(ValueError, match="cutover_future_required"):
        attendance_readiness.validate_cutover(
            datetime(2026, 8, 31, 6, 0, tzinfo=attendance_readiness.shift_config.SITE_TZ),
            now_utc=NOW,
        )


def test_ready_cutover_scheduling_uses_fresh_same_request_report(monkeypatch):
    cutover = datetime(2026, 9, 2, 6, 0, tzinfo=attendance_readiness.shift_config.SITE_TZ)
    monkeypatch.setattr(attendance_readiness, "validate_cutover", lambda value, now_utc: value)
    report = attendance_readiness.ReadinessReport(
        **{
            key: value
            for key, value in asdict(attendance_readiness.build_report.__annotations__.get("return", None)).items()
        }
    ) if False else attendance_readiness.ReadinessReport(
        True, 1.0, 2.0, 0, 0, 0.0, None, 0, 0, 0.0, 0, 0.0, 0, 0.0,
        0.0, None, 0.0, 0, 0, 0, 0, (),
    )
    calls = []
    previous = attendance_location_policy.RolloutConfig("shadow", None, None)
    monkeypatch.setattr(
        attendance_readiness.attendance_location_policy,
        "get_rollout_config_strict",
        lambda: previous,
    )
    monkeypatch.setattr(
        attendance_readiness,
        "build_decision_snapshot",
        lambda now_utc, *, cutover_at=None, production_client=None: calls.append(
            (now_utc, cutover_at, production_client)
        )
        or _decision(report),
    )
    monkeypatch.setattr(
        attendance_readiness,
        "_store_pending_cutover",
        lambda config, **kwargs: calls.append((config, kwargs)),
    )

    config = attendance_readiness.schedule_live_cutover(
        cutover,
        now_utc=NOW,
        production_client="meter",
    )

    assert config.mode == "live"
    assert config.live_gate == attendance_location_policy.LiveGate(
        checked_at=NOW,
        report_digest=attendance_readiness.report_digest(report),
        activated_at=None,
    )
    assert calls[0] == (NOW, cutover, "meter")
    assert calls[1] == (
        config,
        {"checked_at": NOW, "expected_config": previous, "decision": _decision(report)},
    )


def test_active_live_cannot_be_replaced_by_a_new_pending_live_schedule(monkeypatch):
    cutover = datetime(2026, 9, 2, 6, 0, tzinfo=attendance_readiness.shift_config.SITE_TZ)
    active = attendance_location_policy.RolloutConfig(
        "live",
        datetime(2026, 9, 1, 6, 0, tzinfo=attendance_readiness.shift_config.SITE_TZ),
        attendance_location_policy.LiveGate(NOW, "active-proof", NOW),
    )
    monkeypatch.setattr(attendance_readiness, "validate_cutover", lambda value, now_utc: value)
    monkeypatch.setattr(
        attendance_readiness.attendance_location_policy,
        "get_rollout_config_strict",
        lambda: active,
    )
    monkeypatch.setattr(
        attendance_readiness,
        "build_decision_snapshot",
        lambda *_args, **_kwargs: pytest.fail("active Live cannot build a new schedule"),
    )

    with pytest.raises(ValueError, match="live_already_active"):
        attendance_readiness.schedule_live_cutover(cutover, now_utc=NOW)


def test_live_schedule_rejects_blocked_or_slow_readiness(monkeypatch):
    cutover = datetime(2026, 9, 2, 6, 0, tzinfo=attendance_readiness.shift_config.SITE_TZ)
    blocked = attendance_readiness.ReadinessReport(
        False, 91.0, 2.0, 0, 0, 0.0, None, 0, 0, 0.0, 0, 0.0, 0, 0.0,
        0.0, None, 0.0, 0, 0, 0, 0, ("mirror_stale",),
    )
    monkeypatch.setattr(attendance_readiness, "validate_cutover", lambda value, now_utc: value)
    monkeypatch.setattr(
        attendance_readiness.attendance_location_policy,
        "get_rollout_config_strict",
        lambda: attendance_location_policy.RolloutConfig("shadow", None, None),
    )
    monkeypatch.setattr(
        attendance_readiness,
        "build_decision_snapshot",
        lambda *_args, **_kwargs: _decision(blocked),
    )

    with pytest.raises(ValueError, match="live_readiness_blocked:mirror_stale"):
        attendance_readiness.schedule_live_cutover(cutover, now_utc=NOW)

    monkeypatch.setattr(attendance_readiness, "build_decision_snapshot", lambda *_args, **_kwargs: _decision(attendance_readiness.ReadinessReport(
        True, 1.0, 2.0, 0, 0, 0.0, None, 0, 0, 0.0, 0, 0.0, 0, 0.0,
        0.0, None, 0.0, 0, 0, 0, 0, (),
    )))
    monkeypatch.setattr(attendance_readiness, "_utc_now", lambda: NOW + timedelta(minutes=5, microseconds=1))
    with pytest.raises(ValueError, match="live_readiness_expired"):
        attendance_readiness.schedule_live_cutover(cutover, now_utc=NOW)


def test_schedule_rejects_when_mirror_threshold_crosses_before_db_acceptance(
    monkeypatch,
):
    report = attendance_readiness.ReadinessReport(
        True, 89.0, 2.0, 0, 0, 0.0, None, 0, 0, 0.0, 0, 0.0, 0, 0.0,
        0.0, None, 0.0, 0, 0, 0, 0, (),
    )
    decision = _bound_decision(report, valid_for=timedelta(seconds=1))
    config = attendance_location_policy.RolloutConfig(
        "live",
        NOW + timedelta(days=1),
        attendance_location_policy.LiveGate(NOW, decision.report_digest, None),
    )
    cursor = _AtomicCursor(_raw_config(config), accepted_at=NOW + timedelta(seconds=2))
    monkeypatch.setattr(attendance_readiness.db, "cursor", lambda: _CursorContext(cursor))
    monkeypatch.setattr(attendance_readiness, "_lock_readiness_sources_cur", lambda _cur: None)
    monkeypatch.setattr(
        attendance_readiness,
        "_source_fingerprints",
        lambda **_kwargs: ("local-source", "production-source"),
    )
    monkeypatch.setattr(
        attendance_readiness.attendance_location_policy,
        "set_rollout_config",
        lambda *_args, **_kwargs: pytest.fail("expired decision was scheduled"),
    )

    with pytest.raises(ValueError, match="^live_readiness_expired$"):
        attendance_readiness._store_pending_cutover(
            config,
            checked_at=NOW,
            decision=decision,
        )


def test_activation_rejects_when_threshold_crosses_during_boundary_recheck(monkeypatch):
    cutover = NOW - timedelta(seconds=1)
    pending = attendance_location_policy.RolloutConfig(
        "live",
        cutover,
        attendance_location_policy.LiveGate(NOW - timedelta(minutes=1), "old", None),
    )
    report = attendance_readiness.ReadinessReport(
        True, 89.0, 2.0, 0, 0, 0.0, None, 0, 0, 0.0, 0, 0.0, 0, 0.0,
        0.0, None, 0.0, 0, 0, 0, 0, (),
    )
    decision = _bound_decision(report, valid_for=timedelta(seconds=1))
    cursor = _AtomicCursor(
        _raw_config(pending),
        accepted_at=NOW + timedelta(seconds=2),
    )
    monkeypatch.setattr(attendance_readiness.db, "cursor", lambda: _CursorContext(cursor))
    monkeypatch.setattr(attendance_readiness, "_lock_readiness_sources_cur", lambda _cur: None)
    monkeypatch.setattr(
        attendance_readiness,
        "_source_fingerprints",
        lambda **_kwargs: ("local-source", "production-source"),
    )
    monkeypatch.setattr(
        attendance_readiness.attendance_location_policy,
        "set_rollout_config",
        lambda *_args, **_kwargs: pytest.fail("expired decision activated"),
    )

    assert attendance_readiness._settle_due_cutover(
        expected_config=pending,
        report=report,
        decision=decision,
        now_utc=NOW,
    ) == "superseded"


@pytest.mark.parametrize("lateness", [timedelta(hours=3), timedelta(days=1)])
def test_missed_activation_boundary_returns_to_shadow_without_strict_write(
    monkeypatch,
    lateness,
):
    cutover = NOW - lateness
    pending = attendance_location_policy.RolloutConfig(
        "live",
        cutover,
        attendance_location_policy.LiveGate(cutover - timedelta(minutes=1), "old", None),
    )
    report = attendance_readiness.ReadinessReport(
        True, 1.0, 2.0, 0, 0, 0.0, None, 0, 0, 0.0, 0, 0.0, 0, 0.0,
        0.0, None, 0.0, 0, 0, 0, 0, (),
    )
    cursor = _AtomicCursor(_raw_config(pending))
    saved = []
    alerts = []
    monkeypatch.setattr(attendance_readiness.db, "cursor", lambda: _CursorContext(cursor))
    monkeypatch.setattr(
        attendance_readiness.attendance_location_policy,
        "set_rollout_config",
        lambda config, *, cur: saved.append(config),
    )
    monkeypatch.setattr(
        attendance_readiness.app_settings,
        "set_setting",
        lambda key, value, *, cur: alerts.append((key, value)),
    )

    assert attendance_readiness._settle_due_cutover(
        expected_config=pending,
        report=report,
        now_utc=NOW,
    ) == "rolled_back"
    assert saved == [attendance_location_policy.RolloutConfig("shadow", None, None)]
    assert "cutover_boundary_missed" in alerts[0][1]["blockers"]
    assert not any(
        "INSERT INTO attendance_strict_days" in sql for sql, _params in cursor.statements
    )


def test_activation_success_and_failure_delegate_one_atomic_decision(monkeypatch):
    cutover = NOW - timedelta(seconds=1)
    pending = attendance_location_policy.RolloutConfig(
        "live",
        cutover,
        attendance_location_policy.LiveGate(NOW - timedelta(minutes=1), "abc", None),
    )
    ready = attendance_readiness.ReadinessReport(
        True, 1.0, 2.0, 0, 0, 0.0, None, 0, 0, 0.0, 0, 0.0, 0, 0.0,
        0.0, None, 0.0, 0, 0, 0, 0, (),
    )
    calls = []
    @contextmanager
    def claimed():
        yield True

    monkeypatch.setattr(attendance_readiness, "_activation_claim", claimed)
    monkeypatch.setattr(
        attendance_readiness.attendance_location_policy,
        "get_rollout_config_strict",
        lambda: pending,
    )
    monkeypatch.setattr(
        attendance_readiness,
        "build_decision_snapshot",
        lambda *_args, **_kwargs: _decision(ready),
    )
    monkeypatch.setattr(attendance_readiness, "_settle_due_cutover", lambda **kwargs: calls.append(kwargs) or "activated")

    assert attendance_readiness.activate_due_cutover(NOW, production_client="meter") == "activated"
    assert calls == [{
        "expected_config": pending,
        "report": ready,
        "decision": _decision(ready),
        "now_utc": NOW,
    }]

    blocked = SimpleNamespace(ready=False, blockers=("mirror_stale",))
    monkeypatch.setattr(
        attendance_readiness,
        "build_decision_snapshot",
        lambda *_args, **_kwargs: attendance_readiness.DecisionSnapshot(
            blocked, "blocked", "local-source", "production-source"
        ),
    )
    assert attendance_readiness.activate_due_cutover(NOW) == "activated"
    assert calls[-1]["report"] is blocked


def test_not_due_or_already_activated_cutover_does_nothing(monkeypatch):
    @contextmanager
    def claimed():
        yield True

    monkeypatch.setattr(attendance_readiness, "_activation_claim", claimed)
    for config in (
        attendance_location_policy.RolloutConfig("shadow", None, None),
        attendance_location_policy.RolloutConfig(
            "live",
            NOW + timedelta(hours=1),
            attendance_location_policy.LiveGate(NOW, "abc", None),
        ),
        attendance_location_policy.RolloutConfig(
            "live",
            NOW - timedelta(hours=1),
            attendance_location_policy.LiveGate(NOW, "abc", NOW),
        ),
    ):
        monkeypatch.setattr(
            attendance_readiness.attendance_location_policy,
            "get_rollout_config_strict",
            lambda config=config: config,
        )
        monkeypatch.setattr(
            attendance_readiness,
            "build_decision_snapshot",
            lambda *_args, **_kwargs: pytest.fail("not-due cutover must not recheck"),
        )
        assert attendance_readiness.activate_due_cutover(NOW) == "not_due"


def _issue(
    kind,
    *,
    start=NOW - timedelta(minutes=10),
    end=NOW,
    units=None,
    wc=None,
):
    return SimpleNamespace(
        kind=kind,
        start_utc=start,
        end_utc=end,
        units=units,
        app_work_center_name=wc,
    )


def test_issue_aggregation_counts_minutes_units_age_and_output_sensitive_overlap():
    snapshot = SimpleNamespace(
        complete=True,
        issues=(
            _issue("attendance_conflicting_location"),
            _issue("attendance_unmapped_location", start=NOW - timedelta(minutes=8)),
            _issue("attendance_missing_location", start=NOW - timedelta(minutes=6)),
            _issue(
                "production_unassigned_run",
                start=NOW - timedelta(minutes=7),
                end=NOW - timedelta(minutes=2),
                units=12.5,
                wc="Repair 1",
            ),
        ),
        source_errors=(),
    )

    metrics = attendance_readiness._issue_metrics(snapshot, now_utc=NOW)

    assert metrics == attendance_readiness._IssueMetrics(
        projection_complete=True,
        open_conflicts=1,
        conflict_minutes_today=10.0,
        open_unmapped=1,
        unmapped_minutes_today=8.0,
        open_missing_required=1,
        missing_minutes_today=6.0,
        unassigned_units_today=12.5,
        oldest_unassigned_age_seconds=420.0,
        unmapped_affects_production=True,
        missing_affects_production=True,
    )


def test_incomplete_issue_snapshot_never_claims_projection_complete():
    metrics = attendance_readiness._issue_metrics(
        SimpleNamespace(complete=False, issues=(), source_errors=("Production",)),
        now_utc=NOW,
    )
    assert metrics.projection_complete is False


def test_shadow_comparison_uses_canonical_ids_and_keeps_same_names_separate(monkeypatch):
    strict = {
        (101, "Alex"): {"Repair 1": {"units": 4.0}},
        (102, "Alex"): {"Repair 1": {"units": 6.0}},
    }
    monkeypatch.setattr(
        attendance_readiness.production_history,
        "_strict_attribution_for",
        lambda day, client, now_utc: strict,
    )
    query = MagicMock(
        return_value=[
            {"emp_id": "101", "wc_name": "Repair 1", "units": 10.0},
            {"emp_id": "102", "wc_name": "Repair 1", "units": 0.0},
        ]
    )
    monkeypatch.setattr(attendance_readiness.db, "query", query)

    result = attendance_readiness.compute_shadow_comparison(
        date(2026, 9, 1),
        "meter",
        now_utc=NOW,
    )

    assert result.complete is True
    assert result.changed_worker_units == 6.0
    assert result.comparison_keys == 2
    assert result.strict_worker_units == 10.0
    assert result.current_worker_units == 10.0
    assert "Alex" not in attendance_readiness.shadow_comparison_json(result)
    assert query.call_count == 1


def test_shadow_comparison_fails_closed_on_noncanonical_current_identity(monkeypatch):
    monkeypatch.setattr(
        attendance_readiness.production_history,
        "_strict_attribution_for",
        lambda *_args, **_kwargs: {(101, "Alex"): {"Repair 1": {"units": 4.0}}},
    )
    monkeypatch.setattr(
        attendance_readiness.db,
        "query",
        lambda *_args, **_kwargs: [
            {"emp_id": "Alex", "wc_name": "Repair 1", "units": 4.0}
        ],
    )

    result = attendance_readiness.compute_shadow_comparison(
        date(2026, 9, 1), "meter", now_utc=NOW
    )

    assert result.complete is False
    assert result.error == "noncanonical_current_employee_id"


def test_shadow_refresh_persists_aggregate_only_and_never_production(monkeypatch):
    comparison = attendance_readiness.ShadowComparison(
        day=date(2026, 9, 1),
        checked_at=NOW,
        complete=True,
        changed_worker_units=6.0,
        comparison_keys=2,
        strict_worker_units=10.0,
        current_worker_units=10.0,
        error=None,
    )
    config = SimpleNamespace(mode="shadow")
    monkeypatch.setattr(
        attendance_readiness.attendance_location_policy,
        "get_rollout_config",
        lambda: config,
    )
    monkeypatch.setattr(
        attendance_readiness.attendance_location_policy,
        "get_rollout_config_cur",
        lambda _cur: config,
    )
    monkeypatch.setattr(
        attendance_readiness.attendance_location_policy,
        "lock_rollout_decision_cur",
        lambda _cur: None,
    )
    monkeypatch.setattr(
        attendance_readiness,
        "compute_shadow_comparison",
        lambda *_args, **_kwargs: comparison,
    )
    monkeypatch.setattr(attendance_readiness.db, "read_snapshot", _source_fence)
    monkeypatch.setattr(attendance_readiness.db, "cursor", _source_fence)
    monkeypatch.setattr(
        attendance_readiness,
        "_snapshot_production_day",
        lambda day: _frozen_day(day),
    )
    monkeypatch.setattr(
        attendance_readiness,
        "_lock_production_config_sources_cur",
        lambda _cur: None,
    )
    monkeypatch.setattr(
        attendance_readiness,
        "_shadow_epoch_entered_at_cur",
        lambda _cur: NOW - timedelta(days=2),
    )
    monkeypatch.setattr(
        attendance_readiness,
        "_snapshot_work_center_mapper",
        lambda: lambda _odoo_id: None,
    )
    monkeypatch.setattr(
        attendance_readiness,
        "_freeze_leaderboard_rows",
        lambda *_args, **_kwargs: ("meter", ()),
    )
    monkeypatch.setattr(
        attendance_readiness,
        "_shadow_source_fingerprint",
        lambda *_args, **_kwargs: "shadow-source-a",
    )
    mirror_at = NOW - timedelta(seconds=15)
    monkeypatch.setattr(
        attendance_readiness.attendance_mirror,
        "health_snapshot",
        lambda: SimpleNamespace(
            last_incremental_completed_at=NOW,
            last_incremental_observed_at=mirror_at,
            baseline_completed_at=None,
        ),
    )
    monkeypatch.setattr(
        attendance_readiness.shift_config,
        "shift_start_for",
        lambda _day: datetime.min.time().replace(hour=7),
    )
    monkeypatch.setattr(
        attendance_readiness.shift_config,
        "shift_end_for",
        lambda _day: datetime.min.time().replace(hour=15),
    )
    writes = []
    monkeypatch.setattr(
        attendance_readiness.app_settings,
        "set_setting",
        lambda key, value, **_kwargs: writes.append((key, value)),
    )
    monkeypatch.setattr(
        precompute,
        "upsert_production_daily",
        lambda *_args, **_kwargs: pytest.fail("shadow comparison cannot write production"),
    )

    result = attendance_readiness.refresh_shadow_comparison(
        date(2026, 9, 1),
        "meter",
        now_utc=NOW,
        shadow_entered_at=NOW - timedelta(days=2),
    )
    assert result.source_mirror_at == mirror_at
    assert writes == [
        (
            "odoo_attendance_shadow_health",
            {
                "schema_version": 1,
                "day": "2026-09-01",
                "checked_at": NOW.isoformat(),
                "shadow_entered_at": (NOW - timedelta(days=2)).isoformat(),
                "shift_start_utc": datetime(
                    2026, 9, 1, 12, tzinfo=UTC
                ).isoformat(),
                "shift_end_utc": datetime(
                    2026, 9, 1, 20, tzinfo=UTC
                ).isoformat(),
                "source_binding": attendance_readiness._combined_shadow_source_binding(
                    "shadow-source-a",
                    attendance_readiness._leaderboard_rows_fingerprint(
                        date(2026, 9, 1), ()
                    ),
                ),
                "production_source_fingerprint": attendance_readiness._leaderboard_rows_fingerprint(
                    date(2026, 9, 1), ()
                ),
                "complete": True,
                "projection_complete": True,
                "source_mirror_at": mirror_at.isoformat(),
                "changed_worker_units": 6.0,
                "comparison_keys": 2,
                "strict_worker_units": 10.0,
                "current_worker_units": 10.0,
                "error": None,
            },
        )
    ]


def test_shadow_refresh_never_labels_old_comparison_with_later_mirror_observation(
    monkeypatch,
):
    old_observed = datetime(2026, 9, 1, 19, 59, 59, tzinfo=UTC)
    new_observed = datetime(2026, 9, 1, 20, 0, 1, tzinfo=UTC)
    observed = {"at": old_observed}
    comparison = attendance_readiness.ShadowComparison(
        day=date(2026, 9, 1),
        checked_at=NOW,
        complete=True,
        changed_worker_units=0.0,
        comparison_keys=1,
        strict_worker_units=1.0,
        current_worker_units=1.0,
        error=None,
    )
    config = SimpleNamespace(mode="shadow")
    monkeypatch.setattr(
        attendance_readiness.attendance_location_policy,
        "get_rollout_config",
        lambda: config,
    )
    monkeypatch.setattr(
        attendance_readiness.attendance_location_policy,
        "get_rollout_config_cur",
        lambda _cur: config,
    )
    monkeypatch.setattr(
        attendance_readiness.attendance_location_policy,
        "lock_rollout_decision_cur",
        lambda _cur: None,
    )

    def compare(*_args, **_kwargs):
        observed["at"] = new_observed
        return comparison

    monkeypatch.setattr(attendance_readiness, "compute_shadow_comparison", compare)
    monkeypatch.setattr(attendance_readiness.db, "read_snapshot", _source_fence)
    monkeypatch.setattr(attendance_readiness.db, "cursor", _source_fence)
    monkeypatch.setattr(
        attendance_readiness,
        "_snapshot_production_day",
        lambda day: _frozen_day(day),
    )
    monkeypatch.setattr(
        attendance_readiness,
        "_lock_production_config_sources_cur",
        lambda _cur: None,
    )
    monkeypatch.setattr(
        attendance_readiness,
        "_shadow_epoch_entered_at_cur",
        lambda _cur: NOW - timedelta(days=1),
    )
    monkeypatch.setattr(
        attendance_readiness,
        "_snapshot_work_center_mapper",
        lambda: lambda _odoo_id: None,
    )
    monkeypatch.setattr(
        attendance_readiness,
        "_freeze_leaderboard_rows",
        lambda *_args, **_kwargs: ("meter", ()),
    )
    monkeypatch.setattr(
        attendance_readiness,
        "_shadow_source_fingerprint",
        lambda *_args, **_kwargs: "shadow-source-a",
    )
    monkeypatch.setattr(
        attendance_readiness.attendance_mirror,
        "health_snapshot",
        lambda: SimpleNamespace(
            last_incremental_completed_at=observed["at"],
            last_incremental_observed_at=observed["at"],
            baseline_completed_at=None,
        ),
    )
    monkeypatch.setattr(
        attendance_readiness.shift_config,
        "shift_start_for",
        lambda _day: datetime.min.time().replace(hour=7),
    )
    monkeypatch.setattr(
        attendance_readiness.shift_config,
        "shift_end_for",
        lambda _day: datetime.min.time().replace(hour=15),
    )
    writes = []
    monkeypatch.setattr(
        attendance_readiness.app_settings,
        "set_setting",
        lambda _key, value, **_kwargs: writes.append(value),
    )

    result = attendance_readiness.refresh_shadow_comparison(
        date(2026, 9, 1),
        "meter",
        now_utc=NOW,
        shadow_entered_at=NOW - timedelta(days=1),
    )

    assert result.source_mirror_at == old_observed
    assert writes[0]["source_mirror_at"] == old_observed.isoformat()


def test_off_and_live_modes_do_not_write_shadow_health(monkeypatch):
    for mode in ("off", "live"):
        monkeypatch.setattr(
            attendance_readiness.attendance_location_policy,
            "get_rollout_config",
            lambda mode=mode: SimpleNamespace(mode=mode),
        )
        monkeypatch.setattr(
            attendance_readiness,
            "compute_shadow_comparison",
            lambda *_args, **_kwargs: pytest.fail("wrong rollout mode"),
        )
        assert (
            attendance_readiness.refresh_shadow_comparison(
                date(2026, 9, 1), "meter", now_utc=NOW
            )
            is None
        )


def test_collect_db_metrics_uses_one_statement_for_coherent_health(monkeypatch):
    row = {
        "baseline_complete": True,
        "mirror_error": None,
        "mirror_completed_at": NOW - timedelta(seconds=15),
        "mirror_observed_at": NOW - timedelta(seconds=16),
        "full_sweep_completed_at": NOW - timedelta(minutes=10),
        "open_rows_not_refreshed": 0,
        "last_sweep_deletion_count": 3,
        "oldest_recalc_requested_at": None,
        "recalc_queue_depth": 0,
        "failed_corrections": 0,
        "correction_retries_today": 2,
        "correction_verification_failures_today": 1,
        "failed_department_repairs": 0,
            "shadow_health": {
            "schema_version": 1,
            "day": "2026-08-31",
            "checked_at": NOW.isoformat(),
            "shadow_entered_at": (NOW - timedelta(days=4)).isoformat(),
            "shift_start_utc": datetime(2026, 8, 31, 12, tzinfo=UTC).isoformat(),
            "shift_end_utc": datetime(2026, 8, 31, 20, tzinfo=UTC).isoformat(),
            "projection_complete": True,
            "source_mirror_at": (NOW - timedelta(seconds=20)).isoformat(),
                "source_binding": attendance_readiness._combined_shadow_source_binding(
                    "shadow-source-a", "meter-source-a"
                ),
                "production_source_fingerprint": "meter-source-a",
        },
        "shadow_epoch": {
            "schema_version": 1,
            "entered_at": (NOW - timedelta(days=4)).isoformat(),
        },
    }
    query = MagicMock(return_value=[row])
    monkeypatch.setattr(attendance_readiness.db, "query", query)

    metrics = attendance_readiness._collect_db_metrics(NOW)

    assert metrics["mirror_age_seconds"] == 15.0
    assert metrics["last_full_sweep_age_seconds"] == 600.0
    assert metrics["correction_retries_today"] == 2
    assert metrics["shadow_day_complete"] is True
    assert metrics["projection_lag_seconds"] == 5.0
    assert metrics["correction_job_ids"] == ()
    assert query.call_count == 1
    assert "WITH sync AS" in query.call_args.args[0]
    assert "array_agg(DISTINCT correction_job_id" in query.call_args.args[0]


def test_missing_or_same_day_shadow_proof_fails_the_complete_day_precondition(monkeypatch):
    base = {
        "baseline_complete": True,
        "mirror_error": None,
        "mirror_completed_at": NOW,
        "mirror_observed_at": NOW,
        "full_sweep_completed_at": NOW,
        "open_rows_not_refreshed": 0,
        "last_sweep_deletion_count": 0,
        "oldest_recalc_requested_at": None,
        "recalc_queue_depth": 0,
        "failed_corrections": 0,
        "correction_retries_today": 0,
        "correction_verification_failures_today": 0,
        "failed_department_repairs": 0,
    }
    query = MagicMock(
        side_effect=[
            [{**base, "shadow_health": None}],
            [{
                **base,
                "shadow_health": {
                    "day": "2026-09-01",
                    "projection_complete": True,
                    "source_mirror_at": NOW.isoformat(),
                },
            }],
        ]
    )
    monkeypatch.setattr(attendance_readiness.db, "query", query)

    assert attendance_readiness._collect_db_metrics(NOW)["shadow_day_complete"] is False
    assert attendance_readiness._collect_db_metrics(NOW)["shadow_day_complete"] is False


def test_shadow_proof_requires_the_same_epoch_from_before_the_observed_shift(monkeypatch):
    shadow_day = date(2026, 8, 31)
    shift_start = datetime.combine(
        shadow_day,
        datetime.min.time().replace(hour=7),
        tzinfo=attendance_readiness.shift_config.SITE_TZ,
    ).astimezone(UTC)
    base = {
        "baseline_complete": True,
        "mirror_error": None,
        "mirror_completed_at": NOW,
        "mirror_observed_at": NOW,
        "full_sweep_completed_at": NOW,
        "open_rows_not_refreshed": 0,
        "last_sweep_deletion_count": 0,
        "oldest_recalc_requested_at": None,
        "recalc_queue_depth": 0,
        "failed_corrections": 0,
        "correction_retries_today": 0,
        "correction_verification_failures_today": 0,
        "failed_department_repairs": 0,
    }
    old_epoch = (shift_start - timedelta(days=3)).isoformat()
    query = MagicMock(
        side_effect=[
            [{
                **base,
                "shadow_epoch": {"schema_version": 1, "entered_at": old_epoch},
                "shadow_health": {
                    "day": shadow_day.isoformat(),
                    "checked_at": NOW.isoformat(),
                    "projection_complete": True,
                    "source_mirror_at": NOW.isoformat(),
                    "shadow_entered_at": (shift_start + timedelta(minutes=1)).isoformat(),
                    "shift_start_utc": shift_start.isoformat(),
                    "shift_end_utc": (shift_start + timedelta(hours=8)).isoformat(),
                },
            }],
            [{
                **base,
                "shadow_epoch": {
                    "schema_version": 1,
                    "entered_at": (shift_start + timedelta(minutes=1)).isoformat(),
                },
                "shadow_health": {
                    "day": shadow_day.isoformat(),
                    "checked_at": NOW.isoformat(),
                    "projection_complete": True,
                    "source_mirror_at": NOW.isoformat(),
                    "shadow_entered_at": (shift_start + timedelta(minutes=1)).isoformat(),
                    "shift_start_utc": shift_start.isoformat(),
                    "shift_end_utc": (shift_start + timedelta(hours=8)).isoformat(),
                },
            }],
        ]
    )
    monkeypatch.setattr(attendance_readiness.db, "query", query)
    monkeypatch.setattr(
        attendance_readiness.shift_config,
        "shift_start_for",
        lambda _day: datetime.min.time().replace(hour=7),
    )
    monkeypatch.setattr(
        attendance_readiness.shift_config,
        "shift_end_for",
        lambda _day: datetime.min.time().replace(hour=15),
    )

    assert attendance_readiness._collect_db_metrics(NOW)["shadow_day_complete"] is False
    assert attendance_readiness._collect_db_metrics(NOW)["shadow_day_complete"] is False


def test_shadow_proof_requires_source_observation_through_shift_end(monkeypatch):
    shadow_day = date(2026, 8, 31)
    shift_start = datetime(2026, 8, 31, 12, tzinfo=UTC)
    shift_end = datetime(2026, 8, 31, 20, tzinfo=UTC)
    entered_at = shift_start - timedelta(days=1)
    row = {
        "baseline_complete": True,
        "mirror_error": None,
        "mirror_completed_at": NOW,
        "mirror_observed_at": shift_end - timedelta(seconds=1),
        "full_sweep_completed_at": NOW,
        "open_rows_not_refreshed": 0,
        "last_sweep_deletion_count": 0,
        "oldest_recalc_requested_at": None,
        "recalc_queue_depth": 0,
        "failed_corrections": 0,
        "correction_retries_today": 0,
        "correction_verification_failures_today": 0,
        "failed_department_repairs": 0,
        "shadow_epoch": {"schema_version": 1, "entered_at": entered_at.isoformat()},
        "shadow_health": {
            "schema_version": 1,
            "day": shadow_day.isoformat(),
            "checked_at": NOW.isoformat(),
            "shadow_entered_at": entered_at.isoformat(),
            "shift_start_utc": shift_start.isoformat(),
            "shift_end_utc": shift_end.isoformat(),
            "projection_complete": True,
            "source_mirror_at": (shift_end - timedelta(seconds=1)).isoformat(),
        },
    }
    monkeypatch.setattr(attendance_readiness.db, "query", MagicMock(return_value=[row]))

    assert attendance_readiness._collect_db_metrics(NOW)["shadow_day_complete"] is False


class _AtomicCursor:
    def __init__(self, raw_config, *, accepted_at=NOW):
        self.raw_config = raw_config
        self.accepted_at = accepted_at
        self.statements = []
        self._row = None

    def execute(self, sql, params=None):
        normalized = " ".join(sql.split())
        self.statements.append((normalized, params))
        if normalized.startswith("SELECT value FROM app_settings"):
            self._row = {"value": self.raw_config}
        elif normalized.startswith("SELECT clock_timestamp() AS accepted_at"):
            self._row = {"accepted_at": self.accepted_at}
        else:
            self._row = None

    def fetchone(self):
        return self._row


class _CursorContext:
    def __init__(self, cursor):
        self.cursor = cursor

    def __enter__(self):
        return self.cursor

    def __exit__(self, *_args):
        return False


def _raw_config(config):
    return {
        "mode": config.mode,
        "cutover_at": config.cutover_at.isoformat() if config.cutover_at else None,
        "live_gate": (
            {
                "checked_at": config.live_gate.checked_at.isoformat(),
                "report_digest": config.live_gate.report_digest,
                "activated_at": (
                    config.live_gate.activated_at.isoformat()
                    if config.live_gate.activated_at
                    else None
                ),
            }
            if config.live_gate
            else None
        ),
    }


def test_ready_settlement_uses_precompute_lock_order_and_enqueues_strict_day(monkeypatch):
    cutover = datetime(2026, 9, 1, 12, tzinfo=UTC)
    settle_now = cutover + timedelta(seconds=1)
    pending = attendance_location_policy.RolloutConfig(
        "live",
        cutover,
        attendance_location_policy.LiveGate(settle_now - timedelta(minutes=1), "old", None),
    )
    cursor = _AtomicCursor(_raw_config(pending))
    monkeypatch.setattr(attendance_readiness.db, "cursor", lambda: _CursorContext(cursor))
    saved = []
    monkeypatch.setattr(
        attendance_readiness.attendance_location_policy,
        "set_rollout_config",
        lambda config, *, cur: saved.append(config),
    )
    report = attendance_readiness.ReadinessReport(
        True, 1.0, 2.0, 0, 0, 0.0, None, 0, 0, 0.0, 0, 0.0, 0, 0.0,
        0.0, None, 0.0, 0, 0, 0, 0, (),
    )

    result = attendance_readiness._settle_due_cutover(
        expected_config=pending,
        report=report,
        now_utc=settle_now,
    )

    assert result == "activated"
    assert cursor.statements[0][0] == (
        "LOCK TABLE app_settings, attendance_strict_days IN SHARE ROW EXCLUSIVE MODE"
    )
    assert saved[0].live_gate.activated_at == settle_now
    assert saved[0].live_gate.report_digest == attendance_readiness.report_digest(report)
    sql = "\n".join(statement for statement, _params in cursor.statements)
    assert "INSERT INTO attendance_strict_days" in sql
    assert "INSERT INTO attendance_recalc_queue" in sql
    assert "INSERT INTO attendance_rollout_audit" in sql
    assert "DELETE FROM app_settings" in sql
    assert any(
        params and params[0] == "odoo_attendance_readiness_report"
        for statement, params in cursor.statements
        if "INSERT INTO app_settings" in statement
    )


def test_structured_readiness_evidence_logs_only_bounded_identifiers(caplog):
    segments = [
        attendance_timeline.LocationSpan(
            employee_odoo_id=101,
            employee_name="Worker Secret",
            start_utc=NOW,
            end_utc=NOW + timedelta(minutes=5),
            status="unmapped_location",
            app_work_center_name=None,
            odoo_work_center_id=71,
            odoo_work_center_name="Raw Secret",
            attendance_ids=tuple(range(1, 130)),
            department_repair=None,
        ),
        attendance_timeline.LocationSpan(
            employee_odoo_id=101,
            employee_name="Worker Secret",
            start_utc=NOW + timedelta(minutes=5),
            end_utc=NOW + timedelta(minutes=10),
            status="missing_required_location",
            app_work_center_name=None,
            odoo_work_center_id=None,
            odoo_work_center_name=None,
            attendance_ids=(130,),
            department_repair=None,
        ),
        attendance_timeline.LocationSpan(
            employee_odoo_id=101,
            employee_name="Worker Secret",
            start_utc=NOW + timedelta(minutes=10),
            end_utc=NOW + timedelta(minutes=15),
            status="missing_required_location",
            app_work_center_name=None,
            odoo_work_center_id=None,
            odoo_work_center_name=None,
            attendance_ids=(130,),
            department_repair=None,
        ),
    ]
    db_metrics = {
        "correction_job_ids": (11, 12),
        "repair_attendance_ids": (21,),
        "recalculation_ids": ("2026-08-31",),
    }

    with caplog.at_level("INFO", logger=attendance_readiness.__name__):
        attendance_readiness._log_readiness_identifiers(
            segments,
            db_metrics,
            now_utc=NOW,
        )

    record = caplog.records[-1]
    assert record.employee_ids == (101,)
    assert record.work_center_ids == (71,)
    assert len(record.attendance_ids) == 100
    assert record.correction_ids == (11, 12)
    assert record.repair_ids == (21,)
    assert record.recalculation_ids == ("2026-08-31",)
    assert record.exception_ids == (
        attendance_readiness.inbox_keys.attendance_issue_key(
            "attendance_missing_location", 101, (130,), NOW + timedelta(minutes=5)
        ),
        attendance_readiness.inbox_keys.attendance_issue_key(
            "attendance_unmapped_location", 101, tuple(range(1, 130)), NOW
        ),
    )
    assert "Worker" not in record.getMessage()


def test_blocked_settlement_rolls_back_once_with_stable_exception_identity(monkeypatch):
    cutover = datetime(2026, 9, 1, 12, tzinfo=UTC)
    settle_now = cutover + timedelta(seconds=1)
    pending = attendance_location_policy.RolloutConfig(
        "live",
        cutover,
        attendance_location_policy.LiveGate(settle_now - timedelta(minutes=1), "old", None),
    )
    cursor = _AtomicCursor(_raw_config(pending))
    monkeypatch.setattr(attendance_readiness.db, "cursor", lambda: _CursorContext(cursor))
    saved = []
    settings = []
    monkeypatch.setattr(
        attendance_readiness.attendance_location_policy,
        "set_rollout_config",
        lambda config, *, cur: saved.append(config),
    )
    monkeypatch.setattr(
        attendance_readiness.app_settings,
        "set_setting",
        lambda key, value, *, cur: settings.append((key, value)),
    )
    blocked = attendance_readiness.ReadinessReport(
        False, 91.0, 2.0, 0, 0, 0.0, None, 0, 0, 0.0, 0, 0.0, 0, 0.0,
        0.0, None, 0.0, 0, 0, 0, 0, ("mirror_stale",),
    )

    assert attendance_readiness._settle_due_cutover(
        expected_config=pending,
        report=blocked,
        now_utc=settle_now,
    ) == "rolled_back"

    assert saved == [attendance_location_policy.RolloutConfig("shadow", None, None)]
    assert settings[0][0] == "odoo_attendance_cutover_blocked"
    payload = settings[0][1]
    assert payload["item_key"] == attendance_readiness.cutover_blocked_item_key(cutover)
    assert payload["blockers"] == ["mirror_stale"]
    assert "employee" not in json.dumps(payload).lower()


def test_blocked_cutover_payload_materializes_one_stable_urgent_issue(monkeypatch):
    cutover = NOW - timedelta(seconds=1)
    monkeypatch.setattr(
        attendance_exceptions.app_settings,
        "get_setting",
        lambda _key: {
            "item_key": "untrusted",
            "cutover_at": cutover.isoformat(),
            "checked_at": NOW.isoformat(),
            "blockers": ["mirror_stale"],
        },
    )

    issue = attendance_exceptions._cutover_blocked_issue(NOW.date())

    assert issue is not None
    assert issue.item_key == attendance_readiness.cutover_blocked_item_key(cutover)
    assert issue.priority == "urgent"
    assert issue.employee_name is None


def test_settlement_rechecks_locked_pending_config_and_loses_duplicate_race(monkeypatch):
    cutover = NOW - timedelta(seconds=1)
    pending = attendance_location_policy.RolloutConfig(
        "live",
        cutover,
        attendance_location_policy.LiveGate(NOW - timedelta(minutes=1), "old", None),
    )
    already_activated = attendance_location_policy.RolloutConfig(
        "live",
        cutover,
        attendance_location_policy.LiveGate(NOW - timedelta(minutes=1), "new", NOW),
    )
    cursor = _AtomicCursor(_raw_config(already_activated))
    monkeypatch.setattr(attendance_readiness.db, "cursor", lambda: _CursorContext(cursor))
    monkeypatch.setattr(
        attendance_readiness.attendance_location_policy,
        "set_rollout_config",
        lambda *_args, **_kwargs: pytest.fail("duplicate activation cannot mutate"),
    )
    report = attendance_readiness.ReadinessReport(
        True, 1.0, 2.0, 0, 0, 0.0, None, 0, 0, 0.0, 0, 0.0, 0, 0.0,
        0.0, None, 0.0, 0, 0, 0, 0, (),
    )

    assert attendance_readiness._settle_due_cutover(
        expected_config=pending,
        report=report,
        now_utc=NOW,
    ) == "superseded"


def test_settlement_rejects_changed_bound_source_before_any_activation_write(monkeypatch):
    cutover = NOW - timedelta(seconds=1)
    pending = attendance_location_policy.RolloutConfig(
        "live",
        cutover,
        attendance_location_policy.LiveGate(NOW - timedelta(minutes=1), "old", None),
    )
    cursor = _AtomicCursor(_raw_config(pending))
    monkeypatch.setattr(attendance_readiness.db, "cursor", lambda: _CursorContext(cursor))
    monkeypatch.setattr(attendance_readiness, "_lock_readiness_sources_cur", lambda cur: None)
    monkeypatch.setattr(
        attendance_readiness,
        "_source_fingerprints",
        lambda **_kwargs: ("local-new", "production-old"),
    )
    monkeypatch.setattr(
        attendance_readiness.attendance_location_policy,
        "set_rollout_config",
        lambda *_args, **_kwargs: pytest.fail("stale decision cannot mutate"),
    )
    report = attendance_readiness.ReadinessReport(
        True, 1.0, 2.0, 0, 0, 0.0, None, 0, 0, 0.0, 0, 0.0, 0, 0.0,
        0.0, None, 0.0, 0, 0, 0, 0, (),
    )
    decision = attendance_readiness.DecisionSnapshot(
        report, attendance_readiness.report_digest(report), "local-old", "production-old"
    )

    assert attendance_readiness._settle_due_cutover(
        expected_config=pending,
        report=report,
        decision=decision,
        now_utc=NOW,
    ) == "superseded"
    assert not any(
        "INSERT INTO attendance_strict_days" in sql for sql, _ in cursor.statements
    )


def test_settlement_rejects_a_tampered_frozen_meter_binding(monkeypatch):
    cutover = NOW - timedelta(seconds=1)
    pending = attendance_location_policy.RolloutConfig(
        "live",
        cutover,
        attendance_location_policy.LiveGate(NOW - timedelta(minutes=1), "old", None),
    )
    cursor = _AtomicCursor(_raw_config(pending))
    monkeypatch.setattr(attendance_readiness.db, "cursor", lambda: _CursorContext(cursor))
    monkeypatch.setattr(attendance_readiness, "_lock_readiness_sources_cur", lambda cur: None)
    monkeypatch.setattr(
        attendance_readiness,
        "_source_fingerprints",
        lambda **_kwargs: ("local", "production"),
    )
    monkeypatch.setattr(
        attendance_readiness.attendance_location_policy,
        "set_rollout_config",
        lambda *_args, **_kwargs: pytest.fail("tampered meter proof cannot mutate"),
    )
    report = attendance_readiness.ReadinessReport(
        True, 1.0, 2.0, 0, 0, 0.0, None, 0, 0, 0.0, 0, 0.0, 0, 0.0,
        0.0, None, 0.0, 0, 0, 0, 0, (),
    )
    digest = attendance_readiness.report_digest(report)
    original = attendance_readiness.DecisionSnapshot(
        report=report,
        report_digest=digest,
        local_source_fingerprint="local",
        production_fingerprint="production",
        frozen_production_fingerprint="meter-a",
        source_binding_digest=attendance_readiness._decision_binding_digest(
            digest, "local", "production", "meter-a"
        ),
    )
    tampered = replace(original, frozen_production_fingerprint="meter-b")

    assert attendance_readiness._settle_due_cutover(
        expected_config=pending,
        report=report,
        decision=tampered,
        now_utc=NOW,
    ) == "superseded"


def test_prior_strict_day_survives_failed_cutover_rollback(monkeypatch):
    prior = date(2026, 8, 31)
    monkeypatch.setattr(attendance_location_policy, "strict_days", lambda: {prior})
    monkeypatch.setattr(
        attendance_location_policy,
        "get_rollout_config",
        lambda: attendance_location_policy.RolloutConfig("shadow", None, None),
    )

    assert attendance_location_policy.day_is_strict(prior) is True


def test_recalc_completion_takes_rollout_lock_before_queue_row(monkeypatch):
    day = date(2026, 9, 1)
    lease = NOW + timedelta(minutes=15)

    class Cursor:
        def __init__(self):
            self.statements = []
            self.row = None

        def execute(self, sql, params=None):
            normalized = " ".join(sql.split())
            self.statements.append(normalized)
            if normalized.startswith("SELECT day, attempt_count"):
                self.row = {
                    "day": day,
                    "attempt_count": 1,
                    "started_at": lease,
                    "completed_at": None,
                }
            elif "RETURNING day" in normalized:
                self.row = {"day": day}
            else:
                self.row = None

        def fetchone(self):
            return self.row

    cursor = Cursor()
    monkeypatch.setattr(attendance_recalc.db, "cursor", lambda: _CursorContext(cursor))
    monkeypatch.setattr(precompute, "store_prepared_day", lambda *_args, **_kwargs: 2)

    assert attendance_recalc._complete_claim(
        attendance_recalc.RecalcClaim(day, 1, lease),
        SimpleNamespace(day=day, expected_match_state=None),
        NOW,
    ) == 2
    assert cursor.statements[0] == (
        "LOCK TABLE app_settings, attendance_strict_days IN SHARE ROW EXCLUSIVE MODE"
    )


def test_ordinary_precompute_is_fenced_until_cutover_recalc_completes(monkeypatch):
    day = date(2026, 9, 1)
    prepared = precompute.PreparedProductionDay(
        day=day,
        rows=(),
        strict_day=day,
        expected_match_state="strict",
        source_fingerprint="strict-source",
        request_fingerprint="strict-request-source",
    )

    class Cursor:
        def __init__(self):
            self.row = None

        def execute(self, sql, params=None):
            normalized = " ".join(sql.split())
            if normalized.startswith("SELECT s.reason"):
                self.row = {"reason": "live_cutover", "completed_at": None}
            else:
                self.row = None

        def fetchone(self):
            return self.row

    monkeypatch.setattr(
        attendance_location_policy,
        "match_state_for_day_cur",
        lambda *_args, **_kwargs: "strict",
    )
    monkeypatch.setattr(production_history, "lock_strict_sources_cur", lambda _cur: None)
    monkeypatch.setattr(
        production_history,
        "strict_local_source_fingerprint",
        lambda _day, *, cur: "strict-source",
    )

    with pytest.raises(precompute.CutoverRecalcPending):
        precompute._validate_prepared_match_state_cur(
            Cursor(), prepared
        )

    precompute._validate_prepared_match_state_cur(
        Cursor(), prepared, allow_cutover_recalc=True
    )


def test_cutover_fence_does_not_reset_the_active_recalc_claim(monkeypatch):
    day = date(2026, 9, 1)
    prepared = precompute.PreparedProductionDay(
        day=day,
        rows=(),
        strict_day=day,
        expected_match_state="strict",
        source_fingerprint="strict-source",
        request_fingerprint="strict-request-source",
    )
    monkeypatch.setattr(precompute, "prepare_day", lambda *_args: prepared)
    queued = []
    monkeypatch.setattr(
        attendance_readiness.attendance_mirror,
        "ensure_recalc_queued",
        lambda days, reason, **kwargs: queued.append(
            (tuple(days), reason, kwargs["source_fingerprint"])
        ),
    )
    monkeypatch.setattr(
        precompute,
        "store_prepared_day",
        lambda *_args, **_kwargs: pytest.fail("direct strict write bypassed queue"),
    )
    monkeypatch.setattr(
        attendance_readiness.attendance_mirror,
        "enqueue_recalc",
        lambda *_args, **_kwargs: pytest.fail("active cutover claim must not be reset"),
    )

    assert precompute.precompute_day(day, object()) == {
        "day": day.isoformat(),
        "rows_written": 0,
        "queued": True,
    }
    assert queued == [((day,), "strict_direct_refresh", "strict-request-source")]


@pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="needs local Postgres")
def test_postgres_sync_state_persists_the_open_row_observation_clock():
    from zira_dashboard import db

    db.init_pool()
    db.bootstrap_schema()
    columns = db.query(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema = current_schema() "
        "AND table_name = 'odoo_attendance_sync_state' "
        "AND column_name = 'last_incremental_observed_at'"
    )

    assert columns == [{"column_name": "last_incremental_observed_at"}]


@pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="needs local Postgres")
def test_postgres_two_due_warmers_build_one_boundary_report(monkeypatch):
    from zira_dashboard import app_settings, db

    db.init_pool()
    checked = datetime.now(UTC) - timedelta(seconds=1)
    cutover = checked - timedelta(seconds=1)
    pending = attendance_location_policy.RolloutConfig(
        "live", cutover, attendance_location_policy.LiveGate(checked, "scheduled", None)
    )
    app_settings.set_setting("odoo_attendance_location", _raw_config(pending))
    entered = Event()
    release = Event()
    count_lock = Lock()
    build_count = 0
    ready = attendance_readiness.ReadinessReport(
        True, 1.0, 2.0, 0, 0, 0.0, None, 0, 0, 0.0, 0, 0.0, 0, 0.0,
        0.0, None, 0.0, 0, 0, 0, 0, (),
    )

    def build(*_args, **_kwargs):
        nonlocal build_count
        with count_lock:
            build_count += 1
        entered.set()
        assert release.wait(timeout=5)
        return _decision(ready)

    monkeypatch.setattr(attendance_readiness, "build_decision_snapshot", build)
    monkeypatch.setattr(
        attendance_readiness,
        "_settle_due_cutover",
        lambda **_kwargs: "activated",
    )
    results = []
    errors = []

    def run():
        try:
            results.append(attendance_readiness.activate_due_cutover(checked, production_client=object()))
        except Exception as exc:  # pragma: no cover - asserted in parent thread
            errors.append(exc)

    first = Thread(target=run)
    second = Thread(target=run)
    first.start()
    assert entered.wait(timeout=5)
    second.start()
    second.join(timeout=5)
    release.set()
    first.join(timeout=5)
    try:
        assert not errors
        assert build_count == 1
        assert sorted(results) == ["activated", "busy"]
    finally:
        app_settings.set_setting(
            "odoo_attendance_location",
            {"mode": "shadow", "cutover_at": None, "live_gate": None},
        )


@pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="needs local Postgres")
def test_postgres_activation_commits_gate_strict_day_and_recalc_together(monkeypatch):
    from zira_dashboard import app_settings, db

    monkeypatch.undo()
    db.init_pool()
    now, cutover, original_schedule = _set_persisted_due_boundary(db)
    day = cutover.date()
    pending = attendance_location_policy.RolloutConfig(
        "live", cutover, attendance_location_policy.LiveGate(now - timedelta(minutes=1), "scheduled", None)
    )
    monkeypatch.setattr(attendance_location_policy, "_utc_now", lambda: datetime.now(UTC))
    with db.cursor() as cur:
        cur.execute("DELETE FROM attendance_recalc_queue WHERE day = %s", (day,))
        cur.execute("DELETE FROM attendance_strict_days WHERE day = %s", (day,))
        cur.execute("DELETE FROM attendance_rollout_audit WHERE cutover_at = %s", (cutover,))
    app_settings.set_setting("odoo_attendance_location", _raw_config(pending))
    local_fp, production_fp = attendance_readiness._source_fingerprints(now_utc=now)
    ready = attendance_readiness.ReadinessReport(
        True, 1.0, 2.0, 0, 0, 0.0, None, 0, 0, 0.0, 0, 0.0, 0, 0.0,
        0.0, None, 0.0, 0, 0, 0, 0, (),
    )
    decision = _bound_decision(ready, local_fp, production_fp, checked_at=now)

    try:
        assert attendance_readiness._settle_due_cutover(
            expected_config=pending,
            report=ready,
            decision=decision,
            now_utc=now,
        ) == "activated"
        config = attendance_location_policy.get_rollout_config_strict()
        assert config.live_gate is not None
        assert now <= config.live_gate.activated_at <= now + timedelta(seconds=5)
        assert db.query("SELECT reason FROM attendance_strict_days WHERE day = %s", (day,)) == [
            {"reason": "live_cutover"}
        ]
        assert db.query("SELECT reason FROM attendance_recalc_queue WHERE day = %s", (day,)) == [
            {"reason": "live_cutover"}
        ]
        assert db.query(
            "SELECT event_kind, rollout_mode, cutover_at, report_digest "
            "FROM attendance_rollout_audit WHERE cutover_at = %s",
            (cutover,),
        ) == [{
            "event_kind": "live_activated",
            "rollout_mode": "live",
            "cutover_at": cutover.astimezone(UTC),
            "report_digest": attendance_readiness.report_digest(ready),
        }]
    finally:
        with db.cursor() as cur:
            cur.execute("DELETE FROM attendance_recalc_queue WHERE day = %s", (day,))
            cur.execute("DELETE FROM attendance_strict_days WHERE day = %s", (day,))
            cur.execute("DELETE FROM attendance_rollout_audit WHERE cutover_at = %s", (cutover,))
        app_settings.set_setting(
            "odoo_attendance_location",
            {"mode": "shadow", "cutover_at": None, "live_gate": None},
        )
        _restore_persisted_boundary(db, original_schedule)


@pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="needs local Postgres")
def test_postgres_activation_rolls_back_every_success_write_on_enqueue_failure(monkeypatch):
    from zira_dashboard import app_settings, db

    monkeypatch.undo()
    db.init_pool()
    now, cutover, original_schedule = _set_persisted_due_boundary(db)
    day = cutover.date()
    pending = attendance_location_policy.RolloutConfig(
        "live", cutover, attendance_location_policy.LiveGate(now - timedelta(minutes=1), "scheduled", None)
    )
    monkeypatch.setattr(attendance_location_policy, "_utc_now", lambda: datetime.now(UTC))
    with db.cursor() as cur:
        cur.execute("DELETE FROM attendance_recalc_queue WHERE day = %s", (day,))
        cur.execute("DELETE FROM attendance_strict_days WHERE day = %s", (day,))
        cur.execute("DELETE FROM attendance_rollout_audit WHERE cutover_at = %s", (cutover,))
    app_settings.set_setting("odoo_attendance_location", _raw_config(pending))
    local_fp, production_fp = attendance_readiness._source_fingerprints(now_utc=now)
    ready = attendance_readiness.ReadinessReport(
        True, 1.0, 2.0, 0, 0, 0.0, None, 0, 0, 0.0, 0, 0.0, 0, 0.0,
        0.0, None, 0.0, 0, 0, 0, 0, (),
    )
    decision = _bound_decision(ready, local_fp, production_fp, checked_at=now)
    monkeypatch.setattr(
        attendance_readiness.attendance_mirror,
        "_enqueue_recalc_cur",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("injected enqueue failure")),
    )

    try:
        with pytest.raises(RuntimeError, match="injected enqueue failure"):
            attendance_readiness._settle_due_cutover(
                expected_config=pending,
                report=ready,
                decision=decision,
                now_utc=now,
            )
        assert attendance_location_policy.get_rollout_config_strict() == pending
        assert db.query("SELECT day FROM attendance_strict_days WHERE day = %s", (day,)) == []
        assert db.query("SELECT day FROM attendance_recalc_queue WHERE day = %s", (day,)) == []
        assert db.query(
            "SELECT id FROM attendance_rollout_audit WHERE cutover_at = %s",
            (cutover,),
        ) == []
    finally:
        with db.cursor() as cur:
            cur.execute("DELETE FROM attendance_recalc_queue WHERE day = %s", (day,))
            cur.execute("DELETE FROM attendance_strict_days WHERE day = %s", (day,))
            cur.execute("DELETE FROM attendance_rollout_audit WHERE cutover_at = %s", (cutover,))
        app_settings.set_setting(
            "odoo_attendance_location",
            {"mode": "shadow", "cutover_at": None, "live_gate": None},
        )
        _restore_persisted_boundary(db, original_schedule)


@pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="needs local Postgres")
def test_postgres_concurrent_live_schedules_use_one_exact_config_cas(monkeypatch):
    from zira_dashboard import app_settings, db

    db.init_pool()
    checked = datetime.now(UTC) - timedelta(seconds=1)
    previous = attendance_location_policy.RolloutConfig("shadow", None, None)
    app_settings.set_setting("odoo_attendance_location", _raw_config(previous))
    monkeypatch.setattr(attendance_readiness, "_utc_now", lambda: checked)
    monkeypatch.setattr(attendance_readiness.shift_config, "shift_start_for", lambda _day: datetime.min.time().replace(hour=7))
    monkeypatch.setattr(attendance_readiness.shift_config, "is_workday", lambda _day: True)
    local_fp, production_fp = attendance_readiness._source_fingerprints(now_utc=checked)
    ready = attendance_readiness.ReadinessReport(
        True, 1.0, 2.0, 0, 0, 0.0, None, 0, 0, 0.0, 0, 0.0, 0, 0.0,
        0.0, None, 0.0, 0, 0, 0, 0, (),
    )
    decision = _bound_decision(ready, local_fp, production_fp, checked_at=checked)
    configs = [
        attendance_location_policy.RolloutConfig(
            "live",
            datetime(2099, 1, day, 7, tzinfo=attendance_readiness.shift_config.SITE_TZ),
            attendance_location_policy.LiveGate(checked, decision.report_digest, None),
        )
        for day in (5, 6)
    ]
    results = []

    def save(config):
        try:
            attendance_readiness._store_pending_cutover(
                config,
                checked_at=checked,
                expected_config=previous,
                decision=decision,
            )
            results.append("saved")
        except ValueError as exc:
            results.append(str(exc))

    threads = [Thread(target=save, args=(config,)) for config in configs]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    try:
        assert all(not thread.is_alive() for thread in threads)
        assert sorted(results) == ["live_schedule_superseded", "saved"]
        assert attendance_location_policy.get_rollout_config_strict() in configs
    finally:
        app_settings.set_setting(
            "odoo_attendance_location",
            {"mode": "shadow", "cutover_at": None, "live_gate": None},
        )


@pytest.mark.parametrize("requested_mode", ["off", "shadow"])
@pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="needs local Postgres")
def test_postgres_stale_non_live_settings_cannot_overwrite_boundary_activation(
    monkeypatch,
    requested_mode,
):
    from zira_dashboard import app_settings, db
    from zira_dashboard.routes import settings

    monkeypatch.undo()
    db.init_pool()
    now, cutover, original_schedule = _set_persisted_due_boundary(db)
    day = cutover.date()
    pending = attendance_location_policy.RolloutConfig(
        "live",
        cutover,
        attendance_location_policy.LiveGate(now - timedelta(minutes=1), "scheduled", None),
    )
    monkeypatch.setattr(attendance_location_policy, "_utc_now", lambda: datetime.now(UTC))
    with db.cursor() as cur:
        cur.execute("DELETE FROM attendance_recalc_queue WHERE day = %s", (day,))
        cur.execute("DELETE FROM attendance_strict_days WHERE day = %s", (day,))
        cur.execute("DELETE FROM attendance_rollout_audit WHERE cutover_at = %s", (cutover,))
    app_settings.set_setting("odoo_attendance_location", _raw_config(pending))
    local_fp, production_fp = attendance_readiness._source_fingerprints(now_utc=now)
    ready = attendance_readiness.ReadinessReport(
        True, 1.0, 2.0, 0, 0, 0.0, None, 0, 0, 0.0, 0, 0.0, 0, 0.0,
        0.0, None, 0.0, 0, 0, 0, 0, (),
    )
    decision = _bound_decision(ready, local_fp, production_fp, checked_at=now)
    activation_inside_transaction = Event()
    allow_activation_commit = Event()
    settings_started = Event()
    settings_finished = Event()
    results = []
    original_audit = attendance_readiness._record_rollout_audit_cur

    def hold_activation_transaction(cur, **kwargs):
        original_audit(cur, **kwargs)
        if kwargs.get("event_kind") == "live_activated":
            activation_inside_transaction.set()
            assert allow_activation_commit.wait(timeout=5)

    monkeypatch.setattr(
        attendance_readiness,
        "_record_rollout_audit_cur",
        hold_activation_transaction,
    )

    def boundary_activation():
        results.append(
            attendance_readiness._settle_due_cutover(
                expected_config=pending,
                report=ready,
                decision=decision,
                now_utc=now,
            )
        )

    def stale_settings_request():
        assert activation_inside_transaction.wait(timeout=5)
        settings_started.set()
        try:
            settings._save_non_live_attendance_location(
                mode=requested_mode,
                cutover_at=(cutover + timedelta(days=1) if requested_mode == "shadow" else None),
                selected_departments=set(),
                departments=(),
                expected_config=pending,
            )
        except ValueError as exc:
            results.append(str(exc))
        finally:
            settings_finished.set()

    activation_thread = Thread(target=boundary_activation)
    settings_thread = Thread(target=stale_settings_request)
    settings_thread.start()
    activation_thread.start()
    assert activation_inside_transaction.wait(timeout=5)
    assert settings_started.wait(timeout=5)
    assert settings_finished.wait(timeout=0.2) is False
    allow_activation_commit.set()
    activation_thread.join(timeout=10)
    settings_thread.join(timeout=10)
    try:
        assert not activation_thread.is_alive()
        assert not settings_thread.is_alive()
        assert sorted(results) == ["activated", "rollout_save_superseded"]
        stored = attendance_location_policy.get_rollout_config_strict()
        assert stored.mode == "live"
        assert stored.live_gate is not None
        assert now <= stored.live_gate.activated_at <= now + timedelta(seconds=5)
    finally:
        with db.cursor() as cur:
            cur.execute("DELETE FROM attendance_recalc_queue WHERE day = %s", (day,))
            cur.execute("DELETE FROM attendance_strict_days WHERE day = %s", (day,))
            cur.execute("DELETE FROM attendance_rollout_audit WHERE cutover_at = %s", (cutover,))
        app_settings.set_setting(
            "odoo_attendance_location",
            {"mode": "shadow", "cutover_at": None, "live_gate": None},
        )
        _restore_persisted_boundary(db, original_schedule)


@pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="needs local Postgres")
def test_postgres_prepared_legacy_snapshot_is_rejected_after_boundary_activation(
    monkeypatch,
):
    from zira_dashboard import app_settings, db

    db.init_pool()
    day = date(2099, 1, 8)
    cutover = datetime(2099, 1, 8, 7, tzinfo=attendance_readiness.shift_config.SITE_TZ)
    now = cutover.astimezone(UTC) + timedelta(seconds=1)
    pending = attendance_location_policy.RolloutConfig(
        "live",
        cutover,
        attendance_location_policy.LiveGate(now - timedelta(minutes=1), "scheduled", None),
    )
    prepared = precompute.PreparedProductionDay(
        day,
        ({
            "day": day,
            "emp_id": "101",
            "name": "Worker 101",
            "wc_name": "WC A",
            "units": 7.0,
            "downtime": 0.0,
            "hours": 1.0,
            "days_worked": 1.0,
            "excluded_minutes": 0.0,
        },),
        None,
        "legacy",
    )
    ready = attendance_readiness.ReadinessReport(
        True, 1.0, 2.0, 0, 0, 0.0, None, 0, 0, 0.0, 0, 0.0, 0, 0.0,
        0.0, None, 0.0, 0, 0, 0, 0, (),
    )
    monkeypatch.setattr(attendance_location_policy, "_utc_now", lambda: now)
    with db.cursor() as cur:
        cur.execute("DELETE FROM production_daily WHERE day = %s", (day,))
        cur.execute("DELETE FROM attendance_recalc_queue WHERE day = %s", (day,))
        cur.execute("DELETE FROM attendance_strict_days WHERE day = %s", (day,))
    app_settings.set_setting("odoo_attendance_location", _raw_config(pending))
    activated = Event()
    results = []

    def activate():
        results.append(
            attendance_readiness._settle_due_cutover(
                expected_config=pending,
                report=ready,
                now_utc=now,
            )
        )
        activated.set()

    def stale_store():
        assert activated.wait(timeout=5)
        try:
            precompute.store_prepared_day(prepared)
        except production_history.ProductionSourceUnavailable:
            results.append("legacy_rejected")

    threads = [Thread(target=stale_store), Thread(target=activate)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    try:
        assert all(not thread.is_alive() for thread in threads)
        assert sorted(results) == ["activated", "legacy_rejected"]
        assert db.query("SELECT emp_id FROM production_daily WHERE day = %s", (day,)) == []
    finally:
        with db.cursor() as cur:
            cur.execute("DELETE FROM production_daily WHERE day = %s", (day,))
            cur.execute("DELETE FROM attendance_recalc_queue WHERE day = %s", (day,))
            cur.execute("DELETE FROM attendance_strict_days WHERE day = %s", (day,))
        app_settings.set_setting(
            "odoo_attendance_location",
            {"mode": "shadow", "cutover_at": None, "live_gate": None},
        )


@pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="needs local Postgres")
def test_postgres_activation_and_recalc_completion_share_lock_order_without_deadlock(
    monkeypatch,
):
    from zira_dashboard import app_settings, db

    db.init_pool()
    day = date(2099, 1, 9)
    cutover = datetime(2099, 1, 9, 7, tzinfo=attendance_readiness.shift_config.SITE_TZ)
    now = cutover.astimezone(UTC) + timedelta(seconds=1)
    lease = now + timedelta(minutes=15)
    pending = attendance_location_policy.RolloutConfig(
        "live",
        cutover,
        attendance_location_policy.LiveGate(now - timedelta(minutes=1), "scheduled", None),
    )
    prepared = precompute.PreparedProductionDay(day, (), None, None)
    claim = attendance_recalc.RecalcClaim(day, 1, lease)
    ready = attendance_readiness.ReadinessReport(
        True, 1.0, 2.0, 0, 0, 0.0, None, 0, 0, 0.0, 0, 0.0, 0, 0.0,
        0.0, None, 0.0, 0, 0, 0, 0, (),
    )
    monkeypatch.setattr(attendance_location_policy, "_utc_now", lambda: now)
    with db.cursor() as cur:
        cur.execute("DELETE FROM attendance_recalc_queue WHERE day = %s", (day,))
        cur.execute("DELETE FROM attendance_strict_days WHERE day = %s", (day,))
        cur.execute(
            "INSERT INTO attendance_recalc_queue "
            "(day, reason, requested_at, started_at, attempt_count) "
            "VALUES (%s, %s, %s, %s, 1)",
            (day, "source_change", now - timedelta(minutes=1), lease),
        )
    app_settings.set_setting("odoo_attendance_location", _raw_config(pending))
    start = Event()
    results = []
    errors = []

    def activate():
        assert start.wait(timeout=5)
        try:
            results.append(
                attendance_readiness._settle_due_cutover(
                    expected_config=pending,
                    report=ready,
                    now_utc=now,
                )
            )
        except Exception as exc:  # pragma: no cover - asserted in parent thread
            errors.append(exc)

    def complete():
        assert start.wait(timeout=5)
        try:
            results.append(attendance_recalc._complete_claim(claim, prepared, now))
        except Exception as exc:  # pragma: no cover - asserted in parent thread
            errors.append(exc)

    threads = [Thread(target=activate), Thread(target=complete)]
    for thread in threads:
        thread.start()
    start.set()
    for thread in threads:
        thread.join(timeout=10)
    try:
        assert all(not thread.is_alive() for thread in threads)
        assert errors == []
        assert "activated" in results
        assert any(value in (None, 0) for value in results)
    finally:
        with db.cursor() as cur:
            cur.execute("DELETE FROM attendance_recalc_queue WHERE day = %s", (day,))
            cur.execute("DELETE FROM attendance_strict_days WHERE day = %s", (day,))
        app_settings.set_setting(
            "odoo_attendance_location",
            {"mode": "shadow", "cutover_at": None, "live_gate": None},
        )


@pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="needs local Postgres")
def test_postgres_ordinary_live_write_waits_for_cutover_recalc_completion(monkeypatch):
    from zira_dashboard import app_settings, db

    db.init_pool()
    day = date(2099, 1, 12)
    cutover = datetime(2099, 1, 12, 7, tzinfo=attendance_readiness.shift_config.SITE_TZ)
    now = cutover.astimezone(UTC) + timedelta(seconds=1)
    pending = attendance_location_policy.RolloutConfig(
        "live",
        cutover,
        attendance_location_policy.LiveGate(now - timedelta(minutes=1), "scheduled", None),
    )
    ready = attendance_readiness.ReadinessReport(
        True, 1.0, 2.0, 0, 0, 0.0, None, 0, 0, 0.0, 0, 0.0, 0, 0.0,
        0.0, None, 0.0, 0, 0, 0, 0, (),
    )
    row = {
        "day": day,
        "emp_id": "101",
        "name": "Worker 101",
        "wc_name": "WC A",
        "units": 9.0,
        "downtime": 0.0,
        "hours": 1.0,
        "days_worked": 1.0,
        "excluded_minutes": 0.0,
    }
    source_fingerprint = production_history.strict_local_source_fingerprint(day)
    prepared = precompute.PreparedProductionDay(
        day, (row,), day, "strict", source_fingerprint, "strict-request-source"
    )
    monkeypatch.setattr(attendance_location_policy, "_utc_now", lambda: now)
    with db.cursor() as cur:
        cur.execute("DELETE FROM production_daily WHERE day = %s", (day,))
        cur.execute("DELETE FROM attendance_recalc_queue WHERE day = %s", (day,))
        cur.execute("DELETE FROM attendance_strict_days WHERE day = %s", (day,))
    app_settings.set_setting("odoo_attendance_location", _raw_config(pending))
    try:
        assert attendance_readiness._settle_due_cutover(
            expected_config=pending,
            report=ready,
            now_utc=now,
        ) == "activated"
        with pytest.raises(precompute.CutoverRecalcPending):
            precompute.store_prepared_day(prepared)
        assert db.query("SELECT units FROM production_daily WHERE day = %s", (day,)) == []

        lease = now + timedelta(minutes=15)
        with db.cursor() as cur:
            cur.execute(
                "UPDATE attendance_recalc_queue SET started_at = %s, attempt_count = 1 "
                "WHERE day = %s",
                (lease, day),
            )
        assert attendance_recalc._complete_claim(
            attendance_recalc.RecalcClaim(day, 1, lease),
            prepared,
            now,
        ) == 1
        assert db.query("SELECT units FROM production_daily WHERE day = %s", (day,)) == [
            {"units": 9.0}
        ]
    finally:
        with db.cursor() as cur:
            cur.execute("DELETE FROM production_daily WHERE day = %s", (day,))
            cur.execute("DELETE FROM attendance_recalc_queue WHERE day = %s", (day,))
            cur.execute("DELETE FROM attendance_strict_days WHERE day = %s", (day,))
        app_settings.set_setting(
            "odoo_attendance_location",
            {"mode": "shadow", "cutover_at": None, "live_gate": None},
        )


@pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="needs local Postgres")
def test_postgres_schedule_drift_blocks_due_live_activation(monkeypatch):
    from zira_dashboard import app_settings, db

    db.init_pool()
    db.bootstrap_schema()
    monkeypatch.undo()
    day = date(2099, 9, 1)
    cutover = datetime.combine(
        day,
        datetime.min.time().replace(hour=7),
        tzinfo=attendance_readiness.shift_config.SITE_TZ,
    ).astimezone(UTC)
    now = cutover + timedelta(seconds=1)
    pending = attendance_location_policy.RolloutConfig(
        "live",
        cutover,
        attendance_location_policy.LiveGate(now - timedelta(minutes=1), "scheduled", None),
    )
    ready = attendance_readiness.ReadinessReport(
        True, 1.0, 2.0, 0, 0, 0.0, None, 0, 0, 0.0, 0, 0.0, 0, 0.0,
        0.0, None, 0.0, 0, 0, 0, 0, (),
    )
    prior_schedule = db.query("SELECT * FROM global_schedule WHERE id = 1")
    prior_rollout = app_settings.get_setting("odoo_attendance_location")
    monkeypatch.setattr(attendance_location_policy, "_utc_now", lambda: now)
    try:
        with db.cursor() as cur:
            cur.execute(
                "INSERT INTO global_schedule "
                "(id, shift_start, shift_end, work_weekdays, breaks, updated_at) "
                "VALUES (1, '08:00', '15:30', %s, '[]'::jsonb, now()) "
                "ON CONFLICT (id) DO UPDATE SET shift_start = EXCLUDED.shift_start, "
                "shift_end = EXCLUDED.shift_end, work_weekdays = EXCLUDED.work_weekdays, "
                "breaks = EXCLUDED.breaks, updated_at = now()",
                ([day.weekday()],),
            )
            app_settings.set_setting(
                "odoo_attendance_location",
                _raw_config(pending),
                cur=cur,
            )
            cur.execute("DELETE FROM attendance_strict_days WHERE day = %s", (day,))
            cur.execute("DELETE FROM attendance_recalc_queue WHERE day = %s", (day,))

        assert attendance_readiness._settle_due_cutover(
            expected_config=pending,
            report=ready,
            now_utc=now,
        ) == "rolled_back"
        assert attendance_location_policy.get_rollout_config_strict() == (
            attendance_location_policy.RolloutConfig("shadow", None, None)
        )
        alert = app_settings.get_setting("odoo_attendance_cutover_blocked")
        assert alert["blockers"] == ["cutover_boundary_changed"]
        assert db.query("SELECT day FROM attendance_strict_days WHERE day = %s", (day,)) == []
        assert db.query("SELECT day FROM attendance_recalc_queue WHERE day = %s", (day,)) == []
    finally:
        with db.cursor() as cur:
            cur.execute("DELETE FROM attendance_strict_days WHERE day = %s", (day,))
            cur.execute("DELETE FROM attendance_recalc_queue WHERE day = %s", (day,))
            cur.execute(
                "DELETE FROM attendance_rollout_audit WHERE cutover_at = %s",
                (cutover,),
            )
            cur.execute(
                "DELETE FROM app_settings WHERE key = ANY(%s)",
                (["odoo_attendance_location", "odoo_attendance_cutover_blocked"],),
            )
            if prior_rollout is not None:
                app_settings.set_setting(
                    "odoo_attendance_location",
                    prior_rollout,
                    cur=cur,
                )
            cur.execute("DELETE FROM global_schedule WHERE id = 1")
            if prior_schedule:
                row = prior_schedule[0]
                cur.execute(
                    "INSERT INTO global_schedule "
                    "(id, shift_start, shift_end, work_weekdays, breaks, updated_at) "
                    "VALUES (1, %s, %s, %s, %s::jsonb, %s)",
                    (
                        row["shift_start"],
                        row["shift_end"],
                        row["work_weekdays"],
                        json.dumps(row["breaks"]),
                        row["updated_at"],
                    ),
                )


@pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="needs local Postgres")
def test_postgres_schedule_drift_keeps_pending_rollback_strict(monkeypatch):
    from zira_dashboard import app_settings, db

    db.init_pool()
    db.bootstrap_schema()
    monkeypatch.undo()
    activated_day = date(2099, 8, 31)
    rollback_day = date(2099, 9, 1)
    activated = datetime.combine(
        activated_day,
        datetime.min.time().replace(hour=7),
        tzinfo=attendance_readiness.shift_config.SITE_TZ,
    ).astimezone(UTC)
    rollback = datetime.combine(
        rollback_day,
        datetime.min.time().replace(hour=7),
        tzinfo=attendance_readiness.shift_config.SITE_TZ,
    ).astimezone(UTC)
    now = rollback + timedelta(seconds=1)
    pending = attendance_location_policy.RolloutConfig(
        "shadow",
        rollback,
        attendance_location_policy.LiveGate(
            activated - timedelta(minutes=1),
            "activated",
            activated,
        ),
    )
    prior_schedule = db.query("SELECT * FROM global_schedule WHERE id = 1")
    prior_rollout = app_settings.get_setting("odoo_attendance_location")
    try:
        with db.cursor() as cur:
            cur.execute(
                "INSERT INTO global_schedule "
                "(id, shift_start, shift_end, work_weekdays, breaks, updated_at) "
                "VALUES (1, '08:00', '15:30', %s, '[]'::jsonb, now()) "
                "ON CONFLICT (id) DO UPDATE SET shift_start = EXCLUDED.shift_start, "
                "shift_end = EXCLUDED.shift_end, work_weekdays = EXCLUDED.work_weekdays, "
                "breaks = EXCLUDED.breaks, updated_at = now()",
                ([rollback_day.weekday()],),
            )
            app_settings.set_setting(
                "odoo_attendance_location",
                _raw_config(pending),
                cur=cur,
            )
        monkeypatch.setattr(attendance_location_policy, "strict_days", lambda: set())

        assert attendance_location_policy.match_state_for_day(
            rollback_day,
            now_utc=now,
        ) == "strict"
        assert attendance_readiness._settle_due_rollback(
            pending,
            now_utc=now,
        ) == "superseded"
        assert attendance_location_policy.get_rollout_config_strict() == pending
        alert = app_settings.get_setting("odoo_attendance_cutover_blocked")
        assert alert["blockers"] == ["rollback_boundary_changed"]
    finally:
        with db.cursor() as cur:
            cur.execute(
                "DELETE FROM app_settings WHERE key = ANY(%s)",
                (["odoo_attendance_location", "odoo_attendance_cutover_blocked"],),
            )
            if prior_rollout is not None:
                app_settings.set_setting(
                    "odoo_attendance_location",
                    prior_rollout,
                    cur=cur,
                )
            cur.execute("DELETE FROM global_schedule WHERE id = 1")
            if prior_schedule:
                row = prior_schedule[0]
                cur.execute(
                    "INSERT INTO global_schedule "
                    "(id, shift_start, shift_end, work_weekdays, breaks, updated_at) "
                    "VALUES (1, %s, %s, %s, %s::jsonb, %s)",
                    (
                        row["shift_start"],
                        row["shift_end"],
                        row["work_weekdays"],
                        json.dumps(row["breaks"]),
                        row["updated_at"],
                    ),
                )


def test_collect_inputs_uses_one_pending_safe_strict_bundle(monkeypatch):
    segment = SimpleNamespace(
        status="missing_required_location",
        start_utc=NOW - timedelta(minutes=5),
        end_utc=NOW,
        app_work_center_name=None,
    )
    strict_inputs = SimpleNamespace(segments=(segment,))
    calls = []
    monkeypatch.setattr(
        attendance_readiness,
        "_collect_db_metrics",
        lambda _now: {
            "baseline_complete": True,
            "mirror_error": None,
            "mirror_age_seconds": 1.0,
            "last_full_sweep_age_seconds": 2.0,
            "open_rows_not_refreshed": 0,
            "last_sweep_deletion_count": 0,
            "recalc_queue_age_seconds": None,
            "recalc_queue_depth": 0,
            "failed_corrections": 0,
            "correction_retries_today": 0,
            "correction_verification_failures_today": 0,
            "failed_department_repairs": 0,
        },
    )
    snapshot_mapper = lambda odoo_id: "Repair 1" if odoo_id == 71 else None
    monkeypatch.setattr(
        attendance_readiness,
        "_snapshot_work_center_mapper",
        lambda: snapshot_mapper,
    )
    monkeypatch.setattr(
        attendance_readiness.production_history,
        "_strict_inputs_for_day",
        lambda day, client, now_utc, map_work_center: calls.append(
            (day, client, now_utc, map_work_center)
        )
        or strict_inputs,
    )
    strict = {(101, "Alex"): {"Repair 1": {"units": 4.0}}}
    monkeypatch.setattr(
        attendance_readiness.production_history,
        "_strict_attribution_from_inputs",
        lambda day, inputs: strict,
    )
    run = SimpleNamespace(
        start_utc=NOW - timedelta(minutes=4),
        end_utc=NOW,
        units=4.0,
        wc_name="Repair 1",
    )
    monkeypatch.setattr(
        attendance_readiness.production_history,
        "_strict_unassigned_runs_from_inputs",
        lambda day, inputs, now_utc: (run,),
    )
    monkeypatch.setattr(
        attendance_readiness,
        "_compare_strict_to_current",
        lambda day, strict_attribution, now_utc: attendance_readiness.ShadowComparison(
            day, now_utc, True, 4.0, 1, 4.0, 4.0, None
        ),
    )
    monkeypatch.setattr(
        attendance_exceptions,
        "build_snapshot",
        lambda *_args, **_kwargs: pytest.fail("pending readiness cannot use inbox snapshot"),
    )
    monkeypatch.setattr(
        attendance_readiness,
        "_log_readiness_identifiers",
        lambda *_args, **_kwargs: None,
    )

    result = attendance_readiness._collect_inputs(NOW, production_client="meter")

    assert calls == [(NOW.date(), "meter", NOW, snapshot_mapper)]
    assert result.projection_complete is True
    assert result.open_missing_required == 1
    assert result.unassigned_units_today == 4.0


def test_historical_source_change_invalidates_saved_complete_shadow_proof(monkeypatch):
    shadow_day = date(2026, 8, 31)
    monkeypatch.setattr(
        attendance_readiness,
        "_collect_db_metrics",
        lambda _now: {
            "baseline_complete": True,
            "mirror_error": None,
            "mirror_age_seconds": 1.0,
            "last_full_sweep_age_seconds": 2.0,
            "open_rows_not_refreshed": 0,
            "last_sweep_deletion_count": 0,
            "recalc_queue_age_seconds": None,
            "recalc_queue_depth": 0,
            "failed_corrections": 0,
            "correction_retries_today": 0,
            "correction_verification_failures_today": 0,
            "failed_department_repairs": 0,
            "shadow_day_complete": True,
            "shadow_source_day": shadow_day,
            "shadow_source_binding": attendance_readiness._combined_shadow_source_binding(
                "old-source", "meter-source"
            ),
            "shadow_production_fingerprint": "meter-source",
        },
    )
    monkeypatch.setattr(
        attendance_readiness,
        "_shadow_source_fingerprint",
        lambda day: "new-source" if day == shadow_day else "unexpected",
    )
    strict_inputs = SimpleNamespace(segments=(), location_spans=())
    monkeypatch.setattr(
        attendance_readiness.production_history,
        "_strict_inputs_for_day",
        lambda *_args, **_kwargs: strict_inputs,
    )
    monkeypatch.setattr(
        attendance_readiness.production_history,
        "_strict_attribution_from_inputs",
        lambda *_args, **_kwargs: {},
    )
    monkeypatch.setattr(
        attendance_readiness.production_history,
        "_strict_unassigned_runs_from_inputs",
        lambda *_args, **_kwargs: (),
    )
    monkeypatch.setattr(
        attendance_readiness,
        "_compare_strict_to_current",
        lambda day, *_args, **_kwargs: attendance_readiness.ShadowComparison(
            day, NOW, True, 0.0, 0, 0.0, 0.0, None
        ),
    )
    monkeypatch.setattr(
        attendance_readiness,
        "_log_readiness_identifiers",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        attendance_readiness,
        "_snapshot_work_center_mapper",
        lambda: lambda _odoo_id: None,
    )

    inputs = attendance_readiness._collect_inputs(
        NOW,
        production_client="meter",
        frozen_shadow_day=shadow_day,
        frozen_shadow_fingerprint="meter-source",
    )
    report = attendance_readiness._report_from_inputs(
        inputs,
        cutover_at=None,
        now_utc=NOW,
    )

    assert inputs.shadow_day_complete is False
    assert "shadow_day_incomplete" in report.blockers


def test_historical_meter_change_invalidates_saved_complete_shadow_proof(monkeypatch):
    shadow_day = date(2026, 8, 31)
    local = "local-source"
    old_meter = "meter-old"
    monkeypatch.setattr(
        attendance_readiness,
        "_collect_db_metrics",
        lambda _now: {
            "baseline_complete": True,
            "mirror_error": None,
            "mirror_age_seconds": 1.0,
            "last_full_sweep_age_seconds": 2.0,
            "open_rows_not_refreshed": 0,
            "last_sweep_deletion_count": 0,
            "recalc_queue_age_seconds": None,
            "recalc_queue_depth": 0,
            "failed_corrections": 0,
            "correction_retries_today": 0,
            "correction_verification_failures_today": 0,
            "failed_department_repairs": 0,
            "shadow_day_complete": True,
            "shadow_source_day": shadow_day,
            "shadow_source_binding": attendance_readiness._combined_shadow_source_binding(
                local, old_meter
            ),
            "shadow_production_fingerprint": old_meter,
        },
    )
    monkeypatch.setattr(
        attendance_readiness,
        "_shadow_source_fingerprint",
        lambda _day: local,
    )
    monkeypatch.setattr(
        attendance_readiness,
        "_snapshot_work_center_mapper",
        lambda: lambda _odoo_id: None,
    )
    strict_inputs = SimpleNamespace(segments=(), location_spans=())
    monkeypatch.setattr(
        attendance_readiness.production_history,
        "_strict_inputs_for_day",
        lambda *_args, **_kwargs: strict_inputs,
    )
    monkeypatch.setattr(
        attendance_readiness.production_history,
        "_strict_attribution_from_inputs",
        lambda *_args, **_kwargs: {},
    )
    monkeypatch.setattr(
        attendance_readiness.production_history,
        "_strict_unassigned_runs_from_inputs",
        lambda *_args, **_kwargs: (),
    )
    monkeypatch.setattr(
        attendance_readiness,
        "_compare_strict_to_current",
        lambda day, *_args, **_kwargs: attendance_readiness.ShadowComparison(
            day, NOW, True, 0.0, 0, 0.0, 0.0, None
        ),
    )
    monkeypatch.setattr(
        attendance_readiness,
        "_log_readiness_identifiers",
        lambda *_args, **_kwargs: None,
    )

    inputs = attendance_readiness._collect_inputs(
        NOW,
        production_client="meter",
        frozen_shadow_day=shadow_day,
        frozen_shadow_fingerprint="meter-new",
    )

    assert inputs.shadow_day_complete is False


def test_snapshot_work_center_mapper_reads_pinned_db_not_process_cache(monkeypatch):
    monkeypatch.setattr(
        attendance_readiness.db,
        "query",
        lambda *_args, **_kwargs: [
            {"name": "Fresh Mapping", "odoo_work_center_id": 71}
        ],
    )
    monkeypatch.setattr(
        attendance_timeline.work_centers_store,
        "app_work_center_name_for_odoo_id",
        lambda _odoo_id: pytest.fail("readiness used the process-local TTL cache"),
    )

    mapper = attendance_readiness._snapshot_work_center_mapper()

    assert mapper(71) == "Fresh Mapping"
    assert mapper(999) is None


@pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="needs local Postgres")
def test_postgres_snapshot_mapper_ignores_a_stale_process_cache():
    from zira_dashboard import db, work_centers_store

    db.init_pool()
    odoo_id = 991399
    with db.cursor() as cur:
        cur.execute("DELETE FROM work_centers WHERE odoo_work_center_id = %s", (odoo_id,))
        cur.execute(
            "INSERT INTO work_centers (name, category, odoo_work_center_id) "
            "VALUES ('Task13 Stale Mapping', 'test', %s)",
            (odoo_id,),
        )
    work_centers_store._invalidate_caches()  # noqa: SLF001
    try:
        assert (
            work_centers_store.app_work_center_name_for_odoo_id(odoo_id)
            == "Task13 Stale Mapping"
        )
        db.execute(
            "UPDATE work_centers SET name = 'Task13 Fresh Mapping' "
            "WHERE odoo_work_center_id = %s",
            (odoo_id,),
        )
        assert (
            work_centers_store.app_work_center_name_for_odoo_id(odoo_id)
            == "Task13 Stale Mapping"
        )

        with db.read_snapshot():
            mapper = attendance_readiness._snapshot_work_center_mapper()
            assert mapper(odoo_id) == "Task13 Fresh Mapping"
    finally:
        db.execute("DELETE FROM work_centers WHERE odoo_work_center_id = %s", (odoo_id,))
        work_centers_store._invalidate_caches()  # noqa: SLF001


@pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="needs local Postgres")
def test_postgres_sync_source_read_does_not_reverse_activation_lock_order(monkeypatch):
    from zira_dashboard import attendance_sync, db

    db.init_pool()
    source_started = Event()
    activation_has_rollout = Event()
    results = []
    errors = []
    original_state = db.query(
        "SELECT last_incremental_started_at, last_incremental_completed_at, "
        "last_incremental_observed_at, last_error FROM odoo_attendance_sync_state "
        "WHERE singleton = TRUE"
    )[0]

    class Source:
        def fetch_attendance_changes(self, **_kwargs):
            source_started.set()
            assert activation_has_rollout.wait(timeout=5)
            return []

        def fetch_open_attendance_rows(self):
            return []

    monkeypatch.setattr(attendance_sync, "_source", Source())
    monkeypatch.setattr(attendance_sync, "_backend", attendance_sync._MirrorBackend())
    monkeypatch.setattr(
        attendance_sync,
        "_enqueue_department_repairs_after_sync",
        lambda *_args, **_kwargs: None,
    )

    def sync_run():
        try:
            results.append(attendance_sync.run_incremental_sync(now_utc=NOW))
        except Exception as exc:  # pragma: no cover - asserted in parent thread
            errors.append(exc)

    def activation_fence():
        try:
            assert source_started.wait(timeout=5)
            with db.cursor() as cur:
                attendance_location_policy.lock_rollout_decision_cur(cur)
                activation_has_rollout.set()
                cur.execute(
                    "LOCK TABLE odoo_attendance_sync_state IN SHARE MODE"
                )
        except Exception as exc:  # pragma: no cover - asserted in parent thread
            errors.append(exc)

    sync_thread = Thread(target=sync_run)
    activation_thread = Thread(target=activation_fence)
    sync_thread.start()
    activation_thread.start()
    sync_thread.join(timeout=10)
    activation_thread.join(timeout=10)
    try:
        assert not sync_thread.is_alive()
        assert not activation_thread.is_alive()
        assert errors == []
        assert len(results) == 1 and results[0].success is True
    finally:
        with db.cursor() as cur:
            cur.execute(
                "UPDATE odoo_attendance_sync_state SET "
                "last_incremental_started_at = %s, "
                "last_incremental_completed_at = %s, "
                "last_incremental_observed_at = %s, last_error = %s "
                "WHERE singleton = TRUE",
                (
                    original_state["last_incremental_started_at"],
                    original_state["last_incremental_completed_at"],
                    original_state["last_incremental_observed_at"],
                    original_state["last_error"],
                ),
            )


def test_source_fingerprint_covers_names_and_correction_events():
    source_sql = attendance_readiness._SOURCE_FINGERPRINT_SQL
    shadow_sql = attendance_readiness._SHADOW_SOURCE_FINGERPRINT_SQL
    strict_sql = production_history._STRICT_LOCAL_SOURCE_SQL

    assert "employee_name" in strict_sql
    assert "employee_name" in shadow_sql
    assert "person_name" in strict_sql
    assert "person_name" in shadow_sql
    assert "attendance_correction_job_events" in source_sql


def test_department_policy_lookup_is_constant_count_for_many_mirror_rows(monkeypatch):
    rows = [
        {"odoo_department_name": f"{index % 3 + 1} Production"}
        for index in range(100)
    ]
    query = MagicMock(
        return_value=[{"name": "Production", "requires_work_center": True}]
    )
    monkeypatch.setattr(attendance_timeline.db, "query", query)

    requirement = attendance_timeline._department_requirements_for_rows(rows)

    assert requirement("1 Production") is True
    assert requirement("2 Production") is True
    assert query.call_count == 1
    assert "name = ANY" in query.call_args.args[0]


def test_strict_rollout_reader_rejects_malformed_config(monkeypatch):
    monkeypatch.setattr(
        attendance_location_policy.app_settings,
        "get_setting",
        lambda _key: {"mode": "live", "cutover_at": "broken"},
    )

    with pytest.raises(ValueError):
        attendance_location_policy.get_rollout_config_strict()


def test_busy_activation_claim_skips_expensive_report(monkeypatch):
    @contextmanager
    def busy_claim():
        yield False

    monkeypatch.setattr(attendance_readiness, "_activation_claim", busy_claim)
    monkeypatch.setattr(
        attendance_readiness,
        "build_report",
        lambda *_args, **_kwargs: pytest.fail("loser cannot build readiness"),
    )

    assert attendance_readiness.activate_due_cutover(NOW) == "busy"


def test_local_cutover_parser_rejects_dst_gap_and_repeated_hour():
    with pytest.raises(ValueError, match="cutover_invalid_local_time"):
        attendance_readiness.parse_local_cutover("2026-03-08T02:30")
    with pytest.raises(ValueError, match="cutover_ambiguous_local_time"):
        attendance_readiness.parse_local_cutover("2026-11-01T01:30")


def test_cutover_rejects_non_workday(monkeypatch):
    sunday = datetime(2026, 9, 6, 6, 0, tzinfo=attendance_readiness.shift_config.SITE_TZ)
    monkeypatch.setattr(
        attendance_readiness.shift_config,
        "shift_start_for",
        lambda _day: sunday.time().replace(tzinfo=None),
    )
    monkeypatch.setattr(attendance_readiness.shift_config, "is_workday", lambda _day: False)
    monkeypatch.setattr(
        attendance_readiness.shift_config,
        "snapshot_for",
        lambda day, **_kwargs: SimpleNamespace(
            day=day,
            shift_start=sunday.time().replace(tzinfo=None),
            shift_end=datetime.min.time().replace(hour=15),
            breaks=(),
            is_workday=False,
        ),
    )

    with pytest.raises(ValueError, match="cutover_workday_required"):
        attendance_readiness.validate_cutover(sunday, now_utc=NOW)
