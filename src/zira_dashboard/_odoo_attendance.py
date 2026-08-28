"""Private Odoo attendance reads and normalization used by the client facade."""

from __future__ import annotations

import logging
from datetime import date, datetime, time as _time, timedelta, UTC
from typing import Any, Callable, Sequence

from . import shift_config


def _unwrap_m2o(value: Any) -> Any:
    return value[0] if isinstance(value, (list, tuple)) and value else value


def to_odoo_dt(ts: datetime) -> str:
    """Odoo expects naive UTC strings in 'YYYY-MM-DD HH:MM:SS' format.
    Accepts aware or naive datetimes; aware ones are converted to UTC."""
    if ts.tzinfo is not None:
        ts = ts.astimezone(UTC).replace(tzinfo=None)
    return ts.strftime("%Y-%m-%d %H:%M:%S")


def odoo_dt_to_iso(value: Any) -> str | None:
    """Odoo returns datetimes as naive-UTC 'YYYY-MM-DD HH:MM:SS' strings
    (and False for empty). Return an ISO-8601 string with an explicit UTC
    offset, or None."""
    if not value:
        return None
    if isinstance(value, str):
        dt = datetime.strptime(value, "%Y-%m-%d %H:%M:%S").replace(
            tzinfo=UTC
        )
        return dt.isoformat()
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return None


def _parse_odoo_datetime(value: Any) -> datetime | None:
    """Return an Odoo datetime as an aware UTC ``datetime``."""
    if not value:
        return None
    if isinstance(value, str):
        parsed = datetime.fromisoformat(value)
    elif isinstance(value, datetime):
        parsed = value
    else:
        raise TypeError(f"Unsupported Odoo datetime value: {value!r}")
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _require_aware_utc_datetime(value: Any, field_name: str) -> datetime:
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise TypeError(f"{field_name} must be an aware datetime")
    return value.astimezone(UTC)


def _require_response_fields(
    row: dict, fields: Sequence[str], *, context: str
) -> None:
    missing = [field for field in fields if field not in row]
    if missing:
        raise RuntimeError(
            f"Odoo {context} omitted requested field(s): {', '.join(missing)}"
        )


