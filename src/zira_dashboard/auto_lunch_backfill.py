"""Pure planning and execution primitives for historical auto-lunch repairs."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Iterable, Mapping

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
        if check_in is None or check_out is None:
            continue
        if check_in <= window.out_at < check_out:
            repairs.append(Repair(
                attendance_id=int(row["id"]),
                person_odoo_id=person_odoo_id,
                out_at=window.out_at,
                in_at=window.in_at,
                wc_name=row.get("wc_name"),
                create_return=check_out >= window.in_at,
            ))
    return repairs
