from datetime import datetime, timedelta, timezone, UTC

import pytest

from zira_dashboard import _odoo_attendance, odoo_client


WC_FIELD = "x_studio_work_center"
DEPARTMENT_FIELD = "x_kiosk_department_id"


def _raw_row(
    attendance_id: int,
    *,
    employee_id: int = 44,
    employee_name: str = "Adrian A.",
    check_in: str | datetime = "2026-08-28 13:00:00",
    check_out: str | datetime | bool = False,
    work_center: list[object] | bool = None,
    department: list[object] | bool = None,
    write_date: str | datetime = "2026-08-28 13:01:00",
) -> dict:
    return {
        "id": attendance_id,
        "employee_id": [employee_id, employee_name],
        "check_in": check_in,
        "check_out": check_out,
        WC_FIELD: (
            [72, "Luke Floor / Dismantler 1"]
            if work_center is None
            else work_center
        ),
        DEPARTMENT_FIELD: (
            [8, "01 Recycled"] if department is None else department
        ),
        "write_date": write_date,
    }


def _expected_row(attendance_id: int = 901) -> dict:
    return {
        "odoo_attendance_id": attendance_id,
        "employee_odoo_id": 44,
        "employee_name": "Adrian A.",
        "check_in_utc": datetime(2026, 8, 28, 13, 0, tzinfo=UTC),
        "check_out_utc": None,
        "odoo_work_center_id": 72,
        "odoo_work_center_name": "Luke Floor / Dismantler 1",
        "odoo_department_id": 8,
        "odoo_department_name": "01 Recycled",
        "odoo_write_date": datetime(2026, 8, 28, 13, 1, tzinfo=UTC),
    }


def test_fetch_attendance_changes_keeps_unknown_many2one_names_and_keyset_pages():
    calls = []
    pages = [
        [
            _raw_row(901),
            _raw_row(902, write_date="2026-08-28 13:01:00"),
        ],
        [_raw_row(903, write_date="2026-08-28 13:02:00")],
    ]

    def execute(*args, **kwargs):
        calls.append((args, kwargs))
        return pages.pop(0)

    rows = _odoo_attendance.fetch_attendance_changes(
        execute,
        WC_FIELD,
        DEPARTMENT_FIELD,
        after_write_date=datetime(2026, 8, 28, 13, 0, tzinfo=UTC),
        after_id=900,
        overlap=timedelta(minutes=2),
        page_size=2,
    )

    assert rows[0] == _expected_row()
    assert [row["odoo_attendance_id"] for row in rows] == [901, 902, 903]
    assert calls[0][0][:2] == ("hr.attendance", "search_read")
    assert calls[0][1]["fields"] == [
        "id",
        "employee_id",
        "check_in",
        "check_out",
        WC_FIELD,
        DEPARTMENT_FIELD,
        "write_date",
    ]
    assert calls[0][1]["order"] == "write_date asc, id asc"
    assert calls[0][1]["limit"] == 2
    assert ("write_date", ">", "2026-08-28 12:58:00") in calls[0][0][2]
    assert ("write_date", "=", "2026-08-28 12:58:00") in calls[0][0][2]
    assert ("id", ">", 0) in calls[0][0][2]
    assert ("write_date", ">", "2026-08-28 13:01:00") in calls[1][0][2]
    assert ("write_date", "=", "2026-08-28 13:01:00") in calls[1][0][2]
    assert ("id", ">", 902) in calls[1][0][2]


def test_fetch_attendance_changes_uses_original_id_without_overlap():
    calls = []

    def execute(*args, **kwargs):
        calls.append((args, kwargs))
        return []

    assert _odoo_attendance.fetch_attendance_changes(
        execute,
        WC_FIELD,
        DEPARTMENT_FIELD,
        after_write_date=datetime(2026, 8, 28, 13, 0, tzinfo=UTC),
        after_id=900,
        overlap=timedelta(0),
    ) == []
    assert ("write_date", ">", "2026-08-28 13:00:00") in calls[0][0][2]
    assert ("write_date", "=", "2026-08-28 13:00:00") in calls[0][0][2]
    assert ("id", ">", 900) in calls[0][0][2]


def test_fetch_open_attendance_rows_refreshes_every_open_row_with_keyset_paging():
    calls = []
    pages = [
        [_raw_row(901), _raw_row(902)],
        [],
    ]

    def execute(*args, **kwargs):
        calls.append((args, kwargs))
        return pages.pop(0)

    rows = _odoo_attendance.fetch_open_attendance_rows(
        execute, WC_FIELD, DEPARTMENT_FIELD, page_size=2
    )

    assert [row["odoo_attendance_id"] for row in rows] == [901, 902]
    assert ("check_out", "=", False) in calls[0][0][2]
    assert ("check_out", "=", False) in calls[1][0][2]
    assert ("write_date", ">", "2026-08-28 13:01:00") in calls[1][0][2]
    assert ("id", ">", 902) in calls[1][0][2]
    assert all("offset" not in kwargs for _, kwargs in calls)


