from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, date, datetime, time, timedelta
import os
from threading import Event, Thread
from types import SimpleNamespace

import pytest

from zira_dashboard import assignment_windows, precompute, production_history
from zira_dashboard.attendance_timeline import LocationSpan
from zira_dashboard.production_history import ProductionSourceUnavailable


DAY = date(2026, 8, 20)
START = datetime(2026, 8, 20, 12, tzinfo=UTC)
END = datetime(2026, 8, 20, 20, tzinfo=UTC)


_needs_postgres = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"), reason="needs local Postgres"
)


def at(hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 8, 20, hour, minute, tzinfo=UTC)


def work_segments_from_timeline(*args, **kwargs):
    function = getattr(assignment_windows, "work_segments_from_timeline", None)
    if function is None:
        pytest.fail("work_segments_from_timeline is not implemented")
    return function(*args, **kwargs)


def span(
    employee_id: int,
    name: str,
    start: datetime,
    end: datetime,
    *,
    status: str = "valid",
    wc: str | None = "Repair 4",
) -> LocationSpan:
    return LocationSpan(
        employee_odoo_id=employee_id,
        employee_name=name,
        start_utc=start,
        end_utc=end,
        status=status,
        app_work_center_name=wc,
        odoo_work_center_id=44 if wc else None,
        odoo_work_center_name=wc,
        attendance_ids=(employee_id,),
        department_repair=None,
    )


def station_total(
    *,
    units: float,
    samples: tuple[tuple[datetime, float], ...],
    active_intervals: tuple[tuple[datetime, datetime], ...] = ((START, END),),
    wc: str = "Repair 4",
    downtime: int = 0,
):
    return SimpleNamespace(
        station=SimpleNamespace(name=wc),
        units=units,
        reading_count=len(samples),
        truncated=False,
        downtime_minutes=downtime,
        active_minutes=480,
        last_reading_at=samples[-1][0] if samples else None,
        last_status="Working",
        samples=samples,
        active_intervals=active_intervals,
    )


def install_strict_dependencies(
    monkeypatch,
    *,
    spans: tuple[LocationSpan, ...],
    totals: tuple[SimpleNamespace, ...],
    baseline: datetime | None = START,
    verified: datetime | None = END,
    testing: dict | None = None,
    breakdown: dict | None = None,
):
    from zira_dashboard import (
        attendance_location_policy,
        attendance_mirror,
        attendance_timeline,
        db,
        shift_config,
        staffing,
        timeclock_windows,
        wc_attributions,
    )

    state_calls = []

    def state_for(day, *, now_utc=None):
        state_calls.append((day, now_utc))
        return "strict"

    monkeypatch.setattr(attendance_location_policy, "match_state_for_day", state_for)
    monkeypatch.setattr(
        attendance_mirror,
        "health_snapshot",
        lambda: SimpleNamespace(
            baseline_completed_at=baseline,
            last_incremental_completed_at=verified,
        ),
    )
    monkeypatch.setattr(
        attendance_timeline,
        "timeline_for_range",
        lambda start, end, **_kwargs: spans,
    )
    @contextmanager
    def source_snapshot():
        yield object()

    monkeypatch.setattr(db, "read_snapshot", source_snapshot)
    monkeypatch.setattr(
        production_history,
        "strict_source_snapshot",
        lambda _day, **_kwargs: SimpleNamespace(
            shift_start_utc=START,
            shift_end_utc=END,
            break_windows=(),
            stations=(),
            shift_by_day={},
            work_center_by_odoo_id={44: "Repair 4"},
            source_fingerprint="strict-test-source",
        ),
    )
    monkeypatch.setattr(
        production_history,
        "strict_local_source_fingerprint",
        lambda _day, **_kwargs: "strict-local-test",
    )
    monkeypatch.setattr(
        production_history, "_metered_leaderboard", lambda client, day, **kwargs: list(totals)
    )
    monkeypatch.setattr(wc_attributions, "testing_windows_for_day", lambda day: testing or {})
    monkeypatch.setattr(wc_attributions, "breakdown_windows_for_day", lambda day: breakdown or {})
    monkeypatch.setattr(shift_config, "shift_start_for", lambda day: time(7))
    monkeypatch.setattr(shift_config, "shift_end_for", lambda day: time(15))
    monkeypatch.setattr(shift_config, "breaks_for", lambda day: ())
    monkeypatch.setattr(
        shift_config,
        "productive_minutes_in_window",
        lambda day, start, end: (end - start).total_seconds() / 60,
    )
    monkeypatch.setattr(
        staffing,
        "load_schedule",
        lambda day: pytest.fail("strict attribution loaded a schedule"),
    )
    monkeypatch.setattr(
        timeclock_windows,
        "attendance_windows_for_day_with_availability",
        lambda day: pytest.fail("strict attribution loaded legacy attendance"),
    )
    monkeypatch.setattr(
        wc_attributions,
        "creditable_for_day",
        lambda day: pytest.fail("strict attribution loaded manual attribution"),
    )
    return state_calls


