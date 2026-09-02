"""Resolve feedback submitters against one current Odoo employee snapshot."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import Mapping

from . import odoo_client


_MAX_SIGNED_64 = 9_223_372_036_854_775_807


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


def _normalized_email(value: object) -> str | None:
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


def _fetched_rows() -> tuple[Mapping[str, object], ...]:
    try:
        statuses = odoo_client.fetch_employee_statuses()
    except Exception as error:
        raise SubmitterError("employee list is unavailable") from error
    if type(statuses) is not list or any(not isinstance(row, Mapping) for row in statuses):
        raise SubmitterError("employee list is unavailable")

    rows = [dict(row) for row in statuses]
    active_missing_details = any(
        row.get("active") is True and ("name" not in row or "work_email" not in row) for row in rows
    )
    if active_missing_details:
        try:
            details = odoo_client.fetch_employees()
        except Exception as error:
            raise SubmitterError("employee list is unavailable") from error
        if type(details) is not list or any(not isinstance(row, Mapping) for row in details):
            raise SubmitterError("employee list is unavailable")
        details_by_id: dict[int, list[Mapping[str, object]]] = {}
        for detail in details:
            employee_id = _positive_employee_id(detail.get("id"))
            if employee_id is not None:
                details_by_id.setdefault(employee_id, []).append(detail)
        for row in rows:
            employee_id = _positive_employee_id(row.get("id"))
            matches = details_by_id.get(employee_id or 0, [])
            if row.get("active") is True and len(matches) == 1:
                row.update(matches[0])
                row["active"] = True
    return tuple(rows)


def _resolved(row: Mapping[str, object]) -> ResolvedSubmitter | None:
    employee_id = _positive_employee_id(row.get("id"))
    name = _employee_name(row.get("name"))
    email = _normalized_email(row.get("work_email"))
    if employee_id is None or name is None or email is None:
        return None
    return ResolvedSubmitter(employee_id=employee_id, name=name, email=email)


def active_choices() -> tuple[SubmitterChoice, ...]:
    """Return only uniquely identified active employees, sorted for display."""
    rows = _fetched_rows()
    id_counts = Counter(
        employee_id
        for row in rows
        if (employee_id := _positive_employee_id(row.get("id"))) is not None
    )
    choices = []
    for row in rows:
        resolved = _resolved(row)
        if (
            row.get("active") is True
            and resolved is not None
            and id_counts[resolved.employee_id] == 1
        ):
            choices.append(SubmitterChoice(employee_id=resolved.employee_id, name=resolved.name))
    return tuple(sorted(choices, key=lambda item: (item.name.casefold(), item.employee_id)))


def resolve_timeclock(employee_id: object) -> ResolvedSubmitter:
    """Resolve one posted positive ID to exactly one active employee."""
    exact_id = _positive_employee_id(employee_id)
    if exact_id is None:
        raise SubmitterError("employee id is required")
    matches = [row for row in _fetched_rows() if row.get("id") == exact_id]
    if len(matches) != 1 or matches[0].get("active") is not True:
        raise SubmitterError("employee id is not one active employee")
    resolved = _resolved(matches[0])
    if resolved is None:
        raise SubmitterError("employee record is incomplete")
    return resolved


def resolve_private(user_upn: object) -> ResolvedSubmitter:
    """Resolve an authenticated UPN to one active employee work email."""
    normalized_upn = _normalized_email(user_upn)
    if normalized_upn is None:
        raise SubmitterError("authenticated email is invalid")
    matches = []
    for row in _fetched_rows():
        if row.get("active") is not True:
            continue
        resolved = _resolved(row)
        if resolved is not None and resolved.email == normalized_upn:
            matches.append(resolved)
    if len(matches) != 1:
        raise SubmitterError("authenticated email is not one active employee")
    return matches[0]
