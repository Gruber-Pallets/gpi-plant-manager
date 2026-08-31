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


class _StatefulAttendanceOdoo:
    def __init__(
        self,
        rows,
        *,
        overtime_hours=None,
        open_sweep_visibility=None,
        fail_checkout_once=None,
        fail_status_once=None,
        ignore_checkout=None,
    ):
        self.rows = {int(row["id"]): dict(row) for row in rows}
        self.overtime_hours = dict(overtime_hours or {})
        self.open_sweep_visibility = list(open_sweep_visibility or [])
        self.fail_checkout_once = set(fail_checkout_once or set())
        self.fail_status_once = set(fail_status_once or set())
        self.ignore_checkout = set(ignore_checkout or set())
        self.calls = []
        self._visible_open_ids = None

    @staticmethod
    def _terms(domain):
        return [item for item in domain if isinstance(item, tuple)]

    def _matching_rows(self, domain):
        terms = self._terms(domain)
        rows = list(self.rows.values())
        if ("check_out", "=", False) in terms:
            rows = [row for row in rows if not row["check_out"]]
            if self._visible_open_ids is not None:
                rows = [row for row in rows if row["id"] in self._visible_open_ids]
        for field, operator, value in terms:
            if field == "employee_id" and operator == "=":
                rows = [row for row in rows if row["employee_id"][0] == value]
            elif field == "check_out" and operator == "=" and value is not False:
                rows = [row for row in rows if row["check_out"] == value]
            elif field == "id" and operator == "in":
                rows = [row for row in rows if row["id"] in value]
            elif field == "id" and operator == "=":
                rows = [row for row in rows if row["id"] == value]
            elif field == "id" and operator == ">":
                rows = [row for row in rows if row["id"] > value]
            elif field == "id" and operator == "<=":
                rows = [row for row in rows if row["id"] <= value]
        return rows

    def execute(self, model, method, *args, **kwargs):
        self.calls.append(((model, method, *args), kwargs))
        assert model == "hr.attendance"
        if method == "search_read":
            domain = args[0]
            fields = kwargs["fields"]
            if (
                fields == ["id"]
                and kwargs.get("order") == "id desc"
                and ("check_out", "=", False) in self._terms(domain)
            ):
                if self.open_sweep_visibility:
                    self._visible_open_ids = set(
                        self.open_sweep_visibility.pop(0)
                    )
                else:
                    self._visible_open_ids = {
                        row["id"] for row in self.rows.values() if not row["check_out"]
                    }
                rows = self._matching_rows(domain)
                return [{"id": max(row["id"] for row in rows)}] if rows else []
            if fields == ["overtime_hours"]:
                rows = self._matching_rows(domain)
                return [
                    {
                        "id": row["id"],
                        "overtime_hours": self.overtime_hours.get(row["id"], 0),
                    }
                    for row in rows
                ]
            rows = self._matching_rows(domain)
            reverse = kwargs.get("order", "").endswith("desc")
            rows.sort(key=lambda row: row["id"], reverse=reverse)
            limit = kwargs.get("limit")
            if limit is not None:
                rows = rows[:limit]
            return [dict(row) for row in rows]
        if method == "write":
            attendance_id = int(args[0][0])
            values = args[1]
            if "check_out" in values:
                if attendance_id in self.fail_checkout_once:
                    self.fail_checkout_once.remove(attendance_id)
                    return False
                if attendance_id not in self.ignore_checkout:
                    self.rows[attendance_id]["check_out"] = values["check_out"]
                return True
            if "overtime_status" in values:
                if attendance_id in self.fail_status_once:
                    self.fail_status_once.remove(attendance_id)
                    return False
                self.rows[attendance_id]["overtime_status"] = values[
                    "overtime_status"
                ]
                return True
        raise AssertionError((model, method, args, kwargs))


