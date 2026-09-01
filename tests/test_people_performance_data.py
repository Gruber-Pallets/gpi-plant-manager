from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from types import SimpleNamespace

from zira_dashboard import forklift_score
from zira_dashboard import people_performance_data as data
from zira_dashboard.attendance_timeline import AttendanceTimelineSnapshot
from zira_dashboard.forklift_event_store import ForkliftCompletionCoverage
from zira_dashboard.leaderboard import StationTotal
from zira_dashboard.people_performance import BreakSpan
from zira_dashboard.production_segments import SegmentScore
from zira_dashboard.stations import Station

from tests.people_performance_fixtures import DAY, END, START, event, score, span


NOW = START + timedelta(hours=5)
CALENDAR_START = datetime.combine(
    DAY, datetime.min.time(), tzinfo=data.shift_config.SITE_TZ
).astimezone(UTC)
CALENDAR_END = CALENDAR_START + timedelta(days=1)


def test_dashboard_window_ends_at_renamed_trailing_break_start(monkeypatch):
    midday = BreakSpan(
        START + timedelta(hours=2),
        START + timedelta(hours=2, minutes=15),
        "Lunch",
    )
    wind_down = BreakSpan(
        END - timedelta(minutes=15),
        END,
        "Put tools away",
    )
    monkeypatch.setattr(data, "_bounds", lambda day, now: (START, END, END, False))
    monkeypatch.setattr(data, "_breaks", lambda day: (midday, wind_down))

    start, end, cap, is_today, breaks = data._dashboard_window(DAY, END)

    assert start == START
    assert end == wind_down.start_utc
    assert cap == wind_down.start_utc
    assert is_today is False
    assert breaks == (midday,)


def test_dashboard_window_keeps_shift_end_without_valid_trailing_break(monkeypatch):
    midday = BreakSpan(
        START + timedelta(hours=2),
        START + timedelta(hours=2, minutes=15),
        "Lunch",
    )
    monkeypatch.setattr(data, "_bounds", lambda day, now: (START, END, END, False))
    monkeypatch.setattr(data, "_breaks", lambda day: (midday,))

    assert data._dashboard_window(DAY, END) == (
        START,
        END,
        END,
        False,
        (midday,),
    )


def _attendance(spans, *, blockers=()):
    return AttendanceTimelineSnapshot(
        spans=tuple(spans),
        open_employee_ids=frozenset(item.employee_odoo_id for item in spans if item.is_open),
        verified_through_utc=NOW,
        freshness_blockers=tuple(blockers),
    )


def _coverage(count: int, *, through: datetime = CALENDAR_END):
    return ForkliftCompletionCoverage(
        day=DAY,
        covered_through_utc=through,
        raw_event_count=count,
        successful_at=through,
    )


def _driver_row(driver_id: str, name: str, calls: int, *, on_time=0, late=0):
    return {
        "day": DAY,
        "driver_id": driver_id,
        "name": name,
        "calls": calls,
        "on_time": on_time,
        "late": late,
        "avg_ms": 60000,
        "max_ms": 60000,
        "utilization_pct": 50,
        "on_call_ms": calls * 120000,
        "available_ms": 3_600_000,
    }


def _total(
    name: str,
    *,
    units: int = 20,
    truncated: bool = False,
    samples=(),
    downtime=(),
):
    station = Station(f"meter-{name}", name, "Repair", "Bay")
    return StationTotal(
        station=station,
        units=units,
        reading_count=len(samples),
        truncated=truncated,
        downtime_minutes=0,
        active_minutes=0,
        last_reading_at=None,
        last_status=None,
        samples=tuple(samples),
        active_intervals=(),
        downtime_intervals=tuple(downtime),
    )


