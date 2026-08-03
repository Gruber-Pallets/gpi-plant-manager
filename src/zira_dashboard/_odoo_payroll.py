from __future__ import annotations

from datetime import date, datetime, time, timedelta
from typing import Any, Callable

from . import _odoo_attendance, shift_config

_TYPE_CODES = ("WORK100", "OVERTIME")
_WORK_FIELDS = [
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
_ATTENDANCE_FIELDS = [
    "id",
    "employee_id",
    "check_in",
    "worked_hours",
    "overtime_hours",
    "validated_overtime_hours",
    "overtime_status",
    "expected_hours",
]


def _m2o_id(value: Any) -> int | None:
    if isinstance(value, (list, tuple)) and value:
        return int(value[0])
    return int(value) if value else None


def _m2o_name(value: Any) -> str:
    return (
        str(value[1])
        if isinstance(value, (list, tuple)) and len(value) > 1
        else ""
    )


def _type_maps(
    execute_fn: Callable[..., Any],
) -> tuple[dict[str, int], dict[int, str]]:
    rows = execute_fn(
        "hr.work.entry.type",
        "search_read",
        [("code", "in", list(_TYPE_CODES))],
        fields=["id", "code"],
    )
    by_code = {str(row["code"]): int(row["id"]) for row in rows}
    missing = set(_TYPE_CODES) - set(by_code)
    if missing:
        raise RuntimeError(
            f"Missing Odoo Work Entry type code(s): {', '.join(sorted(missing))}"
        )
    return by_code, {entry_id: code for code, entry_id in by_code.items()}


def _normalize_work(row: dict, codes_by_id: dict[int, str]) -> dict:
    employee = row.get("employee_id")
    type_id = _m2o_id(row.get("work_entry_type_id"))
    return {
        "id": int(row["id"]),
        "employee_id": _m2o_id(employee),
        "employee_name": _m2o_name(employee),
        "date": date.fromisoformat(str(row["date"])),
        "duration": float(row.get("duration") or 0),
        "state": str(row.get("state") or ""),
        "conflict": bool(row.get("conflict")),
        "active": bool(row.get("active")),
        "type_code": codes_by_id.get(type_id, ""),
        "attendance_id": _m2o_id(row.get("attendance_id")),
        "write_date": row.get("write_date"),
    }


def _normalize_attendance(row: dict) -> dict:
    employee = row.get("employee_id")
    check_in = datetime.strptime(row["check_in"], "%Y-%m-%d %H:%M:%S").replace(
        tzinfo=_odoo_attendance.UTC
    )
    return {
        "id": int(row["id"]),
        "employee_id": _m2o_id(employee),
        "employee_name": _m2o_name(employee),
        "date": check_in.astimezone(shift_config.SITE_TZ).date(),
        "worked_hours": float(row.get("worked_hours") or 0),
        "overtime_hours": float(row.get("overtime_hours") or 0),
        "validated_overtime_hours": float(
            row.get("validated_overtime_hours") or 0
        ),
        "overtime_status": row.get("overtime_status") or "",
        "expected_hours": float(row.get("expected_hours") or 0),
    }


def fetch_recent_candidates(
    execute_fn: Callable[..., Any], written_since: datetime
) -> list[dict]:
    by_code, codes_by_id = _type_maps(execute_fn)
    rows = execute_fn(
        "hr.work.entry",
        "search_read",
        [
            ("active", "=", True),
            ("attendance_id", "!=", False),
            ("work_entry_type_id", "=", by_code["WORK100"]),
            ("write_date", ">=", _odoo_attendance.to_odoo_dt(written_since)),
        ],
        fields=_WORK_FIELDS,
        order="employee_id,date,id",
    )
    return [_normalize_work(row, codes_by_id) for row in rows]


def fetch_inputs(
    execute_fn: Callable[..., Any],
    employee_ids,
    start_day: date,
    end_day: date,
) -> tuple[list[dict], list[dict]]:
    ids = sorted({int(value) for value in employee_ids})
    if not ids:
        return [], []
    _by_code, codes_by_id = _type_maps(execute_fn)
    work_rows = execute_fn(
        "hr.work.entry",
        "search_read",
        [
            ("active", "=", True),
            ("employee_id", "in", ids),
            ("date", ">=", start_day.isoformat()),
            ("date", "<=", end_day.isoformat()),
        ],
        fields=_WORK_FIELDS,
        order="employee_id,date,id",
    )
    local_start = datetime.combine(start_day, time.min, tzinfo=shift_config.SITE_TZ)
    local_stop = datetime.combine(
        end_day + timedelta(days=1), time.min, tzinfo=shift_config.SITE_TZ
    )
    attendance_rows = execute_fn(
        "hr.attendance",
        "search_read",
        [
            ("employee_id", "in", ids),
            ("check_in", ">=", _odoo_attendance.to_odoo_dt(local_start)),
            ("check_in", "<", _odoo_attendance.to_odoo_dt(local_stop)),
        ],
        fields=_ATTENDANCE_FIELDS,
        order="employee_id,check_in,id",
    )
    work = [_normalize_work(row, codes_by_id) for row in work_rows]
    attendance = [_normalize_attendance(row) for row in attendance_rows]
    attendance = [row for row in attendance if start_day <= row["date"] <= end_day]
    return work, attendance


def read_work_entry(execute_fn: Callable[..., Any], entry_id: int) -> dict | None:
    _by_code, codes_by_id = _type_maps(execute_fn)
    rows = execute_fn(
        "hr.work.entry", "read", [int(entry_id)], fields=_WORK_FIELDS
    )
    return _normalize_work(rows[0], codes_by_id) if rows else None


def write_duration(
    execute_fn: Callable[..., Any], entry_id: int, duration: float
) -> None:
    if duration <= 0:
        raise ValueError("Odoo Work Entry duration must be positive")
    execute_fn(
        "hr.work.entry", "write", [int(entry_id)], {"duration": float(duration)}
    )


def delete_entry(execute_fn: Callable[..., Any], entry_id: int) -> None:
    execute_fn("hr.work.entry", "unlink", [int(entry_id)])


def entry_exists(execute_fn: Callable[..., Any], entry_id: int) -> bool:
    return bool(
        execute_fn(
            "hr.work.entry", "search_count", [("id", "=", int(entry_id))]
        )
    )