def test_fetch_attendance_changes_keeps_unknown_many2one_names_and_keyset_pages():
    calls = []
    pages = [
        [{"id": 905, "write_date": "2026-08-28 13:03:00"}],
        [
            _raw_row(901),
            _raw_row(902, write_date="2026-08-28 13:01:00"),
        ],
        [
            _raw_row(901, write_date="2026-08-28 13:02:00"),
            _raw_row(905, write_date="2026-08-28 13:03:00"),
        ],
        [],
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

    assert [row["odoo_attendance_id"] for row in rows] == [902, 901, 905]
    assert rows[1]["odoo_write_date"] == datetime(
        2026, 8, 28, 13, 2, tzinfo=UTC
    )
    assert calls[0] == (
        (
            "hr.attendance",
            "search_read",
            [
                "|",
                ("write_date", ">", "2026-08-28 12:58:00"),
                "&",
                ("write_date", "=", "2026-08-28 12:58:00"),
                ("id", ">", 0),
            ],
        ),
        {
            "fields": ["id", "write_date"],
            "order": "write_date desc, id desc",
            "limit": 1,
        },
    )
    assert calls[1][0][:2] == ("hr.attendance", "search_read")
    assert calls[1][1]["fields"] == [
        "id",
        "employee_id",
        "check_in",
        "check_out",
        WC_FIELD,
        DEPARTMENT_FIELD,
        "write_date",
    ]
    assert calls[1][1]["order"] == "write_date asc, id asc"
    assert calls[1][1]["limit"] == 2
    assert calls[1][0][2] == [
        "&",
        "|",
        ("write_date", ">", "2026-08-28 12:58:00"),
        "&",
        ("write_date", "=", "2026-08-28 12:58:00"),
        ("id", ">", 0),
        "|",
        ("write_date", "<", "2026-08-28 13:03:00"),
        "&",
        ("write_date", "=", "2026-08-28 13:03:00"),
        ("id", "<=", 905),
    ]
    assert ("write_date", ">", "2026-08-28 13:01:00") in calls[2][0][2]
    assert ("write_date", "=", "2026-08-28 13:01:00") in calls[2][0][2]
    assert ("id", ">", 902) in calls[2][0][2]
    assert all(("id", "<=", 905) in call[0][2] for call in calls[1:])
    assert len(calls) == 4


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
        [{"id": 902}],
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
    assert calls[0] == (
        ("hr.attendance", "search_read", [("check_out", "=", False)]),
        {"fields": ["id"], "order": "id desc", "limit": 1},
    )
    assert calls[1][0][2] == [
        "&",
        "&",
        ("check_out", "=", False),
        ("id", ">", 0),
        ("id", "<=", 902),
    ]
    assert calls[1][1]["order"] == "id asc"
    assert calls[2][0][2] == [
        "&",
        "&",
        ("check_out", "=", False),
        ("id", ">", 902),
        ("id", "<=", 902),
    ]
    assert all("offset" not in kwargs for _, kwargs in calls)


def test_fetch_all_attendance_ids_uses_id_keyset_paging():
    calls = []
    pages = [[{"id": 12}], [{"id": 4}, {"id": 9}], [{"id": 12}]]

    def execute(*args, **kwargs):
        calls.append((args, kwargs))
        return pages.pop(0)

    assert _odoo_attendance.fetch_all_attendance_ids(execute, page_size=2) == [
        4,
        9,
        12,
    ]
    assert calls[0] == (
        ("hr.attendance", "search_read", []),
        {"fields": ["id"], "order": "id desc", "limit": 1},
    )
    assert calls[1][0][2] == [
        "&",
        ("id", ">", 0),
        ("id", "<=", 12),
    ]
    assert calls[2][0][2] == [
        "&",
        ("id", ">", 9),
        ("id", "<=", 12),
    ]
    assert all("offset" not in kwargs for _, kwargs in calls)


def test_change_paging_stops_when_a_read_row_moves_past_the_fixed_watermark():
    calls = []
    page_number = 0

    def execute(*args, **kwargs):
        nonlocal page_number
        calls.append((args, kwargs))
        if kwargs["order"] == "write_date desc, id desc":
            return [{"id": 902, "write_date": "2026-08-28 13:02:00"}]
        page_number += 1
        if page_number > 3:
            raise AssertionError("change paging did not terminate at its watermark")
        if page_number == 1:
            return [_raw_row(901, write_date="2026-08-28 13:01:00")]
        if page_number == 2:
            # Row 901 moved to 13:05 and is therefore outside the captured
            # 13:02 watermark; only the old watermark row remains eligible.
            assert ("write_date", "<", "2026-08-28 13:02:00") in args[2]
            assert ("id", "<=", 902) in args[2]
            return [_raw_row(902, write_date="2026-08-28 13:02:00")]
        return []

    rows = _odoo_attendance.fetch_attendance_changes(
        execute,
        WC_FIELD,
        DEPARTMENT_FIELD,
        after_write_date=datetime(2026, 8, 28, 13, 0, tzinfo=UTC),
        after_id=900,
        overlap=timedelta(0),
        page_size=1,
    )

    assert [row["odoo_attendance_id"] for row in rows] == [901, 902]
    assert page_number == 3


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
    if end_utc is None:
        assert domain == [
            "&",
            ("employee_id", "=", 44),
            "|",
            ("check_out", "=", False),
            ("check_out", ">", "2026-08-28 12:00:00"),
        ]
    else:
        assert domain == [
            "&",
            "&",
            ("employee_id", "=", 44),
            "|",
            ("check_out", "=", False),
            ("check_out", ">", "2026-08-28 12:00:00"),
            ("check_in", "<", "2026-08-29 00:00:00"),
        ]


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


@pytest.mark.parametrize(
    "missing_field",
    [
        "id",
        "employee_id",
        "check_in",
        "check_out",
        WC_FIELD,
        DEPARTMENT_FIELD,
        "write_date",
    ],
)
def test_normalized_rows_reject_every_missing_requested_field(missing_field):
    row = _raw_row(901)
    del row[missing_field]

    with pytest.raises(RuntimeError, match=missing_field):
        _odoo_attendance.fetch_attendance_rows_by_ids(
            lambda *_args, **_kwargs: [row],
            WC_FIELD,
            DEPARTMENT_FIELD,
            [901],
        )


def test_normalized_rows_distinguish_present_false_many2ones_from_missing_fields():
    rows = _odoo_attendance.fetch_attendance_rows_by_ids(
        lambda *_args, **_kwargs: [
            _raw_row(901, check_out=False, work_center=False, department=False)
        ],
        WC_FIELD,
        DEPARTMENT_FIELD,
        [901],
    )

    assert rows[0]["check_out_utc"] is None
    assert rows[0]["odoo_work_center_id"] is None
    assert rows[0]["odoo_work_center_name"] is None
    assert rows[0]["odoo_department_id"] is None
    assert rows[0]["odoo_department_name"] is None


@pytest.mark.parametrize(
    "bad_cursor",
    [datetime(2026, 8, 28, 13, 0), "2026-08-28T13:00:00+00:00"],
)
def test_fetch_attendance_changes_rejects_non_aware_datetime_cursor(bad_cursor):
    with pytest.raises(TypeError, match="after_write_date must be an aware datetime"):
        _odoo_attendance.fetch_attendance_changes(
            lambda *_args, **_kwargs: pytest.fail("invalid cursor reached Odoo"),
            WC_FIELD,
            DEPARTMENT_FIELD,
            after_write_date=bad_cursor,
            after_id=900,
        )


@pytest.mark.parametrize(
    ("start_utc", "end_utc", "message"),
    [
        (
            datetime(2026, 8, 28, 12, 0),
            datetime(2026, 8, 29, tzinfo=UTC),
            "start_utc",
        ),
        (
            datetime(2026, 8, 28, 12, 0, tzinfo=UTC),
            "2026-08-29T00:00:00+00:00",
            "end_utc",
        ),
    ],
)
def test_fetch_employee_attendance_rows_rejects_non_aware_range_values(
    start_utc, end_utc, message
):
    with pytest.raises(TypeError, match=f"{message} must be an aware datetime"):
        _odoo_attendance.fetch_employee_attendance_rows(
            lambda *_args, **_kwargs: pytest.fail("invalid range reached Odoo"),
            WC_FIELD,
            DEPARTMENT_FIELD,
            44,
            start_utc,
            end_utc,
        )


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


@pytest.mark.parametrize(
    ("check_in_utc", "check_out_utc", "message"),
    [
        (
            datetime(2026, 8, 28, 13, 0),
            None,
            "check_in_utc",
        ),
        (
            datetime(2026, 8, 28, 13, 0, tzinfo=UTC),
            "2026-08-28T17:00:00+00:00",
            "check_out_utc",
        ),
    ],
)
def test_create_attendance_interval_rejects_non_aware_utc_values(
    monkeypatch, check_in_utc, check_out_utc, message
):
    monkeypatch.setenv("ODOO_KIOSK_WC_FIELD", WC_FIELD)
    monkeypatch.setattr(
        odoo_client,
        "execute",
        lambda *_args, **_kwargs: pytest.fail("invalid interval reached Odoo"),
    )

    with pytest.raises(TypeError, match=f"{message} must be an aware datetime"):
        odoo_client.create_attendance_interval(
            employee_odoo_id=44,
            check_in_utc=check_in_utc,
            check_out_utc=check_out_utc,
            odoo_work_center_id=72,
            odoo_department_id=None,
        )


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


@pytest.mark.parametrize(
    "unknown_key",
    ["check_in", "check_otu_utc", WC_FIELD],
)
def test_update_attendance_interval_rejects_raw_unknown_or_typo_keys(
    monkeypatch, unknown_key
):
    monkeypatch.delenv("ODOO_KIOSK_WC_FIELD", raising=False)
    monkeypatch.setattr(
        odoo_client,
        "execute",
        lambda *_args, **_kwargs: pytest.fail("invalid update reached Odoo"),
    )

    with pytest.raises(ValueError, match=f"Unsupported attendance update field: {unknown_key}"):
        odoo_client.update_attendance_interval(
            901,
            values={unknown_key: datetime(2026, 8, 28, 13, 0, tzinfo=UTC)},
        )


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("check_in_utc", datetime(2026, 8, 28, 13, 0)),
        ("check_out_utc", "2026-08-28T17:00:00+00:00"),
    ],
)
def test_update_attendance_interval_rejects_non_aware_utc_values(
    monkeypatch, key, value
):
    monkeypatch.setenv("ODOO_KIOSK_WC_FIELD", WC_FIELD)
    monkeypatch.setattr(
        odoo_client,
        "execute",
        lambda *_args, **_kwargs: pytest.fail("invalid update reached Odoo"),
    )

    with pytest.raises(TypeError, match=f"{key} must be an aware datetime"):
        odoo_client.update_attendance_interval(901, values={key: value})