def install_sources(
    monkeypatch,
    *,
    spans,
    events=(),
    driver_rows=(),
    coverage=None,
    calls_row=None,
    resolved=None,
    totals=(),
    catalog=(),
    scores=(),
    attribution_rows=(),
    attendance_blockers=(),
):
    monkeypatch.setattr(data, "_bounds", lambda day, now: (START, END, NOW, True))
    monkeypatch.setattr(data, "_breaks", lambda day: ())
    monkeypatch.setattr(
        data.attendance_timeline,
        "snapshot_for_range",
        lambda start, end, as_of_utc=None: _attendance(spans, blockers=attendance_blockers),
    )
    monkeypatch.setattr(
        data.production_history,
        "metered_station_totals",
        lambda client, day, now_utc=None: tuple(totals),
    )
    monkeypatch.setattr(
        data.production_history,
        "metered_station_catalog",
        lambda: tuple(catalog),
    )
    monkeypatch.setattr(
        data.production_history,
        "production_scores_for_timeline",
        lambda *args, **kwargs: tuple(scores),
    )
    monkeypatch.setattr(data.wc_attributions, "for_day", lambda day: list(attribution_rows))
    monkeypatch.setattr(
        data.forklift_event_store,
        "completion_events_for_range",
        lambda start, end: tuple(events),
    )
    monkeypatch.setattr(
        data.forklift_event_store,
        "completion_coverage_for_day",
        lambda day: coverage,
    )
    monkeypatch.setattr(data.forklift_store, "calls_row_for_day", lambda day: calls_row)
    monkeypatch.setattr(data.forklift_store, "driver_rows_for_day", lambda day: list(driver_rows))
    monkeypatch.setattr(
        data.forklift_store,
        "resolve_forklift_driver_ids",
        lambda names_by_id, *, allowed_employee_ids=None: dict(resolved or {}),
    )
    monkeypatch.setattr(data.forklift_settings, "current", lambda: object())
    monkeypatch.setattr(
        data.forklift_settings,
        "resolve",
        lambda settings, algo_throughput: SimpleNamespace(
            score_config=lambda: forklift_score.DEFAULT_SCORE_CONFIG
        ),
    )


TRENT_SPAN = span(60, "Trent Iverson", 0, 300, "Tablets")
TRENT_CALL = event("Trent", 30, on_time=True)
TRENT_ROW = _driver_row("driver-Trent", "Trent", 1, on_time=1)


def test_load_dashboard_uses_one_cap_and_id_only_forklift_join(monkeypatch):
    seen = {}
    install_sources(
        monkeypatch,
        spans=(TRENT_SPAN,),
        events=(TRENT_CALL,),
        driver_rows=(TRENT_ROW,),
        coverage=_coverage(1),
        calls_row={"day": DAY, "total_calls": 1},
        resolved={"driver-Trent": 60},
    )
    monkeypatch.setattr(
        data.attendance_timeline,
        "snapshot_for_range",
        lambda start, end, as_of_utc=None: (
            seen.update(start=start, end=end, attendance_cap=as_of_utc)
            or _attendance((TRENT_SPAN,))
        ),
    )
    monkeypatch.setattr(
        data.production_history,
        "metered_station_totals",
        lambda client, day, now_utc=None: seen.update(production_cap=now_utc) or (),
    )
    monkeypatch.setattr(
        data.forklift_event_store,
        "completion_events_for_range",
        lambda start, end: seen.update(forklift_start=start, forklift_end=end) or (TRENT_CALL,),
    )

    model = data.load_dashboard(DAY, client=object(), now_utc=NOW)

    assert model.as_of_utc == NOW
    assert seen["attendance_cap"] == seen["production_cap"] == NOW
    assert seen["start"] == START and seen["end"] == END
    assert seen["forklift_start"] == CALENDAR_START
    assert seen["forklift_end"] == CALENDAR_END
    assert model.rows[0].employee_odoo_id == 60
    assert model.rows[0].summary[0] == ("Calls", "1")
    assert model.rows[0].summary[-1] == ("Score", "N/A")


def test_ambiguous_or_unchanged_name_resolution_never_attaches_calls(monkeypatch):
    ambiguous = event("Jesus", 30, on_time=True)
    row = _driver_row(ambiguous.driver_id, "Jesus", 1, on_time=1)
    install_sources(
        monkeypatch,
        spans=(span(62, "Jesus Ramos", 0, 300, "Tablets"),),
        events=(ambiguous,),
        driver_rows=(row,),
        coverage=_coverage(1),
        calls_row={"day": DAY, "total_calls": 1},
        resolved={},
    )

    model = data.load_dashboard(DAY, client=object(), now_utc=NOW)

    assert model.rows[0].summary[0] == ("Calls", "N/A")
    assert model.rows[0].unattached_forklift_calls == 0
    assert "Unmatched forklift calls: 1" in model.source_warnings


