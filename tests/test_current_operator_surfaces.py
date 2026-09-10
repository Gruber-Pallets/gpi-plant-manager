from __future__ import annotations

from copy import deepcopy
from datetime import UTC, date, datetime, time
from types import SimpleNamespace

from zira_dashboard import (
    assignment_windows,
    attendance,
    attendance_timeline,
    current_operators,
    live_cache,
    machine_breakdown,
    production_segments,
    recycling_data,
    recycling_range,
    settings_store,
    shift_config,
    staffing,
    wc_attributions,
)
from zira_dashboard.assignment_windows import WorkSegment
from zira_dashboard.deps import templates
from zira_dashboard.routes import departments
from zira_dashboard.stations import Station


DAY = date(2026, 9, 10)
NOW = datetime(2026, 9, 10, 16, 0, tzinfo=UTC)
OPERATOR_ROWS = (
    current_operators.OperatorDisplayRow("Planned Person", 11, True, False),
    current_operators.OperatorDisplayRow("Christian C.", 8, False, True),
)


def test_zero_production_current_operator_is_appended_only_after_bar_build(
    monkeypatch,
):
    station = Station("dismantler-3", "Dismantler 3", "Dismantler", "Recycling")
    span = attendance_timeline.LocationSpan(
        employee_odoo_id=8,
        employee_name="Christian C.",
        start_utc=NOW.replace(hour=12),
        end_utc=NOW,
        status="valid",
        app_work_center_name="Dismantler 3",
        odoo_work_center_id=77,
        odoo_work_center_name="Dismantler #3",
        attendance_ids=(91,),
        department_repair=None,
    )
    snapshot = SimpleNamespace(
        policy=live_cache.AttendanceReadPolicy(
            mirror_owned=True,
            available=True,
            refreshed_at=NOW,
            mode="shadow",
        ),
        spans=(span,),
        verified_cap_utc=NOW,
        current_attendance_ids=frozenset({91}),
    )
    monkeypatch.setattr(
        departments,
        "leaderboard",
        lambda *_args, **_kwargs: [
            SimpleNamespace(
                station=station,
                units=0,
                downtime_minutes=0,
                active_intervals=(),
                last_reading_at=None,
                samples=(),
            )
        ],
    )
    monkeypatch.setattr(
        staffing,
        "load_schedule",
        lambda day: staffing.Schedule(
            day=day,
            published=True,
            assignments={
                "Dismantler 1": ["Planned Person"],
                "Dismantler 3": [],
            },
        ),
    )
    monkeypatch.setattr(departments, "_absent_names", lambda _day: set())
    monkeypatch.setattr(departments, "shift_elapsed_minutes", lambda *_args: 240)
    monkeypatch.setattr(shift_config, "shift_start_for", lambda _day: time(7))
    monkeypatch.setattr(shift_config, "shift_end_for", lambda _day: time(15))
    monkeypatch.setattr(shift_config, "breaks_for", lambda _day: ())
    monkeypatch.setattr(
        shift_config,
        "productive_minutes_in_window",
        lambda _day, start, end: (end - start).total_seconds() / 60,
    )
    monkeypatch.setattr(settings_store, "station_target", lambda _station: 60)
    monkeypatch.setattr(departments, "progress_buckets", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(attendance, "partial_off_intervals", lambda _day: {})
    monkeypatch.setattr(
        attendance,
        "name_to_person_id",
        lambda: {"Planned Person": "11", "Christian C.": "8"},
    )
    monkeypatch.setattr(wc_attributions, "breakdown_windows_for_day", lambda _day: {})
    monkeypatch.setattr(
        machine_breakdown,
        "excluded_minutes_overlapping",
        lambda *_args, **_kwargs: 0.0,
    )
    monkeypatch.setattr(
        production_segments, "credit_work_segments", lambda *_args, **_kwargs: ()
    )
    monkeypatch.setattr(
        production_segments, "score_work_segments", lambda *_args, **_kwargs: ()
    )
    monkeypatch.setattr(
        departments,
        "_canonical_department_segments",
        lambda *_args, **_kwargs: SimpleNamespace(
            segments=(),
            cap_utc=NOW,
            location_snapshot=snapshot,
        ),
    )

    live = departments._department_day_data(
        DAY,
        NOW,
        True,
        stations=[station],
        labor_department="Recycled",
        group_categories=("Dismantler",),
    )

    assert live["current_operator_rows"]["Dismantler 3"] == (
        current_operators.OperatorDisplayRow(
            "Christian C.", 8, planned=False, physically_present=True
        ),
    )
    assert live["schedule_assignments"]["Dismantler 3"] == []
    assert live["active_wc_names"] == set()
    assert live["per_wc_units"] == {}
    assert live["per_wc_downtime"] == {}
    assert live["per_wc_expected"] == {}
    assert live["per_wc_state"] == {}
    assert live["per_wc_who"] == {}
    assert live["per_wc_category"] == {}
    assert live["per_wc_station_obj"] == {}

    built = recycling_data.build_bars(
        "Dismantler",
        agg_active_names=live["active_wc_names"],
        agg_category=live["per_wc_category"],
        agg_units=live["per_wc_units"],
        agg_expected=live["per_wc_expected"],
        agg_who_today=live["per_wc_who"],
        is_range=False,
        agg_downtime=live["per_wc_downtime"],
    )
    assert built == []

    presented = departments._present_current_operator_rows(
        built,
        live["current_operator_rows"],
        configured_stations=[station],
        categories=("Dismantler",),
        is_live=True,
        is_range=False,
        row_kind="bar",
    )

    assert presented == [
        {
            "name": "Dismantler 3",
            "who": None,
            "units": 0,
            "pct_of_target": None,
            "expected": 0,
            "color": None,
            "downtime_minutes": 0,
            "uses_split_format": False,
            "producer_names": (),
            "sole_producer_name": None,
            "show_segment_worker_names": False,
            "segments": [],
            "has_segments": False,
            "has_worker_history": False,
            "no_one_here_now": False,
            "pct": 0.0,
            "target_pct": None,
            "current_operators": (
                current_operators.OperatorDisplayRow(
                    "Christian C.", 8, planned=False, physically_present=True
                ),
            ),
        }
    ]

    downtime_rows = departments._present_current_operator_rows(
        recycling_data.build_downtime_rows(
            agg_active_names=live["active_wc_names"],
            agg_category=live["per_wc_category"],
            agg_downtime=live["per_wc_downtime"],
            total_elapsed=240,
            agg_who_today=live["per_wc_who"],
            is_range=False,
            categories=("Dismantler",),
        ),
        live["current_operator_rows"],
        configured_stations=[station],
        categories=("Dismantler",),
        is_live=True,
        is_range=False,
        row_kind="downtime",
    )
    assert downtime_rows == [
        {
            "name": "Dismantler 3",
            "who": None,
            "working": 0,
            "down": 0,
            "working_pct": 0.0,
            "down_pct": 0.0,
            "current_operators": (
                current_operators.OperatorDisplayRow(
                    "Christian C.", 8, planned=False, physically_present=True
                ),
            ),
        }
    ]


def test_canonical_department_projection_reuses_exact_location_snapshot(monkeypatch):
    snapshot = SimpleNamespace(
        policy=live_cache.AttendanceReadPolicy(
            mirror_owned=True,
            available=True,
            refreshed_at=NOW,
            mode="shadow",
        ),
        spans=(),
        verified_cap_utc=NOW,
        current_attendance_ids=frozenset(),
    )
    monkeypatch.setattr(
        "zira_dashboard.attendance_location_snapshot.read_location_snapshot",
        lambda *_args, **_kwargs: snapshot,
    )
    monkeypatch.setattr(
        assignment_windows,
        "work_segments_from_timeline",
        lambda *_args, **_kwargs: (),
    )

    projection = departments._canonical_department_segments(
        DAY,
        datetime.combine(DAY, time(7), tzinfo=UTC),
        NOW,
        now_utc=NOW,
    )

    assert projection.location_snapshot is snapshot


def test_real_transfer_bar_is_unchanged_when_current_rows_are_attached():
    bars = recycling_data.build_bars(
        "Repair",
        agg_active_names={"Repair 4"},
        agg_category={"Repair 4": "Repair"},
        agg_units={"Repair 4": 548},
        agg_expected={"Repair 4": 725.0},
        agg_who_today={"Repair 4": "Ana M."},
        is_range=False,
        agg_downtime={},
        agg_segments={
            "Repair 4": (
                {
                    "person_name": "Humberto S.",
                    "person_label": "Humberto S.",
                    "time_label": "7a-2:33p",
                    "actual_units": 516.0,
                    "goal_units": 700.0,
                    "runway_units": 700.0,
                    "is_active": False,
                    "result": "behind",
                    "result_label": "184 behind",
                },
                {
                    "person_name": "Ana M.",
                    "person_label": "Ana M.",
                    "time_label": "since 2:35p",
                    "actual_units": 32.0,
                    "goal_units": 25.0,
                    "runway_units": 32.0,
                    "is_active": True,
                    "result": "ahead",
                    "result_label": "7 ahead",
                },
            )
        },
        agg_segment_display={"Repair 4": True},
        agg_producers={"Repair 4": ("Humberto S.", "Ana M.")},
        is_live=True,
    )
    before = deepcopy(bars)

    returned = departments._present_current_operator_rows(
        bars,
        {"Repair 4": OPERATOR_ROWS},
        configured_stations=[
            Station("repair-4", "Repair 4", "Repair", "Recycling")
        ],
        categories=("Repair",),
        is_live=True,
        is_range=False,
        row_kind="bar",
    )

    assert returned is bars
    assert bars[0]["has_segments"] is True
    assert len(bars[0]["segments"]) == 2
    assert bars[0]["current_operators"] == OPERATOR_ROWS
    assert {key: value for key, value in bars[0].items() if key != "current_operators"} == (
        before[0]
    )


def _build_and_render_midday_transfer(current_rows):
    def at(hour, minute=0):
        return datetime(2026, 9, 10, hour, minute, tzinfo=UTC)

    credits = production_segments.credit_work_segments(
        (
            WorkSegment("Repair 4", "Humberto S.", at(12), at(19, 33), "punch"),
            WorkSegment("Repair 4", "Ana M.", at(19, 35), at(19, 50), "punch"),
        ),
        wc_totals={"Repair 4": 548.0},
        samples_by_wc={
            "Repair 4": (
                (at(18), 516.0),
                (at(19, 40), 32.0),
            )
        },
        productive_minutes=lambda person, *_args: {
            "Humberto S.": 420.0,
            "Ana M.": 15.0,
        }[person],
        live_cap_utc=at(19, 50),
    )
    scored = production_segments.score_work_segments(
        credits,
        target_per_hour={"Repair 4": 100.0},
    )
    segments, split_by_wc, producers, _continuous = (
        departments._prepare_segment_display(
            scored,
            break_windows=(),
            window_start_utc=at(12),
            window_end_utc=at(19, 50),
            is_live=True,
        )
    )
    bars = recycling_data.build_bars(
        "Repair",
        agg_active_names={"Repair 4"},
        agg_category={"Repair 4": "Repair"},
        agg_units={"Repair 4": 548},
        agg_expected={"Repair 4": 725.0},
        agg_who_today={"Repair 4": "Ana M."},
        is_range=False,
        agg_downtime={},
        agg_segments=segments,
        agg_segment_display=split_by_wc,
        agg_producers=producers,
        is_live=True,
    )
    departments._present_current_operator_rows(
        bars,
        {"Repair 4": current_rows},
        configured_stations=[
            Station("repair-4", "Repair 4", "Repair", "Recycling")
        ],
        categories=("Repair",),
        is_live=True,
        is_range=False,
        row_kind="bar",
    )
    return bars[0], _render_bar_with_item(bars[0], orientation="horizontal")


def test_midday_transfer_math_and_markup_change_only_current_operator_labels():
    original_rows = OPERATOR_ROWS
    substitute_rows = (
        current_operators.OperatorDisplayRow(
            "Different Current Person", 21, planned=False, physically_present=True
        ),
        current_operators.OperatorDisplayRow(
            "Different Planned Person", 22, planned=True, physically_present=False
        ),
    )

    original_bar, original_html = _build_and_render_midday_transfer(original_rows)
    substitute_bar, substitute_html = _build_and_render_midday_transfer(substitute_rows)

    segment_fields = (
        "person_name",
        "person_label",
        "time_label",
        "actual_units",
        "goal_units",
        "result",
        "result_label",
        "runway_units",
        "runway_pct",
        "start_pct",
        "actual_pct",
        "shortfall_start_pct",
        "shortfall_pct",
        "finish_pct",
    )
    assert [
        tuple(segment[field] for field in segment_fields)
        for segment in original_bar["segments"]
    ] == [
        tuple(segment[field] for field in segment_fields)
        for segment in substitute_bar["segments"]
    ]
    assert {
        key: value for key, value in original_bar.items() if key != "current_operators"
    } == {
        key: value for key, value in substitute_bar.items() if key != "current_operators"
    }

    assert (
        '<span class="name-primary current-operator planned-only">Planned Person</span>'
        in original_html
    )
    assert (
        '<span class="name-primary current-operator physically-present">Christian C.</span>'
        in original_html
    )
    assert (
        '<span class="name-primary current-operator physically-present">'
        "Different Current Person</span>"
        in substitute_html
    )
    assert (
        '<span class="name-primary current-operator planned-only">'
        "Different Planned Person</span>"
        in substitute_html
    )

    def without_current_label_differences(html, rows):
        for row in rows:
            html = html.replace(row.person_name, "CURRENT OPERATOR")
        return html.replace("planned-only", "current-state").replace(
            "physically-present", "current-state"
        )

    assert without_current_label_differences(
        original_html, original_rows
    ) == without_current_label_differences(substitute_html, substitute_rows)


def _range_day(current_operator_rows):
    return {
        "total_units": 100,
        "total_downtime": 0,
        "elapsed": 60,
        "available": 60,
        "uptime_minutes": 60,
        "total_man_hours": 1.0,
        "active_wc_names": {"Repair 1"},
        "per_wc_units": {"Repair 1": 100},
        "per_wc_downtime": {"Repair 1": 0},
        "per_wc_expected": {"Repair 1": 90.0},
        "per_wc_who": {"Repair 1": "Production Worker"},
        "per_wc_category": {"Repair 1": "Repair"},
        "per_wc_station_obj": {"Repair 1": object()},
        "schedule_assignments": {"Repair 1": ["Production Worker"]},
        "per_wc_segments": {},
        "per_wc_segment_display": {},
        "per_wc_producers": {},
        "is_live_dashboard": True,
        "current_operator_rows": current_operator_rows,
    }


def test_range_aggregate_maps_ignore_current_operator_rows():
    single = recycling_range.aggregate_range(
        [_range_day({"Dismantler 3": OPERATOR_ROWS})],
        [DAY],
        is_range=False,
    )
    ranged = recycling_range.aggregate_range(
        [
            _range_day({"Dismantler 3": OPERATOR_ROWS}),
            _range_day({"Dismantler 3": OPERATOR_ROWS}),
        ],
        [DAY, DAY],
        is_range=True,
    )

    for result in (single, ranged):
        assert result.agg_active_names == {"Repair 1"}
        assert result.agg_units == {"Repair 1": 100 if result is single else 200}
        assert result.agg_downtime == {"Repair 1": 0}
        assert result.agg_expected == {
            "Repair 1": 90.0 if result is single else 180.0
        }
        assert result.agg_category == {"Repair 1": "Repair"}
        assert not hasattr(result, "single_day_current_operator_rows")


def test_current_only_rows_require_nonempty_configured_live_matching_category():
    dismantler = Station(
        "dismantler-3", "Dismantler 3", "Dismantler", "Recycling"
    )
    empty_dismantler = Station(
        "dismantler-2", "Dismantler 2", "Dismantler", "Recycling"
    )
    repair = Station("repair-1", "Repair 1", "Repair", "Recycling")
    by_work_center = {
        "Dismantler 3": OPERATOR_ROWS,
        "Repair 1": OPERATOR_ROWS,
        "Dismantler 4": OPERATOR_ROWS,
        "Dismantler 2": (),
    }

    presented = departments._present_current_operator_rows(
        [],
        by_work_center,
        configured_stations=[dismantler, empty_dismantler, repair],
        categories=("Dismantler",),
        is_live=True,
        is_range=False,
        row_kind="bar",
    )
    range_rows = departments._present_current_operator_rows(
        [],
        by_work_center,
        configured_stations=[dismantler, empty_dismantler, repair],
        categories=("Dismantler",),
        is_live=True,
        is_range=True,
        row_kind="bar",
    )
    historical_rows = departments._present_current_operator_rows(
        [],
        by_work_center,
        configured_stations=[dismantler, empty_dismantler, repair],
        categories=("Dismantler",),
        is_live=False,
        is_range=False,
        row_kind="bar",
    )

    assert [row["name"] for row in presented] == ["Dismantler 3"]
    assert range_rows == []
    assert historical_rows == []


def _bar(*, name: str = "Dismantler 3", current_rows=OPERATOR_ROWS):
    return {
        "name": name,
        "who": "Past Worker",
        "units": 100,
        "expected": 90,
        "pct": 80,
        "target_pct": None,
        "pct_of_target": 111,
        "color": None,
        "has_segments": False,
        "segments": (),
        "sole_producer_name": "Past Worker",
        "no_one_here_now": False,
        "show_segment_worker_names": False,
        "has_worker_history": True,
        "current_operators": current_rows,
    }


def _render_bar(*, orientation: str) -> str:
    return _render_bar_with_item(_bar(), orientation=orientation)


def _render_bar_with_item(item, *, orientation: str) -> str:
    template = templates.env.from_string(
        '{% from "_department_dashboard_widgets.html" import '
        "department_bar_chart with context %}"
        "{{ department_bar_chart('bars', items) }}"
    )
    return template.render(
        items=[item],
        customs={"bars": {"orientation": orientation}},
        is_range=False,
        tv_mode=orientation == "vertical",
        operator_links_by_wc={},
        assignments_todo_by_wc={},
        today=DAY.isoformat(),
        shift_start_label="7:00",
        now_label="11:00",
        goat_badges=lambda *_args: "",
        goat_holders=lambda: {},
    )


def test_horizontal_and_vertical_labels_render_presence_classes():
    for orientation in ("horizontal", "vertical"):
        html = _render_bar(orientation=orientation)
        assert "current-operator planned-only" in html
        assert "current-operator physically-present" in html
        assert "Planned Person" in html
        assert "Christian C." in html
        assert "Dismantler 3" in html
        assert (
            'title="Planned Person + Christian C. — 100 / 90 expected (111%)"'
            in html
        )
        assert 'title="Past Worker — 100 / 90 expected (111%)"' not in html
        assert "from Odoo" not in html
        assert "unplanned" not in html


def test_bar_title_keeps_legacy_name_without_current_rows():
    for orientation in ("horizontal", "vertical"):
        html = _render_bar_with_item(_bar(current_rows=()), orientation=orientation)

        assert 'title="Past Worker — 100 / 90 expected (111%)"' in html


def _render_downtime(*, current_rows):
    template = templates.env.from_string(
        '{% from "_department_dashboard_widgets.html" import '
        "department_downtime_report with context %}"
        "{{ department_downtime_report(rows, 60, 100) }}"
    )
    row = {
        "name": "Dismantler 3",
        "who": "Past Worker",
        "working": 60,
        "down": 0,
        "working_pct": 100,
        "down_pct": 0,
        "current_operators": current_rows,
    }

    return template.render(
        rows=[row],
        is_range=False,
        tv_mode=False,
        operator_links_by_wc={},
        goat_badges=lambda *_args: "",
        goat_holders=lambda: {},
    )


def test_downtime_label_renders_presence_classes():
    html = _render_downtime(current_rows=OPERATOR_ROWS)

    assert "current-operator planned-only" in html
    assert "current-operator physically-present" in html
    assert "Dismantler 3" in html
    assert (
        'title="Planned Person + Christian C. — Working 60m · Down 0m"'
        in html
    )
    assert 'title="Past Worker — Working 60m · Down 0m"' not in html


def test_downtime_title_keeps_legacy_name_without_current_rows():
    html = _render_downtime(current_rows=())

    assert 'title="Past Worker — Working 60m · Down 0m"' in html
