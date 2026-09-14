from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace

import pytest

from zira_dashboard import (
    current_operators,
    db,
    goat_categories,
    goat_watch,
    production_history,
    shift_config,
)
from zira_dashboard.deps import templates


def test_next_business_day_uses_configured_fallback_when_shared_lookup_raises(
    monkeypatch,
):
    monkeypatch.setattr(
        shift_config,
        "is_workday",
        lambda _candidate: (_ for _ in ()).throw(RuntimeError("lookup failed")),
    )
    monkeypatch.setattr(
        shift_config,
        "work_weekdays",
        lambda: frozenset({1}),
    )

    assert goat_watch.next_business_day(date(2026, 7, 3)) == date(2026, 7, 7)


def test_next_business_day_returns_next_calendar_day_when_search_is_exhausted(
    monkeypatch,
):
    monkeypatch.setattr(shift_config, "is_workday", lambda _candidate: False)

    assert goat_watch.next_business_day(date(2026, 7, 3)) == date(2026, 7, 4)


def test_goat_alert_remains_visible_through_closed_holiday(monkeypatch):
    friday = date(2026, 11, 27)
    monday_holiday = date(2026, 11, 30)
    tuesday = date(2026, 12, 1)
    monkeypatch.setattr(goat_watch, "maybe_finalize_today", lambda _today: None)
    monkeypatch.setattr(
        shift_config,
        "is_workday",
        lambda candidate: candidate.weekday() < 5 and candidate != monday_holiday,
    )
    monkeypatch.setattr(
        db,
        "query",
        lambda *_args, **_kwargs: [
            {
                "id": 1,
                "category_key": "repairs",
                "achieved_day": friday,
                "group_name": "Repair",
                "person": "Ana",
                "wc_name": "Repair 1",
                "units": 240,
                "prior_record_units": 230,
                "prior_record_holder": "Ben",
                "prior_record_day": date(2026, 10, 2),
            }
        ],
    )

    assert [row["id"] for row in goat_watch.active_alerts(tuesday)] == [1]


def test_active_alerts_exclude_future_and_noncanonical_rows(monkeypatch):
    today = date(2026, 8, 12)
    monkeypatch.setattr(goat_watch, "maybe_finalize_today", lambda _today: None)
    monkeypatch.setattr(
        db,
        "query",
        lambda *_args, **_kwargs: [
            {
                "id": 1,
                "category_key": "repairs",
                "achieved_day": date(2099, 1, 2),
            },
            {
                "id": 2,
                "category_key": "pytest-goat",
                "achieved_day": today,
            },
        ],
    )

    assert goat_watch.active_alerts(today) == []


def test_active_alerts_hide_saved_hand_build_alert_before_day_30(monkeypatch):
    today = date(2026, 8, 18)
    monkeypatch.setattr(goat_watch, "maybe_finalize_today", lambda _today: None)
    monkeypatch.setattr(
        db,
        "query",
        lambda *_args, **_kwargs: [
            {
                "id": 1,
                "category_key": "hand_build",
                "achieved_day": today,
                "group_name": "Hand Build",
                "person": "Builder",
                "wc_name": "Hand Build #1",
                "units": 500,
                "prior_record_units": 490,
                "prior_record_holder": "Other Builder",
                "prior_record_day": date(2026, 8, 17),
            }
        ],
    )
    monkeypatch.setattr(
        goat_categories,
        "work_center_names",
        lambda _: {"Hand Build #1"},
    )
    monkeypatch.setattr(
        production_history,
        "daily_records",
        lambda *_: [
            {
                "day": date(2026, 7, 1),
                "person": "Builder",
                "wc": "Hand Build #1",
                "units": 100,
            }
        ],
    )

    assert goat_watch.active_alerts(today) == []


def test_active_alerts_do_not_replay_day_29_alert_after_day_30(monkeypatch):
    achieved_day = date(2026, 8, 18)
    today = date(2026, 8, 19)
    monkeypatch.setattr(goat_watch, "maybe_finalize_today", lambda _today: None)
    monkeypatch.setattr(
        db,
        "query",
        lambda *_args, **_kwargs: [
            {
                "id": 1,
                "category_key": "hand_build",
                "achieved_day": achieved_day,
                "group_name": "Hand Build",
                "person": "Builder",
                "wc_name": "Hand Build #1",
                "units": 500,
                "prior_record_units": 490,
                "prior_record_holder": "Other Builder",
                "prior_record_day": date(2026, 8, 17),
            }
        ],
    )
    monkeypatch.setattr(
        goat_categories,
        "work_center_names",
        lambda _: {"Hand Build #1"},
    )
    records = [
        {
            "day": date(2026, 7, 1) + timedelta(days=offset),
            "person": "Builder",
            "wc": "Hand Build #1",
            "units": 100,
        }
        for offset in range(30)
    ]
    requested_through = []

    def daily_records(_start, end):
        requested_through.append(end)
        return records[:29] if end == achieved_day else records

    monkeypatch.setattr(production_history, "daily_records", daily_records)

    assert goat_watch.active_alerts(today) == []
    assert requested_through == [achieved_day]