def test_two_external_driver_ids_resolving_to_one_employee_are_refused(monkeypatch):
    first = event("Trent", 30, on_time=True, event_id="one")
    second = SimpleNamespace(**{**first.__dict__, "event_id": "two", "driver_id": "d2"})
    # Keep real immutable event values while changing the external identity.
    second = type(first)(**second.__dict__)
    install_sources(
        monkeypatch,
        spans=(TRENT_SPAN,),
        events=(first, second),
        driver_rows=(
            _driver_row(first.driver_id, "Trent", 1, on_time=1),
            _driver_row("d2", "Trent", 1, on_time=1),
        ),
        coverage=_coverage(2),
        calls_row={"day": DAY, "total_calls": 2},
        resolved={first.driver_id: 60, "d2": 60},
    )

    model = data.load_dashboard(DAY, client=object(), now_utc=NOW)

    assert model.rows[0].summary[0] == ("Calls", "N/A")
    assert "Forklift driver identity conflict" in model.source_warnings
    assert "Unmatched forklift calls: 2" in model.source_warnings


def test_raw_event_without_driver_daily_row_is_unavailable(monkeypatch):
    install_sources(
        monkeypatch,
        spans=(TRENT_SPAN,),
        events=(TRENT_CALL,),
        driver_rows=(),
        coverage=_coverage(1),
        calls_row={"day": DAY, "total_calls": 1},
        resolved={"driver-Trent": 60},
    )

    model = data.load_dashboard(DAY, client=object(), now_utc=NOW)

    assert model.rows[0].summary[0] == ("Calls", "N/A")
    assert "Forklift timeline incomplete" in model.source_warnings


def test_driver_daily_calls_without_raw_events_are_unavailable(monkeypatch):
    install_sources(
        monkeypatch,
        spans=(TRENT_SPAN,),
        events=(),
        driver_rows=(_driver_row("driver-Trent", "Trent", 2, on_time=2),),
        coverage=_coverage(0),
        calls_row={"day": DAY, "total_calls": 0},
        resolved={"driver-Trent": 60},
    )

    model = data.load_dashboard(DAY, client=object(), now_utc=NOW)

    assert model.rows[0].summary[0] == ("Calls", "N/A")
    assert "Forklift timeline incomplete" in model.source_warnings


def test_never_fetched_day_is_unavailable_not_a_false_zero(monkeypatch):
    install_sources(
        monkeypatch,
        spans=(TRENT_SPAN,),
        coverage=None,
        calls_row=None,
    )

    model = data.load_dashboard(DAY, client=object(), now_utc=NOW)

    assert model.rows[0].summary[0] == ("Calls", "N/A")
    assert "Forklift data unavailable" in model.source_warnings


def test_explicit_covered_zero_is_a_real_zero(monkeypatch):
    install_sources(
        monkeypatch,
        spans=(TRENT_SPAN,),
        coverage=_coverage(0),
        calls_row={"day": DAY, "total_calls": 0},
    )

    model = data.load_dashboard(DAY, client=object(), now_utc=NOW)

    assert model.rows[0].summary[:3] == (
        ("Calls", "0"),
        ("On time", "N/A"),
        ("Handling", "0 min"),
    )
    assert "Forklift data unavailable" not in model.source_warnings


def test_calls_after_shift_are_reconciled_but_not_displayed_or_scored(monkeypatch):
    in_shift = TRENT_CALL
    after_shift = type(in_shift)(
        **{
            **in_shift.__dict__,
            "event_id": "after-shift",
            "created_at_utc": END + timedelta(minutes=30),
        }
    )
    install_sources(
        monkeypatch,
        spans=(TRENT_SPAN,),
        events=(in_shift, after_shift),
        driver_rows=(_driver_row("driver-Trent", "Trent", 2, on_time=2),),
        coverage=_coverage(2),
        calls_row={"day": DAY, "total_calls": 2},
        resolved={"driver-Trent": 60},
    )

    model = data.load_dashboard(DAY, client=object(), now_utc=NOW)

    assert model.rows[0].summary[0] == ("Calls", "1")
    assert model.rows[0].summary[-1] == ("Score", "N/A")
    assert (
        sum(
            bucket.calls
            for interval in model.rows[0].intervals
            for bucket in interval.forklift_buckets
        )
        == 1
    )


