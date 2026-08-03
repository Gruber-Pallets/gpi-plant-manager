from datetime import UTC, date, datetime
from math import inf, nan
from unittest.mock import MagicMock

import pytest

from zira_dashboard import _odoo_payroll as payroll

WORK_FIELDS = [
    "id",
    "employee_id",
    "date",
    "duration",
    "state",
    "conflict",
    "active",
    "work_entry_type_id",
    "attendance_id",
    "write_date",
]
ATTENDANCE_FIELDS = [
    "id",
    "employee_id",
    "check_in",
    "worked_hours",
    "overtime_hours",
    "validated_overtime_hours",
    "overtime_status",
    "expected_hours",
]


def fake_execute(responses, calls):
    def execute(model, method, *args, **kwargs):
        calls.append((model, method, args, kwargs))
        value = responses[(model, method)]
        return value(*args, **kwargs) if callable(value) else value

    return execute


def test_recent_candidates_use_write_date_date_and_linked_work100():
    calls = []
    execute = fake_execute(
        {
            ("hr.work.entry.type", "search_read"): [
                {"id": 1, "code": "WORK100"},
                {"id": 2, "code": "OVERTIME"},
            ],
            ("hr.work.entry", "search_read"): [
                {
                    "id": 8508,
                    "employee_id": [6, "Caleb Asmussen"],
                    "date": "2026-07-24",
                    "duration": 0.5,
                    "state": "draft",
                    "conflict": False,
                    "active": True,
                    "work_entry_type_id": [1, "Attendance"],
                    "attendance_id": [3804, "12:06"],
                    "write_date": "2026-07-30 18:14:48",
                }
            ],
        },
        calls,
    )

    rows = payroll.fetch_recent_candidates(
        execute, datetime(2026, 5, 5, tzinfo=UTC)
    )

    assert rows[0] == {
        "id": 8508,
        "employee_id": 6,
        "employee_name": "Caleb Asmussen",
        "date": date(2026, 7, 24),
        "duration": 0.5,
        "state": "draft",
        "conflict": False,
        "active": True,
        "type_code": "WORK100",
        "attendance_id": 3804,
        "write_date": "2026-07-30 18:14:48",
    }
    assert calls == [
        (
            "hr.work.entry.type",
            "search_read",
            ([("code", "in", ["WORK100", "OVERTIME"])],),
            {"fields": ["id", "code"]},
        ),
        (
            "hr.work.entry",
            "search_read",
            (
                [
                    ("active", "=", True),
                    ("attendance_id", "!=", False),
                    ("work_entry_type_id", "=", 1),
                    ("write_date", ">=", "2026-05-05 00:00:00"),
                ],
            ),
            {"fields": WORK_FIELDS, "order": "employee_id,date,id"},
        ),
    ]


def test_fetch_inputs_maps_utc_attendance_to_central_work_date():
    calls = []
    execute = fake_execute(
        {
            ("hr.work.entry.type", "search_read"): [
                {"id": 1, "code": "WORK100"},
                {"id": 2, "code": "OVERTIME"},
            ],
            ("hr.work.entry", "search_read"): [],
            ("hr.attendance", "search_read"): [
                {
                    "id": 3996,
                    "employee_id": [9, "Darren Donahue"],
                    "check_in": "2026-08-01 02:30:00",
                    "worked_hours": 10.548333333,
                    "overtime_hours": 10.5483,
                    "validated_overtime_hours": 10.5483,
                    "overtime_status": "approved",
                    "expected_hours": 0.000033333,
                }
            ],
        },
        calls,
    )

    work, attendance = payroll.fetch_inputs(
        execute, [9], date(2026, 7, 31), date(2026, 7, 31)
    )

    assert work == []
    assert attendance[0]["date"] == date(2026, 7, 31)
    assert attendance[0]["employee_id"] == 9
    assert attendance[0]["overtime_status"] == "approved"
    assert calls == [
        (
            "hr.work.entry.type",
            "search_read",
            ([("code", "in", ["WORK100", "OVERTIME"])],),
            {"fields": ["id", "code"]},
        ),
        (
            "hr.work.entry",
            "search_read",
            (
                [
                    ("active", "=", True),
                    ("employee_id", "in", [9]),
                    ("date", ">=", "2026-07-31"),
                    ("date", "<=", "2026-07-31"),
                ],
            ),
            {"fields": WORK_FIELDS, "order": "employee_id,date,id"},
        ),
        (
            "hr.attendance",
            "search_read",
            (
                [
                    ("employee_id", "in", [9]),
                    ("check_in", ">=", "2026-07-31 05:00:00"),
                    ("check_in", "<", "2026-08-01 05:00:00"),
                ],
            ),
            {
                "fields": ATTENDANCE_FIELDS,
                "order": "employee_id,check_in,id",
            },
        ),
    ]