def test_fetch_all_attendance_ids_uses_id_keyset_paging():
    calls = []
    pages = [[{"id": 4}, {"id": 9}], [{"id": 12}]]

    def execute(*args, **kwargs):
        calls.append((args, kwargs))
        return pages.pop(0)

    assert _odoo_attendance.fetch_all_attendance_ids(execute, page_size=2) == [
        4,
        9,
        12,
    ]
    assert calls[0] == (
        ("hr.attendance", "search_read", [("id", ">", 0)]),
        {"fields": ["id"], "order": "id asc", "limit": 2},
    )
    assert calls[1][0][2] == [("id", ">", 9)]
    assert all("offset" not in kwargs for _, kwargs in calls)


def test_fetch_attendance_rows_by_ids_reads_complete_rows_and_handles_empty_ids():
    calls = []

    def execute(*args, **kwargs):
        calls.append((args, kwargs))
        return [_raw_row(901)]

    assert _odoo_attendance.fetch_attendance_rows_by_ids(
        execute, WC_FIELD, DEPARTMENT_FIELD, [901, 901]
    ) == [_expected_row()]
    assert calls[0][0][2] == [("id", "in", [901])]
    assert calls[0][1]["order"] == "id asc"
    assert _odoo_attendance.fetch_attendance_rows_by_ids(
        execute, WC_FIELD, DEPARTMENT_FIELD, []
    ) == []
    assert len(calls) == 1


@pytest.mark.parametrize("end_utc", [datetime(2026, 8, 29, tzinfo=UTC), None])
def test_fetch_employee_attendance_rows_reads_overlapping_range(end_utc):
    calls = []

    def execute(*args, **kwargs):
        calls.append((args, kwargs))
        return [_raw_row(901)]

    rows = _odoo_attendance.fetch_employee_attendance_rows(
        execute,
        WC_FIELD,
        DEPARTMENT_FIELD,
        44,
        datetime(2026, 8, 28, 12, 0, tzinfo=UTC),
        end_utc,
    )

    assert rows == [_expected_row()]
    domain = calls[0][0][2]
    assert ("employee_id", "=", 44) in domain
    assert ("check_out", "=", False) in domain
    assert ("check_out", ">", "2026-08-28 12:00:00") in domain
    if end_utc is None:
        assert not any(item[:2] == ("check_in", "<") for item in domain if isinstance(item, tuple))
    else:
        assert ("check_in", "<", "2026-08-29 00:00:00") in domain


def test_normalized_rows_parse_naive_and_aware_odoo_datetimes_as_utc():
    eastern = timezone(timedelta(hours=-4))

    rows = _odoo_attendance.fetch_attendance_rows_by_ids(
        lambda *_args, **_kwargs: [
            _raw_row(
                901,
                check_in=datetime(2026, 8, 28, 9, 0, tzinfo=eastern),
                check_out=datetime(2026, 8, 28, 14, 0),
                write_date=datetime(2026, 8, 28, 9, 1, tzinfo=eastern),
            )
        ],
        WC_FIELD,
        DEPARTMENT_FIELD,
        [901],
    )

    assert rows[0]["check_in_utc"] == datetime(2026, 8, 28, 13, 0, tzinfo=UTC)
    assert rows[0]["check_out_utc"] == datetime(2026, 8, 28, 14, 0, tzinfo=UTC)
    assert rows[0]["odoo_write_date"] == datetime(2026, 8, 28, 13, 1, tzinfo=UTC)


def test_create_attendance_interval_writes_configured_field_names(monkeypatch):
    calls = []
    monkeypatch.setenv("ODOO_KIOSK_WC_FIELD", WC_FIELD)
    monkeypatch.setenv("ODOO_KIOSK_DEPARTMENT_FIELD", DEPARTMENT_FIELD)
    monkeypatch.setattr(
        odoo_client,
        "execute",
        lambda *args, **kwargs: calls.append((args, kwargs)) or 901,
    )

    attendance_id = odoo_client.create_attendance_interval(
        employee_odoo_id=44,
        check_in_utc=datetime(2026, 8, 28, 13, 0, tzinfo=UTC),
        check_out_utc=datetime(2026, 8, 28, 17, 0, tzinfo=UTC),
        odoo_work_center_id=72,
        odoo_department_id=8,
    )

    assert attendance_id == 901
    assert calls == [
        (
            ("hr.attendance", "create", {
                "employee_id": 44,
                "check_in": "2026-08-28 13:00:00",
                "check_out": "2026-08-28 17:00:00",
                WC_FIELD: 72,
                DEPARTMENT_FIELD: 8,
            }),
            {},
        )
    ]


