from dataclasses import replace
from datetime import timedelta

from zira_dashboard import people_performance
from zira_dashboard.people_performance import BreakSpan, assemble_dashboard

from tests.people_performance_fixtures import (
    DAY,
    END,
    START,
    busy_dashboard_model,
    driver_metric,
    event,
    score,
    span,
)


def _assemble(*, spans, scores=(), events=None, day_metrics=None, **kwargs):
    return assemble_dashboard(
        day=DAY,
        as_of_utc=END,
        window_start_utc=START,
        window_end_utc=END,
        spans=spans,
        production_scores=scores,
        downtime_by_wc=kwargs.pop("downtime_by_wc", {}),
        breakdown_exclusions_by_person_wc=kwargs.pop("breakdowns", {}),
        forklift_events_by_employee_id=events or {},
        forklift_day_metrics_by_employee_id=day_metrics or {},
        breaks=kwargs.pop("breaks", ()),
        metered_wc_names=kwargs.pop("metered_wc_names", {"Repair 1", "Repair 2"}),
        source_warnings=kwargs.pop("source_warnings", ()),
        is_today=kwargs.pop("is_today", True),
        **kwargs,
    )


def test_mixed_role_person_renders_once_and_final_role_owns_completed_section():
    model = _assemble(
        spans=(
            span(50, "Alex Worker", 0, 60, "Repair 1"),
            span(50, "Alex Worker", 60, 120, "Tablets"),
        ),
        scores=(score(50, "Alex Worker", "Repair 1", 0, 60, 25, 20),),
        downtime_by_wc={"Repair 1": ()},
        events={50: (event("Alex Worker", 90, late=True),)},
        day_metrics={50: driver_metric(1, 0, 1)},
    )
    assert len(model.rows) == 1
    row = model.rows[0]
    assert row.section == "forklift"
    assert [interval.role for interval in row.intervals] == ["production", "forklift"]
    assert row.intervals[1].is_transfer is True


def test_fixed_section_order_and_needs_attention_sort_are_stable():
    model = busy_dashboard_model()
    assert [(row.section, row.person_name) for row in model.rows] == [
        ("production", "Sam Stale"),
        ("production", "Amy Behind"),
        ("production", "Zed Ahead"),
        ("forklift", "Ben Driver"),
        ("forklift", "Mia Mixed"),
        ("other", "Cal Missing"),
    ]


def test_no_goal_rows_sort_after_goal_based_metered_rows():
    model = _assemble(
        spans=(
            span(80, "No Goal", 0, 480, "Measured Work 1"),
            span(81, "Repair Ahead", 0, 480, "Repair 1"),
            span(82, "Trim Behind", 0, 480, "Trim Saw 1"),
        ),
        scores=(
            score(80, "No Goal", "Measured Work 1", 0, 480, 100, 0),
            score(81, "Repair Ahead", "Repair 1", 0, 480, 110, 100),
            score(82, "Trim Behind", "Trim Saw 1", 0, 480, 50, 100),
        ),
        downtime_by_wc={"Measured Work 1": (), "Repair 1": (), "Trim Saw 1": ()},
        metered_wc_names={"Measured Work 1", "Repair 1", "Trim Saw 1"},
    )

    assert [row.person_name for row in model.rows] == [
        "Trim Behind",
        "Repair Ahead",
        "No Goal",
    ]
    assert all(row.section == "production" for row in model.rows)


def test_final_work_center_goal_owns_subgroup_after_transfer():
    model = _assemble(
        spans=(
            span(83, "Moved To Goal", 0, 60, "Measured Work 1"),
            span(83, "Moved To Goal", 60, 480, "Repair 1"),
            span(84, "Ended Without Goal", 0, 60, "Repair 1"),
            span(84, "Ended Without Goal", 60, 480, "Measured Work 1"),
        ),
        scores=(
            score(83, "Moved To Goal", "Measured Work 1", 0, 60, 5, 0),
            score(83, "Moved To Goal", "Repair 1", 60, 480, 50, 100),
            score(84, "Ended Without Goal", "Repair 1", 0, 60, 5, 10),
            score(84, "Ended Without Goal", "Measured Work 1", 60, 480, 50, 0),
        ),
        downtime_by_wc={"Measured Work 1": (), "Repair 1": ()},
        metered_wc_names={"Measured Work 1", "Repair 1"},
    )

    assert [row.person_name for row in model.rows] == [
        "Moved To Goal",
        "Ended Without Goal",
    ]
    assert model.rows[0].intervals[-1].production.goal_units == 100
    assert model.rows[1].intervals[-1].production.goal_units == 0


