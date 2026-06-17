from datetime import date
from zira_dashboard import odoo_client


def test_fetch_attendances_for_day_reduces_to_earliest_and_open(monkeypatch):
    # Two punches for emp 7 (earlier one wins), one still-open punch for emp 9.
    fake_rows = [
        {"id": 1, "employee_id": [7, "A"], "check_in": "2026-06-01 13:10:00", "check_out": "2026-06-01 17:00:00"},
        {"id": 2, "employee_id": [7, "A"], "check_in": "2026-06-01 12:02:00", "check_out": "2026-06-01 12:30:00"},
        {"id": 3, "employee_id": [9, "B"], "check_in": "2026-06-01 12:05:00", "check_out": False},
    ]
    monkeypatch.setattr(odoo_client, "execute", lambda *a, **k: fake_rows)

    out = odoo_client.fetch_attendances_for_day(date(2026, 6, 1))
    by_id = {r["employee_odoo_id"]: r for r in out}

    assert by_id[7]["first_check_in"].startswith("2026-06-01T12:02:00")
    assert by_id[7]["currently_open"] is False
    assert by_id[9]["currently_open"] is True


def test_fetch_attendances_for_day_empty(monkeypatch):
    monkeypatch.setattr(odoo_client, "execute", lambda *a, **k: [])
    assert odoo_client.fetch_attendances_for_day(date(2026, 6, 1)) == []


def test_fetch_attendances_for_day_skips_zero_duration_rows(monkeypatch):
    fake_rows = [
        {"id": 1, "employee_id": [7, "A"], "check_in": "2026-06-01 05:00:00", "check_out": "2026-06-01 05:00:00"},
        {"id": 2, "employee_id": [8, "B"], "check_in": "2026-06-01 13:00:00", "check_out": "2026-06-01 17:00:00"},
    ]
    monkeypatch.setattr(odoo_client, "execute", lambda *a, **k: fake_rows)

    out = odoo_client.fetch_attendances_for_day(date(2026, 6, 1))

    assert [r["employee_odoo_id"] for r in out] == [8]


def test_fetch_attendance_intervals_for_day_skips_zero_duration_rows(monkeypatch):
    fake_rows = [
        {"id": 1, "employee_id": [7, "A"], "check_in": "2026-06-01 05:00:00", "check_out": "2026-06-01 05:00:00"},
        {"id": 2, "employee_id": [8, "B"], "check_in": "2026-06-01 13:00:00", "check_out": "2026-06-01 17:00:00"},
    ]
    monkeypatch.setattr(odoo_client, "execute", lambda *a, **k: fake_rows)

    out = odoo_client.fetch_attendance_intervals_for_day(date(2026, 6, 1))

    assert len(out) == 1
    assert out[0]["employee_odoo_id"] == 8


def test_fetch_attendances_for_day_skips_near_zero_duration_rows(monkeypatch):
    fake_rows = [
        {"id": 1, "employee_id": [7, "A"], "check_in": "2026-06-01 05:00:00", "check_out": "2026-06-01 05:00:01"},
        {"id": 2, "employee_id": [8, "B"], "check_in": "2026-06-01 13:00:00", "check_out": "2026-06-01 17:00:00"},
    ]
    monkeypatch.setattr(odoo_client, "execute", lambda *a, **k: fake_rows)

    out = odoo_client.fetch_attendances_for_day(date(2026, 6, 1))

    assert [r["employee_odoo_id"] for r in out] == [8]


def test_fetch_attendance_intervals_for_day_skips_near_zero_duration_rows(monkeypatch):
    fake_rows = [
        {"id": 1, "employee_id": [7, "A"], "check_in": "2026-06-01 05:00:00", "check_out": "2026-06-01 05:00:01"},
        {"id": 2, "employee_id": [8, "B"], "check_in": "2026-06-01 13:00:00", "check_out": "2026-06-01 17:00:00"},
    ]
    monkeypatch.setattr(odoo_client, "execute", lambda *a, **k: fake_rows)

    out = odoo_client.fetch_attendance_intervals_for_day(date(2026, 6, 1))

    assert len(out) == 1
    assert out[0]["employee_odoo_id"] == 8
