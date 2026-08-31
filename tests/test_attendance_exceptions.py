from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
import pytest

from zira_dashboard import (
    attendance_exceptions,
    attendance_location_policy,
    attendance_mirror,
    attendance_timeline,
    production_history,
    wc_attributions,
)
from zira_dashboard.production_segments import UnassignedRun


DAY = date(2026, 8, 31)
NOW = datetime(2026, 8, 31, 13, 0, tzinfo=UTC)


def _health(*, verified=NOW, baseline=NOW - timedelta(hours=2), error=None):
    return attendance_mirror.MirrorHealth(
        last_incremental_completed_at=verified,
        last_full_sweep_completed_at=NOW - timedelta(minutes=20),
        baseline_completed_at=baseline,
        oldest_recalc_requested_at=None,
        last_error=error,
    )


def _config(mode="shadow"):
    return attendance_location_policy.RolloutConfig(
        mode=mode,
        cutover_at=NOW - timedelta(hours=1) if mode == "live" else NOW + timedelta(days=1),
        live_gate=None,
    )


def _span(
    status,
    start,
    end,
    *,
    employee_id=42,
    employee_name="Adrian A.",
    attendance_ids=(901,),
    app_wc=None,
    odoo_wc_id=None,
    odoo_wc_name=None,
):
    return attendance_timeline.LocationSpan(
        employee_odoo_id=employee_id,
        employee_name=employee_name,
        start_utc=start,
        end_utc=end,
        status=status,
        app_work_center_name=app_wc,
        odoo_work_center_id=odoo_wc_id,
        odoo_work_center_name=odoo_wc_name,
        attendance_ids=attendance_ids,
        department_repair=None,
    )


def _raw(attendance_id, *, wc_id=None, wc_name=None, check_in=None, check_out=None):
    return {
        "odoo_attendance_id": attendance_id,
        "employee_odoo_id": 42,
        "employee_name": "Adrian A.",
        "check_in_utc": check_in or NOW - timedelta(minutes=10),
        "check_out_utc": check_out,
        "odoo_work_center_id": wc_id,
        "odoo_work_center_name": wc_name,
        "odoo_department_id": 8,
        "odoo_department_name": "Production",
        "odoo_write_date": NOW - timedelta(minutes=1),
    }


@pytest.fixture
def source(monkeypatch):
    state = {"spans": (), "rows": (), "health": _health(), "repairs": ()}
    monkeypatch.setattr(attendance_mirror, "health_snapshot", lambda: state["health"])
    monkeypatch.setattr(attendance_location_policy, "get_rollout_config", lambda: _config("shadow"))
    monkeypatch.setattr(
        attendance_location_policy, "match_state_for_day", lambda *_a, **_k: "legacy"
    )
    monkeypatch.setattr(attendance_timeline, "timeline_for_range", lambda *_a, **_k: state["spans"])
    monkeypatch.setattr(attendance_mirror, "rows_overlapping", lambda *_a, **_k: state["rows"])
    monkeypatch.setattr(
        attendance_exceptions, "_failed_department_repairs", lambda *_a: state["repairs"]
    )
    monkeypatch.setattr(production_history, "unassigned_runs_for_day", lambda *_a, **_k: ())
    monkeypatch.setattr(
        wc_attributions,
        "shadow_unassigned_runs_for_day",
        lambda *_a, **_k: production_history.unassigned_runs_for_day(*_a, **_k),
    )
    return state


def _one(snapshot, kind):
    return next(issue for issue in snapshot.issues if issue.kind == kind)


def test_first_location_grace_changes_urgency_without_changing_key(source):
    start = NOW - timedelta(minutes=4)
    source["rows"] = (_raw(901, check_in=start),)
    source["spans"] = (_span("pending_first_location", start, NOW),)

    pending = _one(
        attendance_exceptions.build_snapshot(DAY, now_utc=NOW), "attendance_missing_location"
    )

    later = NOW + timedelta(minutes=3)
    source["health"] = _health(verified=later)
    source["spans"] = (
        _span("pending_first_location", start, start + timedelta(minutes=5)),
        _span("missing_required_location", start + timedelta(minutes=5), later),
    )
    urgent = _one(
        attendance_exceptions.build_snapshot(DAY, now_utc=later), "attendance_missing_location"
    )

    assert (
        pending.item_key
        == urgent.item_key
        == ("attendance_missing_location:42:901:2026-08-31T12:56:00+00:00")
    )
    assert pending.priority == "warn"
    assert urgent.priority == "urgent"
    assert pending.start_utc == urgent.start_utc == start
    assert pending.end_utc == NOW
    assert urgent.end_utc == later
    assert urgent.employee_odoo_id == 42
    assert urgent.employee_name == "Adrian A."
    assert urgent.attendance_ids == (901,)