def test_live_contender_uses_frozen_current_operator_rows(monkeypatch):
    day = date(2026, 9, 10)
    now = datetime(2026, 9, 10, 18, 0, tzinfo=UTC)
    rows = (
        current_operators.OperatorDisplayRow("Planned Person", 11, True, False),
        current_operators.OperatorDisplayRow("Christian C.", 8, False, True),
    )
    monkeypatch.setattr(goat_watch, "_final_break_passed", lambda *_args: True)
    monkeypatch.setattr(goat_watch, "_shift_elapsed_fraction", lambda *_args: 0.5)
    monkeypatch.setattr(goat_watch, "_group_names_today", lambda: ["Dismantler"])
    monkeypatch.setattr(
        "zira_dashboard.awards.goat",
        lambda _group: {
            "units": 200,
            "name": "Record Holder",
            "day": date(2026, 9, 1),
        },
    )
    monkeypatch.setattr(
        "zira_dashboard.work_centers_store.members",
        lambda *_args: [SimpleNamespace(name="Dismantler 3")],
    )
    monkeypatch.setattr(goat_watch, "_wc_units_today", lambda *_args: 100)
    monkeypatch.setattr(
        goat_watch,
        "_primary_operator",
        lambda *_args: (_ for _ in ()).throw(
            AssertionError("live GOAT must not read the schedule again")
        ),
    )

    contenders = goat_watch.contenders_for_now(
        day,
        now,
        current_operator_rows_by_wc={"Dismantler 3": rows},
        eligible_planned_work_centers={"Dismantler 3"},
    )

    assert contenders[0].current_operators == rows


@pytest.mark.parametrize(
    "ineligible_rows",
    [
        (),
        (
            current_operators.OperatorDisplayRow(
                "Unplanned Physical Person", 8, False, True
            ),
        ),
    ],
)
def test_frozen_goat_path_keeps_plan_eligibility_separate_from_labels(
    monkeypatch,
    ineligible_rows,
):
    day = date(2026, 9, 10)
    now = datetime(2026, 9, 10, 18, 0, tzinfo=UTC)
    eligible_rows = (
        current_operators.OperatorDisplayRow("Scheduled Person", 11, True, True),
    )
    monkeypatch.setattr(goat_watch, "_final_break_passed", lambda *_args: True)
    monkeypatch.setattr(goat_watch, "_shift_elapsed_fraction", lambda *_args: 0.5)
    monkeypatch.setattr(goat_watch, "_group_names_today", lambda: ["Dismantler"])
    monkeypatch.setattr(
        "zira_dashboard.awards.goat",
        lambda _group: {
            "units": 180,
            "name": "Record Holder",
            "day": date(2026, 9, 1),
        },
    )
    monkeypatch.setattr(
        "zira_dashboard.work_centers_store.members",
        lambda *_args: [
            SimpleNamespace(name="Dismantler 3"),
            SimpleNamespace(name="Dismantler 2"),
        ],
    )
    monkeypatch.setattr(
        goat_watch,
        "_wc_units_today",
        lambda wc_name, _day: {
            "Dismantler 3": 120,
            "Dismantler 2": 100,
        }[wc_name],
    )
    monkeypatch.setattr(
        goat_watch,
        "_primary_operator",
        lambda *_args: (_ for _ in ()).throw(
            AssertionError("frozen GOAT path must not reread the schedule")
        ),
    )

    contenders = goat_watch.contenders_for_now(
        day,
        now,
        current_operator_rows_by_wc={
            "Dismantler 3": ineligible_rows,
            "Dismantler 2": eligible_rows,
        },
        eligible_planned_work_centers={"Dismantler 2"},
    )

    assert [(row.wc, row.person, row.current_operators) for row in contenders] == [
        ("Dismantler 2", "Scheduled Person", eligible_rows)
    ]


def test_live_contender_banner_renders_producers_after_departure():
    contender = SimpleNamespace(
        group="Dismantler",
        wc="Dismantler 3",
        projected=220,
        record_units=200,
        record_holder="Record Holder",
        record_day=date(2026, 9, 1),
        producer_names=("Earlier Producer", "Shared Producer"),
        current_operators=(
            current_operators.OperatorDisplayRow(
                "Planned Person", 11, True, False
            ),
            current_operators.OperatorDisplayRow(
                "Christian C.", 8, False, True
            ),
        ),
    )

    html = templates.get_template("_goat_watch_banner.html").render(
        goat_alerts_active=[],
        goat_contenders=[contender],
    )

    assert "Earlier Producer" in html
    assert "Shared Producer" in html
    assert "Planned Person" not in html
    assert "Christian C." not in html
    assert "No one here now" not in html
    assert "physically-present" not in html


