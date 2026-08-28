"""Private stateless Odoo time-off operations used by the client facade."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Callable

from ._odoo_attendance import to_odoo_dt


# hr.leave.allocation states that contribute to allocated_total.
_ALLOCATION_STATE_VALIDATED = "validate"
# hr.leave states pulled together; "validate" is taken, others are pending.
_LEAVE_STATES_OPEN = ("confirm", "validate1", "validate")
_LEAVE_STATE_TAKEN = "validate"
_LEAVE_SNAPSHOT_FIELDS = [
    "id",
    "employee_id",
    "holiday_status_id",
    "request_date_from",
    "request_date_to",
    "state",
]
_LEAVE_TERMINAL_STATES = ["cancel", "refuse"]


class OdooLeavePayloadError(RuntimeError):
    """Odoo returned a leave row that cannot be verified safely."""


def _record_id(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise OdooLeavePayloadError(
            f"Odoo leave payload has an invalid {field}"
        )
    return value


def _m2o_id(value: Any, field: str) -> int:
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        raise OdooLeavePayloadError(
            f"Odoo leave payload has an invalid {field}"
        )
    display_name = value[1]
    if not isinstance(display_name, str) or not display_name.strip():
        raise OdooLeavePayloadError(
            f"Odoo leave payload has an invalid {field} display name"
        )
    return _record_id(value[0], field)


def _snapshot_from_row(
    row: Any,
    *,
    expected_leave_id: int | None = None,
    expected_employee_id: int | None = None,
    expected_type_id: int | None = None,
    expected_day: date | None = None,
) -> dict:
    if not isinstance(row, dict):
        raise OdooLeavePayloadError("Odoo leave payload row was malformed")
    leave_id = _record_id(row.get("id"), "id")
    employee_id = _m2o_id(row.get("employee_id"), "employee_id")
    type_id = _m2o_id(
        row.get("holiday_status_id"), "holiday_status_id"
    )
    try:
        date_from = date.fromisoformat(row["request_date_from"])
        date_to = date.fromisoformat(row["request_date_to"])
    except (KeyError, TypeError, ValueError) as exc:
        raise OdooLeavePayloadError(
            "Odoo leave payload has invalid request dates"
        ) from exc
    state = row.get("state")
    if not isinstance(state, str) or not state or state != state.strip():
        raise OdooLeavePayloadError(
            "Odoo leave payload has an invalid state"
        )
    if expected_leave_id is not None and leave_id != expected_leave_id:
        raise OdooLeavePayloadError(
            "Odoo leave payload did not echo the requested id"
        )
    if (
        expected_employee_id is not None
        and employee_id != expected_employee_id
    ):
        raise OdooLeavePayloadError(
            "Odoo leave payload did not echo the requested employee"
        )
    if expected_type_id is not None and type_id != expected_type_id:
        raise OdooLeavePayloadError(
            "Odoo leave payload did not echo the requested type"
        )
    if expected_day is not None and (
        date_from != expected_day or date_to != expected_day
    ):
        raise OdooLeavePayloadError(
            "Odoo leave payload did not echo the requested day"
        )
    return {
        "id": leave_id,
        "employee_id": employee_id,
        "holiday_status_id": type_id,
        "date_from": date_from,
        "date_to": date_to,
        "state": state,
    }


def _validated_rows(rows: Any) -> list:
    if not isinstance(rows, list) or len(rows) > 2:
        raise OdooLeavePayloadError("Odoo leave payload was malformed")
    return rows


def _norm_requires_allocation(value) -> str:
    """Canonicalize hr.leave.type.requires_allocation to 'yes' / 'no'.

    Odoo <=18 exposes this as a Selection ('yes'/'no'); Odoo 19+ changed it
    to a Boolean, so XML-RPC returns a Python bool. The rest of the app assumes
    the string form.
    """
    if isinstance(value, str):
        return "yes" if value.strip().lower() in ("yes", "true", "1") else "no"
    return "yes" if value else "no"


def fetch_leave_types(
    execute_fn: Callable[..., Any],
    norm_requires_allocation_fn: Callable[[Any], str] | None = None,
) -> list[dict]:
    """Fetch and normalize all active hr.leave.type rows."""
    normalize = norm_requires_allocation_fn or _norm_requires_allocation
    rows = execute_fn(
        "hr.leave.type",
        "search_read",
        [("active", "=", True)],
        fields=[
            "id",
            "name",
            "request_unit",
            "requires_allocation",
            "color",
            "active",
        ],
    )
    for row in rows:
        row["requires_allocation"] = normalize(
            row.get("requires_allocation")
        )
    return rows


def fetch_leaves_for_range(
    execute_fn: Callable[..., Any],
    start_d,
    end_d,
    modified_since=None,
    to_odoo_dt_fn: Callable[[Any], str] | None = None,
) -> list[dict]:
    """Fetch hr.leave records overlapping the requested date range."""
    domain = [
        ("request_date_to", ">=", start_d.isoformat()),
        ("request_date_from", "<=", end_d.isoformat()),
        ("employee_id.active", "=", True),
    ]
    if modified_since is not None:
        convert_datetime = to_odoo_dt_fn or to_odoo_dt
        domain.append(("write_date", ">", convert_datetime(modified_since)))
    return execute_fn(
        "hr.leave",
        "search_read",
        domain,
        fields=[
            "id",
            "employee_id",
            "holiday_status_id",
            "state",
            "date_from",
            "date_to",
            "request_date_from",
            "request_date_to",
            "request_hour_from",
            "request_hour_to",
            "request_unit_hours",
            "number_of_days",
            "number_of_hours",
            "name",
        ],
    )


def fetch_balances_for_many(
    execute_fn: Callable[..., Any],
    unwrap_m2o_fn: Callable[[Any], Any],
    types: list[dict],
    employee_odoo_ids: list[int],
    aggregate_balances_fn: Callable[..., list[dict]] | None = None,
) -> dict[int, list[dict]]:
    """Fetch and aggregate time-off balances for many employees."""
    ids = list(dict.fromkeys(employee_odoo_ids))
    if not ids:
        return {}
    allocations = execute_fn(
        "hr.leave.allocation",
        "search_read",
        [
            ("employee_id", "in", ids),
            ("state", "=", _ALLOCATION_STATE_VALIDATED),
        ],
        fields=[
            "employee_id",
            "holiday_status_id",
            "number_of_days_display",
            "number_of_hours_display",
        ],
    )
    leaves = execute_fn(
        "hr.leave",
        "search_read",
        [
            ("employee_id", "in", ids),
            ("state", "in", list(_LEAVE_STATES_OPEN)),
        ],
        fields=[
            "employee_id",
            "holiday_status_id",
            "state",
            "number_of_days",
            "number_of_hours",
        ],
    )
    alloc_by_emp: dict[int, list[dict]] = {eid: [] for eid in ids}
    leave_by_emp: dict[int, list[dict]] = {eid: [] for eid in ids}
    for allocation in allocations:
        employee_id = unwrap_m2o_fn(allocation.get("employee_id"))
        if employee_id in alloc_by_emp:
            alloc_by_emp[employee_id].append(allocation)
    for leave in leaves:
        employee_id = unwrap_m2o_fn(leave.get("employee_id"))
        if employee_id in leave_by_emp:
            leave_by_emp[employee_id].append(leave)
    def _for_employee(employee_id: int) -> list[dict]:
        if aggregate_balances_fn is not None:
            return aggregate_balances_fn(
                types,
                alloc_by_emp[employee_id],
                leave_by_emp[employee_id],
            )
        return _aggregate_balances(
            types,
            alloc_by_emp[employee_id],
            leave_by_emp[employee_id],
            unwrap_m2o_fn,
        )

    return {employee_id: _for_employee(employee_id) for employee_id in ids}


def _aggregate_balances(
    types: list[dict],
    allocations: list[dict],
    leaves: list[dict],
    unwrap_m2o_fn: Callable[[Any], Any] | None = None,
) -> list[dict]:
    """Reduce one employee's allocation + leave rows to per-type balances."""

    def _unwrap(value: Any) -> Any:
        if unwrap_m2o_fn is not None:
            return unwrap_m2o_fn(value)
        return value[0] if isinstance(value, (list, tuple)) and value else value

    def _holiday_status_id(row: dict) -> int:
        return _unwrap(row["holiday_status_id"])

    out: list[dict] = []
    for leave_type in types:
        type_id = leave_type["id"]
        unit = "hours" if leave_type["request_unit"] == "hour" else "days"
        allocation_field = (
            "number_of_hours_display"
            if unit == "hours"
            else "number_of_days_display"
        )
        leave_field = "number_of_hours" if unit == "hours" else "number_of_days"
        allocated = 0.0
        for allocation in allocations:
            if _holiday_status_id(allocation) == type_id:
                allocated += float(allocation.get(allocation_field) or 0)
        taken = 0.0
        pending = 0.0
        for leave in leaves:
            if _holiday_status_id(leave) != type_id:
                continue
            value = float(leave.get(leave_field) or 0)
            if leave["state"] == _LEAVE_STATE_TAKEN:
                taken += value
            else:
                pending += value
        available = allocated - taken
        practical = allocated - taken - pending
        out.append(
            {
                "holiday_status_id": type_id,
                "unit": unit,
                "allocated_total": allocated,
                "taken": taken,
                "pending": pending,
                "available": available,
                "available_practical": practical,
            }
        )
    return out