def test_work_segments_from_timeline_keeps_only_clipped_positive_valid_spans():
    spans = (
        span(1, "Ana", at(11), at(13)),
        span(2, "Bob", at(19), at(21)),
        span(3, "Cara", at(13), at(14), status="conflicting_location", wc=None),
        span(4, "Dan", at(10), at(11), status="valid"),
    )

    segments = work_segments_from_timeline(spans, window_start_utc=START, window_end_utc=END)

    assert [
        (
            segment.person_odoo_id,
            segment.person_name,
            segment.start_utc,
            segment.end_utc,
            segment.source,
        )
        for segment in segments
    ] == [
        (1, "Ana", START, at(13), "odoo"),
        (2, "Bob", at(19), END, "odoo"),
    ]


def test_strict_branch_is_chosen_once_and_splits_duplicate_names_by_odoo_id(monkeypatch):
    state_calls = install_strict_dependencies(
        monkeypatch,
        spans=(span(101, "Alex", START, END), span(202, "Alex", START, END)),
        totals=(station_total(units=40, samples=((at(13), 10), (at(14), 30))),),
    )

    result = production_history.attribution_for(DAY, object(), now_utc=END)

    assert len(state_calls) == 1
    assert result[(101, "Alex")]["Repair 4"]["units"] == 20.0
    assert result[(202, "Alex")]["Repair 4"]["units"] == 20.0
    assert sum(wcs["Repair 4"]["units"] for wcs in result.values()) == 40.0
    assert result.is_strict is True
    assert result.source_fingerprint == "strict-local-test"
    assert isinstance(result.request_fingerprint, str)
    assert result.request_fingerprint != result.source_fingerprint


def test_strict_request_fingerprint_changes_when_only_historical_meter_data_changes(
    monkeypatch,
):
    totals = [station_total(units=10, samples=((at(13), 10),))]
    install_strict_dependencies(
        monkeypatch,
        spans=(span(101, "Alex", START, END),),
        totals=totals,
    )

    first = production_history.attribution_for(DAY, object(), now_utc=END)
    totals[:] = [station_total(units=20, samples=((at(13), 20),))]
    second = production_history.attribution_for(DAY, object(), now_utc=END)

    assert first.source_fingerprint == second.source_fingerprint == "strict-local-test"
    assert first.request_fingerprint != second.request_fingerprint


def test_pending_cutover_fails_before_loading_any_attribution_source(monkeypatch):
    from zira_dashboard import attendance_location_policy

    calls = []
    monkeypatch.setattr(
        attendance_location_policy,
        "match_state_for_day",
        lambda day, *, now_utc=None: calls.append(day) or "pending",
    )
    monkeypatch.setattr(
        production_history,
        "_metered_leaderboard",
        lambda *_args, **_kwargs: pytest.fail("pending cutover loaded production"),
    )

    with pytest.raises(ProductionSourceUnavailable, match="pending"):
        production_history.attribution_for(DAY, object(), now_utc=END)
    assert calls == [DAY]


