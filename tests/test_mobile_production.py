"""Rendering contracts independent of browser layout."""

import pytest


def phone(html):
    return html.split('<div class="rm-page pm-page">', 1)[1].split('<div class="grid-stack"', 1)[0]


from tests.test_mobile_production_browser import render, fixture


@pytest.mark.parametrize("kind", ["new", "operator"])
def test_tv_has_no_phone_content(kind):
    html = render(kind, tv=True)
    assert "data-production-mobile" not in html
    assert "rm-page pm-page" not in html


@pytest.mark.parametrize(
    "actual,goal,state",
    [
        (0, 0, "no-goal"),
        (33, 0, "no-goal"),
        (0, 60, "below"),
        (59, 60, "below"),
        (60, 60, "met"),
        (61, 60, "met"),
        (180, 60, "met"),
    ],
)
def test_operator_goal_semantics(actual, goal, state):
    html = phone(
        render("operator", pallets=dict(units_today=actual, target_today=goal, target_full_day=240))
    )
    assert f"rm-production-row {state}" in html
    assert ('class="rm-now-marker"' in html) == (goal > 0)
    assert f"{actual} pallets" in html


def test_historical_operator_does_not_use_current_people():
    html = phone(
        render(
            "operator",
            is_today=False,
            is_live_operator_display=False,
            operators_display="Past Worker",
            operator_day="2026-09-20",
        )
    )
    assert 'class="rm-now">Goal' in html
    assert "Past Worker" in html
    assert "Morgan Secondworker" not in html


def test_assignment_permissions_and_stints():
    c = fixture("new")
    c["new_bars"][0]["segments"] = [
        dict(person_label="Earlier Worker", time_label="7–8 AM", actual_units=20, goal_units=25)
    ]
    c["new_bars"][0]["has_worker_history"] = True
    c.update(assignments_todo_by_wc={"Dismantler 1": dict(first_iso="start", last_iso="end")})
    c["can_operate"] = lambda: False
    html = phone(render("new", **c))
    assert "no-assign-btn" not in html
    assert "Earlier Worker" in html
    assert "includes work across operators" in html


def test_empty_new_preserves_setup_message():
    html = phone(
        render("new", new_bars=[], new_progress=[], downtime_rows=[], configured_new_meter_count=0)
    )
    assert "Configure a Zira meter" in html


def test_empty_operator_preserves_all_sections():
    html = phone(
        render(
            "operator",
            no_activity=True,
            progress_buckets=[],
            downtime_elapsed_minutes=0,
            pallets=dict(units_today=0, target_today=0, target_full_day=0),
        )
    )
    assert "no-goal" in html
    assert "No shift data" in html
    assert "No tracked downtime" in html


def test_custom_titles_survive_phone_presentation():
    html = phone(
        render(
            "new",
            customs={
                key: dict(title="Custom " + key)
                for key in [
                    "kpi-pallets",
                    "kpi-palletshr",
                    "new-bars",
                    "new-cumulative",
                    "new-progress",
                    "downtime-report",
                ]
            },
        )
    )
    for key in [
        "kpi-pallets",
        "kpi-palletshr",
        "new-bars",
        "new-cumulative",
        "new-progress",
        "downtime-report",
    ]:
        assert "Custom " + key in html