def test_production_summary_shows_current_units_against_goal_across_transfers():
    model = _assemble(
        spans=(
            span(86, "Current Worker", 0, 60, "Repair 1"),
            span(86, "Current Worker", 60, 120, "Repair 2"),
        ),
        scores=(
            score(86, "Current Worker", "Repair 1", 0, 60, 41.4, 50.4),
            score(86, "Current Worker", "Repair 2", 60, 120, 100.4, 110.4),
        ),
        downtime_by_wc={"Repair 1": (), "Repair 2": ()},
    )

    assert model.rows[0].summary == (
        ("Goal", "88%"),
        ("Uptime", "100%"),
        ("Downtime", "0 min"),
        ("Production", "142/161"),
    )


def test_same_location_across_lunch_does_not_create_transfer():
    lunch = BreakSpan(
        START + timedelta(minutes=60),
        START + timedelta(minutes=90),
        "Lunch",
    )
    model = _assemble(
        spans=(
            span(51, "Lunch Worker", 0, 60, "Repair 1"),
            span(51, "Lunch Worker", 90, 120, "Repair 1"),
        ),
        scores=(
            score(51, "Lunch Worker", "Repair 1", 0, 60, 20, 20),
            score(51, "Lunch Worker", "Repair 1", 90, 120, 10, 10),
        ),
        downtime_by_wc={"Repair 1": ()},
        breaks=(lunch,),
    )
    assert [interval.is_transfer for interval in model.rows[0].intervals] == [False, False]


def test_activity_outside_valid_tablet_span_stays_unattached():
    model = _assemble(
        spans=(span(52, "No Location", 0, 480, None, "missing_required_location"),),
        events={52: (event("No Location", 30, on_time=True),)},
        day_metrics={52: driver_metric(1, 1, 0)},
    )
    row = model.rows[0]
    assert row.status == "location missing"
    assert row.unattached_forklift_calls == 1
    assert all(not interval.forklift_buckets for interval in row.intervals)


def test_one_metric_failure_keeps_that_person_and_every_other_row(monkeypatch):
    original = people_performance.production_metric
    monkeypatch.setattr(
        people_performance,
        "production_metric",
        lambda score, **kwargs: (
            (_ for _ in ()).throw(ValueError("bad row"))
            if score.person_odoo_id == 44
            else original(score, **kwargs)
        ),
    )
    model = busy_dashboard_model()
    assert {row.employee_odoo_id for row in model.rows} >= {44, 45}
    failed = next(row for row in model.rows if row.employee_odoo_id == 44)
    assert ("metric unavailable",) == failed.attention_reasons
    assert all(value == "N/A" for _label, value in failed.summary[:3])


def test_same_display_name_keeps_forklift_calls_separate_by_odoo_identity():
    model = _assemble(
        spans=(
            span(70, "Alex Same", 0, 120, "Tablets"),
            span(71, "Alex Same", 0, 120, "Tablets"),
        ),
        events={
            70: (event("Alex Same", 30, on_time=True, event_id="first"),),
            71: (event("Alex Same", 45, late=True, event_id="second"),),
        },
        day_metrics={
            70: driver_metric(1, 1, 0),
            71: driver_metric(1, 0, 1),
        },
    )
    first, second = sorted(model.rows, key=lambda row: row.employee_odoo_id)
    assert first.summary[:2] == (("Calls", "1"), ("On time", "100%"))
    assert second.summary[:2] == (("Calls", "1"), ("On time", "0%"))
    assert first.intervals[0].forklift_buckets[2].calls == 1
    assert second.intervals[0].forklift_buckets[3].calls == 1


def test_display_name_key_is_never_used_to_attach_forklift_activity():
    model = _assemble(
        spans=(
            span(70, "Alex Same", 0, 120, "Tablets"),
            span(71, "Alex Same", 0, 120, "Tablets"),
        ),
        events={"Alex Same": (event("Alex Same", 30, on_time=True),)},
        day_metrics={"Alex Same": driver_metric(1, 1, 0)},
        source_warnings=("Unmatched forklift calls: 1",),
    )
    first, second = sorted(model.rows, key=lambda row: row.employee_odoo_id)
    assert first.summary[0] == ("Calls", "N/A")
    assert not first.intervals[0].forklift_buckets
    assert second.summary[0] == ("Calls", "N/A")
    assert model.source_warnings == ("Unmatched forklift calls: 1",)


def test_stale_mapped_span_keeps_identity_but_cannot_earn_metrics():
    model = _assemble(
        spans=(span(73, "Stale Worker", 0, 480, "Repair 1", "stale_open_location"),),
        scores=(score(73, "Stale Worker", "Repair 1", 0, 480, 999, 1),),
        downtime_by_wc={"Repair 1": ()},
    )
    row = model.rows[0]
    assert row.section == "production"
    assert row.intervals[0].location_name == "Repair 1"
    assert row.intervals[0].metric_available is False
    assert row.intervals[0].production is None
    assert row.summary == (
        ("Goal", "N/A"),
        ("Uptime", "N/A"),
        ("Downtime", "N/A"),
        ("Production", "N/A"),
    )
    assert row.attention_reasons == ("source stale",)