def test_legacy_branch_is_chosen_once_and_preserves_name_identity(monkeypatch):
    from zira_dashboard import (
        attendance_location_policy,
        staffing,
        timeclock_windows,
        wc_attributions,
    )

    calls = []
    monkeypatch.setattr(
        attendance_location_policy,
        "match_state_for_day",
        lambda day, *, now_utc=None: calls.append((day, now_utc)) or "legacy",
    )
    monkeypatch.setattr(
        staffing,
        "load_schedule",
        lambda day: staffing.Schedule(day=day, published=True, assignments={"Repair 4": ["Ana"]}),
    )
    monkeypatch.setattr(
        timeclock_windows,
        "attendance_windows_for_day_with_availability",
        lambda day: ({}, True),
    )
    monkeypatch.setattr(wc_attributions, "testing_windows_for_day", lambda day: {})
    monkeypatch.setattr(wc_attributions, "people_by_wc", lambda day: {})
    monkeypatch.setattr(
        production_history,
        "_fetch_wc_totals",
        lambda client, day: {"Repair 4": (20, 0)},
    )
    monkeypatch.setattr(production_history, "_elapsed_minutes_for", lambda day: 480)
    monkeypatch.setattr(production_history, "_excluded_minutes_by_person_wc", lambda *args: {})

    result = production_history.attribution_for(DAY, object(), now_utc=END)

    assert calls == [(DAY, END)]
    assert result.is_strict is False
    assert list(result) == ["Ana"]
    assert result["Ana"]["Repair 4"]["units"] == 20.0


@pytest.mark.parametrize(
    ("baseline", "verified", "message"),
    [(None, END, "baseline"), (START, None, "verified")],
)
def test_strict_source_requires_completed_baseline_and_verified_incremental(
    monkeypatch, baseline, verified, message
):
    install_strict_dependencies(
        monkeypatch,
        spans=(),
        totals=(),
        baseline=baseline,
        verified=verified,
    )

    with pytest.raises(ProductionSourceUnavailable, match=message):
        production_history.attribution_for(DAY, object(), now_utc=END)


def test_stale_open_suffix_is_unassigned_while_verified_prefix_keeps_credit(monkeypatch):
    install_strict_dependencies(
        monkeypatch,
        spans=(
            span(101, "Ana", START, at(14)),
            span(
                101,
                "Ana",
                at(14),
                END,
                status="stale_open_location",
                wc=None,
            ),
        ),
        totals=(station_total(units=30, samples=((at(13), 10), (at(15), 20))),),
    )

    result = production_history.attribution_for(DAY, object(), now_utc=END)
    runs = production_history.unassigned_runs_for_day(DAY, object(), now_utc=END)

    assert result[(101, "Ana")]["Repair 4"]["units"] == 10.0
    assert [(run.start_utc, run.end_utc, run.units, run.sample_count) for run in runs] == [
        (at(15), at(15), 20.0, 1)
    ]


def test_strict_testing_offsets_and_identity_safe_breakdown_exclusions(monkeypatch):
    install_strict_dependencies(
        monkeypatch,
        spans=(span(101, "Alex", START, END), span(202, "Alex", START, END)),
        totals=(station_total(units=30, samples=((at(13), 10), (at(14), 20))),),
        testing={"Repair 4": [(at(12, 30), at(13, 30))]},
        breakdown={
            ("Alex", "Repair 4"): [(at(15), at(15, 30))],
        },
    )

    result = production_history.attribution_for(DAY, object(), now_utc=END)

    assert result[(101, "Alex")]["Repair 4"]["units"] == 10.0
    assert result[(202, "Alex")]["Repair 4"]["units"] == 10.0
    assert result[(101, "Alex")]["Repair 4"]["excluded_minutes"] == 0.0
    assert result[(202, "Alex")]["Repair 4"]["excluded_minutes"] == 0.0