def _validated_attendance_id(value: Any, *, context: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise RuntimeError(f"Odoo {context} contained an invalid id")
    return value


def _m2o_parts(value: Any) -> tuple[int | None, str | None]:
    if isinstance(value, (list, tuple)) and value:
        raw_id = value[0]
        label = str(value[1]) if len(value) > 1 and value[1] else None
    else:
        raw_id = value
        label = None
    if not raw_id or isinstance(raw_id, bool):
        return None, label
    try:
        return int(raw_id), label
    except (TypeError, ValueError):
        return None, label


def _raw_attendance_fields(
    wc_field: str | None, department_field: str | None
) -> list[str]:
    fields = ["id", "employee_id", "check_in", "check_out"]
    if wc_field:
        fields.append(wc_field)
    if department_field:
        fields.append(department_field)
    fields.append("write_date")
    return fields


def _normalize_raw_attendance(
    row: dict, wc_field: str | None, department_field: str | None
) -> dict:
    requested_fields = _raw_attendance_fields(wc_field, department_field)
    _require_response_fields(
        row, requested_fields, context="attendance row"
    )
    attendance_id = _validated_attendance_id(
        row["id"], context="attendance row"
    )
    employee_id, employee_name = _m2o_parts(row["employee_id"])
    if employee_id is None:
        raise RuntimeError(
            f"Odoo attendance row {attendance_id} contained an invalid employee_id"
        )
    check_in = _parse_odoo_datetime(row["check_in"])
    if check_in is None:
        raise RuntimeError(
            f"Odoo attendance row {attendance_id} contained an invalid check_in"
        )
    write_date = _parse_odoo_datetime(row["write_date"])
    if write_date is None:
        raise RuntimeError(
            f"Odoo attendance row {attendance_id} contained an invalid write_date"
        )
    work_center_id, work_center_name = _m2o_parts(
        row[wc_field] if wc_field else None
    )
    department_id, department_name = _m2o_parts(
        row[department_field] if department_field else None
    )
    return {
        "odoo_attendance_id": attendance_id,
        "employee_odoo_id": employee_id,
        "employee_name": employee_name,
        "check_in_utc": check_in,
        "check_out_utc": _parse_odoo_datetime(row["check_out"]),
        "odoo_work_center_id": work_center_id,
        "odoo_work_center_name": work_center_name,
        "odoo_department_id": department_id,
        "odoo_department_name": department_name,
        "odoo_write_date": write_date,
    }


def _write_date_boundary(cursor_date: datetime, cursor_id: int) -> list[Any]:
    cursor_value = to_odoo_dt(cursor_date)
    return [
        "|",
        ("write_date", ">", cursor_value),
        "&",
        ("write_date", "=", cursor_value),
        ("id", ">", cursor_id),
    ]


def _write_date_upper_boundary(
    watermark_date: datetime, watermark_id: int
) -> list[Any]:
    watermark_value = to_odoo_dt(watermark_date)
    return [
        "|",
        ("write_date", "<", watermark_value),
        "&",
        ("write_date", "=", watermark_value),
        ("id", "<=", watermark_id),
    ]


def _and_domain(*expressions: Any) -> list[Any]:
    """Combine complete Odoo domain expressions with explicit prefix ANDs."""
    parts: list[list[Any]] = []
    for expression in expressions:
        if expression == []:
            continue
        parts.append(expression if isinstance(expression, list) else [expression])
    if not parts:
        return []
    return ["&"] * (len(parts) - 1) + [
        token for part in parts for token in part
    ]


def _deduplicate_raw_attendance_rows(rows: list[dict]) -> list[dict]:
    newest_by_id: dict[int, tuple[datetime, dict]] = {}
    for row in rows:
        _require_response_fields(
            row, ["id", "write_date"], context="attendance page"
        )
        attendance_id = _validated_attendance_id(
            row["id"], context="attendance page"
        )
        write_date = _parse_odoo_datetime(row["write_date"])
        if write_date is None:
            raise RuntimeError("Odoo attendance row omitted write_date")
        current = newest_by_id.get(attendance_id)
        if current is None or write_date >= current[0]:
            newest_by_id[attendance_id] = (write_date, row)
    return [
        item[1]
        for item in sorted(
            newest_by_id.values(),
            key=lambda item: (item[0], int(item[1]["id"])),
        )
    ]


def _fetch_raw_attendance_change_pages(
    execute_fn: Callable[..., Any],
    wc_field: str | None,
    department_field: str | None,
    *,
    base_domain: list[Any],
    cursor_date: datetime | None,
    cursor_id: int,
    page_size: int,
) -> list[dict]:
    if page_size <= 0:
        raise ValueError("page_size must be positive")
    fields = _raw_attendance_fields(wc_field, department_field)
    initial_boundary = (
        _write_date_boundary(cursor_date, cursor_id)
        if cursor_date is not None
        else []
    )
    watermark_rows = execute_fn(
        "hr.attendance",
        "search_read",
        _and_domain(*base_domain, initial_boundary),
        fields=["id", "write_date"],
        order="write_date desc, id desc",
        limit=1,
    )
    if not watermark_rows:
        return []
    watermark_row = watermark_rows[0]
    _require_response_fields(
        watermark_row,
        ["id", "write_date"],
        context="attendance watermark",
    )
    watermark_date = _parse_odoo_datetime(watermark_row["write_date"])
    if watermark_date is None:
        raise RuntimeError("Odoo attendance watermark omitted write_date")
    watermark_id = _validated_attendance_id(
        watermark_row["id"], context="attendance watermark"
    )
    upper_boundary = _write_date_upper_boundary(
        watermark_date, watermark_id
    )
    rows: list[dict] = []
    while True:
        boundary = (
            _write_date_boundary(cursor_date, cursor_id)
            if cursor_date is not None
            else []
        )
        page = execute_fn(
            "hr.attendance",
            "search_read",
            _and_domain(*base_domain, boundary, upper_boundary),
            fields=fields,
            order="write_date asc, id asc",
            limit=page_size,
        )
        rows.extend(page)
        if len(page) < page_size:
            break
        last = page[-1]
        _require_response_fields(
            last, ["id", "write_date"], context="attendance page"
        )
        next_date = _parse_odoo_datetime(last["write_date"])
        if next_date is None:
            raise RuntimeError("Odoo attendance page omitted write_date")
        next_id = _validated_attendance_id(
            last["id"], context="attendance page"
        )
        if cursor_date is not None and (next_date, next_id) <= (
            cursor_date,
            cursor_id,
        ):
            raise RuntimeError("Odoo attendance keyset cursor did not advance")
        cursor_date, cursor_id = next_date, next_id
    return [
        _normalize_raw_attendance(row, wc_field, department_field)
        for row in _deduplicate_raw_attendance_rows(rows)
    ]


def _fetch_raw_attendance_id_pages(
    execute_fn: Callable[..., Any],
    wc_field: str | None,
    department_field: str | None,
    *,
    base_domain: list[Any],
    page_size: int,
) -> list[dict]:
    if page_size <= 0:
        raise ValueError("page_size must be positive")
    watermark_rows = execute_fn(
        "hr.attendance",
        "search_read",
        _and_domain(*base_domain),
        fields=["id"],
        order="id desc",
        limit=1,
    )
    if not watermark_rows:
        return []
    _require_response_fields(
        watermark_rows[0], ["id"], context="attendance ID watermark"
    )
    watermark_id = _validated_attendance_id(
        watermark_rows[0]["id"], context="attendance ID watermark"
    )
    cursor_id = 0
    rows: list[dict] = []
    while True:
        page = execute_fn(
            "hr.attendance",
            "search_read",
            _and_domain(
                *base_domain,
                ("id", ">", cursor_id),
                ("id", "<=", watermark_id),
            ),
            fields=_raw_attendance_fields(wc_field, department_field),
            order="id asc",
            limit=page_size,
        )
        rows.extend(page)
        if len(page) < page_size:
            break
        _require_response_fields(
            page[-1], ["id"], context="attendance ID page"
        )
        next_id = _validated_attendance_id(
            page[-1]["id"], context="attendance ID page"
        )
        if next_id <= cursor_id:
            raise RuntimeError("Odoo attendance ID cursor did not advance")
        cursor_id = next_id
    return [
        _normalize_raw_attendance(row, wc_field, department_field)
        for row in rows
    ]


def fetch_attendance_changes(
    execute_fn: Callable[..., Any],
    wc_field: str | None,
    department_field: str | None,
    *,
    after_write_date: datetime | None,
    after_id: int | None,
    overlap: timedelta = timedelta(minutes=2),
    page_size: int = 250,
) -> list[dict]:
    """Fetch changed rows with an overlapping, stable keyset cursor."""
    if overlap < timedelta(0):
        raise ValueError("overlap cannot be negative")
    cursor_date = None
    cursor_id = 0
    if after_write_date is not None:
        cursor_date = _require_aware_utc_datetime(
            after_write_date, "after_write_date"
        ) - overlap
        cursor_id = int(after_id or 0) if not overlap else 0
    return _fetch_raw_attendance_change_pages(
        execute_fn,
        wc_field,
        department_field,
        base_domain=[],
        cursor_date=cursor_date,
        cursor_id=cursor_id,
        page_size=page_size,
    )


def fetch_open_attendance_rows(
    execute_fn: Callable[..., Any],
    wc_field: str | None,
    department_field: str | None,
    *,
    page_size: int = 250,
) -> list[dict]:
    """Refresh all open rows independently of their last write date."""
    return _fetch_raw_attendance_id_pages(
        execute_fn,
        wc_field,
        department_field,
        base_domain=[("check_out", "=", False)],
        page_size=page_size,
    )


def fetch_open_attendance_rows_for_employee(
    execute_fn: Callable[..., Any],
    wc_field: str | None,
    department_field: str | None,
    employee_odoo_id: int,
    *,
    page_size: int = 250,
) -> list[dict]:
    return _fetch_raw_attendance_id_pages(
        execute_fn,
        wc_field,
        department_field,
        base_domain=[
            ("employee_id", "=", int(employee_odoo_id)),
            ("check_out", "=", False),
        ],
        page_size=page_size,
    )


def fetch_all_attendance_ids(
    execute_fn: Callable[..., Any], *, page_size: int = 500
) -> list[int]:
    """Fetch a complete ID sweep without offset paging."""
    if page_size <= 0:
        raise ValueError("page_size must be positive")
    watermark_rows = execute_fn(
        "hr.attendance",
        "search_read",
        [],
        fields=["id"],
        order="id desc",
        limit=1,
    )
    if not watermark_rows:
        return []
    _require_response_fields(
        watermark_rows[0], ["id"], context="attendance ID sweep watermark"
    )
    watermark_id = _validated_attendance_id(
        watermark_rows[0]["id"], context="attendance ID sweep watermark"
    )
    cursor_id = 0
    ids: list[int] = []
    while True:
        page = execute_fn(
            "hr.attendance",
            "search_read",
            _and_domain(
                ("id", ">", cursor_id),
                ("id", "<=", watermark_id),
            ),
            fields=["id"],
            order="id asc",
            limit=page_size,
        )
        for row in page:
            _require_response_fields(
                row, ["id"], context="attendance ID sweep page"
            )
        page_ids = [
            _validated_attendance_id(
                row["id"], context="attendance ID sweep page"
            )
            for row in page
        ]
        ids.extend(page_ids)
        if len(page) < page_size:
            break
        next_id = page_ids[-1]
        if next_id <= cursor_id:
            raise RuntimeError("Odoo attendance ID cursor did not advance")
        cursor_id = next_id
    return ids


def fetch_attendance_rows_by_ids(
    execute_fn: Callable[..., Any],
    wc_field: str | None,
    department_field: str | None,
    ids: Sequence[int],
) -> list[dict]:
    attendance_ids = sorted({int(attendance_id) for attendance_id in ids})
    if not attendance_ids:
        return []
    rows = execute_fn(
        "hr.attendance",
        "search_read",
        [("id", "in", attendance_ids)],
        fields=_raw_attendance_fields(wc_field, department_field),
        order="id asc",
    )
    return [
        _normalize_raw_attendance(row, wc_field, department_field)
        for row in rows
    ]


def fetch_employee_attendance_rows(
    execute_fn: Callable[..., Any],
    wc_field: str | None,
    department_field: str | None,
    employee_odoo_id: int,
    start_utc: datetime,
    end_utc: datetime | None,
) -> list[dict]:
    start_utc = _require_aware_utc_datetime(start_utc, "start_utc")
    if end_utc is not None:
        end_utc = _require_aware_utc_datetime(end_utc, "end_utc")
    open_overlap = [
        "|",
        ("check_out", "=", False),
        ("check_out", ">", to_odoo_dt(start_utc)),
    ]
    domain = _and_domain(
        ("employee_id", "=", int(employee_odoo_id)),
        open_overlap,
        (
            ("check_in", "<", to_odoo_dt(end_utc))
            if end_utc is not None
            else []
        ),
    )
    rows = execute_fn(
        "hr.attendance",
        "search_read",
        domain,
        fields=_raw_attendance_fields(wc_field, department_field),
        order="check_in asc, id asc",
    )
    return [
        _normalize_raw_attendance(row, wc_field, department_field)
        for row in rows
    ]


def fetch_employee_attendance_rows_at_checkout(
    execute_fn: Callable[..., Any],
    wc_field: str | None,
    department_field: str | None,
    employee_odoo_id: int,
    check_out_utc: datetime,
) -> list[dict]:
    """Find rows closed at one exact Odoo-second retry boundary."""
    check_out_utc = _require_aware_utc_datetime(
        check_out_utc, "check_out_utc"
    )
    rows = execute_fn(
        "hr.attendance",
        "search_read",
        _and_domain(
            ("employee_id", "=", int(employee_odoo_id)),
            ("check_out", "=", to_odoo_dt(check_out_utc)),
        ),
        fields=_raw_attendance_fields(wc_field, department_field),
        order="id asc",
    )
    return [
        _normalize_raw_attendance(row, wc_field, department_field)
        for row in rows
    ]


def is_zero_duration_attendance(row: dict) -> bool:
    """True for closed Odoo rows with no meaningful worked interval.

    Odoo can surface cleanup/no-op rows around midnight as 12:00:00 to
    12:00:01, which displays as 00:00 worked time. Those should not make the
    dashboard treat someone as present for the day.
    """
    check_in = odoo_dt_to_iso(row.get("check_in"))
    check_out = odoo_dt_to_iso(row.get("check_out"))
    if not check_in or not check_out:
        return False
    try:
        start = datetime.fromisoformat(check_in)
        end = datetime.fromisoformat(check_out)
    except (TypeError, ValueError):
        return False
    return 0 <= (end - start).total_seconds() < 60


def get_current_attendance(
    execute_fn: Callable[..., Any],
    employee_odoo_id: int,
    wc_field: str | None,
    department_field: str | None,
) -> dict | None:
    """Return the most recent open attendance row for an employee."""
    del wc_field
    fields = ["id", "employee_id", "check_in"]
    if department_field:
        fields.append(department_field)
    rows = execute_fn(
        "hr.attendance",
        "search_read",
        [("employee_id", "=", employee_odoo_id), ("check_out", "=", False)],
        fields=fields,
        order="check_in desc, id desc",
        limit=1,
    )
    if not rows:
        return None
    row = rows[0]
    department_value = (
        row.get(department_field) if department_field else None
    )
    if isinstance(department_value, list) and department_value:
        row["department_id"] = department_value[0]
        row["department_name"] = (
            department_value[1] if len(department_value) > 1 else None
        )
    else:
        row["department_id"] = None
        row["department_name"] = None
    return row


def fetch_attendances_missing_wc(
    execute_fn: Callable[..., Any], since, wc_field: str | None
) -> list[dict]:
    """Return attendance since ``since`` without a kiosk work-center tag."""
    if not wc_field:
        logging.getLogger("zira_dashboard.odoo_client").warning(
            "ODOO_KIOSK_WC_FIELD not configured; missing-work-center alert disabled"
        )
        return []
    rows = execute_fn(
        "hr.attendance",
        "search_read",
        [("check_in", ">=", to_odoo_dt(since)), (wc_field, "=", False)],
        fields=["id", "employee_id", "check_in", "check_out"],
        order="check_in desc",
        limit=500,
    )
    out: list[dict] = []
    for row in rows:
        employee = row.get("employee_id")
        out.append(
            {
                "att_id": row["id"],
                "employee_odoo_id": _unwrap_m2o(employee),
                "employee_name": (
                    employee[1]
                    if isinstance(employee, list) and len(employee) > 1
                    else None
                ),
                "check_in": odoo_dt_to_iso(row.get("check_in")),
                "check_out": odoo_dt_to_iso(row.get("check_out")),
            }
        )
    return out


def fetch_open_attendances(
    execute_fn: Callable[..., Any],
    wc_field: str | None,
    department_field: str | None,
    app_wc_name_for_odoo_id: Callable[[int | None], str | None],
) -> list[dict]:
    """Return normalized currently-open attendance rows."""
    del department_field
    fields = ["id", "employee_id", "check_in"]
    if wc_field:
        fields.append(wc_field)
    rows = execute_fn(
        "hr.attendance",
        "search_read",
        [("check_out", "=", False)],
        fields=fields,
    )
    out: list[dict] = []
    for row in rows:
        employee_id = _unwrap_m2o(row.get("employee_id"))
        if not employee_id:
            continue
        out.append(
            {
                "att_id": row["id"],
                "employee_odoo_id": employee_id,
                "check_in": odoo_dt_to_iso(row.get("check_in")),
                "wc_name": (
                    app_wc_name_for_odoo_id(_unwrap_m2o(row.get(wc_field)))
                    if wc_field else None
                ),
            }
        )
    return out


def fetch_attendances_for_day(
    execute_fn: Callable[..., Any], day: date
) -> list[dict]:
    """Reduce a local day's attendance to the earliest row per employee."""
    start_local = datetime.combine(day, _time.min, tzinfo=shift_config.SITE_TZ)
    end_local = start_local + timedelta(days=1)
    rows = execute_fn(
        "hr.attendance",
        "search_read",
        [
            ("check_in", ">=", to_odoo_dt(start_local)),
            ("check_in", "<", to_odoo_dt(end_local)),
        ],
        fields=["id", "employee_id", "check_in", "check_out"],
    )
    aggregate: dict[int, dict] = {}
    for row in rows:
        if is_zero_duration_attendance(row):
            continue
        employee_id = _unwrap_m2o(row.get("employee_id"))
        if not employee_id:
            continue
        check_in = odoo_dt_to_iso(row.get("check_in"))
        if check_in is None:
            continue
        is_open = not row.get("check_out")
        current = aggregate.get(employee_id)
        if current is None:
            aggregate[employee_id] = {
                "employee_odoo_id": employee_id,
                "first_check_in": check_in,
                "currently_open": is_open,
            }
        else:
            if check_in < current["first_check_in"]:
                current["first_check_in"] = check_in
            if is_open:
                current["currently_open"] = True
    return list(aggregate.values())


def fetch_employee_attendances_for_day(
    execute_fn: Callable[..., Any], employee_odoo_id: int, day: date
) -> list[dict]:
    start_local = datetime.combine(day, _time.min, tzinfo=shift_config.SITE_TZ)
    end_local = start_local + timedelta(days=1)
    rows = execute_fn(
        "hr.attendance", "search_read",
        [
            ("employee_id", "=", int(employee_odoo_id)),
            ("check_in", ">=", to_odoo_dt(start_local)),
            ("check_in", "<", to_odoo_dt(end_local)),
        ],
        fields=["id", "check_in", "check_out"],
        order="check_in asc, id asc",
    )
    return [
        {
            "id": int(row["id"]),
            "check_in": odoo_dt_to_iso(row.get("check_in")),
            "check_out": odoo_dt_to_iso(row.get("check_out")),
        }
        for row in rows
        if row.get("id") and odoo_dt_to_iso(row.get("check_in"))
    ]


def fetch_attendance_intervals_for_range(
    execute_fn: Callable[..., Any],
    employee_ids,
    start_day: date,
    end_day: date,
) -> list[dict]:
    """Return meaningful attendance intervals that overlap a local date range."""
    start_local = datetime.combine(
        start_day, _time.min, tzinfo=shift_config.SITE_TZ
    )
    stop_local = datetime.combine(
        end_day + timedelta(days=1), _time.min, tzinfo=shift_config.SITE_TZ
    )
    domain = [
        "&", "&",
        ("employee_id", "in", sorted({int(i) for i in employee_ids})),
        ("check_in", "<", to_odoo_dt(stop_local)),
        "|", ("check_out", "=", False), ("check_out", ">", to_odoo_dt(start_local)),
    ]
    rows = execute_fn(
        "hr.attendance",
        "search_read",
        domain,
        fields=["id", "employee_id", "check_in", "check_out"],
        order="employee_id,check_in,id",
    )
    return [
        {
            "id": int(row["id"]),
            "employee_odoo_id": _unwrap_m2o(row["employee_id"]),
            "check_in": odoo_dt_to_iso(row["check_in"]),
            "check_out": odoo_dt_to_iso(row.get("check_out")),
        }
        for row in rows
        if row.get("id") and _unwrap_m2o(row.get("employee_id"))
        and odoo_dt_to_iso(row.get("check_in"))
        and not is_zero_duration_attendance(row)
    ]


def fetch_attendance_intervals_for_day(
    execute_fn: Callable[..., Any],
    day: date,
    wc_field: str | None,
    app_wc_name_for_odoo_id: Callable[[int | None], str | None],
) -> list[dict]:
    """Return every meaningful attendance interval for a local day."""
    start_local = datetime.combine(day, _time.min, tzinfo=shift_config.SITE_TZ)
    end_local = start_local + timedelta(days=1)
    fields = ["id", "employee_id", "check_in", "check_out"]
    if wc_field:
        fields.append(wc_field)
    rows = execute_fn(
        "hr.attendance",
        "search_read",
        [
            ("check_in", ">=", to_odoo_dt(start_local)),
            ("check_in", "<", to_odoo_dt(end_local)),
        ],
        fields=fields,
    )
    out: list[dict] = []
    for row in rows:
        if is_zero_duration_attendance(row):
            continue
        employee_id = _unwrap_m2o(row.get("employee_id"))
        if not employee_id:
            continue
        check_in = odoo_dt_to_iso(row.get("check_in"))
        if check_in is None:
            continue
        out.append(
            {
                "id": int(row["id"]),
                "employee_odoo_id": employee_id,
                "check_in": check_in,
                "check_out": odoo_dt_to_iso(row.get("check_out")),
                "wc_name": (
                    app_wc_name_for_odoo_id(_unwrap_m2o(row.get(wc_field)))
                    if wc_field else None
                ),
            }
        )
    return out
