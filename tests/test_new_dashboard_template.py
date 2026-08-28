from pathlib import Path
import re

from starlette.requests import Request

from zira_dashboard.deps import templates


ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "src" / "zira_dashboard" / "templates" / "new_dept.html"


def _html():
    return TEMPLATE.read_text(encoding="utf-8")


def _render_new(*, customs=None, new_bars=None, configured_new_meter_count=1,
                new_progress=None, tv_mode=False):
    """Render the actual New template with only the dashboard context it needs.

    This deliberately exercises Jinja rather than checking template source so
    customization regressions are caught in the resulting page markup.
    """
    request = Request({"type": "http", "method": "GET", "path": "/new", "headers": []})
    return templates.get_template("new_dept.html").render(
        request=request,
        static_v=lambda path: "test",
        tv_mode=tv_mode,
        tv_theme="dark",
        window="today",
        custom_range_active=False,
        start="2026-07-10",
        end="2026-07-10",
        layout={},
        customs=customs or {},
        total_units=42,
        pph_per_person=3.5,
        new_bars=new_bars or [],
        configured_new_meter_count=configured_new_meter_count,
        downtime_rows=[],
        elapsed_minutes=0,
        uptime_pct=0,
        new_people=0,
        is_range=False,
        is_today=True,
        now_label="2:41",
        shift_start_label="07:00",
        new_progress=new_progress or [
            {"label": "7:00", "actual": 4, "target": 6, "in_progress": False},
        ],
        new_group_target=24,
        range_includes_today=False,
        refreshed_at="1:00:00 PM",
        assignments_todo_by_wc={},
        all_active_people=[],
        operator_links_by_wc={},
        today="2026-07-10",
        goat_alerts_active=[],
        goat_contenders=[],
        goat_holders=lambda: {},
    )


def _segmented_bar():
    return {
        "name": "Repair 4",
        "who": None,
        "units": 548,
        "expected": 725,
        "pct": 68.0,
        "target_pct": None,
        "pct_of_target": 75.6,
        "color": None,
        "downtime_minutes": 0,
        "has_segments": True,
        "no_one_here_now": True,
        "producer_names": ("Humberto S.", "Ana M."),
        "sole_producer_name": None,
        "show_segment_worker_names": True,
        "segments": [
            {
                "person_name": "Humberto S.",
                "person_label": "Humberto S.",
                "time_label": "7a-2:33p",
                "actual_units": 516.0,
                "goal_units": 700.0,
                "result": "behind",
                "result_label": "184 behind",
                "is_active": False,
                "start_pct": 0.0,
                "actual_pct": 59.0,
                "shortfall_start_pct": 59.0,
                "shortfall_pct": 21.0,
                "finish_pct": 80.0,
                "label_below": False,
            },
            {
                "person_name": "Ana M.",
                "person_label": "Ana M.",
                "time_label": "since 2:35p",
                "actual_units": 32.0,
                "goal_units": 25.0,
                "result": "ahead",
                "result_label": "7 ahead",
                "is_active": True,
                "start_pct": 80.0,
                "actual_pct": 15.0,
                "shortfall_start_pct": 95.0,
                "shortfall_pct": 0.0,
                "finish_pct": 92.0,
                "label_below": True,
            },
        ],
    }


def _stopped_sole_producer_bar():
    bar = _segmented_bar()
    bar.update(
        who=None,
        units=516,
        expected=700,
        producer_names=("Humberto S.",),
        sole_producer_name="Humberto S.",
        show_segment_worker_names=False,
        no_one_here_now=False,
        segments=[bar["segments"][0]],
    )
    return bar


def _legacy_worker_bar():
    bar = _segmented_bar()
    bar.update(
        name="Dismantler 1",
        who="Jesus G.",
        units=567,
        expected=520,
        pct=90.0,
        target_pct=80.0,
        has_segments=False,
        has_worker_history=True,
        uses_split_format=False,
        no_one_here_now=False,
        producer_names=("Jesus G.",),
        sole_producer_name="Jesus G.",
        show_segment_worker_names=False,
    )
    return bar


