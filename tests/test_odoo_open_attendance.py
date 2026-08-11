"""Tests for the Odoo open-attendance fetch + WC writer (odoo_client) and
the live_cache snapshot refresh. All pure-logic: odoo_client.execute and the
cache writer are stubbed, so no Odoo and no Postgres are needed.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

from zira_dashboard import odoo_client


def test_fetch_open_attendances_maps_rows(monkeypatch):
    monkeypatch.setenv("ODOO_KIOSK_WC_FIELD", "x_studio_work_center")
    monkeypatch.setattr(
        "zira_dashboard.work_centers_store.app_work_center_name_for_odoo_id",
        lambda odoo_id: {41: "Repair 1"}.get(odoo_id),
    )
    fake = MagicMock(return_value=[
        {"id": 88, "employee_id": [5, "Bob"],
         "check_in": "2026-06-01 11:02:00",
         "x_studio_work_center": [41, "Repair #1"]},
        {"id": 90, "employee_id": [7, "Al"],
         "check_in": "2026-06-01 12:15:00", "x_studio_work_center": False},
    ])
    monkeypatch.setattr(odoo_client, "execute", fake)

    out = odoo_client.fetch_open_attendances()

    # Domain filters to open rows; WC field requested because env is set.
    args, kwargs = fake.call_args
    assert args[0] == "hr.attendance" and args[1] == "search_read"
    assert ("check_out", "=", False) in args[2]
    assert "x_studio_work_center" in kwargs["fields"]

    assert out == [
        {"att_id": 88, "employee_odoo_id": 5,
         "check_in": "2026-06-01T11:02:00+00:00", "wc_name": "Repair 1"},
        {"att_id": 90, "employee_odoo_id": 7,
         "check_in": "2026-06-01T12:15:00+00:00", "wc_name": None},
    ]


def test_open_attendance_omits_unknown_odoo_work_center_display_label(monkeypatch):
    monkeypatch.setenv("ODOO_KIOSK_WC_FIELD", "x_studio_work_center")
    monkeypatch.setattr(
        "zira_dashboard.work_centers_store.app_work_center_name_for_odoo_id",
        lambda odoo_id: None,
    )
    monkeypatch.setattr(odoo_client, "execute", lambda *_a, **_kw: [
        {"id": 88, "employee_id": [5, "Bob"],
         "check_in": "2026-06-01 11:02:00",
         "x_studio_work_center": [999, "Odoo-only work center"]},
    ])

    assert odoo_client.fetch_open_attendances()[0]["wc_name"] is None


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


def test_set_attendance_wc_writes_mapped_many2one_id(monkeypatch):
    monkeypatch.setenv("ODOO_KIOSK_WC_FIELD", "x_studio_work_center")
    monkeypatch.delenv("ODOO_KIOSK_DEPARTMENT_FIELD", raising=False)
    monkeypatch.setattr(
        "zira_dashboard.work_centers_store.odoo_work_center_id_for",
        lambda name: 41 if name == "Repair 1" else None,
    )
    fake = MagicMock()
    monkeypatch.setattr(odoo_client, "execute", fake)

    assert odoo_client.set_attendance_wc(88, "Repair 1") is True

    fake.assert_called_once_with(
        "hr.attendance", "write", [88], {"x_studio_work_center": 41})


def test_set_attendance_wc_returns_false_when_odoo_rejects_write(monkeypatch):
    monkeypatch.setenv("ODOO_KIOSK_WC_FIELD", "x_studio_work_center")
    monkeypatch.delenv("ODOO_KIOSK_DEPARTMENT_FIELD", raising=False)
    monkeypatch.setattr(
        "zira_dashboard.work_centers_store.odoo_work_center_id_for",
        lambda name: 41 if name == "Repair 1" else None,
    )
    fake = MagicMock(return_value=False)
    monkeypatch.setattr(odoo_client, "execute", fake)

    assert odoo_client.set_attendance_wc(88, "Repair 1") is False
    fake.assert_called_once_with(
        "hr.attendance", "write", [88], {"x_studio_work_center": 41})


def test_set_attendance_wc_returns_false_without_mapping(monkeypatch):
    monkeypatch.setenv("ODOO_KIOSK_WC_FIELD", "x_studio_work_center")
    monkeypatch.setattr(
        "zira_dashboard.work_centers_store.odoo_work_center_id_for",
        lambda _name: None,
    )
    fake = MagicMock()
    monkeypatch.setattr(odoo_client, "execute", fake)

    assert odoo_client.set_attendance_wc(88, "Repair 1") is False
    fake.assert_not_called()


def test_set_attendance_wc_noop_without_field(monkeypatch):
    monkeypatch.delenv("ODOO_KIOSK_WC_FIELD", raising=False)
    fake = MagicMock()
    monkeypatch.setattr(odoo_client, "execute", fake)

    assert odoo_client.set_attendance_wc(88, "Bay 3 Nailer") is False
    assert odoo_client.set_attendance_wc(88, None) is False

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


def test_clock_in_skips_wc_mapping_when_field_is_not_configured(monkeypatch):
    monkeypatch.delenv("ODOO_KIOSK_WC_FIELD", raising=False)
    monkeypatch.delenv("ODOO_KIOSK_DEPARTMENT_FIELD", raising=False)

    def unexpected_mapping_lookup(_wc_name):
        raise AssertionError("WC mapping lookup should be skipped")

    monkeypatch.setattr(
        "zira_dashboard.work_centers_store.odoo_work_center_id_for",
        unexpected_mapping_lookup,
    )
    fake = MagicMock(return_value=123)
    monkeypatch.setattr(odoo_client, "execute", fake)

    assert odoo_client.clock_in(
        5, "Repair 1", datetime(2026, 6, 16, 16, 30, tzinfo=timezone.utc)
    ) == 123


def test_clock_in_writes_mapped_many2one_id(monkeypatch):
    monkeypatch.setenv("ODOO_KIOSK_WC_FIELD", "x_studio_work_center")
    monkeypatch.delenv("ODOO_KIOSK_DEPARTMENT_FIELD", raising=False)
    monkeypatch.setattr(
        "zira_dashboard.work_centers_store.odoo_work_center_id_for",
        lambda name: 41 if name == "Repair 1" else None,
    )
    fake = MagicMock(return_value=123)
    monkeypatch.setattr(odoo_client, "execute", fake)

    odoo_client.clock_in(
        5, "Repair 1", datetime(2026, 6, 16, 16, 30, tzinfo=timezone.utc)
    )

    assert fake.call_args.args == (
        "hr.attendance", "create",
        {
            "employee_id": 5,
            "check_in": "2026-06-16 16:30:00",
            "in_mode": "kiosk",
            "overtime_status": "approved",
            "x_studio_work_center": 41,
        },
    )


def test_clock_in_without_mapping_still_creates_attendance(monkeypatch):
    monkeypatch.setenv("ODOO_KIOSK_WC_FIELD", "x_studio_work_center")
    monkeypatch.delenv("ODOO_KIOSK_DEPARTMENT_FIELD", raising=False)
    monkeypatch.setattr(
        "zira_dashboard.work_centers_store.odoo_work_center_id_for",
        lambda _name: None,
    )
    fake = MagicMock(return_value=123)
    monkeypatch.setattr(odoo_client, "execute", fake)

    assert odoo_client.clock_in(
        5, "Unmapped", datetime(2026, 6, 16, 16, 30, tzinfo=timezone.utc)
    ) == 123
    assert fake.call_args.args == (
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


def test_transfer_uses_fresh_open_attendance_cache_positive_hit(monkeypatch):
    from zira_dashboard import live_cache

    ts = datetime(2026, 6, 16, 16, 30, tzinfo=timezone.utc)
    monkeypatch.setattr(
        live_cache,
        "read_open_attendance",
        lambda: (
            {"5": {"att_id": 88, "check_in": "2026-06-16T12:00:00+00:00"}},
            datetime.now(timezone.utc),
        ),
    )
    monkeypatch.setattr(
        odoo_client,
        "get_current_attendance",
        lambda eid: (_ for _ in ()).throw(AssertionError("live lookup not needed")),
    )
    calls = []
    monkeypatch.setattr(
        odoo_client,
        "clock_out",
        lambda att_id, at: calls.append(("out", att_id, at)),
    )
    monkeypatch.setattr(
        odoo_client,
        "clock_in",
        lambda eid, wc, at: calls.append(("in", eid, wc, at)) or 99,
    )

    closed_id, new_id = odoo_client.transfer(5, "Repair 1", ts)

    assert (closed_id, new_id) == (88, 99)
    assert calls == [
        ("out", 88, ts),
        ("in", 5, "Repair 1", ts),
    ]


def test_transfer_falls_back_when_open_attendance_cache_misses_employee(monkeypatch):
    from zira_dashboard import live_cache

    ts = datetime(2026, 6, 16, 16, 30, tzinfo=timezone.utc)
    monkeypatch.setattr(
        live_cache,
        "read_open_attendance",
        lambda: ({}, datetime.now(timezone.utc)),
    )
    live_calls = []
    monkeypatch.setattr(
        odoo_client,
        "get_current_attendance",
        lambda eid: live_calls.append(eid) or {"id": 77},
    )
    monkeypatch.setattr(odoo_client, "clock_out", lambda att_id, at: None)
    monkeypatch.setattr(odoo_client, "clock_in", lambda eid, wc, at: 100)

    closed_id, new_id = odoo_client.transfer(5, "Repair 1", ts)

    assert live_calls == [5]
    assert (closed_id, new_id) == (77, 100)


def test_transfer_falls_back_when_open_attendance_cache_is_stale(monkeypatch):
    from zira_dashboard import live_cache

    ts = datetime(2026, 6, 16, 16, 30, tzinfo=timezone.utc)
    monkeypatch.setattr(
        live_cache,
        "read_open_attendance",
        lambda: (
            {"5": {"att_id": 88, "check_in": "2026-06-16T12:00:00+00:00"}},
            datetime.now(timezone.utc) - timedelta(minutes=10),
        ),
    )
    live_calls = []
    monkeypatch.setattr(
        odoo_client,
        "get_current_attendance",
        lambda eid: live_calls.append(eid) or None,
    )
    monkeypatch.setattr(
        odoo_client,
        "clock_out",
        lambda att_id, at: (_ for _ in ()).throw(AssertionError("nothing to close")),
    )
    monkeypatch.setattr(odoo_client, "clock_in", lambda eid, wc, at: 101)

    closed_id, new_id = odoo_client.transfer(5, "Repair 1", ts)

    assert live_calls == [5]
    assert (closed_id, new_id) == (None, 101)