def test_stale_raw_label_without_verified_continuity_stays_in_other():
    raw_stale = replace(
        span(80, "Unsafe Stale", 0, 480, "Repair 1", "stale_open_location"),
        app_work_center_name=None,
    )
    model = _assemble(spans=(raw_stale,))
    row = model.rows[0]
    assert row.section == "other"
    assert row.intervals[0].location_name == "Repair 1"
    assert row.intervals[0].metric_available is False
    assert row.intervals[0].production is None
    assert row.status == "source stale"


def test_unavailable_sources_keep_rows_and_warning_order_without_false_zeroes():
    model = _assemble(
        spans=(
            span(74, "Prod Down", 0, 480, "Repair 1"),
            span(75, "Fork Down", 0, 480, "Tablets"),
        ),
        scores=(score(74, "Prod Down", "Repair 1", 0, 480, 10, 10),),
        downtime_by_wc={"Repair 1": ()},
        events={75: (event("Fork Down", 30, on_time=True),)},
        day_metrics={75: driver_metric(1, 1, 0)},
        source_warnings=("Production data unavailable", "Forklift data unavailable"),
        production_available=False,
        forklift_available=False,
    )
    prod, fork = model.rows
    assert prod.summary == (
        ("Goal", "N/A"),
        ("Uptime", "N/A"),
        ("Downtime", "N/A"),
        ("Production", "N/A"),
    )
    assert fork.summary == (
        ("Calls", "N/A"),
        ("On time", "N/A"),
        ("Handling", "N/A"),
        ("Score", "N/A"),
    )
    assert model.source_warnings == (
        "Production data unavailable",
        "Forklift data unavailable",
    )


def test_incomplete_driver_timeline_hides_summary_and_partial_bars():
    model = _assemble(
        spans=(span(76, "Partial Driver", 0, 480, "Tablets"),),
        events={76: (event("Partial Driver", 30, on_time=True),)},
        day_metrics={
            76: driver_metric(12, 11, 1, timeline_available=False),
        },
    )
    row = model.rows[0]
    assert all(value == "N/A" for _label, value in row.summary)
    assert row.intervals[0].metric_available is False
    assert row.intervals[0].forklift_buckets == ()


def test_stable_interval_keys_include_odoo_identity_and_exact_boundaries():
    model = _assemble(
        spans=(span(77, "Key Worker", 0, 60, "Repair 1"),),
        scores=(score(77, "Key Worker", "Repair 1", 0, 60, 10, 10),),
        downtime_by_wc={"Repair 1": ()},
    )
    interval = model.rows[0].intervals[0]
    assert interval.key == (
        f"77:production:Repair 1:{START.isoformat()}"
    )


def test_open_interval_key_stays_stable_while_the_shared_cap_moves():
    source = span(78, "Open Worker", 0, 480, "Repair 1", is_open=True)
    score_one = score(78, "Open Worker", "Repair 1", 0, 120, 20, 20)
    score_two = score(78, "Open Worker", "Repair 1", 0, 180, 30, 30)
    common = {
        "day": DAY,
        "window_start_utc": START,
        "window_end_utc": END,
        "spans": (source,),
        "downtime_by_wc": {"Repair 1": ()},
        "breakdown_exclusions_by_person_wc": {},
        "forklift_events_by_employee_id": {},
        "forklift_day_metrics_by_employee_id": {},
        "breaks": (),
        "metered_wc_names": {"Repair 1"},
        "source_warnings": (),
        "is_today": True,
    }
    first = assemble_dashboard(
        **common,
        as_of_utc=START + timedelta(minutes=120),
        production_scores=(score_one,),
    )
    second = assemble_dashboard(
        **common,
        as_of_utc=START + timedelta(minutes=180),
        production_scores=(score_two,),
    )
    first_interval = first.rows[0].intervals[0]
    second_interval = second.rows[0].intervals[0]
    assert first_interval.is_open is second_interval.is_open is True
    assert first_interval.end_utc != second_interval.end_utc
    assert first_interval.key == second_interval.key


