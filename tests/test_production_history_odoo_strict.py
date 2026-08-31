"""Strict production attribution from the durable Odoo location timeline."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import UTC, date, datetime, time
import os
from threading import Event
from types import SimpleNamespace

import pytest

from zira_dashboard import (
    assignment_windows,
    attendance_location_policy,
    attendance_mirror,
    attendance_timeline,
    precompute,
    production_history,
    shift_config,
    staffing,
    timeclock_windows,
    wc_attributions,
)
from zira_dashboard.assignment_windows import WorkSegment


DAY = date(2026, 8, 20)


_needs_postgres = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"), reason="needs local Postgres"
)


def at(hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 8, 20, hour, minute, tzinfo=UTC)


def span(
    status: str,
    start: datetime,
    end: datetime,
    *,
    employee_id: int = 41,
    name: str = "Alex",
    wc_name: str | None = "Repair 4",
) -> attendance_timeline.LocationSpan:
    return attendance_timeline.LocationSpan(
        employee_odoo_id=employee_id,
        employee_name=name,
        start_utc=start,
        end_utc=end,
        status=status,
        app_work_center_name=wc_name,
        odoo_work_center_id=404 if wc_name else None,
        odoo_work_center_name=wc_name,
        attendance_ids=(employee_id * 10,),
        department_repair=None,
    )


def health(
    *, baseline: datetime | None = at(11), verified: datetime | None = at(20)
) -> attendance_mirror.MirrorHealth:
    return attendance_mirror.MirrorHealth(
        last_incremental_completed_at=verified,
        last_full_sweep_completed_at=at(20),
        baseline_completed_at=baseline,
        oldest_recalc_requested_at=None,
        last_error=None,
    )


@pytest.fixture
def strict_sources(monkeypatch):
    monkeypatch.setattr(
        attendance_location_policy,
        "match_state_for_day",
        lambda _day, **_kwargs: "strict",
    )
    monkeypatch.setattr(attendance_mirror, "health_snapshot", health)
    monkeypatch.setattr(shift_config, "shift_start_for", lambda _day: time(7))
    monkeypatch.setattr(shift_config, "shift_end_for", lambda _day: time(15))
    monkeypatch.setattr(
        shift_config,
        "productive_minutes_in_window",
        lambda _day, start, end: (end - start).total_seconds() / 60,
    )
    monkeypatch.setattr(wc_attributions, "testing_windows_for_day", lambda _day: {})
    monkeypatch.setattr(
        production_history, "_excluded_minutes_by_person_wc", lambda *_args: {}
    )
    monkeypatch.setattr(
        attendance_mirror,
        "enqueue_recalc",
        lambda *_args, **_kwargs: None,
    )


def test_only_valid_timeline_spans_become_clipped_odoo_work_segments():
    spans = (
        span("valid", at(11, 30), at(13)),
        span("missing_required_location", at(13), at(14), employee_id=42),
        span("conflicting_location", at(14), at(15), employee_id=43),
        span("unmapped_location", at(15), at(16), employee_id=44, wc_name=None),
        span("stale_open_location", at(16), at(17), employee_id=45),
    )

    assert assignment_windows.work_segments_from_timeline(
        spans,
        window_start_utc=at(12),
        window_end_utc=at(14),
    ) == (
        WorkSegment("Repair 4", "Alex", at(12), at(13), "odoo", 41),
    )


def test_strict_day_ignores_schedule_and_manual_attribution(
    monkeypatch, strict_sources
):
    monkeypatch.setattr(
        staffing,
        "load_schedule",
        lambda _day: pytest.fail("strict attribution read the schedule"),
    )
    monkeypatch.setattr(
        timeclock_windows,
        "attendance_windows_for_day_with_availability",
        lambda _day: pytest.fail("strict attribution read legacy attendance"),
    )
    monkeypatch.setattr(
        wc_attributions,
        "creditable_for_day",
        lambda _day: pytest.fail("strict attribution read manual attribution"),
    )
    monkeypatch.setattr(
        attendance_timeline,
        "timeline_for_range",
        lambda *_args, **_kwargs: (span("valid", at(12), at(13)),),
    )
    monkeypatch.setattr(
        production_history,
        "_fetch_wc_totals",
        lambda *_args: {"Repair 4": (20, 0)},
    )
    monkeypatch.setattr(
        production_history,
        "_fetch_wc_samples",
        lambda *_args: {"Repair 4": [(at(12, 30), 20)]},
    )

    out = production_history.attribution_for(DAY, object())

    assert out[(41, "Alex")]["Repair 4"]["units"] == 20


def test_pre_cutover_day_retains_hybrid_resolution(monkeypatch):
    schedule = staffing.Schedule(
        day=DAY, published=True, assignments={"Repair 4": ["Scheduled"]}
    )
    monkeypatch.setattr(
        attendance_location_policy,
        "match_state_for_day",
        lambda _day, **_kwargs: "legacy",
    )
    monkeypatch.setattr(staffing, "load_schedule", lambda _day: schedule)
    monkeypatch.setattr(
        timeclock_windows,
        "attendance_windows_for_day_with_availability",
        lambda _day: ({}, True),
    )
    monkeypatch.setattr(wc_attributions, "people_by_wc", lambda _day: {})
    monkeypatch.setattr(wc_attributions, "testing_windows_for_day", lambda _day: {})
    monkeypatch.setattr(
        production_history,
        "_fetch_wc_totals",
        lambda *_args: {"Repair 4": (20, 0)},
    )
    monkeypatch.setattr(production_history, "_elapsed_minutes_for", lambda _day: 480)
    monkeypatch.setattr(
        production_history, "_excluded_minutes_by_person_wc", lambda *_args: {}
    )

    out = production_history.attribution_for(DAY, object())

    assert out["Scheduled"]["Repair 4"]["units"] == 20


def test_due_unactivated_cutover_does_not_replace_snapshot(monkeypatch):
    monkeypatch.setattr(
        attendance_location_policy,
        "match_state_for_day",
        lambda _day, **_kwargs: "pending",
    )
    monkeypatch.setattr(
        production_history,
        "_fetch_wc_totals",
        lambda *_args: pytest.fail("pending cutover fetched production"),
    )
    monkeypatch.setattr(
        precompute,
        "upsert_production_daily",
        lambda *_args, **_kwargs: pytest.fail("pending cutover replaced snapshot"),
    )

    with pytest.raises(
        production_history.ProductionSourceUnavailable, match="cutover"
    ):
        precompute.precompute_day(DAY, object())


def test_cutover_state_change_during_compute_preserves_prior_snapshot(monkeypatch):
    from zira_dashboard import attendance

    states = iter(("legacy", "strict"))
    monkeypatch.setattr(
        production_history, "match_state_for_day", lambda _day: next(states)
    )
    monkeypatch.setattr(
        production_history,
        "attribution_for",
        lambda _day, _client: {
            "Scheduled": {
                "Repair 4": {
                    "units": 20,
                    "downtime": 0,
                    "hours": 8,
                    "days_worked": 1,
                }
            }
        },
    )
    monkeypatch.setattr(attendance, "name_to_person_id", lambda: {})
    monkeypatch.setattr(
        precompute,
        "upsert_production_daily",
        lambda *_args, **_kwargs: pytest.fail("mixed-state snapshot was written"),
    )

    with pytest.raises(
        production_history.ProductionSourceUnavailable, match="changed during"
    ):
        precompute.precompute_day(DAY, object())


def test_strict_same_display_name_keeps_odoo_identities_distinct():
    segments = (
        WorkSegment("Repair 4", "Alex", at(12), at(13), "odoo", 41),
        WorkSegment("Repair 4", "Alex", at(12), at(13), "odoo", 42),
    )

    out = production_history.attribute_for_segments(
        segments,
        wc_totals={"Repair 4": (20, 4)},
        samples_by_wc={"Repair 4": [(at(12, 30), 20)]},
        productive_minutes=lambda *_args: 60,
        strict=True,
    )

    assert set(out) == {(41, "Alex"), (42, "Alex")}
    assert out[(41, "Alex")]["Repair 4"]["units"] == 10
    assert out[(42, "Alex")]["Repair 4"]["units"] == 10


def test_strict_invalid_and_gap_samples_are_unassigned_but_conserved():
    segments = (WorkSegment("Repair 4", "Alex", at(12), at(13), "odoo", 41),)

    credits = production_history.credit_work_segments(
        segments,
        wc_totals={"Repair 4": 60},
        samples_by_wc={
            "Repair 4": [(at(12, 30), 20), (at(13, 30), 40)]
        },
        productive_minutes=lambda *_args: 60,
        allow_total_fallback=False,
    )["Repair 4"]

    assert [(row.person_name, row.actual_units) for row in credits] == [
        ("Alex", 20),
        (None, 40),
    ]
    assert sum(row.actual_units for row in credits) == 60


def test_stale_open_suffix_withholds_credit_without_rejecting_verified_prefix(
    monkeypatch, strict_sources
):
    monkeypatch.setattr(
        attendance_timeline,
        "timeline_for_range",
        lambda *_args, **_kwargs: (
            span("valid", at(12), at(13)),
            span("stale_open_location", at(13), at(14)),
        ),
    )
    monkeypatch.setattr(
        production_history,
        "_fetch_wc_totals",
        lambda *_args: {"Repair 4": (30, 0)},
    )
    monkeypatch.setattr(
        production_history,
        "_fetch_wc_samples",
        lambda *_args: {
            "Repair 4": [(at(12, 30), 10), (at(13, 30), 20)]
        },
    )

    out = production_history.attribution_for(DAY, object())

    assert out[(41, "Alex")]["Repair 4"]["units"] == 10


def test_strict_total_without_matching_samples_queues_and_preserves_snapshot(
    monkeypatch, strict_sources
):
    queued = []
    monkeypatch.setattr(
        attendance_mirror,
        "enqueue_recalc",
        lambda days, reason, *, mark_strict: queued.append(
            (tuple(days), reason, mark_strict)
        ),
    )
    monkeypatch.setattr(
        attendance_timeline,
        "timeline_for_range",
        lambda *_args, **_kwargs: (span("valid", at(12), at(13)),),
    )
    monkeypatch.setattr(
        production_history,
        "_fetch_wc_totals",
        lambda *_args: {"Repair 4": (25, 0)},
    )
    monkeypatch.setattr(
        production_history,
        "_fetch_wc_samples",
        lambda *_args: {"Repair 4": [(at(12, 30), 20)]},
    )

    with pytest.raises(
        production_history.ProductionSourceUnavailable, match="timestamped"
    ):
        production_history.attribution_for(DAY, object())

    assert queued and queued[0][0] == (DAY,)
    assert queued[0][2] is True


def test_unassigned_runs_for_day_uses_strict_timeline_and_meter_run_boundaries(
    monkeypatch, strict_sources
):
    monkeypatch.setattr(
        attendance_timeline,
        "timeline_for_range",
        lambda *_args, **_kwargs: (span("valid", at(12), at(13)),),
    )
    monkeypatch.setattr(
        production_history,
        "_metered_leaderboard",
        lambda *_args: [
            SimpleNamespace(
                station=SimpleNamespace(name="Repair 4"),
                units=35,
                downtime_minutes=0,
                samples=(
                    (at(12, 30), 5),
                    (at(13, 5), 7),
                    (at(13, 10), 11),
                    (at(14), 13),
                ),
                active_intervals=(
                    (at(12), at(13, 30)),
                    (at(14), at(14, 30)),
                ),
            )
        ],
    )
    monkeypatch.setattr(shift_config, "breaks_for", lambda _day: ())
    monkeypatch.setattr(wc_attributions, "breakdown_windows_for_day", lambda _day: {})

    runs = production_history.unassigned_runs_for_day(DAY, object())

    assert runs == (
        production_history.UnassignedRun(
            "Repair 4", at(13, 5), at(13, 10), 18, 2
        ),
        production_history.UnassignedRun("Repair 4", at(14), at(14), 13, 1),
    )


@pytest.mark.parametrize("excluded_by", ["testing", "breakdown"])
def test_unassigned_runs_for_day_does_not_restore_excluded_samples(
    monkeypatch, strict_sources, excluded_by
):
    sample_time = at(12, 15)
    monkeypatch.setattr(
        attendance_timeline, "timeline_for_range", lambda *_args, **_kwargs: ()
    )
    monkeypatch.setattr(
        production_history,
        "_metered_leaderboard",
        lambda *_args: [
            SimpleNamespace(
                station=SimpleNamespace(name="Repair 4"),
                samples=((sample_time, 9),),
                active_intervals=((at(12), at(13)),),
            )
        ],
    )
    monkeypatch.setattr(shift_config, "breaks_for", lambda _day: ())
    monkeypatch.setattr(
        wc_attributions,
        "testing_windows_for_day",
        lambda _day: (
            {"Repair 4": [(at(12), at(12, 30))]}
            if excluded_by == "testing"
            else {}
        ),
    )
    monkeypatch.setattr(
        wc_attributions,
        "breakdown_windows_for_day",
        lambda _day: (
            {("Alex", "Repair 4"): [(at(12), at(12, 30))]}
            if excluded_by == "breakdown"
            else {}
        ),
    )

    assert production_history.unassigned_runs_for_day(DAY, object()) == ()


def test_strict_testing_offsets_and_breakdown_exclusions_remain(
    monkeypatch, strict_sources
):
    monkeypatch.setattr(
        attendance_timeline,
        "timeline_for_range",
        lambda *_args, **_kwargs: (span("valid", at(12), at(14)),),
    )
    monkeypatch.setattr(
        production_history,
        "_fetch_wc_totals",
        lambda *_args: {"Repair 4": (30, 0)},
    )
    monkeypatch.setattr(
        production_history,
        "_fetch_wc_samples",
        lambda *_args: {
            "Repair 4": [(at(12, 30), 10), (at(13, 30), 20)]
        },
    )
    monkeypatch.setattr(
        wc_attributions,
        "testing_windows_for_day",
        lambda _day: {"Repair 4": [(at(12), at(13))]},
    )
    monkeypatch.setattr(
        production_history,
        "_excluded_minutes_by_person_wc",
        lambda *_args: {"Alex": {"Repair 4": 15}},
    )

    out = production_history.attribution_for(DAY, object())

    totals = out[(41, "Alex")]["Repair 4"]
    assert totals["units"] == 20
    assert totals["excluded_minutes"] == 15


@pytest.mark.parametrize(
    ("mirror_health", "message"),
    [
        (health(baseline=None), "baseline"),
        (health(verified=None), "verified"),
    ],
)
def test_strict_requires_completed_baseline_and_verified_snapshot(
    monkeypatch, strict_sources, mirror_health, message
):
    monkeypatch.setattr(attendance_mirror, "health_snapshot", lambda: mirror_health)
    monkeypatch.setattr(
        attendance_timeline,
        "timeline_for_range",
        lambda *_args, **_kwargs: pytest.fail("unverified timeline was read"),
    )

    with pytest.raises(production_history.ProductionSourceUnavailable, match=message):
        production_history.attribution_for(DAY, object())


def test_strict_snapshot_that_loses_verification_is_source_unavailable(
    monkeypatch, strict_sources
):
    monkeypatch.setattr(
        attendance_timeline,
        "timeline_for_range",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("attendance mirror has no verified freshness")
        ),
    )

    with pytest.raises(
        production_history.ProductionSourceUnavailable, match="verified snapshot"
    ):
        production_history.attribution_for(DAY, object())


def test_flatten_attribution_writes_strict_odoo_identity_directly():
    rows = precompute.flatten_attribution(
        DAY,
        {
            (41, "Alex"): {
                "Repair 4": {
                    "units": 10,
                    "downtime": 0,
                    "hours": 1,
                    "days_worked": 1,
                }
            },
            (42, "Alex"): {
                "Repair 4": {
                    "units": 10,
                    "downtime": 0,
                    "hours": 1,
                    "days_worked": 1,
                }
            },
        },
        name_to_emp_id={"Alex": "legacy-collision"},
    )

    assert [(row["emp_id"], row["name"], row["units"]) for row in rows] == [
        ("41", "Alex", 10),
        ("42", "Alex", 10),
    ]


def test_strict_day_marker_and_snapshot_replace_share_one_transaction(monkeypatch):
    from zira_dashboard import db

    class Cursor:
        def __init__(self):
            self.statements = []

        def execute(self, sql, params=None):
            self.statements.append((" ".join(sql.split()), params))

    cursor = Cursor()

    @contextmanager
    def fake_cursor():
        yield cursor

    monkeypatch.setattr(db, "cursor", fake_cursor)
    monkeypatch.setattr(
        db,
        "execute_values",
        lambda cur, sql, rows, template=None: cur.statements.append(
            ("VALUES", tuple(rows))
        ),
    )

    precompute.upsert_production_daily(
        [
            {
                "day": DAY,
                "emp_id": "41",
                "name": "Alex",
                "wc_name": "Repair 4",
                "units": 10,
                "downtime": 0,
                "hours": 1,
                "days_worked": 1,
            }
        ],
        replace_days=(DAY,),
        strict_days=(DAY,),
    )

    assert "INSERT INTO attendance_strict_days" in cursor.statements[0][0]
    assert "DELETE FROM production_daily" in cursor.statements[1][0]


def test_precompute_marks_strict_recalculation_for_atomic_upsert(monkeypatch):
    from zira_dashboard import attendance

    captured = {}
    monkeypatch.setattr(
        production_history,
        "attribution_for",
        lambda _day, _client: {
            (41, "Alex"): {
                "Repair 4": {
                    "units": 10,
                    "downtime": 0,
                    "hours": 1,
                    "days_worked": 1,
                }
            }
        },
    )
    monkeypatch.setattr(
        production_history, "match_state_for_day", lambda _day: "strict"
    )
    monkeypatch.setattr(attendance, "name_to_person_id", lambda: {})
    monkeypatch.setattr(
        precompute,
        "upsert_production_daily",
        lambda rows, **kwargs: captured.update(
            {"rows": rows, "kwargs": kwargs}
        )
        or len(rows),
    )

    precompute.precompute_day(DAY, object())

    assert captured["kwargs"] == {
        "replace_days": (DAY,),
        "strict_days": (DAY,),
        "expected_match_states": {DAY: "strict"},
    }


@_needs_postgres
def test_cutover_flip_in_final_write_gap_preserves_prior_snapshot(monkeypatch):
    from zira_dashboard import attendance, db

    test_day = date(2098, 8, 20)
    prior = {
        "day": test_day,
        "emp_id": "old",
        "name": "Prior",
        "wc_name": "Repair 4",
        "units": 7,
        "downtime": 0,
        "hours": 1,
        "days_worked": 1,
    }
    db.init_pool()
    db.bootstrap_schema()
    db.execute("DELETE FROM attendance_strict_days WHERE day = %s", (test_day,))
    db.execute("DELETE FROM production_daily WHERE day = %s", (test_day,))
    precompute.upsert_production_daily([prior], replace_days=(test_day,))
    monkeypatch.setattr(
        production_history,
        "attribution_for",
        lambda _day, _client: {
            "New": {
                "Repair 4": {
                    "units": 99,
                    "downtime": 0,
                    "hours": 1,
                    "days_worked": 1,
                }
            }
        },
    )
    monkeypatch.setattr(attendance, "name_to_person_id", lambda: {"New": "new"})
    real_upsert = precompute.upsert_production_daily
    write_gap_open = Event()
    release_write = Event()

    def blocked_upsert(rows, **kwargs):
        write_gap_open.set()
        if not release_write.wait(timeout=5):
            raise TimeoutError("final write was not released")
        return real_upsert(rows, **kwargs)

    monkeypatch.setattr(precompute, "upsert_production_daily", blocked_upsert)
    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            pending = executor.submit(precompute.precompute_day, test_day, object())
            assert write_gap_open.wait(timeout=5)
            db.execute(
                "INSERT INTO attendance_strict_days "
                "(day, reason, source_changed_at) VALUES (%s, %s, %s)",
                (test_day, "concurrent cutover", at(12)),
            )
            release_write.set()
            with pytest.raises(
                production_history.ProductionSourceUnavailable,
                match="changed before snapshot commit",
            ):
                pending.result(timeout=5)

        stored = db.query(
            "SELECT emp_id, name, units FROM production_daily WHERE day = %s",
            (test_day,),
        )
        assert [dict(row) for row in stored] == [
            {"emp_id": "old", "name": "Prior", "units": 7}
        ]
        assert db.query(
            "SELECT day FROM attendance_strict_days WHERE day = %s", (test_day,)
        ) == [{"day": test_day}]
    finally:
        release_write.set()
        db.execute("DELETE FROM production_daily WHERE day = %s", (test_day,))
        db.execute("DELETE FROM attendance_strict_days WHERE day = %s", (test_day,))