@pytest.mark.parametrize(
    "bad_check_out",
    [datetime(2026, 8, 29, 5, 0), "2026-08-29T05:00:00+00:00"],
)
def test_close_all_open_attendance_rows_rejects_non_aware_utc_value(
    monkeypatch, bad_check_out
):
    monkeypatch.setattr(
        odoo_client,
        "execute",
        lambda *_args, **_kwargs: pytest.fail("invalid close reached Odoo"),
    )

    with pytest.raises(TypeError, match="check_out_utc must be an aware datetime"):
        odoo_client.close_all_open_attendance_rows(44, bad_check_out)


def test_close_all_open_rows_normalizes_microseconds_and_preserves_overtime(
    monkeypatch,
):
    fake = _StatefulAttendanceOdoo(
        [
            {**_raw_row(901), "overtime_status": "to_approve"},
            {**_raw_row(902), "overtime_status": "approved"},
        ],
        overtime_hours={901: -2, 902: 1.25},
    )
    monkeypatch.setenv("ODOO_KIOSK_WC_FIELD", WC_FIELD)
    monkeypatch.setenv("ODOO_KIOSK_DEPARTMENT_FIELD", DEPARTMENT_FIELD)
    monkeypatch.setattr(odoo_client, "execute", fake.execute)

    closed = odoo_client.close_all_open_attendance_rows(
        44, datetime(2026, 8, 29, 5, 0, 0, 987654, tzinfo=UTC)
    )

    assert closed == (901, 902)
    assert fake.rows[901]["check_out"] == "2026-08-29 05:00:00"
    assert fake.rows[902]["check_out"] == "2026-08-29 05:00:00"
    assert fake.rows[901]["overtime_status"] == "approved"
    assert fake.rows[902]["overtime_status"] == "to_approve"
    assert any(
        ("check_out", "=", "2026-08-29 05:00:00") in call[0][2]
        for call in fake.calls
        if call[0][1] == "search_read"
    )