def test_first_pending_gap_that_closed_inside_grace_is_not_actionable(source):
    start = NOW - timedelta(minutes=4)
    source["rows"] = (_raw(901, check_in=start), _raw(902, wc_id=7, wc_name="Dismantler 1"))
    source["spans"] = (
        _span("pending_first_location", start, start + timedelta(minutes=2)),
        _span(
            "valid",
            start + timedelta(minutes=2),
            NOW,
            attendance_ids=(902,),
            app_wc="Dismantler 1",
            odoo_wc_id=7,
            odoo_wc_name="07 Dismantler 1",
        ),
    )

    snapshot = attendance_exceptions.build_snapshot(DAY, now_utc=NOW)

    assert not [i for i in snapshot.issues if i.kind == "attendance_missing_location"]


def test_later_missing_gap_is_urgent_immediately(source):
    gap_start = NOW - timedelta(minutes=1)
    source["rows"] = (_raw(901), _raw(902, wc_id=7, wc_name="Dismantler 1"))
    source["spans"] = (
        _span(
            "valid",
            NOW - timedelta(minutes=20),
            gap_start,
            attendance_ids=(902,),
            app_wc="Dismantler 1",
            odoo_wc_id=7,
            odoo_wc_name="07 Dismantler 1",
        ),
        _span("missing_required_location", gap_start, NOW),
    )

    issue = _one(
        attendance_exceptions.build_snapshot(DAY, now_utc=NOW), "attendance_missing_location"
    )

    assert issue.priority == "urgent"
    assert issue.start_utc == gap_start
    assert issue.reason == "required_work_center_missing_after_location"


def test_missing_gap_after_conflicting_raw_work_centers_is_already_later_and_urgent(source):
    conflict_end = NOW - timedelta(minutes=1)
    source["rows"] = (
        _raw(901, wc_id=7, wc_name="Dismantler 1"),
        _raw(902, wc_id=9, wc_name="Repair 1"),
        _raw(903, check_in=conflict_end),
    )
    source["spans"] = (
        _span(
            "conflicting_location",
            NOW - timedelta(minutes=10),
            conflict_end,
            attendance_ids=(901, 902),
        ),
        _span(
            "missing_required_location",
            conflict_end,
            NOW,
            attendance_ids=(903,),
        ),
    )

    issue = _one(
        attendance_exceptions.build_snapshot(DAY, now_utc=NOW),
        "attendance_missing_location",
    )

    assert issue.priority == "urgent"
    assert issue.reason == "required_work_center_missing_after_location"


def test_unmapped_conflicting_and_duplicate_issues_keep_raw_odoo_identity(source):
    start = NOW - timedelta(minutes=10)
    source["rows"] = (
        _raw(901, wc_id=7, wc_name="07 Dismantler One", check_in=start),
        _raw(902, wc_id=9, wc_name="Luke Custom Cell", check_in=start),
        _raw(903, wc_id=7, wc_name="07 Dismantler One", check_in=start),
    )
    source["spans"] = (
        _span(
            "unmapped_location",
            start,
            NOW,
            attendance_ids=(902,),
            odoo_wc_id=9,
            odoo_wc_name="Luke Custom Cell",
        ),
        _span("conflicting_location", start, NOW, attendance_ids=(901, 902)),
        _span(
            "valid",
            start,
            NOW,
            attendance_ids=(901, 903),
            app_wc="Dismantler 1",
            odoo_wc_id=7,
            odoo_wc_name="07 Dismantler One",
        ),
    )

    snapshot = attendance_exceptions.build_snapshot(DAY, now_utc=NOW)
    unmapped = _one(snapshot, "attendance_unmapped_location")
    conflict = _one(snapshot, "attendance_conflicting_location")
    duplicate = _one(snapshot, "attendance_duplicate_location")

    assert unmapped.raw_work_center_labels == ("Luke Custom Cell",)
    assert unmapped.odoo_work_center_ids == (9,)
    assert unmapped.priority == "urgent"
    assert conflict.attendance_ids == (901, 902)
    assert conflict.raw_work_center_labels == ("07 Dismantler One", "Luke Custom Cell")
    assert conflict.odoo_work_center_ids == (7, 9)
    assert conflict.affected_workers == ((42, "Adrian A."),)
    assert duplicate.app_work_center_name == "Dismantler 1"
    assert duplicate.attendance_ids == (901, 903)
    assert duplicate.priority == "muted"
    assert duplicate.reason == "same_work_center_duplicate_overlap"