def test_bounded_raw_events_compute_the_existing_score_above_the_call_gate(
    monkeypatch,
):
    in_shift = tuple(
        event(
            "Trent",
            index * 15,
            on_time=True,
            event_id=f"in-shift-{index}",
        )
        for index in range(8)
    )
    after_cap = event("Trent", 330, late=True, event_id="after-cap")
    stored_row = _driver_row("driver-Trent", "Trent", 9, on_time=8, late=1)
    stored_row.update(avg_ms=1, utilization_pct=100)
    install_sources(
        monkeypatch,
        spans=(TRENT_SPAN,),
        events=(*in_shift, after_cap),
        driver_rows=(stored_row,),
        coverage=_coverage(9),
        calls_row={"day": DAY, "total_calls": 9},
        resolved={"driver-Trent": 60},
    )

    model = data.load_dashboard(DAY, client=object(), now_utc=NOW)

    expected = forklift_score.daily_score(
        {
            "calls": 8,
            "on_time": 8,
            "late": 0,
            "avg_ms": 60000,
            "utilization_pct": 100 * (8 * 120000) / ((NOW - START).total_seconds() * 1000),
        },
        forklift_score.DEFAULT_SCORE_CONFIG,
    )
    assert expected is not None
    assert model.rows[0].summary == (
        ("Calls", "8"),
        ("On time", "100%"),
        ("Handling", "16 min"),
        ("Score", f"{expected.score:.0f}"),
    )


def test_bounded_raw_event_score_stays_unavailable_below_the_call_gate(monkeypatch):
    in_shift = tuple(
        event(
            "Trent",
            index * 15,
            on_time=True,
            event_id=f"below-gate-{index}",
        )
        for index in range(7)
    )
    install_sources(
        monkeypatch,
        spans=(TRENT_SPAN,),
        events=in_shift,
        driver_rows=(_driver_row("driver-Trent", "Trent", 7, on_time=7),),
        coverage=_coverage(7),
        calls_row={"day": DAY, "total_calls": 7},
        resolved={"driver-Trent": 60},
    )

    model = data.load_dashboard(DAY, client=object(), now_utc=NOW)

    assert model.rows[0].summary[-1] == ("Score", "N/A")


def test_today_accepts_coverage_within_the_ten_minute_warmer_cadence(monkeypatch):
    install_sources(
        monkeypatch,
        spans=(TRENT_SPAN,),
        events=(TRENT_CALL,),
        driver_rows=(TRENT_ROW,),
        coverage=_coverage(1, through=NOW - timedelta(minutes=9)),
        calls_row={"day": DAY, "total_calls": 1},
        resolved={"driver-Trent": 60},
    )

    model = data.load_dashboard(DAY, client=object(), now_utc=NOW)

    assert model.rows[0].summary[0] == ("Calls", "1")
    assert "Forklift data unavailable" not in model.source_warnings


def test_attendance_freshness_warning_uses_frozen_snapshot_without_readiness(monkeypatch):
    install_sources(
        monkeypatch,
        spans=(TRENT_SPAN,),
        attendance_blockers=("mirror_stale",),
    )

    model = data.load_dashboard(DAY, client=object(), now_utc=NOW)

    assert len(model.rows) == 1
    assert "Attendance source stale" in model.source_warnings
    assert not hasattr(data, "attendance_readiness")


def test_historical_day_ignores_current_mirror_freshness_and_is_never_active(monkeypatch):
    install_sources(
        monkeypatch,
        spans=(TRENT_SPAN,),
        attendance_blockers=("mirror_stale",),
    )
    monkeypatch.setattr(data, "_bounds", lambda day, now: (START, END, END, False))

    model = data.load_dashboard(DAY, client=object(), now_utc=END)

    assert model.is_today is False
    assert model.rows[0].is_active is False
    assert all(interval.is_open is False for interval in model.rows[0].intervals)
    assert all(not interval.key.endswith(":open") for interval in model.rows[0].intervals)
    assert "Attendance source stale" not in model.source_warnings