@pytest.mark.parametrize("excluded_by", ["testing", "breakdown"])
def test_unassigned_runs_do_not_restore_excluded_samples(monkeypatch, excluded_by):
    window = (at(12), at(12, 30))
    install_strict_dependencies(
        monkeypatch,
        spans=(),
        totals=(
            station_total(
                units=9,
                samples=((at(12, 15), 9),),
                active_intervals=((at(12), at(13)),),
            ),
        ),
        testing={"Repair 4": [window]} if excluded_by == "testing" else {},
        breakdown=({("Unassigned", "Repair 4"): [window]} if excluded_by == "breakdown" else {}),
    )

    assert production_history.unassigned_runs_for_day(DAY, object(), now_utc=END) == ()


def test_unique_strict_identity_keeps_existing_breakdown_expected_minute_exclusion(
    monkeypatch,
):
    install_strict_dependencies(
        monkeypatch,
        spans=(span(101, "Ana", START, END),),
        totals=(station_total(units=20, samples=((at(14), 20),)),),
        breakdown={("Ana", "Repair 4"): [(at(15), at(15, 30))]},
    )

    result = production_history.attribution_for(DAY, object(), now_utc=END)

    assert result[(101, "Ana")]["Repair 4"]["excluded_minutes"] == 30.0


def test_strict_sample_total_tolerance_boundary_and_total_only_output(monkeypatch):
    tolerance = production_history.SAMPLE_TOTAL_TOLERANCE
    install_strict_dependencies(
        monkeypatch,
        spans=(span(101, "Ana", START, END),),
        totals=(station_total(units=10 + tolerance / 2, samples=((at(14), 10),)),),
    )
    assert (
        production_history.attribution_for(DAY, object(), now_utc=END)[(101, "Ana")]["Repair 4"][
            "units"
        ]
        == 10.0
    )

    install_strict_dependencies(
        monkeypatch,
        spans=(span(101, "Ana", START, END),),
        totals=(station_total(units=10 + tolerance * 2, samples=((at(14), 10),)),),
    )
    with pytest.raises(ProductionSourceUnavailable, match="samples"):
        production_history.attribution_for(DAY, object(), now_utc=END)


@pytest.mark.parametrize("bad_units", [-1, "bad", None])
def test_strict_rejects_negative_or_malformed_samples(monkeypatch, bad_units):
    install_strict_dependencies(
        monkeypatch,
        spans=(span(101, "Ana", START, END),),
        totals=(station_total(units=10, samples=((at(14), bad_units),)),),
    )

    with pytest.raises(ProductionSourceUnavailable, match="sample"):
        production_history.attribution_for(DAY, object(), now_utc=END)


def test_flatten_attribution_uses_strict_tuple_id_without_legacy_name_lookup():
    rows = precompute.flatten_attribution(
        DAY,
        {
            (202, "Alex"): {
                "Repair 4": {
                    "units": 10,
                    "downtime": 0,
                    "hours": 1,
                    "days_worked": 1,
                }
            }
        },
        {"Alex": "wrong-legacy-id"},
    )

    assert rows[0]["emp_id"] == "202"
    assert rows[0]["name"] == "Alex"


class RecordingCursor:
    def __init__(self, *, fail_insert: bool = False):
        self.executed = []
        self.fail_insert = fail_insert

    def execute(self, sql, params=()):
        self.executed.append((" ".join(sql.split()), params))


class RecordingDb:
    def __init__(self, *, fail_insert: bool = False):
        self.cur = RecordingCursor(fail_insert=fail_insert)
        self.events = []

    @contextmanager
    def cursor(self):
        self.events.append("begin")
        try:
            yield self.cur
        except Exception:
            self.events.append("rollback")
            raise
        else:
            self.events.append("commit")

    def execute_values(self, cur, sql, values, template=None):
        self.events.append(("insert", tuple(values)))
        if cur.fail_insert:
            raise RuntimeError("insert failed")


