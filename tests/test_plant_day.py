from datetime import date, datetime, timezone

from zira_dashboard import plant_day


def test_today_uses_plant_local_date_before_utc_midnight_rollover():
    now_utc = datetime(2026, 6, 20, 4, 30, tzinfo=timezone.utc)
    assert plant_day.today(now_utc) == date(2026, 6, 19)


def test_parse_day_defaults_to_plant_today(monkeypatch):
    monkeypatch.setattr(plant_day, "today", lambda: date(2026, 6, 19))
    assert plant_day.parse_day(None) == date(2026, 6, 19)
    assert plant_day.parse_day("2026-06-18") == date(2026, 6, 18)
