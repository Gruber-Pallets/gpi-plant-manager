from __future__ import annotations

from datetime import date

from zira_dashboard import db, goat_watch, shift_config


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
