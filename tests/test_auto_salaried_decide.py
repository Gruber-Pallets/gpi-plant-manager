"""Pure decision core: no DB, no Odoo, no env beyond mode()."""
from datetime import date, datetime, time

from zira_dashboard import auto_salaried as asal
from zira_dashboard import shift_config

TUE = date(2026, 9, 1)   # a Tuesday
SAT = date(2026, 9, 5)   # a Saturday


def _at(day, hh, mm):
    return datetime.combine(day, time(hh, mm), tzinfo=shift_config.SITE_TZ)


def test_punch_times_and_order():
    assert asal.SLOT_ORDER == ("morning_in", "lunch_out", "lunch_in", "day_out")
    assert asal.PUNCH_TIMES["morning_in"] == time(6, 0)
    assert asal.PUNCH_TIMES["lunch_out"] == time(11, 0)
    assert asal.PUNCH_TIMES["lunch_in"] == time(11, 30)
    assert asal.PUNCH_TIMES["day_out"] == time(15, 30)
    assert asal.SLOT_ACTION == {
        "morning_in": "clock_in", "lunch_out": "clock_out",
        "lunch_in": "clock_in", "day_out": "clock_out",
    }


def test_scheduled_at_is_plant_local():
    dt = asal.scheduled_at(TUE, "morning_in")
    assert dt == _at(TUE, 6, 0)
    assert dt.tzinfo is shift_config.SITE_TZ


def test_scheduled_at_survives_dst_days():
    # 2026-03-08 and 2026-11-01 are US DST transitions; punch stays 6:00 local.
    for day in (date(2026, 3, 9), date(2026, 11, 2)):  # Mondays after transitions
        assert asal.scheduled_at(day, "morning_in").time() == time(6, 0)


def test_skip_reasons():
    assert asal.skip_reason(SAT, is_company_holiday=False, has_approved_leave=False) == "weekend"
    assert asal.skip_reason(TUE, is_company_holiday=True, has_approved_leave=False) == "holiday"
    assert asal.skip_reason(TUE, is_company_holiday=False, has_approved_leave=True) == "approved_leave"
    assert asal.skip_reason(TUE, is_company_holiday=False, has_approved_leave=False) is None


def test_due_slots_progression():
    assert asal.due_slots(_at(TUE, 5, 59), TUE, None) == []
    assert asal.due_slots(_at(TUE, 6, 0), TUE, None) == ["morning_in"]
    run = {"morning_in_punch_id": 11}
    assert asal.due_slots(_at(TUE, 10, 59), TUE, run) == []
    assert asal.due_slots(_at(TUE, 11, 0), TUE, run) == ["lunch_out"]
    run = {"morning_in_punch_id": 11, "lunch_out_punch_id": 12, "lunch_in_punch_id": 13}
    assert asal.due_slots(_at(TUE, 15, 30), TUE, run) == ["day_out"]


def test_due_slots_catches_up_after_downtime():
    # App down 5:50-12:10: everything through lunch_in is due at once, in order.
    assert asal.due_slots(_at(TUE, 12, 10), TUE, None) == [
        "morning_in", "lunch_out", "lunch_in"]


def test_due_slots_simulated_id_counts_as_done():
    run = {"morning_in_punch_id": asal.SIMULATED_PUNCH_ID}
    assert asal.due_slots(_at(TUE, 6, 5), TUE, run) == []


def test_mode(monkeypatch):
    monkeypatch.delenv("AUTO_SALARIED_DRY_RUN", raising=False)
    monkeypatch.delenv("AUTO_SALARIED_ENABLED", raising=False)
    assert asal.mode() == "off"
    monkeypatch.setenv("AUTO_SALARIED_ENABLED", "1")
    assert asal.mode() == "live"
    monkeypatch.setenv("AUTO_SALARIED_DRY_RUN", "1")
    assert asal.mode() == "dry_run"  # dry-run wins