def test_strict_marker_delete_and_rows_share_one_atomic_transaction(monkeypatch):
    from zira_dashboard import db

    fake = RecordingDb()
    monkeypatch.setattr(db, "cursor", fake.cursor)
    monkeypatch.setattr(db, "execute_values", fake.execute_values)
    rows = [
        {
            "day": DAY,
            "emp_id": "101",
            "name": "Ana",
            "wc_name": "Repair 4",
            "units": 10,
            "downtime": 0,
            "hours": 1,
            "days_worked": 1,
        }
    ]

    assert precompute.upsert_production_daily(rows, replace_days=(DAY,), strict_day=DAY) == 1

    sql = [statement for statement, _params in fake.cur.executed]
    assert "INSERT INTO attendance_strict_days" in sql[0]
    assert "DELETE FROM production_daily" in sql[1]
    assert fake.events[0] == "begin"
    assert fake.events[-1] == "commit"
    assert fake.events[1][0] == "insert"


def test_empty_strict_result_still_atomically_marks_and_replaces_day(monkeypatch):
    from zira_dashboard import db

    fake = RecordingDb()
    monkeypatch.setattr(db, "cursor", fake.cursor)
    monkeypatch.setattr(db, "execute_values", fake.execute_values)

    assert precompute.upsert_production_daily([], replace_days=(DAY,), strict_day=DAY) == 0

    assert len(fake.cur.executed) == 2
    assert "attendance_strict_days" in fake.cur.executed[0][0]
    assert "DELETE FROM production_daily" in fake.cur.executed[1][0]
    assert fake.events == ["begin", "commit"]


def test_strict_store_failure_rolls_back_marker_and_delete(monkeypatch):
    from zira_dashboard import db

    fake = RecordingDb(fail_insert=True)
    monkeypatch.setattr(db, "cursor", fake.cursor)
    monkeypatch.setattr(db, "execute_values", fake.execute_values)

    with pytest.raises(RuntimeError, match="insert failed"):
        precompute.upsert_production_daily(
            [
                {
                    "day": DAY,
                    "emp_id": "101",
                    "name": "Ana",
                    "wc_name": "Repair 4",
                    "units": 10,
                    "downtime": 0,
                    "hours": 1,
                    "days_worked": 1,
                }
            ],
            replace_days=(DAY,),
            strict_day=DAY,
        )

    assert fake.events[-1] == "rollback"


def test_precompute_carries_computed_strict_state_to_durable_queue(monkeypatch):
    from zira_dashboard import attendance, attendance_mirror

    computed = production_history.AttributionResult(
        {(101, "Ana"): {"Repair 4": {"units": 10, "hours": 1, "days_worked": 1}}},
        is_strict=True,
        source_fingerprint="strict-source",
        request_fingerprint="strict-request-source",
    )
    monkeypatch.setattr(production_history, "attribution_for", lambda day, client: computed)
    monkeypatch.setattr(attendance, "name_to_person_id", lambda: {})
    stored = []
    monkeypatch.setattr(
        precompute,
        "store_prepared_day",
        lambda prepared: stored.append(prepared) or len(prepared.rows),
    )
    monkeypatch.setattr(
        attendance_mirror,
        "ensure_recalc_queued",
        lambda days, reason, **kwargs: stored.append(
            (tuple(days), reason, kwargs["source_fingerprint"])
        ),
    )

    assert precompute.precompute_day(DAY, object()) == {
        "day": DAY.isoformat(),
        "rows_written": 0,
        "queued": True,
    }
    assert stored == [((DAY,), "strict_direct_refresh", "strict-request-source")]


