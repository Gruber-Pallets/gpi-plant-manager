"""Pure planning and execution primitives for historical auto-lunch repairs."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Iterable, Mapping

from .auto_lunch import Window


@dataclass(frozen=True)
class Repair:
    """One historical attendance interval that must be split for lunch."""

    attendance_id: int
    person_odoo_id: int
    out_at: datetime
    in_at: datetime
    wc_name: str | None
    create_return: bool
    return_end_at: datetime | None = None
    needs_clock_out: bool = True


def _as_datetime(value: object) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return None
    return None


def plan_repairs(
    intervals: Iterable[dict],
    windows_by_person: Mapping[int, Window],
    existing_run_people: set[int],
    *,
    as_of: datetime | None = None,
) -> list[Repair]:
    """Return repairs for intervals that actually covered lunch-out.

    Existing run rows are the idempotency boundary: a prior live or backfill
    deduction always wins over a newly proposed repair.
    """
    repairs: list[Repair] = []
    for row in intervals:
        person_odoo_id = int(row["employee_odoo_id"])
        window = windows_by_person.get(person_odoo_id)
        if window is None or person_odoo_id in existing_run_people:
            continue
        check_in = _as_datetime(row.get("check_in"))
        check_out = _as_datetime(row.get("check_out"))
        if check_out is None and (as_of is None or as_of < window.in_at):
            continue
        effective_end = check_out or as_of
        if check_in is None or effective_end is None:
            continue
        if check_in <= window.out_at < effective_end:
            repairs.append(Repair(
                attendance_id=int(row["id"]),
                person_odoo_id=person_odoo_id,
                out_at=window.out_at,
                in_at=window.in_at,
                wc_name=row.get("wc_name"),
                create_return=effective_end >= window.in_at,
                return_end_at=check_out,
            ))
    return repairs


def plan_return_only_repairs(
    intervals: Iterable[dict],
    windows_by_person: Mapping[int, Window],
    existing_run_people: set[int],
    local_clock_outs: Mapping[int, datetime],
) -> list[Repair]:
    """Recover a prior partial repair that ended at lunch-out but never returned.

    A failed Odoo create can leave the original attendance closed at lunch-out.
    The employee's original local clock-out supplies the known end of their
    afternoon interval. Any Odoo interval beginning after lunch means a return
    already exists, so recovery is not proposed.
    """
    rows = list(intervals)
    repairs: list[Repair] = []
    for row in rows:
        person_odoo_id = int(row["employee_odoo_id"])
        window = windows_by_person.get(person_odoo_id)
        check_in = _as_datetime(row.get("check_in"))
        check_out = _as_datetime(row.get("check_out"))
        final_clock_out = local_clock_outs.get(person_odoo_id)
        if (
            window is None
            or person_odoo_id in existing_run_people
            or check_in is None
            or check_out != window.out_at
            or final_clock_out is None
            or final_clock_out <= window.in_at
        ):
            continue
        has_return = any(
            int(other["employee_odoo_id"]) == person_odoo_id
            and (other_in := _as_datetime(other.get("check_in"))) is not None
            and window.in_at <= other_in < final_clock_out
            for other in rows
        )
        if not has_return:
            repairs.append(Repair(
                attendance_id=int(row["id"]),
                person_odoo_id=person_odoo_id,
                out_at=window.out_at,
                in_at=window.in_at,
                wc_name=row.get("wc_name"),
                create_return=True,
                return_end_at=final_clock_out,
                needs_clock_out=False,
            ))
    return repairs


def apply_repair(
    repair: Repair,
    *,
    close: Callable[[int, datetime], None],
    clock_in: Callable[[int, str | None, datetime], int],
    persist: Callable[[Repair, int | None], None],
) -> None:
    """Apply one planned split in Odoo, then persist its local audit trail."""
    if repair.needs_clock_out:
        close(repair.attendance_id, repair.out_at)
    returned_attendance_id = (
        clock_in(repair.person_odoo_id, repair.wc_name, repair.in_at)
        if repair.create_return else None
    )
    persist(repair, returned_attendance_id)


def persist_repair(
    repair: Repair,
    *,
    kind: str,
    returned_attendance_id: int | None,
    write_punch: Callable[[int, str, str | None, datetime, int | None], int],
    write_run: Callable[[int, object, str, datetime, datetime, str | None, int, int | None], None],
) -> None:
    """Write the local auto-lunch audit rows after the Odoo split succeeds."""
    out_punch_id = write_punch(
        repair.person_odoo_id,
        "clock_out",
        None,
        repair.out_at,
        repair.attendance_id,
    )
    in_punch_id = None
    if repair.create_return:
        if returned_attendance_id is None:
            raise ValueError("returned attendance id is required for a lunch return")
        in_punch_id = write_punch(
            repair.person_odoo_id,
            "clock_in",
            repair.wc_name,
            repair.in_at,
            returned_attendance_id,
        )
    write_run(
        repair.person_odoo_id,
        repair.out_at.date(),
        kind,
        repair.out_at,
        repair.in_at,
        repair.wc_name,
        out_punch_id,
        in_punch_id,
    )