def create_leave(
    execute_fn: Callable[..., Any],
    employee_odoo_id: int,
    holiday_status_id: int,
    date_from,
    date_to,
    hour_from: float | None = None,
    hour_to: float | None = None,
    note: str | None = None,
) -> int:
    """Create an hr.leave and return the new leave id."""
    payload: dict[str, Any] = {
        "employee_id": employee_odoo_id,
        "holiday_status_id": holiday_status_id,
        "request_date_from": date_from.isoformat(),
        "request_date_to": date_to.isoformat(),
    }
    if hour_from is not None and hour_to is not None:
        payload["request_unit_hours"] = True
        payload["request_hour_from"] = float(hour_from)
        payload["request_hour_to"] = float(hour_to)
    if note:
        payload["name"] = note
    return execute_fn("hr.leave", "create", payload)


def confirm_leave(execute_fn: Callable[..., Any], leave_id: int) -> None:
    """Submit a draft hr.leave into the approval workflow."""
    rows = execute_fn("hr.leave", "read", [leave_id], ["state"])
    if rows and rows[0].get("state") == "draft":
        execute_fn("hr.leave", "action_confirm", [leave_id])


def confirm_leave_once(execute_fn: Callable[..., Any], leave_id: int) -> None:
    """Perform exactly one confirmation mutation; the caller owns all reads."""
    normalized_leave_id = _record_id(leave_id, "id")
    execute_fn("hr.leave", "action_confirm", [normalized_leave_id])


