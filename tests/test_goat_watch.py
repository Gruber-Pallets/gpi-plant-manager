from __future__ import annotations

from datetime import date, timedelta

from zira_dashboard import (
    db,
    goat_categories,
    goat_watch,
    production_history,
    shift_config,
)


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