def test_close_all_open_rows_checks_again_when_initial_read_is_empty(
    monkeypatch,
):
    fake = _StatefulAttendanceOdoo(
        [_raw_row(902)], open_sweep_visibility=[[], [902]]
    )
    monkeypatch.setenv("ODOO_KIOSK_WC_FIELD", WC_FIELD)
    monkeypatch.setenv("ODOO_KIOSK_DEPARTMENT_FIELD", DEPARTMENT_FIELD)
    monkeypatch.setattr(odoo_client, "execute", fake.execute)

    with pytest.raises(RuntimeError, match="still has open rows: 902"):
        odoo_client.close_all_open_attendance_rows(
            44, datetime(2026, 8, 29, 5, 0, tzinfo=UTC)
        )


def test_close_all_open_rows_empty_result_is_verified_and_idempotent(monkeypatch):
    fake = _StatefulAttendanceOdoo([])
    monkeypatch.setattr(odoo_client, "execute", fake.execute)

    assert odoo_client.close_all_open_attendance_rows(
        44, datetime(2026, 8, 29, 5, 0, tzinfo=UTC)
    ) == ()
    assert len(
        [
            call
            for call in fake.calls
            if call[0][1] == "search_read"
            and call[1]["fields"] == ["id"]
        ]
    ) == 2