@_needs_postgres
def test_cutover_flip_between_prepare_and_store_preserves_prior_snapshot(monkeypatch):
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
        lambda _day, _client: production_history.AttributionResult(
            {
                "New": {
                    "Repair 4": {
                        "units": 99,
                        "downtime": 0,
                        "hours": 1,
                        "days_worked": 1,
                    }
                }
            },
            is_strict=False,
        ),
    )
    monkeypatch.setattr(attendance, "name_to_person_id", lambda: {"New": "new"})
    try:
        prepared = precompute.prepare_day(test_day, object())
        db.execute(
            "INSERT INTO attendance_strict_days "
            "(day, reason, source_changed_at) VALUES (%s, %s, %s)",
            (test_day, "concurrent cutover", at(12)),
        )

        with pytest.raises(
            ProductionSourceUnavailable,
            match="changed before snapshot commit",
        ):
            precompute.store_prepared_day(prepared)

        assert db.query(
            "SELECT emp_id, name, units FROM production_daily WHERE day = %s",
            (test_day,),
        ) == [{"emp_id": "old", "name": "Prior", "units": 7}]
        assert db.query("SELECT day FROM attendance_strict_days WHERE day = %s", (test_day,)) == [
            {"day": test_day}
        ]
    finally:
        db.execute("DELETE FROM production_daily WHERE day = %s", (test_day,))
        db.execute("DELETE FROM attendance_strict_days WHERE day = %s", (test_day,))


def test_precompute_source_failure_preserves_snapshot_and_enqueues_retry(monkeypatch):
    from zira_dashboard import attendance_mirror

    monkeypatch.setattr(
        production_history,
        "attribution_for",
        lambda day, client: (_ for _ in ()).throw(
            ProductionSourceUnavailable("no verified samples")
        ),
    )
    monkeypatch.setattr(
        precompute,
        "upsert_production_daily",
        lambda *_args, **_kwargs: pytest.fail("source failure replaced snapshot"),
    )
    enqueued = []
    monkeypatch.setattr(
        attendance_mirror,
        "enqueue_recalc",
        lambda days, reason: enqueued.append((tuple(days), reason)),
    )

    with pytest.raises(ProductionSourceUnavailable, match="verified"):
        precompute.precompute_day(DAY, object())

    assert enqueued == [((DAY,), "production_source_unavailable")]


def test_precompute_store_failure_enqueues_without_retrying_or_falling_back(monkeypatch):
    from zira_dashboard import attendance, attendance_mirror

    computed = production_history.AttributionResult(
        {(101, "Ana"): {"Repair 4": {"units": 10, "hours": 1, "days_worked": 1}}},
        is_strict=False,
    )
    monkeypatch.setattr(production_history, "attribution_for", lambda day, client: computed)
    monkeypatch.setattr(attendance, "name_to_person_id", lambda: {})
    writes = []

    def fail_store(prepared):
        writes.append(prepared)
        raise RuntimeError("store failed")

    monkeypatch.setattr(precompute, "store_prepared_day", fail_store)
    enqueued = []
    monkeypatch.setattr(
        attendance_mirror,
        "enqueue_recalc",
        lambda days, reason: enqueued.append((tuple(days), reason)),
    )

    with pytest.raises(RuntimeError, match="store failed"):
        precompute.precompute_day(DAY, object())

    assert len(writes) == 1
    assert writes[0].day == DAY
    assert writes[0].strict_day is None
    assert writes[0].expected_match_state == "legacy"
    assert enqueued == [((DAY,), "production_source_unavailable")]