def approve_leave(
    execute_fn: Callable[..., Any], leave_id: int
) -> str | None:
    """Approve a pending hr.leave and return its final Odoo state."""
    state: str | None = None
    for _ in range(3):
        rows = execute_fn("hr.leave", "read", [leave_id], ["state"])
        state = rows[0].get("state") if rows else None
        if state == "draft":
            confirm_leave(execute_fn, leave_id)
            continue
        if state in ("confirm", "validate1"):
            execute_fn("hr.leave", "action_approve", [leave_id])
            continue
        return state
    rows = execute_fn("hr.leave", "read", [leave_id], ["state"])
    return rows[0].get("state") if rows else state


def approve_leave_once(execute_fn: Callable[..., Any], leave_id: int) -> None:
    """Perform exactly one approval transition; the caller owns all rereads."""
    normalized_leave_id = _record_id(leave_id, "id")
    execute_fn("hr.leave", "action_approve", [normalized_leave_id])


def write_leave(
    execute_fn: Callable[..., Any], leave_id: int, **fields: Any
) -> None:
    """Update fields on an existing hr.leave."""
    execute_fn("hr.leave", "write", [leave_id], fields)


def refuse_leave(execute_fn: Callable[..., Any], leave_id: int) -> None:
    """Call hr.leave.action_refuse for the requested leave."""
    execute_fn("hr.leave", "action_refuse", [leave_id])


def reset_leave_to_confirm(
    execute_fn: Callable[..., Any], leave_id: int
) -> None:
    """Reset a refused/cancelled hr.leave to the pending confirm state."""
    execute_fn("hr.leave", "write", [leave_id], {"state": "confirm"})