def test_day_clock_row_plus_one_work_center_row_is_not_a_duplicate(source):
    start = NOW - timedelta(minutes=10)
    source["rows"] = (
        _raw(901, check_in=start),
        _raw(902, wc_id=7, wc_name="07 Dismantler One", check_in=start),
    )
    source["spans"] = (
        _span(
            "valid",
            start,
            NOW,
            attendance_ids=(901, 902),
            app_wc="Dismantler 1",
            odoo_wc_id=7,
            odoo_wc_name="07 Dismantler One",
        ),
    )

    snapshot = attendance_exceptions.build_snapshot(DAY, now_utc=NOW)

    assert not [i for i in snapshot.issues if i.kind == "attendance_duplicate_location"]


def test_failed_department_repair_is_urgent_and_keeps_failure_detail(source):
    start = NOW - timedelta(minutes=10)
    source["rows"] = (_raw(901, wc_id=7, wc_name="07 Dismantler One", check_in=start),)
    source["repairs"] = (
        {
            "odoo_attendance_id": 901,
            "employee_odoo_id": 42,
            "employee_name": "Adrian A.",
            "check_in_utc": start,
            "check_out_utc": None,
            "odoo_work_center_id": 7,
            "odoo_work_center_name": "07 Dismantler One",
            "target_odoo_department_id": 8,
            "last_error": "Odoo did not keep the department",
        },
    )

    issue = _one(
        attendance_exceptions.build_snapshot(DAY, now_utc=NOW),
        "attendance_department_repair_failed",
    )

    assert issue.priority == "urgent"
    assert issue.attendance_ids == (901,)
    assert issue.reason == "Odoo did not keep the department"
    assert issue.target_odoo_department_id == 8


def test_stale_source_is_one_stable_urgent_item_and_snapshot_is_incomplete(source):
    verified = NOW - timedelta(seconds=91)
    source["health"] = _health(verified=verified)

    first = attendance_exceptions.build_snapshot(DAY, now_utc=NOW)
    later = attendance_exceptions.build_snapshot(DAY, now_utc=NOW + timedelta(minutes=3))
    stale = _one(first, "attendance_source_stale")

    assert stale.item_key == _one(later, "attendance_source_stale").item_key
    assert stale.item_key == "attendance_source_stale:odoo_attendance_mirror"
    assert stale.start_utc == verified + timedelta(seconds=90)
    assert stale.end_utc == NOW
    assert stale.priority == "urgent"
    assert first.complete is False
    assert first.fresh is False


@pytest.mark.parametrize(
    ("age_seconds", "expected_fresh"),
    [(89, True), (91, False)],
)
def test_baseline_only_freshness_uses_baseline_timestamp(source, age_seconds, expected_fresh):
    baseline = NOW - timedelta(seconds=age_seconds)
    source["health"] = _health(verified=None, baseline=baseline)

    snapshot = attendance_exceptions.build_snapshot(DAY, now_utc=NOW)
    stale = snapshot.issues_for("attendance_source_stale")

    assert snapshot.fresh is expected_fresh
    assert bool(stale) is not expected_fresh
    if stale:
        assert stale[0].start_utc == baseline + timedelta(seconds=90)


def test_strict_production_source_failure_becomes_urgent_without_aggregate_guess(
    source, monkeypatch
):
    monkeypatch.setattr(
        production_history,
        "unassigned_runs_for_day",
        lambda *_a, **_k: (_ for _ in ()).throw(
            production_history.ProductionSourceUnavailable("timestamped samples do not match")
        ),
    )

    snapshot = attendance_exceptions.build_snapshot(DAY, now_utc=NOW)
    issue = _one(snapshot, "production_source_unavailable")

    assert issue.item_key == "production_source_unavailable:2026-08-31"
    assert issue.reason == "timestamped samples do not match"
    assert issue.priority == "urgent"
    assert snapshot.complete is False
    assert "Strict Production" in snapshot.source_errors


@pytest.mark.parametrize("failure_boundary", ["client", "projection"])
def test_all_production_projection_failures_create_the_day_keyed_urgent_item(
    source, monkeypatch, failure_boundary
):
    if failure_boundary == "client":
        monkeypatch.setattr(
            attendance_exceptions,
            "_shared_production_client",
            lambda: (_ for _ in ()).throw(RuntimeError("shared client unavailable")),
        )
    else:
        malformed = SimpleNamespace(
            wc_name="Dismantler 1",
            start_utc=None,
            end_utc=None,
            units=4,
            sample_count=1,
        )
        monkeypatch.setattr(
            wc_attributions,
            "shadow_unassigned_runs_for_day",
            lambda *_a, **_k: (malformed,),
        )

    snapshot = attendance_exceptions.build_snapshot(DAY, now_utc=NOW)
    issue = _one(snapshot, "production_source_unavailable")

    assert issue.item_key == "production_source_unavailable:2026-08-31"
    assert issue.priority == "urgent"
    assert issue.reason
    assert snapshot.complete is False
    assert "Strict Production" in snapshot.source_errors


