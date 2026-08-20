from datetime import UTC, datetime

from zira_dashboard import shift_config
from zira_dashboard.auto_lunch_backfill import Repair

from scripts import backfill_auto_lunch


def test_main_defaults_to_dry_run(monkeypatch):
    repair = Repair(
        41,
        7,
        datetime(2026, 8, 18, 11, tzinfo=shift_config.SITE_TZ),
        datetime(2026, 8, 18, 11, 30, tzinfo=shift_config.SITE_TZ),
        "Repair 1",
        True,
    )
    applied = []
    monkeypatch.setattr(backfill_auto_lunch, "_names_by_id", lambda: {7: "Ada"})
    monkeypatch.setattr(backfill_auto_lunch, "_repairs_for_day", lambda *_: [("scheduled", repair)])
    monkeypatch.setattr(backfill_auto_lunch, "_apply_one", lambda *args: applied.append(args))

    status = backfill_auto_lunch.main([
        "--from-date", "2026-08-18", "--through-date", "2026-08-18",
    ])

    assert status == 0
    assert applied == []


def test_main_prints_repair_times_in_site_time(monkeypatch, capsys):
    repair = Repair(
        41,
        7,
        datetime(2026, 8, 18, 16, tzinfo=UTC),
        datetime(2026, 8, 18, 16, 30, tzinfo=UTC),
        "Repair 1",
        True,
    )
    monkeypatch.setattr(backfill_auto_lunch, "_names_by_id", lambda: {7: "Ada"})
    monkeypatch.setattr(backfill_auto_lunch, "_repairs_for_day", lambda *_: [("flex", repair)])

    backfill_auto_lunch.main([
        "--from-date", "2026-08-18", "--through-date", "2026-08-18",
    ])

    assert "flex 11:00-11:30" in capsys.readouterr().out


def test_apply_one_creates_closed_return_for_historical_interval(monkeypatch):
    repair = Repair(
        41,
        7,
        datetime(2026, 8, 18, 11, tzinfo=shift_config.SITE_TZ),
        datetime(2026, 8, 18, 11, 30, tzinfo=shift_config.SITE_TZ),
        "Repair 1",
        True,
        datetime(2026, 8, 18, 15, 30, tzinfo=shift_config.SITE_TZ),
    )
    events = []
    monkeypatch.setattr(backfill_auto_lunch.odoo_client, "clock_out", lambda *args: events.append(("normal", args)))
    monkeypatch.setattr(backfill_auto_lunch.odoo_client, "close_historical_attendance", lambda *args: events.append(("historical", args)))
    monkeypatch.setattr(backfill_auto_lunch.odoo_client, "clock_in", lambda *args: events.append(("open", args)))
    monkeypatch.setattr(backfill_auto_lunch.odoo_client, "create_closed_attendance", lambda *args: events.append(("closed", args)) or 92)
    monkeypatch.setattr(backfill_auto_lunch, "_persist", lambda *args: events.append(("persist", args)))

    backfill_auto_lunch._apply_one("scheduled", repair)

    assert [event[0] for event in events] == ["historical", "closed", "persist"]


def test_main_apply_limit_stops_after_requested_number(monkeypatch):
    repair = Repair(
        41, 7,
        datetime(2026, 8, 18, 11, tzinfo=shift_config.SITE_TZ),
        datetime(2026, 8, 18, 11, 30, tzinfo=shift_config.SITE_TZ),
        "Repair 1", True,
    )
    applied = []
    monkeypatch.setattr(backfill_auto_lunch, "_names_by_id", lambda: {7: "Ada"})
    monkeypatch.setattr(
        backfill_auto_lunch,
        "_repairs_for_day",
        lambda *_: [("scheduled", repair), ("scheduled", repair)],
    )
    monkeypatch.setattr(backfill_auto_lunch, "_apply_one", lambda *args: applied.append(args))

    backfill_auto_lunch.main([
        "--from-date", "2026-08-18", "--through-date", "2026-08-18",
        "--apply", "--limit", "1",
    ])

    assert applied == [("scheduled", repair)]