@_needs_postgres
def test_postgres_strict_source_fingerprint_is_day_scoped_and_semantic_only():
    from zira_dashboard import db

    day = date(2098, 8, 24)
    attendance_id = 9_082_401
    check_in = datetime(2098, 8, 24, 12, tzinfo=UTC)
    db.init_pool()
    db.bootstrap_schema()
    original_sync = db.query(
        "SELECT baseline_completed_at, last_incremental_completed_at, "
        "last_incremental_observed_at FROM odoo_attendance_sync_state "
        "WHERE singleton = TRUE"
    )[0]
    with db.cursor() as cur:
        cur.execute(
            "DELETE FROM odoo_attendance_mirror WHERE odoo_attendance_id = %s",
            (attendance_id,),
        )
        cur.execute("DELETE FROM people WHERE odoo_id = 9082401")
        cur.execute(
            "INSERT INTO people (odoo_id, name, active, wage_type) "
            "VALUES (9082401, 'Fingerprint Worker', TRUE, 'hourly')"
        )
        cur.execute(
            "INSERT INTO odoo_attendance_mirror "
            "(odoo_attendance_id, employee_odoo_id, employee_name, check_in_utc, "
            "check_out_utc, odoo_work_center_id, odoo_work_center_name, "
            "odoo_write_date, first_seen_at, last_seen_at) "
            "VALUES (%s, 9082401, 'Fingerprint Worker', %s, %s, NULL, NULL, "
            "%s, %s, %s)",
            (
                attendance_id,
                check_in,
                check_in + timedelta(hours=1),
                check_in,
                check_in,
                check_in,
            ),
        )
        cur.execute(
            "UPDATE odoo_attendance_sync_state SET baseline_completed_at = %s, "
            "last_incremental_completed_at = %s, last_incremental_observed_at = %s "
            "WHERE singleton = TRUE",
            (check_in, check_in, check_in),
        )
    try:
        source_snapshot = production_history.strict_source_snapshot(day)
        verified_after_shift = source_snapshot.shift_end_utc + timedelta(minutes=1)
        db.execute(
            "UPDATE odoo_attendance_sync_state "
            "SET last_incremental_completed_at = %s WHERE singleton = TRUE",
            (verified_after_shift,),
        )
        original = production_history.strict_local_source_fingerprint(day)
        db.execute(
            "UPDATE odoo_attendance_sync_state "
            "SET last_incremental_completed_at = %s, last_incremental_observed_at = %s "
            "WHERE singleton = TRUE",
            (
                verified_after_shift + timedelta(hours=1),
                check_in + timedelta(minutes=1),
            ),
        )
        metadata_only = production_history.strict_local_source_fingerprint(day)
        db.execute(
            "UPDATE people SET wage_type = 'monthly' WHERE odoo_id = 9082401"
        )
        salaried_change = production_history.strict_local_source_fingerprint(day)
        db.execute(
            "UPDATE odoo_attendance_mirror SET employee_name = %s "
            "WHERE odoo_attendance_id = %s",
            ("Changed Fingerprint Worker", attendance_id),
        )
        semantic_change = production_history.strict_local_source_fingerprint(day)

        assert len(original) == 64
        assert metadata_only == original
        assert salaried_change != original
        assert semantic_change != original
        prepared = precompute.PreparedProductionDay(
            day,
            (
                {
                    "day": day,
                    "emp_id": "9082401",
                    "name": "Fingerprint Worker",
                    "wc_name": "Repair 4",
                    "units": 3.0,
                    "downtime": 0.0,
                    "hours": 1.0,
                    "days_worked": 1.0,
                    "excluded_minutes": 0.0,
                },
            ),
            day,
            "strict",
            original,
        )
        db.execute(
            "INSERT INTO attendance_strict_days (day, reason, source_changed_at) "
            "VALUES (%s, 'source_fence_test', now()) ON CONFLICT (day) DO NOTHING",
            (day,),
        )
        with pytest.raises(ProductionSourceUnavailable, match="source changed"):
            precompute.store_prepared_day(prepared)
        assert db.query("SELECT emp_id FROM production_daily WHERE day = %s", (day,)) == []
    finally:
        with db.cursor() as cur:
            cur.execute("DELETE FROM production_daily WHERE day = %s", (day,))
            cur.execute("DELETE FROM attendance_strict_days WHERE day = %s", (day,))
            cur.execute(
                "DELETE FROM odoo_attendance_mirror WHERE odoo_attendance_id = %s",
                (attendance_id,),
            )
            cur.execute("DELETE FROM people WHERE odoo_id = 9082401")
            cur.execute(
                "UPDATE odoo_attendance_sync_state SET baseline_completed_at = %s, "
                "last_incremental_completed_at = %s, "
                "last_incremental_observed_at = %s WHERE singleton = TRUE",
                (
                    original_sync["baseline_completed_at"],
                    original_sync["last_incremental_completed_at"],
                    original_sync["last_incremental_observed_at"],
                ),
            )


