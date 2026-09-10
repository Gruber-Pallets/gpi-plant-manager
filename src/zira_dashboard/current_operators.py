"""Pure read-only current-operator presence extraction and display join."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from . import attendance_location_snapshot, staffing


@dataclass(frozen=True)
class OperatorPresence:
    person_name: str
    wc_name: str
    arrival_utc: datetime
    employee_odoo_id: int | None = None


@dataclass(frozen=True)
class OperatorDeparture:
    person_name: str
    wc_name: str
    arrival_utc: datetime
    departure_utc: datetime
    employee_odoo_id: int | None = None


@dataclass(frozen=True)
class OperatorSourceSnapshot:
    presences: tuple[OperatorPresence, ...]
    departures: tuple[OperatorDeparture, ...]
    available: bool
    mirror_owned: bool
    complete: bool = True


@dataclass(frozen=True)
class OperatorDisplayRow:
    person_name: str
    employee_odoo_id: int | None
    planned: bool
    physically_present: bool


def _identity_order(value: OperatorPresence | OperatorDeparture) -> tuple:
    return (
        value.employee_odoo_id is None,
        value.employee_odoo_id or 0,
        value.person_name,
        value.wc_name,
        value.arrival_utc,
    )


def _employee_id(value: object) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def source_from_location_snapshot(
    snapshot: attendance_location_snapshot.LocationSnapshot,
) -> OperatorSourceSnapshot:
    policy = snapshot.policy
    if not policy.mirror_owned:
        return OperatorSourceSnapshot((), (), False, False, False)
    if not policy.available or policy.stale:
        return OperatorSourceSnapshot((), (), False, True, False)

    presences: list[OperatorPresence] = []
    departures: list[OperatorDeparture] = []
    complete = True
    for span in snapshot.spans:
        is_current = (
            span.start_utc <= snapshot.verified_cap_utc < span.end_utc
            or (
                span.start_utc <= snapshot.verified_cap_utc == span.end_utc
                and bool(
                    snapshot.current_attendance_ids.intersection(
                        span.attendance_ids
                    )
                )
            )
        )
        if span.status == "exempt_no_location":
            continue
        if span.status != "valid" or not span.app_work_center_name:
            complete = complete and not is_current
            continue
        if is_current:
            presences.append(
                OperatorPresence(
                    span.employee_name,
                    span.app_work_center_name,
                    span.start_utc,
                    span.employee_odoo_id,
                )
            )
        elif span.start_utc < span.end_utc <= snapshot.verified_cap_utc:
            departures.append(
                OperatorDeparture(
                    span.employee_name,
                    span.app_work_center_name,
                    span.start_utc,
                    span.end_utc,
                    span.employee_odoo_id,
                )
            )
    return OperatorSourceSnapshot(
        tuple(sorted(presences, key=_identity_order)),
        tuple(sorted(departures, key=_identity_order)),
        True,
        True,
        complete,
    )


def build_display_by_work_center(
    planned_by_wc,
    *,
    planned_employee_ids,
    absent_names,
    source,
    is_today,
):
    if not is_today:
        return {}
    filtered_plan = {
        wc_name: tuple(name for name in names if name not in absent_names)
        for wc_name, names in planned_by_wc.items()
        if wc_name != staffing.TIME_OFF_KEY
    }
    present = (
        source.presences
        if is_today and source.available and source.mirror_owned
        else ()
    )
    present_ids_by_wc = {
        wc_name: {item.employee_odoo_id for item in present if item.wc_name == wc_name}
        for wc_name in {item.wc_name for item in present}
    }
    result = {}
    for wc_name in dict.fromkeys([*filtered_plan, *(item.wc_name for item in present)]):
        rows = []
        seen_ids: set[int | None] = set()
        for name in filtered_plan.get(wc_name, ()):
            employee_id = _employee_id(planned_employee_ids.get(name))
            physically_present = (
                employee_id is not None
                and employee_id in present_ids_by_wc.get(wc_name, set())
            )
            rows.append(OperatorDisplayRow(name, employee_id, True, physically_present))
            if employee_id is not None:
                seen_ids.add(employee_id)
        for item in present:
            if item.wc_name == wc_name and item.employee_odoo_id not in seen_ids:
                rows.append(
                    OperatorDisplayRow(
                        item.person_name,
                        item.employee_odoo_id,
                        False,
                        True,
                    )
                )
                if item.employee_odoo_id is not None:
                    seen_ids.add(item.employee_odoo_id)
        result[wc_name] = tuple(rows)
    return result
