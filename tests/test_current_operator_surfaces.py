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
from zira_dashboard.deps import templates
from zira_dashboard.routes import departments
from zira_dashboard.stations import Station


DAY = date(2026, 9, 10)
NOW = datetime(2026, 9, 10, 16, 0, tzinfo=UTC)
OPERATOR_ROWS = (
    current_operators.OperatorDisplayRow("Planned Person", 11, True, False),
    current_operators.OperatorDisplayRow("Christian C.", 8, False, True),
)


def test_zero_production_station_is_active_from_unplanned_physical_presence(
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
    assert live["active_wc_names"] == {"Dismantler 3"}
    assert live["per_wc_units"] == {"Dismantler 3": 0}


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

    returned = departments._attach_current_operator_rows(
        bars, {"Repair 4": OPERATOR_ROWS}
    )

    assert returned is bars
    assert bars[0]["has_segments"] is True
    assert len(bars[0]["segments"]) == 2
    assert bars[0]["current_operators"] == OPERATOR_ROWS
    assert {key: value for key, value in bars[0].items() if key != "current_operators"} == (
        before[0]
    )


def _range_day(current_operator_rows):
    return {
        "total_units": 100,
        "total_downtime": 0,
        "elapsed": 60,
        "available": 60,
        "uptime_minutes": 60,
        "total_man_hours": 1.0,
        "active_wc_names": {"Dismantler 3"},
        "per_wc_units": {"Dismantler 3": 100},
        "per_wc_downtime": {"Dismantler 3": 0},
        "per_wc_expected": {"Dismantler 3": 90.0},
        "per_wc_who": {"Dismantler 3": "Christian C."},
        "per_wc_category": {"Dismantler 3": "Dismantler"},
        "per_wc_station_obj": {"Dismantler 3": object()},
        "schedule_assignments": {"Dismantler 3": []},
        "per_wc_segments": {},
        "per_wc_segment_display": {},
        "per_wc_producers": {},
        "is_live_dashboard": True,
        "current_operator_rows": current_operator_rows,
    }


def test_range_aggregate_carries_current_rows_only_for_single_day():
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

    assert single.single_day_current_operator_rows == {
        "Dismantler 3": OPERATOR_ROWS
    }
    assert ranged.single_day_current_operator_rows == {}


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