def fetch_leave_state(
    execute_fn: Callable[..., Any], leave_id: int
) -> str | None:
    """Return the current hr.leave state, or None for a missing record."""
    rows = execute_fn(
        "hr.leave",
        "search_read",
        [("id", "=", leave_id)],
        fields=["state"],
    )
    return rows[0]["state"] if rows else None


def fetch_leave_snapshot(
    execute_fn: Callable[..., Any], leave_id: int
) -> dict | None:
    """Read and normalize one archived-inclusive hr.leave identity row."""
    normalized_leave_id = _record_id(leave_id, "id")
    rows = _validated_rows(
        execute_fn(
            "hr.leave",
            "search_read",
            [("id", "=", normalized_leave_id)],
            fields=_LEAVE_SNAPSHOT_FIELDS,
            limit=2,
            context={"active_test": False},
        )
    )
    if not rows:
        return None
    if len(rows) != 1:
        raise OdooLeavePayloadError(
            "Odoo leave payload returned multiple rows for one id"
        )
    return _snapshot_from_row(
        rows[0], expected_leave_id=normalized_leave_id
    )


def find_matching_leaves(
    execute_fn: Callable[..., Any],
    employee_id: int,
    type_id: int,
    day: date,
    include_terminal: bool = True,
) -> list[dict]:
    """Return up to two exact employee/type/day leave snapshots."""
    normalized_employee_id = _record_id(employee_id, "employee_id")
    normalized_type_id = _record_id(type_id, "holiday_status_id")
    if isinstance(day, datetime) or not isinstance(day, date):
        raise ValueError("day must be a date")
    if not isinstance(include_terminal, bool):
        raise ValueError("include_terminal must be a boolean")
    domain = [
        ("employee_id", "=", normalized_employee_id),
        ("holiday_status_id", "=", normalized_type_id),
        ("request_date_from", "=", day.isoformat()),
        ("request_date_to", "=", day.isoformat()),
    ]
    kwargs: dict[str, Any] = {
        "fields": _LEAVE_SNAPSHOT_FIELDS,
        "order": "id asc",
        "limit": 2,
    }
    if include_terminal:
        kwargs["context"] = {"active_test": False}
    else:
        domain.append(("state", "not in", _LEAVE_TERMINAL_STATES))
    rows = _validated_rows(
        execute_fn("hr.leave", "search_read", domain, **kwargs)
    )
    return [
        _snapshot_from_row(
            row,
            expected_employee_id=normalized_employee_id,
            expected_type_id=normalized_type_id,
            expected_day=day,
        )
        for row in rows
    ]


def post_leave_message(
    execute_fn: Callable[..., Any], leave_id: int, body: str
) -> None:
    """Post a chatter message for an hr.leave."""
    execute_fn("hr.leave", "message_post", [leave_id], body=body)


def fetch_company_holidays(execute_fn: Callable[..., Any]) -> list[dict]:
    return execute_fn(
        "resource.calendar.leaves",
        "search_read",
        [("resource_id", "=", False), ("calendar_id", "=", False)],
        fields=["id", "name", "date_from", "date_to"],
    )


def fetch_public_holidays(
    execute_fn: Callable[..., Any], start_d, end_d
) -> list[dict]:
    """Fetch company-wide public holidays overlapping the requested range."""
    domain = [
        ("resource_id", "=", False),
        ("date_to", ">=", start_d.isoformat() + " 00:00:00"),
        ("date_from", "<=", end_d.isoformat() + " 23:59:59"),
    ]
    return execute_fn(
        "resource.calendar.leaves",
        "search_read",
        domain,
        fields=["id", "name", "date_from", "date_to", "calendar_id"],
    )


def find_duplicate_leave(
    execute_fn: Callable[..., Any],
    employee_odoo_id: int,
    holiday_status_id: int,
    date_from,
    date_to,
) -> int | None:
    """Return the id of a matching non-rejected hr.leave, if one exists."""
    rows = execute_fn(
        "hr.leave",
        "search_read",
        [
            ("employee_id", "=", employee_odoo_id),
            ("holiday_status_id", "=", holiday_status_id),
            ("request_date_from", "=", date_from.isoformat()),
            ("request_date_to", "=", date_to.isoformat()),
            ("state", "in", list(_LEAVE_STATES_OPEN)),
        ],
        fields=["id"],
        limit=1,
    )
    return rows[0]["id"] if rows else None
