from datetime import date
from zira_dashboard import scheduler_time_off as sto


def _fake_db(monkeypatch, rows):
    monkeypatch.setattr(sto.db, "query", lambda sql, params=None: rows)


def test_full_day_entry_is_not_partial(monkeypatch):
    _fake_db(monkeypatch, [{
        "name": "Adrian Aragon", "shape": "full_day",
        "hour_from": None, "hour_to": None, "state": "validate",
        "pay_type": "Paid Time Off",
    }])
    out = sto.time_off_entries_for_day(date(2026, 6, 1))
    assert out[0]["name"] == "Adrian Aragon"
    assert out[0]["hours"] is None
    assert out[0]["pending"] is False
    assert out[0]["pay_type"] == "Paid Time Off"
    assert "request_id" not in out[0]


def test_late_arrival_is_partial_with_time_range(monkeypatch):
    _fake_db(monkeypatch, [{
        "name": "Pascual Moreno", "shape": "late_arrival",
        "hour_from": 6.0, "hour_to": 9.0, "state": "validate",
        "pay_type": "Unpaid Time Off",
    }])
    out = sto.time_off_entries_for_day(date(2026, 6, 1))
    assert out[0]["hours"] == 3.0
    assert out[0]["time_range"] == "6:00am–9:00am"
    assert out[0]["pending"] is False


def test_pending_state_flagged(monkeypatch):
    _fake_db(monkeypatch, [{
        "name": "Juan Delgado", "shape": "full_day",
        "hour_from": None, "hour_to": None, "state": "confirm",
        "pay_type": "Paid Time Off",
    }])
    out = sto.time_off_entries_for_day(date(2026, 6, 1))
    assert out[0]["pending"] is True


def test_full_day_off_names_only_full(monkeypatch):
    _fake_db(monkeypatch, [
        {"name": "Full Person", "shape": "full_day", "hour_from": None,
         "hour_to": None, "state": "validate", "pay_type": "PTO"},
        {"name": "Partial Person", "shape": "early_leave", "hour_from": 12.0,
         "hour_to": 14.5, "state": "validate", "pay_type": "PTO"},
    ])
    full = sto.full_day_off_names(date(2026, 6, 1))
    assert full == {"Full Person"}


def test_entries_have_keys_the_template_reads(monkeypatch):
    _fake_db(monkeypatch, [{
        "name": "X", "shape": "midday_gap", "hour_from": 10.0,
        "hour_to": 12.0, "state": "validate", "pay_type": "PTO",
    }])
    e = sto.time_off_entries_for_day(date(2026, 6, 1))[0]
    for key in ("name", "hours", "pay_type", "time_range",
                "derived", "manual_absent", "pending"):
        assert key in e
    assert e["hours"] == 2.0
    assert e["time_range"] == "10:00am–12:00pm"