def test_persisted_goat_alert_keeps_historical_winner_markup():
    html = templates.get_template("_goat_watch_banner.html").render(
        goat_alerts_active=[
            {
                "id": 7,
                "group_name": "Dismantler",
                "person": "Historical Winner",
                "units": 250,
                "wc_name": "Dismantler 3",
                "achieved_day": date(2026, 9, 9),
                "prior_record_units": 200,
                "prior_record_holder": "Prior Holder",
                "prior_record_day": date(2026, 9, 1),
            }
        ],
        goat_contenders=[],
    )

    assert "<b>Historical Winner</b>" in html
    assert "current-operator planned-only" not in html


@pytest.mark.parametrize("available", [True, False])
def test_hand_build_contender_names_credited_producers(monkeypatch, available):
    day = date(2026, 9, 14)
    now = datetime(2026, 9, 14, 20, tzinfo=UTC)
    monkeypatch.setattr(goat_watch, "_final_break_passed", lambda *_: True)
    monkeypatch.setattr(goat_watch, "_shift_elapsed_fraction", lambda *_: 1.0)
    monkeypatch.setattr(goat_watch, "_group_names_today", lambda: ["Hand Builds"])
    monkeypatch.setattr("zira_dashboard.awards.goat", lambda _: {
        "units": 390, "name": "Record Holder", "day": date(2026, 9, 11),
    })
    monkeypatch.setattr("zira_dashboard.work_centers_store.members", lambda *_: [
        SimpleNamespace(name="Hand Build #1"),
    ])
    monkeypatch.setattr(goat_watch, "_wc_units_today", lambda *_: 412)
    calls = []

    def attribution(d, client, *, now_utc):
        calls.append((d, now_utc))
        if not available:
            raise production_history.ProductionSourceUnavailable("snapshot unavailable")
        return {
            (17, "Earlier Producer"): {"Hand Build #1": {"units": 300}},
            "Shared Producer": {"Hand Build #1": {"units": 112}},
            "New Arrival": {"Hand Build #1": {"units": 0}},
            "Other Station": {"Repair 1": {"units": 500}},
        }

    monkeypatch.setattr(production_history, "attribution_for", attribution)
    contenders = goat_watch.contenders_for_now(
        day, now, current_operator_rows_by_wc={},
        eligible_planned_work_centers={"Hand Build #1"},
    )
    assert len(contenders) == 1
    assert calls == [(day, now)]
    assert contenders[0].projected == 412
    assert contenders[0].producer_names == (
        ("Earlier Producer", "Shared Producer") if available else ()
    )
    html = templates.get_template("_goat_watch_banner.html").render(
        goat_alerts_active=[], goat_contenders=contenders,
    )
    assert "No one here now" not in html
    assert "New Arrival" not in html
    assert "Other Station" not in html
    assert ("Producer unknown" in html) is not available
    assert ("Earlier Producer" in html) is available


def test_no_contenders_does_not_load_production_attribution(monkeypatch):
    monkeypatch.setattr(goat_watch, "_final_break_passed", lambda *_: True)
    monkeypatch.setattr(goat_watch, "_shift_elapsed_fraction", lambda *_: 0.5)
    monkeypatch.setattr(goat_watch, "_group_names_today", lambda: [])

    def unexpected(*args, **kwargs):
        pytest.fail("No attribution lookup is needed without contenders")

    monkeypatch.setattr(production_history, "attribution_for", unexpected)
    assert goat_watch.contenders_for_now(
        date(2026, 9, 14), datetime(2026, 9, 14, 20, tzinfo=UTC),
    ) == []


def test_credited_producers_orders_and_deduplicates_positive_credit(monkeypatch):
    monkeypatch.setattr(production_history, "attribution_for", lambda *args, **kwargs: {
        "Small Share": {"Hand Build #1": {"units": 12}},
        (17, "Main Producer"): {"Hand Build #1": {"units": 100}},
        "Main Producer": {"Hand Build #1": {"units": 50}},
        "No Pallets": {"Hand Build #1": {"units": 0}},
        "Other Station": {"Repair 1": {"units": 5}},
    })
    assert goat_watch._credited_producers(
        date(2026, 9, 14), datetime(2026, 9, 14, 20, tzinfo=UTC),
    ) == {
        "Hand Build #1": ("Main Producer", "Small Share"),
        "Repair 1": ("Other Station",),
    }