def test_distinct_unassigned_runs_keep_exact_units_samples_and_boundaries(source, monkeypatch):
    runs = (
        UnassignedRun(
            "Dismantler 1", NOW - timedelta(minutes=20), NOW - timedelta(minutes=18), 8.5, 3
        ),
        UnassignedRun(
            "Dismantler 1", NOW - timedelta(minutes=5), NOW - timedelta(minutes=4), 2.0, 1
        ),
    )
    monkeypatch.setattr(production_history, "unassigned_runs_for_day", lambda *_a, **_k: runs)

    snapshot = attendance_exceptions.build_snapshot(DAY, now_utc=NOW)
    issues = [i for i in snapshot.issues if i.kind == "production_unassigned_run"]

    assert [i.units for i in issues] == [8.5, 2.0]
    assert [i.sample_count for i in issues] == [3, 1]
    assert [i.start_utc for i in issues] == [run.start_utc for run in runs]
    assert [i.end_utc for i in issues] == [run.end_utc for run in runs]
    assert len({i.item_key for i in issues}) == 2


def test_shadow_mode_computes_run_comparisons_without_changing_legacy_actions(source, monkeypatch):
    run = UnassignedRun(
        "Dismantler 1", NOW - timedelta(minutes=5), NOW - timedelta(minutes=4), 2.0, 1
    )
    monkeypatch.setattr(
        attendance_location_policy, "match_state_for_day", lambda *_a, **_k: "legacy"
    )
    monkeypatch.setattr(
        wc_attributions,
        "shadow_unassigned_runs_for_day",
        lambda *_a, **_k: (run,),
    )

    snapshot = attendance_exceptions.build_snapshot(DAY, now_utc=NOW)
    issue = _one(snapshot, "production_unassigned_run")

    assert snapshot.production_mode == "shadow"
    assert issue.comparison_only is True
    assert issue.item_key == ("production_unassigned_run:Dismantler 1:2026-08-31T12:55:00+00:00")


def test_already_strict_day_in_shadow_is_not_labeled_as_a_comparison(source, monkeypatch):
    run = UnassignedRun(
        "Dismantler 1", NOW - timedelta(minutes=5), NOW - timedelta(minutes=4), 2.0, 1
    )
    monkeypatch.setattr(
        attendance_location_policy, "match_state_for_day", lambda *_a, **_k: "strict"
    )
    monkeypatch.setattr(production_history, "unassigned_runs_for_day", lambda *_a, **_k: (run,))

    snapshot = attendance_exceptions.build_snapshot(DAY, now_utc=NOW)
    issue = _one(snapshot, "production_unassigned_run")

    assert snapshot.production_mode == "strict"
    assert issue.comparison_only is False


def test_shadow_run_source_uses_strict_samples_segments_and_active_run_boundaries(
    monkeypatch,
):
    t0 = NOW - timedelta(minutes=10)
    t1 = t0 + timedelta(minutes=1)
    t2 = t0 + timedelta(minutes=5)
    inputs = SimpleNamespace(
        samples_by_wc={"Dismantler 1": [(t0, 1.0), (t1, 2.0), (t2, 3.0)]},
        break_windows=(),
        testing_windows={},
        breakdown_windows={},
        active_intervals_by_wc={
            "Dismantler 1": (
                (t0, t1 + timedelta(seconds=1)),
                (t2, t2 + timedelta(minutes=1)),
            )
        },
        segments=(
            SimpleNamespace(
                wc_name="Dismantler 1", start_utc=t1, end_utc=t1 + timedelta(seconds=1)
            ),
        ),
    )
    monkeypatch.setattr(production_history, "_strict_inputs_for_day", lambda *_a, **_k: inputs)
    monkeypatch.setattr(
        production_history,
        "_strict_shift_bounds",
        lambda _day: (t0, t2 + timedelta(hours=1)),
    )

    runs = wc_attributions.shadow_unassigned_runs_for_day(DAY, object(), now_utc=NOW)

    assert [(run.start_utc, run.units, run.sample_count) for run in runs] == [
        (t0, 1.0, 1),
        (t2, 3.0, 1),
    ]