def test_correction_mutations_use_configured_fields_and_reject_missing_wc(monkeypatch):
    calls = []
    monkeypatch.setenv("ODOO_KIOSK_WC_FIELD", WC_FIELD)
    monkeypatch.setenv("ODOO_KIOSK_DEPARTMENT_FIELD", DEPARTMENT_FIELD)
    monkeypatch.setattr(
        odoo_client,
        "execute",
        lambda *args, **kwargs: calls.append((args, kwargs)) or True,
    )

    odoo_client.update_attendance_interval(
        901,
        values={
            "check_in_utc": datetime(2026, 8, 28, 13, 30, tzinfo=UTC),
            "check_out_utc": None,
            "odoo_work_center_id": 73,
            "odoo_department_id": 9,
        },
    )
    odoo_client.set_attendance_department_id(901, 10)
    odoo_client.delete_attendance_interval(901)

    assert calls == [
        (
            ("hr.attendance", "write", [901], {
                "check_in": "2026-08-28 13:30:00",
                "check_out": False,
                WC_FIELD: 73,
                DEPARTMENT_FIELD: 9,
            }),
            {},
        ),
        (("hr.attendance", "write", [901], {DEPARTMENT_FIELD: 10}), {}),
        (("hr.attendance", "unlink", [901]), {}),
    ]

    monkeypatch.delenv("ODOO_KIOSK_WC_FIELD")
    with pytest.raises(odoo_client.OdooConfigError, match="work-center field"):
        odoo_client.create_attendance_interval(
            employee_odoo_id=44,
            check_in_utc=datetime(2026, 8, 28, 13, 0, tzinfo=UTC),
            check_out_utc=None,
            odoo_work_center_id=72,
            odoo_department_id=None,
        )
    with pytest.raises(odoo_client.OdooConfigError, match="work-center field"):
        odoo_client.update_attendance_interval(901, values={"check_out_utc": None})
    with pytest.raises(odoo_client.OdooConfigError, match="work-center field"):
        odoo_client.delete_attendance_interval(901)
    with pytest.raises(odoo_client.OdooConfigError, match="work-center field"):
        odoo_client.set_attendance_department_id(901, 10)


def test_close_all_open_attendance_rows_closes_and_verifies_every_row(monkeypatch):
    calls = []
    open_reads = iter([[_raw_row(901), _raw_row(902)], []])
    verified_rows = [
        _raw_row(901, check_out="2026-08-29 05:00:00"),
        _raw_row(902, check_out="2026-08-29 05:00:00"),
    ]

    def execute(model, method, *args, **kwargs):
        calls.append(((model, method, *args), kwargs))
        if method == "search_read" and ("check_out", "=", False) in args[0]:
            return next(open_reads)
        if method == "search_read":
            return verified_rows
        return True

    monkeypatch.setenv("ODOO_KIOSK_WC_FIELD", WC_FIELD)
    monkeypatch.setenv("ODOO_KIOSK_DEPARTMENT_FIELD", DEPARTMENT_FIELD)
    monkeypatch.setattr(odoo_client, "execute", execute)

    closed = odoo_client.close_all_open_attendance_rows(
        44, datetime(2026, 8, 29, 5, 0, tzinfo=UTC)
    )

    assert closed == (901, 902)
    write_calls = [call for call in calls if call[0][1] == "write"]
    assert write_calls == [
        (
            ("hr.attendance", "write", [901], {
                "check_out": "2026-08-29 05:00:00",
                "out_mode": "kiosk",
            }),
            {},
        ),
        (
            ("hr.attendance", "write", [902], {
                "check_out": "2026-08-29 05:00:00",
                "out_mode": "kiosk",
            }),
            {},
        ),
    ]
    assert any(
        call[0][2] == [("id", "in", [901, 902])]
        for call in calls
        if call[0][1] == "search_read"
    )


def test_close_all_open_attendance_rows_rejects_a_row_opened_during_verification(
    monkeypatch,
):
    open_reads = iter([[_raw_row(901)], [_raw_row(902)]])

    def execute(_model, method, *args, **_kwargs):
        if method == "search_read" and ("check_out", "=", False) in args[0]:
            return next(open_reads)
        if method == "search_read":
            return [_raw_row(901, check_out="2026-08-29 05:00:00")]
        return True

    monkeypatch.setenv("ODOO_KIOSK_WC_FIELD", WC_FIELD)
    monkeypatch.setenv("ODOO_KIOSK_DEPARTMENT_FIELD", DEPARTMENT_FIELD)
    monkeypatch.setattr(odoo_client, "execute", execute)

    with pytest.raises(RuntimeError, match="still has open rows: 902"):
        odoo_client.close_all_open_attendance_rows(
            44, datetime(2026, 8, 29, 5, 0, tzinfo=UTC)
        )


def test_close_all_open_attendance_rows_is_idempotent_and_rejects_unverified_close(
    monkeypatch,
):
    monkeypatch.setattr(odoo_client, "execute", lambda *_args, **_kwargs: [])
    assert odoo_client.close_all_open_attendance_rows(
        44, datetime(2026, 8, 29, 5, 0, tzinfo=UTC)
    ) == ()

    responses = [[_raw_row(901)], True, [_raw_row(901)]]
    monkeypatch.setattr(
        odoo_client,
        "execute",
        lambda *_args, **_kwargs: responses.pop(0),
    )
    with pytest.raises(RuntimeError, match="did not verify closed"):
        odoo_client.close_all_open_attendance_rows(
            44, datetime(2026, 8, 29, 5, 0, tzinfo=UTC)
        )