def test_close_all_open_rows_rejects_unverified_checkout(monkeypatch):
    fake = _StatefulAttendanceOdoo(
        [_raw_row(901)], ignore_checkout={901}
    )
    monkeypatch.setenv("ODOO_KIOSK_WC_FIELD", WC_FIELD)
    monkeypatch.setenv("ODOO_KIOSK_DEPARTMENT_FIELD", DEPARTMENT_FIELD)
    monkeypatch.setattr(odoo_client, "execute", fake.execute)

    with pytest.raises(RuntimeError, match="did not verify closed"):
        odoo_client.close_all_open_attendance_rows(
            44, datetime(2026, 8, 29, 5, 0, tzinfo=UTC)
        )


def test_close_all_open_rows_retries_after_partial_checkout_failure(monkeypatch):
    fake = _StatefulAttendanceOdoo(
        [_raw_row(901), _raw_row(902)],
        overtime_hours={901: 0, 902: 1.25},
        fail_checkout_once={902},
    )
    monkeypatch.setenv("ODOO_KIOSK_WC_FIELD", WC_FIELD)
    monkeypatch.setenv("ODOO_KIOSK_DEPARTMENT_FIELD", DEPARTMENT_FIELD)
    monkeypatch.setattr(odoo_client, "execute", fake.execute)
    check_out = datetime(2026, 8, 29, 5, 0, tzinfo=UTC)

    with pytest.raises(RuntimeError, match="did not close attendance 902"):
        odoo_client.close_all_open_attendance_rows(44, check_out)

    assert odoo_client.close_all_open_attendance_rows(44, check_out) == (901, 902)
    assert fake.rows[901]["overtime_status"] == "approved"
    assert fake.rows[902]["overtime_status"] == "to_approve"


def test_close_all_open_rows_retries_after_partial_overtime_status_failure(
    monkeypatch,
):
    fake = _StatefulAttendanceOdoo(
        [{**_raw_row(901), "overtime_status": "to_approve"}],
        overtime_hours={901: 0},
        fail_status_once={901},
    )
    monkeypatch.setenv("ODOO_KIOSK_WC_FIELD", WC_FIELD)
    monkeypatch.setenv("ODOO_KIOSK_DEPARTMENT_FIELD", DEPARTMENT_FIELD)
    monkeypatch.setattr(odoo_client, "execute", fake.execute)
    check_out = datetime(2026, 8, 29, 5, 0, tzinfo=UTC)

    with pytest.raises(RuntimeError, match="overtime status for attendance 901"):
        odoo_client.close_all_open_attendance_rows(44, check_out)

    assert odoo_client.close_all_open_attendance_rows(44, check_out) == (901,)
    assert fake.rows[901]["overtime_status"] == "approved"
