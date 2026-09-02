"""Resolve feedback submitters against one current Odoo employee snapshot."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import Mapping

from . import db


_MAX_SIGNED_64 = 9_223_372_036_854_775_807
_MAX_LOCAL_PEOPLE = 10_000


class SubmitterError(ValueError):
    """A submitter could not be resolved to one active Odoo employee."""


@dataclass(frozen=True)
class SubmitterChoice:
    employee_id: int
    name: str


@dataclass(frozen=True)
class ResolvedSubmitter:
    employee_id: int
    name: str
    email: str


def _positive_employee_id(value: object) -> int | None:
    if type(value) is not int or not 0 < value <= _MAX_SIGNED_64:
        return None
    return value


def normalize_work_email(value: object) -> str | None:
    """Return the canonical stored work email, or None for unsafe input."""
    if type(value) is not str:
        return None
    normalized = value.strip().casefold()
    if (
        not normalized
        or normalized.count("@") != 1
        or any(character.isspace() for character in normalized)
    ):
        return None
    return normalized


def _employee_name(value: object) -> str | None:
    if type(value) is not str or not value.strip():
        return None
    return value.strip()


def _local_rows() -> tuple[Mapping[str, object], ...]:
    try:
        rows = db.query(
            "SELECT odoo_id AS employee_id, name, active, work_email "
            "FROM people WHERE odoo_id IS NOT NULL ORDER BY odoo_id, id LIMIT %s",
            (_MAX_LOCAL_PEOPLE + 1,),
        )
    except Exception as error:
        raise SubmitterError("employee list is unavailable") from error
    if (
        type(rows) is not list
        or len(rows) > _MAX_LOCAL_PEOPLE
        or any(not isinstance(row, Mapping) for row in rows)
        or any(
            not {"employee_id", "name", "active", "work_email"} <= set(row)
            or type(row.get("active")) is not bool
            for row in rows
        )
    ):
        raise SubmitterError("employee list is unavailable")
    return tuple(rows)


def _active_counts(
    rows: tuple[Mapping[str, object], ...],
) -> tuple[Counter[int], Counter[str]]:
    active_rows = [row for row in rows if row.get("active") is True]
    return (
        Counter(
            employee_id
            for row in active_rows
            if (employee_id := _positive_employee_id(row.get("employee_id"))) is not None
        ),
        Counter(
            email
            for row in active_rows
            if (email := normalize_work_email(row.get("work_email"))) is not None
        ),
    )


def _resolved(row: Mapping[str, object]) -> ResolvedSubmitter | None:
    employee_id = _positive_employee_id(row.get("employee_id"))
    name = _employee_name(row.get("name"))
    email = normalize_work_email(row.get("work_email"))
    if row.get("active") is not True or employee_id is None or name is None or email is None:
        return None
    return ResolvedSubmitter(employee_id=employee_id, name=name, email=email)


def active_choices() -> tuple[SubmitterChoice, ...]:
    """Return only uniquely identified active employees, sorted for display."""
    rows = _local_rows()
    id_counts, email_counts = _active_counts(rows)
    choices = []
    for row in rows:
        resolved = _resolved(row)
        if (
            resolved is not None
            and id_counts[resolved.employee_id] == 1
            and email_counts[resolved.email] == 1
        ):
            choices.append(SubmitterChoice(employee_id=resolved.employee_id, name=resolved.name))
    return tuple(sorted(choices, key=lambda item: (item.name.casefold(), item.employee_id)))


def resolve_timeclock(employee_id: object) -> ResolvedSubmitter:
    """Resolve one posted positive ID to exactly one active employee."""
    exact_id = _positive_employee_id(employee_id)
    if exact_id is None:
        raise SubmitterError("employee id is required")
    rows = _local_rows()
    id_counts, email_counts = _active_counts(rows)
    matches = [
        row for row in rows if row.get("active") is True and row.get("employee_id") == exact_id
    ]
    if len(matches) != 1 or id_counts[exact_id] != 1:
        raise SubmitterError("employee id is not one active employee")
    resolved = _resolved(matches[0])
    if resolved is None:
        raise SubmitterError("employee record is incomplete")
    if email_counts[resolved.email] != 1:
        raise SubmitterError("employee email is not unique")
    return resolved


def resolve_private(user_upn: object) -> ResolvedSubmitter:
    """Resolve an authenticated UPN to one active employee work email."""
    normalized_upn = normalize_work_email(user_upn)
    if normalized_upn is None:
        raise SubmitterError("authenticated email is invalid")
    matches = []
    rows = _local_rows()
    id_counts, email_counts = _active_counts(rows)
    for row in rows:
        if row.get("active") is not True:
            continue
        resolved = _resolved(row)
        if resolved is not None and resolved.email == normalized_upn:
            matches.append(resolved)
    if (
        len(matches) != 1
        or id_counts[matches[0].employee_id] != 1
        or email_counts[normalized_upn] != 1
    ):
        raise SubmitterError("authenticated email is not one active employee")
    return matches[0]