def test_new_has_full_recycling_range_toolbar():
    html = _html()
    for label in (
        "Today", "Yesterday", "This Week", "Last Week",
        "This Month", "Last Month", "Custom",
    ):
        assert label in html
    assert '<form class="rc-toolbar"' in html
    assert '<div class="edit-bar">' in html


def test_new_is_independent_editable_gridstack_page():
    html = _html()
    assert "/static/vendor/gridstack.min.css" in html
    assert "/static/vendor/gridstack-all.js" in html
    assert "/static/dashboard-grid.js" in html
    assert 'data-layout-page="new"' in html
    assert 'id="reset-layout"' in html


def test_new_default_layout_matches_reference():
    html = _html()
    expected = {
        "kpi-pallets": (0, 0, 2, 3),
        "kpi-palletshr": (0, 3, 2, 3),
        "new-bars": (2, 0, 5, 6),
        "downtime-report": (7, 0, 5, 6),
        "new-progress": (0, 6, 12, 5),
        "new-cumulative": (0, 11, 12, 5),
    }
    for widget_id, defaults in expected.items():
        assert f"widget_attrs('{widget_id}', {', '.join(map(str, defaults))})" in html


def test_new_daily_progress_is_cumulative_bars_and_no_stop_widget():
    html = _html()
    assert "cumulative_progress_chart(new_progress, 'new-cumulative')" in html
    assert "Unplanned Stops" not in html
    assert '<div class="target-line"' in _render_new()


def test_new_keeps_shared_dashboard_surfaces_and_refresh_behavior():
    html = _html()
    for surface in (
        'include "_topnav.html"',
        'include "_performance_subnav.html"',
        'include "_goat_watch_banner.html"',
        '/static/assign-popover.js',
        '/static/worker-stint-popover.js',
        '/static/tv-refresh.js',
        "include '_footer.html'",
    ):
        assert surface in html
    assert "tv_mode or range_includes_today" in html


def test_new_renders_saved_kpi_and_progress_customizations():
    html = _render_new(customs={
        "kpi-pallets": {"align": "right", "color": "#13579b"},
        "kpi-palletshr": {"align": "left", "color": "#97531f"},
        "new-progress": {"color": "#2468ac"},
        "new-cumulative": {"color": "#96351f"},
        "downtime-report": {"color": "#4caf50"},
    })

    assert 'class="grid-stack-item-content align-right"' in html
    assert 'class="grid-stack-item-content align-left"' in html
    assert '--wc: #13579b; color: #13579b !important' in html
    assert '--wc: #97531f; color: #97531f !important' in html
    assert re.search(
        r'gs-id="new-progress".*?style="--hit: #2468ac"', html, re.DOTALL
    )
    assert re.search(
        r'gs-id="new-cumulative".*?style="--good: #96351f"', html, re.DOTALL
    )
    assert re.search(
        r'gs-id="downtime-report".*?style="--good-color: #4caf50"', html, re.DOTALL
    )


def test_new_cumulative_hides_saved_target_line():
    html = _render_new(customs={"new-cumulative": {"show_target": False}})

    assert 'class="cum-progress no-legend no-target"' in html
    assert '<div class="target-line"' not in html


def test_new_cumulative_hides_target_line_when_total_target_is_zero():
    html = _render_new(new_progress=[
        {"label": "7:00", "actual": 4, "target": 0, "in_progress": False},
    ])

    assert '<div class="target-line"' not in html


def test_new_empty_state_distinguishes_unconfigured_meters_from_no_readings():
    unconfigured = _render_new(configured_new_meter_count=0)
    offline = _render_new(configured_new_meter_count=1)

    assert "Configure a Zira meter" in unconfigured
    assert "No readings received" in offline