def test_attendance_failure_returns_empty_page_without_loading_other_sources(monkeypatch):
    calls = []
    monkeypatch.setattr(data, "_bounds", lambda day, now: (START, END, NOW, True))
    monkeypatch.setattr(data, "_breaks", lambda day: ())
    monkeypatch.setattr(
        data.attendance_timeline,
        "snapshot_for_range",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("mirror down")),
    )
    monkeypatch.setattr(
        data.production_history,
        "metered_station_totals",
        lambda *args, **kwargs: calls.append("production") or (),
    )
    monkeypatch.setattr(
        data.forklift_event_store,
        "completion_events_for_range",
        lambda *args, **kwargs: calls.append("forklift") or (),
    )
    monkeypatch.setattr(
        data.forklift_store,
        "resolve_forklift_driver_ids",
        lambda *args, **kwargs: calls.append("identity") or {},
    )

    model = data.load_dashboard(DAY, client=object(), now_utc=NOW)

    assert model.rows == ()
    assert "Attendance data unavailable" in model.source_warnings
    assert "identity" not in calls


def test_one_bad_meter_is_unavailable_without_hiding_good_meter(monkeypatch):
    repair_1 = span(71, "Good Meter", 0, 300, "Repair 1")
    repair_2 = span(72, "Missing Meter", 0, 300, "Repair 2")
    station_1 = _total("Repair 1")
    station_2 = Station("meter-Repair 2", "Repair 2", "Repair", "Bay")
    good_score = score(71, "Good Meter", "Repair 1", 0, 300, 20, 10)
    install_sources(
        monkeypatch,
        spans=(repair_1, repair_2),
        totals=(station_1,),
        catalog=(station_1.station, station_2),
        scores=(good_score,),
    )
    monkeypatch.setattr(data.settings_store, "station_target", lambda station: 10.0)

    model = data.load_dashboard(DAY, client=object(), now_utc=NOW)

    by_name = {row.person_name: row for row in model.rows}
    assert by_name["Good Meter"].intervals[0].metric_available is True
    assert by_name["Missing Meter"].intervals[0].metric_available is False
    assert "Production metric unavailable: Repair 2" in model.source_warnings


def test_nonpositive_goal_and_truncated_total_are_unavailable_per_meter(monkeypatch):
    zero_goal = span(73, "Zero Goal", 0, 300, "Repair 1")
    truncated = span(74, "Truncated", 0, 300, "Repair 2")
    first = _total("Repair 1")
    second = _total("Repair 2", truncated=True)
    install_sources(
        monkeypatch,
        spans=(zero_goal, truncated),
        totals=(first, second),
        catalog=(first.station, second.station),
    )
    monkeypatch.setattr(
        data.settings_store,
        "station_target",
        lambda station: 0.0 if station.name == "Repair 1" else 10.0,
    )

    model = data.load_dashboard(DAY, client=object(), now_utc=NOW)

    assert all(row.intervals[0].metric_available is False for row in model.rows)
    assert "Production metric unavailable: Repair 1" in model.source_warnings
    assert "Production metric unavailable: Repair 2" in model.source_warnings


def test_duplicate_totals_make_only_that_meter_unavailable(monkeypatch):
    repair = span(77, "Repair Worker", 0, 300, "Repair 1")
    first = _total("Repair 1")
    install_sources(
        monkeypatch,
        spans=(repair,),
        totals=(first, first),
        catalog=(first.station,),
    )
    monkeypatch.setattr(data.settings_store, "station_target", lambda station: 10.0)

    model = data.load_dashboard(DAY, client=object(), now_utc=NOW)

    assert model.rows[0].intervals[0].metric_available is False
    assert "Production metric unavailable: Repair 1" in model.source_warnings