def test_interval_key_stays_stable_when_open_interval_closes_on_transfer():
    open_source = span(88, "Lifecycle Worker", 0, 480, "Repair 1", is_open=True)
    closed_source = span(88, "Lifecycle Worker", 0, 120, "Repair 1", is_open=False)
    common = {
        "day": DAY,
        "window_start_utc": START,
        "window_end_utc": END,
        "downtime_by_wc": {"Repair 1": ()},
        "breakdown_exclusions_by_person_wc": {},
        "forklift_events_by_employee_id": {},
        "forklift_day_metrics_by_employee_id": {},
        "breaks": (),
        "metered_wc_names": {"Repair 1"},
        "source_warnings": (),
        "is_today": True,
    }
    open_model = assemble_dashboard(
        **common,
        as_of_utc=START + timedelta(minutes=60),
        spans=(open_source,),
        production_scores=(
            score(88, "Lifecycle Worker", "Repair 1", 0, 60, 10, 10),
        ),
    )
    closed_model = assemble_dashboard(
        **common,
        as_of_utc=START + timedelta(minutes=120),
        spans=(closed_source,),
        production_scores=(
            score(88, "Lifecycle Worker", "Repair 1", 0, 120, 20, 20),
        ),
    )
    open_interval = open_model.rows[0].intervals[0]
    closed_interval = closed_model.rows[0].intervals[0]

    assert open_interval.is_open is True
    assert closed_interval.is_open is False
    assert open_interval.end_utc != closed_interval.end_utc
    assert open_interval.key == closed_interval.key


def test_historical_row_is_completed_even_when_source_marks_attendance_open():
    model = _assemble(
        spans=(span(79, "Old Open", 0, 480, "Repair 1", is_open=True),),
        scores=(score(79, "Old Open", "Repair 1", 0, 480, 20, 20),),
        downtime_by_wc={"Repair 1": ()},
        is_today=False,
    )
    row = model.rows[0]
    assert model.is_today is False
    assert row.is_active is False
    assert row.status.startswith("clocked out at ")


def test_identity_backed_breakdown_window_is_removed_from_downtime():
    model = _assemble(
        spans=(span(81, "Break Worker", 0, 60, "Repair 1"),),
        scores=(score(81, "Break Worker", "Repair 1", 0, 60, 20, 20),),
        downtime_by_wc={
            "Repair 1": (
                (
                    START + timedelta(minutes=10),
                    START + timedelta(minutes=20),
                ),
            ),
        },
        breakdowns={
            (81, "Break Worker", "Repair 1"): (
                (
                    START + timedelta(minutes=10),
                    START + timedelta(minutes=20),
                ),
            ),
        },
    )
    metric = model.rows[0].intervals[0].production
    assert metric is not None
    assert metric.downtime_minutes == 0


def test_legacy_name_breakdown_is_not_shared_by_same_named_employees():
    model = _assemble(
        spans=(
            span(85, "Same Break", 0, 60, "Repair 1"),
            span(86, "Same Break", 0, 60, "Repair 1"),
        ),
        scores=(
            score(85, "Same Break", "Repair 1", 0, 60, 20, 20),
            score(86, "Same Break", "Repair 1", 0, 60, 20, 20),
        ),
        downtime_by_wc={
            "Repair 1": (
                (
                    START + timedelta(minutes=10),
                    START + timedelta(minutes=20),
                ),
            ),
        },
        breakdowns={
            ("Same Break", "Repair 1"): (
                (
                    START + timedelta(minutes=10),
                    START + timedelta(minutes=20),
                ),
            ),
        },
    )
    assert [
        row.intervals[0].production.downtime_minutes  # type: ignore[union-attr]
        for row in model.rows
    ] == [10, 10]


def test_malformed_forklift_event_only_makes_its_employee_unavailable():
    malformed = replace(
        event("Bad Driver", 30, on_time=True),
        created_at_utc=None,  # type: ignore[arg-type]
    )
    model = _assemble(
        spans=(
            span(82, "Bad Driver", 0, 480, "Tablets"),
            span(83, "Good Driver", 0, 480, "Tablets"),
        ),
        events={
            82: (malformed,),
            83: (event("Good Driver", 30, on_time=True),),
        },
        day_metrics={
            82: driver_metric(1, 1, 0),
            83: driver_metric(1, 1, 0),
        },
    )
    bad = next(row for row in model.rows if row.employee_odoo_id == 82)
    good = next(row for row in model.rows if row.employee_odoo_id == 83)
    assert bad.attention_reasons == ("metric unavailable",)
    assert all(value == "N/A" for _label, value in bad.summary)
    assert good.summary[0] == ("Calls", "1")


def test_exempt_non_metered_span_is_neutral_not_metric_unavailable():
    model = _assemble(
        spans=(span(84, "Exempt Worker", 0, 480, None, "exempt_no_location"),),
    )
    row = model.rows[0]
    assert row.section == "other"
    assert row.intervals[0].metric_available is True
    assert row.attention_reasons == ()