@_needs_postgres
def test_postgres_source_write_serializes_before_stale_strict_store():
    from zira_dashboard import db

    day = date(2098, 8, 25)
    attendance_id = 9_082_502
    check_in = datetime(2098, 8, 25, 12, tzinfo=UTC)
    db.init_pool()
    db.bootstrap_schema()
    with db.cursor() as cur:
        cur.execute(
            "DELETE FROM odoo_attendance_mirror WHERE odoo_attendance_id = %s",
            (attendance_id,),
        )
        cur.execute("DELETE FROM production_daily WHERE day = %s", (day,))
        cur.execute("DELETE FROM attendance_strict_days WHERE day = %s", (day,))
        cur.execute(
            "INSERT INTO odoo_attendance_mirror "
            "(odoo_attendance_id, employee_odoo_id, employee_name, check_in_utc, "
            "check_out_utc, odoo_write_date, first_seen_at, last_seen_at) "
            "VALUES (%s, 9082502, 'Race Worker Before', %s, %s, %s, %s, %s)",
            (
                attendance_id,
                check_in,
                check_in + timedelta(hours=1),
                check_in,
                check_in,
                check_in,
            ),
        )
        cur.execute(
            "INSERT INTO attendance_strict_days (day, reason, source_changed_at) "
            "VALUES (%s, 'source_race_test', now())",
            (day,),
        )
        cur.execute(
            "INSERT INTO production_daily "
            "(day, emp_id, name, wc_name, units, downtime, hours, days_worked) "
            "VALUES (%s, '9082502', 'Prior Saved Worker', 'Repair 4', 7, 0, 1, 1)",
            (day,),
        )
    original = production_history.strict_local_source_fingerprint(day)
    prepared = precompute.PreparedProductionDay(
        day,
        (
            {
                "day": day,
                "emp_id": "9082502",
                "name": "Stale Prepared Worker",
                "wc_name": "Repair 4",
                "units": 99.0,
                "downtime": 0.0,
                "hours": 1.0,
                "days_worked": 1.0,
                "excluded_minutes": 0.0,
            },
        ),
        day,
        "strict",
        original,
    )
    writer_holds_change = Event()
    release_writer = Event()
    store_done = Event()
    errors = []

    def mutate_source():
        try:
            with db.cursor() as cur:
                cur.execute(
                    "UPDATE odoo_attendance_mirror SET employee_name = %s "
                    "WHERE odoo_attendance_id = %s",
                    ("Race Worker After", attendance_id),
                )
                writer_holds_change.set()
                assert release_writer.wait(timeout=5)
        except Exception as exc:  # pragma: no cover - asserted in parent thread
            errors.append(exc)

    def store_stale_result():
        try:
            precompute.store_prepared_day(prepared)
        except Exception as exc:  # expected stale-source rejection
            errors.append(exc)
        finally:
            store_done.set()

    writer = Thread(target=mutate_source)
    store = Thread(target=store_stale_result)
    writer.start()
    assert writer_holds_change.wait(timeout=5)
    store.start()
    try:
        assert not store_done.wait(timeout=0.25)
        release_writer.set()
        writer.join(timeout=5)
        store.join(timeout=5)

        assert not writer.is_alive()
        assert not store.is_alive()
        assert len(errors) == 1
        assert isinstance(errors[0], ProductionSourceUnavailable)
        assert "source changed" in str(errors[0])
        assert db.query(
            "SELECT name, units FROM production_daily "
            "WHERE day = %s AND emp_id = '9082502' AND wc_name = 'Repair 4'",
            (day,),
        ) == [{"name": "Prior Saved Worker", "units": 7}]
    finally:
        release_writer.set()
        writer.join(timeout=5)
        store.join(timeout=5)
        with db.cursor() as cur:
            cur.execute("DELETE FROM production_daily WHERE day = %s", (day,))
            cur.execute("DELETE FROM attendance_strict_days WHERE day = %s", (day,))
            cur.execute(
                "DELETE FROM odoo_attendance_mirror WHERE odoo_attendance_id = %s",
                (attendance_id,),
            )
