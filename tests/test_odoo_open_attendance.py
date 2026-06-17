"""Tests for the Odoo open-attendance fetch + WC writer (odoo_client) and
the live_cache snapshot refresh. All pure-logic: odoo_client.execute and the
cache writer are stubbed, so no Odoo and no Postgres are needed.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock

from zira_dashboard import odoo_client


def test_fetch_open_attendances_maps_rows(monkeypatch):
    monkeypatch.setenv("ODOO_KIOSK_WC_FIELD", "x_kiosk_wc")
    fake = MagicMock(return_value=[
        {"id": 88, "employee_id": [5, "Bob"],
         "check_in": "2026-06-01 11:02:00", "x_kiosk_wc": "Bay 3 Nailer"},
        {"id": 90, "employee_id": [7, "Al"],
         "check_in": "2026-06-01 12:15:00", "x_kiosk_wc": False},
    ])
    monkeypatch.setattr(odoo_client, "execute", fake)

    out = odoo_client.fetch_open_attendances()

    # Domain filters to open rows; WC field requested because env is set.
    args, kwargs = fake.call_args
    assert args[0] == "hr.attendance" and args[1] == "search_read"
    assert ("check_out", "=", False) in args[2]
    assert "x_kiosk_wc" in kwargs["fields"]

    assert out == [
        {"att_id": 88, "employee_odoo_id": 5,
         "check_in": "2026-06-01T11:02:00+00:00", "wc_name": "Bay 3 Nailer"},
        {"att_id": 90, "employee_odoo_id": 7,
         "check_in": "2026-06-01T12:15:00+00:00", "wc_name": None},
    ]


def test_fetch_open_attendances_no_wc_field(monkeypatch):
    monkeypatch.delenv("ODOO_KIOSK_WC_FIELD", raising=False)
    fake = MagicMock(return_value=[
        {"id": 88, "employee_id": [5, "Bob"], "check_in": "2026-06-01 11:02:00"},
    ])
    monkeypatch.setattr(odoo_client, "execute", fake)

    out = odoo_client.fetch_open_attendances()

    _args, kwargs = fake.call_args
    assert kwargs["fields"] == ["id", "employee_id", "check_in"]
    assert out == [{"att_id": 88, "employee_odoo_id": 5,
                    "check_in": "2026-06-01T11:02:00+00:00", "wc_name": None}]


def test_odoo_dt_to_iso_parses_naive_utc():
    assert (odoo_client._odoo_dt_to_iso("2026-06-01 11:02:00")
            == datetime(2026, 6, 1, 11, 2, tzinfo=timezone.utc).isoformat())
    assert odoo_client._odoo_dt_to_iso(False) is None
    assert odoo_client._odoo_dt_to_iso(None) is None


def test_set_attendance_wc_writes_field(monkeypatch):
    monkeypatch.setenv("ODOO_KIOSK_WC_FIELD", "x_kiosk_wc")
    monkeypatch.delenv("ODOO_KIOSK_DEPARTMENT_FIELD", raising=False)
    fake = MagicMock()
    monkeypatch.setattr(odoo_client, "execute", fake)

    odoo_client.set_attendance_wc(88, "Bay 3 Nailer")

    fake.assert_called_once_with(
        "hr.attendance", "write", [88], {"x_kiosk_wc": "Bay 3 Nailer"})


def test_set_attendance_wc_noop_without_field(monkeypatch):
    monkeypatch.delenv("ODOO_KIOSK_WC_FIELD", raising=False)
    fake = MagicMock()
    monkeypatch.setattr(odoo_client, "execute", fake)

    odoo_client.set_attendance_wc(88, "Bay 3 Nailer")
    odoo_client.set_attendance_wc(88, None)  # also no-op

    fake.assert_not_called()


def test_clock_in_marks_kiosk_attendance_approved(monkeypatch):
    monkeypatch.delenv("ODOO_KIOSK_WC_FIELD", raising=False)
    monkeypatch.delenv("ODOO_KIOSK_DEPARTMENT_FIELD", raising=False)
    fake = MagicMock(return_value=123)
    monkeypatch.setattr(odoo_client, "execute", fake)

    ts = datetime(2026, 6, 16, 16, 30, tzinfo=timezone.utc)
    assert odoo_client.clock_in(5, None, ts) == 123

    fake.assert_called_once_with(
        "hr.attendance", "create",
        {
            "employee_id": 5,
            "check_in": "2026-06-16 16:30:00",
            "in_mode": "kiosk",
            "overtime_status": "approved",
        },
    )


def test_clock_out_approves_regular_hours_after_odoo_computes_no_overtime(monkeypatch):
    fake = MagicMock(side_effect=[True, [{"overtime_hours": 0}], True])
    monkeypatch.setattr(odoo_client, "execute", fake)

    ts = datetime(2026, 6, 16, 21, 30, tzinfo=timezone.utc)
    odoo_client.clock_out(88, ts)

    assert fake.call_args_list[0].args == (
        "hr.attendance", "write",
        [88],
        {
            "check_out": "2026-06-16 21:30:00",
            "out_mode": "kiosk",
        },
    )
    assert fake.call_args_list[1].args == (
        "hr.attendance", "search_read", [("id", "=", 88)]
    )
    assert fake.call_args_list[1].kwargs == {
        "fields": ["overtime_hours"],
        "limit": 1,
    }
    assert fake.call_args_list[2].args == (
        "hr.attendance", "write", [88], {"overtime_status": "approved"}
    )


def test_clock_out_can_mark_automatic_checkout(monkeypatch):
    fake = MagicMock(side_effect=[True, [{"overtime_hours": 0}], True])
    monkeypatch.setattr(odoo_client, "execute", fake)

    ts = datetime(2026, 6, 17, 5, 0, tzinfo=timezone.utc)
    odoo_client.clock_out(88, ts, mode="auto_check_out")

    assert fake.call_args_list[0].args == (
        "hr.attendance", "write",
        [88],
        {
            "check_out": "2026-06-17 05:00:00",
            "out_mode": "auto_check_out",
        },
    )


def test_clock_out_leaves_positive_overtime_to_approve(monkeypatch):
    fake = MagicMock(side_effect=[True, [{"overtime_hours": 1.25}], True])
    monkeypatch.setattr(odoo_client, "execute", fake)

    ts = datetime(2026, 6, 16, 23, 0, tzinfo=timezone.utc)
    odoo_client.clock_out(88, ts)

    assert fake.call_args_list[2].args == (
        "hr.attendance", "write", [88], {"overtime_status": "to_approve"}
    )


def test_clock_out_approves_negative_extra_hours(monkeypatch):
    fake = MagicMock(side_effect=[True, [{"overtime_hours": -24}], True])
    monkeypatch.setattr(odoo_client, "execute", fake)

    ts = datetime(2026, 6, 16, 21, 30, tzinfo=timezone.utc)
    odoo_client.clock_out(88, ts)

    assert fake.call_args_list[2].args == (
        "hr.attendance", "write", [88], {"overtime_status": "approved"}
    )


def test_refresh_builds_keyed_snapshot(monkeypatch):
    from zira_dashboard import live_cache
    monkeypatch.setattr(
        "zira_dashboard.odoo_client.fetch_open_attendances",
        lambda: [
            {"att_id": 88, "employee_odoo_id": 5,
             "check_in": "2026-06-01T11:02:00+00:00", "wc_name": "Bay 3"},
            {"att_id": 90, "employee_odoo_id": 7,
             "check_in": "2026-06-01T12:15:00+00:00", "wc_name": None},
        ],
    )
    written = {}
    monkeypatch.setattr(live_cache, "write_open_attendance",
                        lambda snap: written.update(snap))

    live_cache.refresh_odoo_open_attendance()

    assert written == {
        "5": {"att_id": 88, "check_in": "2026-06-01T11:02:00+00:00",
              "wc_name": "Bay 3"},
        "7": {"att_id": 90, "check_in": "2026-06-01T12:15:00+00:00",
              "wc_name": None},
    }


def test_refresh_swallows_errors(monkeypatch):
    from zira_dashboard import live_cache

    def boom():
        raise RuntimeError("odoo down")

    monkeypatch.setattr("zira_dashboard.odoo_client.fetch_open_attendances", boom)
    wrote = []
    monkeypatch.setattr(live_cache, "write_open_attendance",
                        lambda snap: wrote.append(snap))

    # Must not raise — the warmer relies on this.
    live_cache.refresh_odoo_open_attendance()
    assert wrote == []  # nothing written on failure