def test_new_horizontal_bar_keeps_segments_and_moves_details_to_hitareas():
    html = _render_new(new_bars=[_segmented_bar()])

    assert 'class="worker-segment-fill result-behind"' in html
    assert 'class="worker-segment-shortfall"' in html
    assert 'class="worker-segment-goal completed"' in html
    assert 'class="worker-segment-goal live"' in html
    assert html.count('type="button" class="worker-stint-hitarea') == 2
    assert 'class="worker-stint-hitarea has-boundary"' in html
    assert 'style="left:0.0%;width:80.0%"' in html
    assert 'style="left:80.0%;width:15.0%"' in html
    assert 'aria-label="Humberto S. · 7a-2:33p · 516/700 · 184 behind"' in html
    assert 'title="Humberto S. · 7a-2:33p · 516/700 · 184 behind"' in html
    assert 'aria-label="Ana M. · since 2:35p · 32/25 · 7 ahead"' in html
    assert 'class="worker-segment-name"' not in html
    assert 'class="worker-segment-labels"' not in html
    assert 'class="bar-target-line"' not in html


def test_stopped_sole_producer_name_is_left_while_finish_marker_stays_in_bar():
    html = _render_new(new_bars=[_stopped_sole_producer_bar()])

    assert '<span class="name-primary">Humberto S.</span>' in html
    assert '<span class="name-secondary">Repair 4</span>' in html
    assert 'class="worker-segment-goal completed"' in html
    assert 'aria-label="Humberto S. · 7a-2:33p · 516/700 · 184 behind"' in html
    assert '<span class="worker-segment-person">Humberto S.</span>' not in html
    assert "No one here now" not in html


def test_multiple_producer_details_are_accessible_but_not_always_visible():
    html = _render_new(new_bars=[_segmented_bar()])

    assert 'aria-label="Humberto S. · 7a-2:33p · 516/700 · 184 behind"' in html
    assert 'aria-label="Ana M. · since 2:35p · 32/25 · 7 ahead"' in html
    assert '<span class="worker-segment-person">' not in html
    assert '<span class="name-primary">Humberto S.</span>' not in html
    assert '<span class="name-primary">Ana M.</span>' not in html


def test_active_multi_producer_row_identifies_work_center_on_left():
    bar = _segmented_bar()
    bar.update(who="Ana M.", no_one_here_now=False)

    html = _render_new(new_bars=[bar])

    assert '<span class="name-primary">Repair 4</span>' in html
    assert 'aria-label="Humberto S. · 7a-2:33p · 516/700 · 184 behind"' in html
    assert 'aria-label="Ana M. · since 2:35p · 32/25 · 7 ahead"' in html
    assert '<span class="name-primary">Ana M.</span>' not in html


def test_vacant_multi_producer_row_keeps_empty_status_on_left():
    html = _render_new(new_bars=[_segmented_bar()])

    assert '<span class="name-primary current-empty">No one here now</span>' in html
    assert 'aria-label="Humberto S. · 7a-2:33p · 516/700 · 184 behind"' in html
    assert 'aria-label="Ana M. · since 2:35p · 32/25 · 7 ahead"' in html


def test_unassigned_stint_uses_a_neutral_accessible_name():
    bar = _segmented_bar()
    bar["segments"][1].update(
        person_name=None,
        person_label="Unassigned production",
        result="neutral",
        result_label="unassigned",
    )

    html = _render_new(new_bars=[bar])

    assert (
        'aria-label="Unassigned production · since 2:35p · 32/25 · unassigned"'
        in html
    )
    assert '<span class="worker-segment-person">Unassigned production</span>' not in html


def test_vertical_and_tv_views_keep_sole_producer_left_without_duplication():
    vertical = _render_new(
        customs={"new-bars": {"orientation": "vertical"}},
        new_bars=[_stopped_sole_producer_bar()],
    )
    tv = _render_new(tv_mode=True, new_bars=[_stopped_sole_producer_bar()])

    for html in (vertical, tv):
        assert '<span class="name-primary">Humberto S.</span>' in html
        assert '<span class="worker-segment-person">Humberto S.</span>' not in html
        assert 'aria-label="Humberto S. · 7a-2:33p · 516/700 · 184 behind"' in html
        assert "worker-segment-goal completed" in html


