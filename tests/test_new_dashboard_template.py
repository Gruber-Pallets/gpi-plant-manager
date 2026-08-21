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


def test_new_horizontal_bar_renders_worker_segments_and_finish_states():
    html = _render_new(new_bars=[_segmented_bar()])
    assert 'class="worker-segment-fill result-behind"' in html
    assert 'class="worker-segment-shortfall"' in html
    assert 'class="worker-segment-goal completed"' in html
    assert 'class="worker-segment-goal live"' in html
    assert "Humberto S." in html and "7a-2:33p" in html
    assert "Ana M." in html and "since 2:35p" in html
    assert "184 behind" in html and "7 ahead" in html
    assert 'class="worker-segment-result"' in html
    assert "516/700" in html and "32/25" in html
    assert "No one here now" in html
    assert 'class="bar-target-line"' not in html


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


def test_new_tv_keeps_full_worker_text_visible_in_shared_markup():
    html = _render_new(tv_mode=True, new_bars=[_segmented_bar()])
    assert "Humberto S." in html and "7a-2:33p" in html
    assert "516/700" in html and "184 behind" in html
    assert "Ana M." in html and "32/25" in html and "7 ahead" in html


def test_new_vertical_bar_renders_segment_blocks_finish_markers_and_visible_list():
    html = _render_new(
        customs={"new-bars": {"orientation": "vertical"}},
        new_bars=[_segmented_bar()],
    )
    assert 'class="vworker-segment-fill result-behind"' in html
    assert 'class="vworker-segment-shortfall"' in html
    assert 'class="vworker-segment-goal completed"' in html
    assert 'class="vworker-segment-goal live"' in html
    assert 'class="vworker-segment-list"' in html
    assert "Humberto S." in html and "184 behind" in html
    assert "Ana M." in html and "7 ahead" in html


def test_new_completed_shift_keeps_history_without_no_assignment_wording():
    bar = _segmented_bar()
    bar.update(no_one_here_now=False, has_worker_history=True)
    html = _render_new(new_bars=[bar])
    assert "Repair 4" in html and "Humberto S." in html
    assert "No one here now" not in html
    assert "(no assignment)" not in html