def test_unassigned_run_keeps_overlapping_worker_identity(source, monkeypatch):
    run_start = NOW - timedelta(minutes=5)
    source["rows"] = (_raw(901, check_in=run_start),)
    source["spans"] = (_span("missing_required_location", run_start, NOW),)
    monkeypatch.setattr(
        production_history,
        "unassigned_runs_for_day",
        lambda *_a, **_k: (UnassignedRun("Dismantler 1", run_start, run_start, 2.0, 1),),
    )

    issue = _one(
        attendance_exceptions.build_snapshot(DAY, now_utc=NOW),
        "production_unassigned_run",
    )

    assert issue.affected_workers == ((42, "Adrian A."),)


def test_recent_sync_error_is_incomplete_but_not_called_stale_before_threshold(source):
    source["health"] = _health(
        verified=NOW - timedelta(seconds=20), error="incremental: Odoo unavailable"
    )

    snapshot = attendance_exceptions.build_snapshot(DAY, now_utc=NOW)

    assert snapshot.complete is False
    assert "Attendance Timeline" in snapshot.source_errors
    assert not [i for i in snapshot.issues if i.kind == "attendance_source_stale"]


def test_strict_state_survives_stale_attendance_projection(source, monkeypatch):
    source["health"] = _health(verified=NOW - timedelta(seconds=91))
    monkeypatch.setattr(
        attendance_location_policy,
        "match_state_for_day",
        lambda *_a, **_k: "strict",
    )
    monkeypatch.setattr(
        production_history,
        "unassigned_runs_for_day",
        lambda *_a, **_k: pytest.fail("stale projection queried strict runs"),
    )

    snapshot = attendance_exceptions.build_snapshot(DAY, now_utc=NOW)

    assert snapshot.production_mode == "strict"
    assert snapshot.fresh is False
    assert snapshot.complete is False
    assert snapshot.issues_for("attendance_source_stale")


def test_strict_state_survives_attendance_health_failure(source, monkeypatch):
    monkeypatch.setattr(
        attendance_location_policy,
        "match_state_for_day",
        lambda *_a, **_k: "strict",
    )
    monkeypatch.setattr(
        attendance_mirror,
        "health_snapshot",
        lambda: (_ for _ in ()).throw(RuntimeError("mirror unavailable")),
    )

    snapshot = attendance_exceptions.build_snapshot(DAY, now_utc=NOW)

    assert snapshot.production_mode == "strict"
    assert snapshot.complete is False
    assert "Attendance Timeline" in snapshot.source_errors


def test_unexpected_strict_read_failure_still_creates_source_unavailable_item(source, monkeypatch):
    monkeypatch.setattr(
        production_history,
        "unassigned_runs_for_day",
        lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("Zira cache unavailable")),
    )

    snapshot = attendance_exceptions.build_snapshot(DAY, now_utc=NOW)

    issue = _one(snapshot, "production_source_unavailable")
    assert issue.reason == "Zira cache unavailable"
    assert snapshot.complete is False


def test_failed_repairs_are_scoped_to_the_requested_day(monkeypatch):
    captured = {}

    def query(sql, params):
        captured["sql"] = sql
        captured["params"] = params
        return []

    monkeypatch.setattr(attendance_exceptions.db, "query", query)
    start = NOW - timedelta(hours=6)
    end = NOW + timedelta(hours=6)

    assert attendance_exceptions._failed_department_repairs(start, end) == ()
    assert "check_in_utc < %s" in captured["sql"]
    assert "check_out_utc IS NULL OR m.check_out_utc > %s" in captured["sql"]
    assert captured["params"] == (end, start)


def test_off_or_incomplete_baseline_does_not_claim_timeline_signal(source, monkeypatch):
    monkeypatch.setattr(attendance_location_policy, "get_rollout_config", lambda: _config("off"))
    off = attendance_exceptions.build_snapshot(DAY, now_utc=NOW)
    assert off.mode == "off"
    assert off.issues == ()
    assert off.complete is False

    monkeypatch.setattr(attendance_location_policy, "get_rollout_config", lambda: _config("shadow"))
    source["health"] = _health(baseline=None)
    incomplete = attendance_exceptions.build_snapshot(DAY, now_utc=NOW)
    assert incomplete.baseline_complete is False
    assert incomplete.issues == ()
    assert incomplete.complete is False


def test_snapshot_rejects_naive_now(source):
    with pytest.raises(ValueError, match="timezone-aware"):
        attendance_exceptions.build_snapshot(DAY, now_utc=datetime(2026, 8, 31, 8, 0))