def test_new_segmented_bar_keeps_widget_number_position():
    html = _render_new(
        customs={"new-bars": {"number_position": "inside"}},
        new_bars=[_segmented_bar()],
    )
    assert 'class="segment-total in"' in html
    assert ">548<" in html


def test_new_unsegmented_bar_keeps_legacy_fill_and_target():
    bar = _segmented_bar()
    bar.update(
        has_segments=False,
        segments=[],
        no_one_here_now=False,
        target_pct=80.0,
    )
    html = _render_new(new_bars=[bar])
    assert 'class="bar-fill"' in html
    assert 'class="bar-target-line"' in html


def test_new_uninterrupted_worker_uses_legacy_horizontal_bar():
    html = _render_new(new_bars=[_legacy_worker_bar()])
    assert 'class="bar-fill"' in html
    assert 'class="bar-target-line"' in html
    assert 'class="worker-segment-fill' not in html
    assert "Jesus G." in html


def test_new_uninterrupted_worker_uses_legacy_vertical_bar():
    html = _render_new(
        customs={"new-bars": {"orientation": "vertical"}},
        new_bars=[_legacy_worker_bar()],
    )
    assert 'class="vbar-fill"' in html
    assert 'class="vbar-target-line"' in html
    assert 'class="vworker-segment-fill' not in html


def test_new_tv_uninterrupted_worker_keeps_legacy_bar():
    html = _render_new(tv_mode=True, new_bars=[_legacy_worker_bar()])
    assert 'class="bar-fill"' in html
    assert 'class="worker-segment-fill' not in html
    assert "Jesus G." in html


def test_new_tv_keeps_worker_details_available_without_visible_detail_rows():
    html = _render_new(tv_mode=True, new_bars=[_segmented_bar()])

    assert 'aria-label="Humberto S. · 7a-2:33p · 516/700 · 184 behind"' in html
    assert 'aria-label="Ana M. · since 2:35p · 32/25 · 7 ahead"' in html
    assert 'class="worker-segment-name"' not in html
    assert 'class="worker-segment-labels"' not in html


def test_new_vertical_bar_keeps_geometry_without_visible_worker_list():
    html = _render_new(
        customs={"new-bars": {"orientation": "vertical"}},
        new_bars=[_segmented_bar()],
    )

    assert 'class="vworker-segment-fill result-behind"' in html
    assert 'class="vworker-segment-shortfall"' in html
    assert 'class="vworker-segment-goal completed"' in html
    assert 'class="vworker-segment-goal live"' in html
    assert html.count('type="button" class="vworker-stint-hitarea') == 2
    assert 'class="vworker-stint-hitarea has-boundary"' in html
    assert 'aria-label="Humberto S. · 7a-2:33p · 516/700 · 184 behind"' in html
    assert 'aria-label="Ana M. · since 2:35p · 32/25 · 7 ahead"' in html
    assert 'class="vworker-segment-list"' not in html


def test_recycling_and_new_load_worker_stint_details_in_screen_and_tv_modes():
    recycling = (
        ROOT / "src/zira_dashboard/templates/recycling.html"
    ).read_text(encoding="utf-8")
    new_source = _html()
    new_tv = _render_new(tv_mode=True, new_bars=[_segmented_bar()])

    for html in (recycling, new_source, new_tv):
        assert "/static/worker-stint-popover.js" in html


def test_new_completed_shift_keeps_history_without_no_assignment_wording():
    bar = _segmented_bar()
    bar.update(no_one_here_now=False, has_worker_history=True)
    html = _render_new(new_bars=[bar])
    assert "Repair 4" in html and "Humberto S." in html
    assert "No one here now" not in html
    assert "(no assignment)" not in html