@pytest.mark.parametrize("duration", [0.0, -0.01, nan, inf, -inf])
def test_write_duration_rejects_invalid_value_before_xmlrpc(duration):
    calls = []
    execute = fake_execute({}, calls)
    with pytest.raises(ValueError, match="positive"):
        payroll.write_duration(execute, 8508, duration)
    assert calls == []


def test_mutation_helpers_touch_only_one_work_entry():
    calls = []
    execute = fake_execute(
        {
            ("hr.work.entry", "write"): True,
            ("hr.work.entry", "unlink"): True,
            ("hr.work.entry", "search_count"): 0,
        },
        calls,
    )
    payroll.write_duration(execute, 8502, 3.121355556)
    payroll.delete_entry(execute, 8508)
    assert payroll.entry_exists(execute, 8508) is False
    assert calls == [
        ("hr.work.entry", "write", ([8502], {"duration": 3.121355556}), {}),
        ("hr.work.entry", "unlink", ([8508],), {}),
        ("hr.work.entry", "search_count", ([("id", "=", 8508)],), {}),
    ]


def test_read_work_entry_returns_fresh_normalized_row():
    calls = []
    execute = fake_execute(
        {
            ("hr.work.entry.type", "search_read"): [
                {"id": 1, "code": "WORK100"},
                {"id": 2, "code": "OVERTIME"},
            ],
            ("hr.work.entry", "read"): [
                {
                    "id": 8502,
                    "employee_id": [19, "Isidro Moctezuma Aviles"],
                    "date": "2026-07-24",
                    "duration": 3.6214,
                    "state": "draft",
                    "conflict": False,
                    "active": True,
                    "work_entry_type_id": [1, "Attendance"],
                    "attendance_id": [3811, "08:26"],
                    "write_date": "2026-08-03 20:00:00",
                }
            ],
        },
        calls,
    )

    row = payroll.read_work_entry(execute, 8502)

    assert row["id"] == 8502
    assert row["type_code"] == "WORK100"
    assert row["attendance_id"] == 3811
    assert calls[1][2] == ([8502],)


def test_public_odoo_client_wrappers_delegate(monkeypatch):
    from zira_dashboard import odoo_client

    recent = MagicMock(return_value=[{"id": 1}])
    monkeypatch.setattr(odoo_client._odoo_payroll, "fetch_recent_candidates", recent)
    since = datetime(2026, 5, 5, tzinfo=UTC)
    assert odoo_client.fetch_recent_payroll_candidates(since) == [{"id": 1}]
    recent.assert_called_once_with(odoo_client.execute, since)


def test_remaining_public_odoo_client_wrappers_delegate(monkeypatch):
    from zira_dashboard import odoo_client

    start_day = date(2026, 7, 31)
    end_day = date(2026, 8, 1)
    inputs = MagicMock(return_value=([{"id": 2}], [{"id": 3}]))
    read = MagicMock(return_value={"id": 8502})
    write = MagicMock()
    delete = MagicMock()
    exists = MagicMock(return_value=True)
    monkeypatch.setattr(odoo_client._odoo_payroll, "fetch_inputs", inputs)
    monkeypatch.setattr(odoo_client._odoo_payroll, "read_work_entry", read)
    monkeypatch.setattr(odoo_client._odoo_payroll, "write_duration", write)
    monkeypatch.setattr(odoo_client._odoo_payroll, "delete_entry", delete)
    monkeypatch.setattr(odoo_client._odoo_payroll, "entry_exists", exists)

    assert odoo_client.fetch_payroll_inputs([9, 4], start_day, end_day) == (
        [{"id": 2}],
        [{"id": 3}],
    )
    assert odoo_client.fetch_payroll_work_entry(8502) == {"id": 8502}
    assert odoo_client.set_payroll_work_entry_duration(8502, 3.5) is None
    assert odoo_client.delete_payroll_work_entry(8508) is None
    assert odoo_client.payroll_work_entry_exists(8508) is True

    inputs.assert_called_once_with(
        odoo_client.execute, [9, 4], start_day, end_day
    )
    read.assert_called_once_with(odoo_client.execute, 8502)
    write.assert_called_once_with(odoo_client.execute, 8502, 3.5)
    delete.assert_called_once_with(odoo_client.execute, 8508)
    exists.assert_called_once_with(odoo_client.execute, 8508)
