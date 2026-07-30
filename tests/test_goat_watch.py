from __future__ import annotations

from datetime import date

from zira_dashboard import db, goat_watch, shift_config


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