def test_each_available_meter_is_scored_only_against_its_own_spans(monkeypatch):
    first_span = span(91, "First", 0, 300, "Repair 1")
    second_span = span(92, "Second", 0, 300, "Repair 2")
    first_total = _total("Repair 1")
    second_total = _total("Repair 2")
    install_sources(
        monkeypatch,
        spans=(first_span, second_span),
        totals=(first_total, second_total),
        catalog=(first_total.station, second_total.station),
    )
    monkeypatch.setattr(data.settings_store, "station_target", lambda station: 10.0)
    seen = []

    def score_one(_client, _day, spans, **kwargs):
        names = tuple(item.app_work_center_name for item in spans)
        seen.append(names)
        item = spans[0]
        return (
            SegmentScore(
                segment_id=item.employee_odoo_id,
                wc_name=item.app_work_center_name,
                person_name=item.employee_name,
                start_utc=item.start_utc,
                end_utc=item.end_utc,
                source="odoo",
                productive_minutes=300,
                actual_units=20,
                goal_units=10,
                runway_units=20,
                is_active=False,
                result="ahead",
                person_odoo_id=item.employee_odoo_id,
            ),
        )

    monkeypatch.setattr(data.production_history, "production_scores_for_timeline", score_one)

    model = data.load_dashboard(DAY, client=object(), now_utc=NOW)

    assert seen == [("Repair 1",), ("Repair 2",)]
    assert all(row.intervals[0].metric_available for row in model.rows)


def test_production_reader_error_does_not_hide_attendance(monkeypatch):
    repair = Station("meter-1", "Repair 1", "Repair", "Bay")
    install_sources(
        monkeypatch,
        spans=(span(75, "Repair Worker", 0, 300, "Repair 1"),),
        catalog=(repair,),
    )
    monkeypatch.setattr(
        data.production_history,
        "metered_station_totals",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("zira down")),
    )

    model = data.load_dashboard(DAY, client=object(), now_utc=NOW)

    assert len(model.rows) == 1
    assert model.rows[0].intervals[0].metric_available is False
    assert "Production data unavailable" in model.source_warnings


def test_attribution_reader_error_does_not_assume_empty_exclusions(monkeypatch):
    repair = Station("meter-1", "Repair 1", "Repair", "Bay")
    install_sources(
        monkeypatch,
        spans=(span(78, "Repair Worker", 0, 300, "Repair 1"),),
        catalog=(repair,),
    )
    monkeypatch.setattr(
        data.wc_attributions,
        "for_day",
        lambda day: (_ for _ in ()).throw(RuntimeError("attributions down")),
    )

    model = data.load_dashboard(DAY, client=object(), now_utc=NOW)

    assert len(model.rows) == 1
    assert model.rows[0].intervals[0].metric_available is False
    assert "Production data unavailable" in model.source_warnings


def test_bounds_normalizes_one_aware_cap_and_explicit_is_today(monkeypatch):
    local_day = date(2026, 8, 28)
    local_noon = datetime(2026, 8, 28, 12, 0, tzinfo=data.shift_config.SITE_TZ)
    monkeypatch.setattr(data.shift_config, "shift_start_for", lambda day: time(6, 0))
    monkeypatch.setattr(data.shift_config, "shift_end_for", lambda day: time(14, 0))

    start, end, cap, is_today = data._bounds(local_day, local_noon)

    assert start.tzinfo is UTC and end.tzinfo is UTC and cap.tzinfo is UTC
    assert cap == local_noon.astimezone(UTC)
    assert is_today is True


def test_load_dashboard_passes_explicit_is_today_to_production_scorer(monkeypatch):
    station_total = _total("Repair 1")
    install_sources(
        monkeypatch,
        spans=(span(76, "Historical", 0, 300, "Repair 1"),),
        totals=(station_total,),
        catalog=(station_total.station,),
    )
    monkeypatch.setattr(data, "_bounds", lambda day, now: (START, END, END, False))
    monkeypatch.setattr(data.settings_store, "station_target", lambda station: 10.0)
    seen = {}
    monkeypatch.setattr(
        data.production_history,
        "production_scores_for_timeline",
        lambda *args, **kwargs: seen.update(kwargs) or (),
    )

    data.load_dashboard(DAY, client=object(), now_utc=END)

    assert seen["now_utc"] == END
    assert seen["is_today"] is False
