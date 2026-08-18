"""Conservative, auditable discovery and storage of legacy production IDs.

This module intentionally does not alter ``production_daily``.  It only
proposes aliases for the small, reviewed set of names and records approved
proposals in ``production_identity_aliases``.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from collections.abc import Iterable

from . import db


APPROVED_NAMES: frozenset[str] = frozenset({
    "Adrian Aragon",
    "Alejandro Velazquez",
    "Christian Chanta",
    "Domingo Recinos",
    "Eulogio Mendez",
    "Gerardo Vergara",
    "Jesus Galindo",
    "Jesus Martinez",
    "Jose Cabezas",
    "Jose Luis",
    "Jose Ochoa",
    "Lauro Benitez",
    "Porfirio Cazares",
})
SOURCE = "legacy_name_reconciliation"


@dataclass(frozen=True)
class AliasCandidate:
    """One legacy production ID that may be mapped to its Odoo ID."""

    legacy_emp_id: str
    canonical_emp_id: str
    confirmed_name: str
    production_days: tuple[date, ...]


@dataclass(frozen=True)
class SkippedName:
    """An approved name that could not be reconciled without a guess."""

    name: str
    reason: str
    canonical_emp_id: str | None
    observed_emp_ids: tuple[str, ...]
    production_days: tuple[date, ...]


@dataclass(frozen=True)
class ReconciliationResult:
    """The safe aliases and every approved name that needs operator review."""

    candidates: tuple[AliasCandidate, ...]
    skipped: tuple[SkippedName, ...]


def _nonempty_id(value: object) -> str | None:
    if value is None:
        return None
    identifier = str(value)
    return identifier if identifier.strip() else None


def _production_days(rows: list[dict]) -> tuple[date, ...]:
    return tuple(sorted({row["day"] for row in rows if row.get("day") is not None}))


def _observed_emp_ids(rows: list[dict]) -> tuple[str, ...]:
    return tuple(sorted({
        "" if row.get("emp_id") is None else str(row["emp_id"])
        for row in rows
        if row.get("emp_id") is not None
    }))


def find_confirmed_aliases() -> ReconciliationResult:
    """Find aliases that satisfy every reviewed reconciliation safeguard.

    The query begins with the fixed allow-list, so people and production facts
    are considered only when their display names match an approved name exactly.
    Returned candidates retain the source days needed by a later dry-run report.
    """
    rows = db.query(
        """
        WITH approved_names(name) AS (
            SELECT unnest(%s::text[])
        )
        SELECT approved.name AS approved_name,
               p.id AS person_id,
               p.odoo_id,
               pd.emp_id,
               pd.day
        FROM approved_names approved
        LEFT JOIN people p ON p.name = approved.name
        LEFT JOIN production_daily pd ON pd.name = approved.name
        ORDER BY approved.name, p.id, pd.emp_id, pd.day
        """,
        (sorted(APPROVED_NAMES),),
    )
    rows_by_name = {name: [] for name in APPROVED_NAMES}
    for row in rows:
        name = row.get("approved_name")
        if name in APPROVED_NAMES:
            rows_by_name[name].append(row)

    candidates: list[AliasCandidate] = []
    skipped: list[SkippedName] = []
    for name in sorted(APPROVED_NAMES):
        name_rows = rows_by_name[name]
        person_rows = {
            (row["person_id"], row.get("odoo_id"))
            for row in name_rows
            if row.get("person_id") is not None
        }
        observed_ids = _observed_emp_ids(name_rows)
        days = _production_days(name_rows)

        if len(person_rows) != 1:
            skipped.append(SkippedName(
                name=name,
                reason=("missing_exact_roster_name" if not person_rows
                        else "duplicate_exact_roster_name"),
                canonical_emp_id=None,
                observed_emp_ids=observed_ids,
                production_days=days,
            ))
            continue

        _, raw_canonical_id = next(iter(person_rows))
        canonical_emp_id = _nonempty_id(raw_canonical_id)
        if canonical_emp_id is None:
            skipped.append(SkippedName(
                name=name,
                reason="missing_canonical_odoo_id",
                canonical_emp_id=None,
                observed_emp_ids=observed_ids,
                production_days=days,
            ))
            continue

        production_rows = [
            row for row in name_rows
            if row.get("emp_id") is not None or row.get("day") is not None
        ]
        if any(_nonempty_id(row.get("emp_id")) is None for row in production_rows):
            skipped.append(SkippedName(
                name=name,
                reason="blank_production_id",
                canonical_emp_id=canonical_emp_id,
                observed_emp_ids=observed_ids,
                production_days=days,
            ))
            continue

        production_ids = {str(row["emp_id"]) for row in production_rows}
        if canonical_emp_id not in production_ids:
            skipped.append(SkippedName(
                name=name,
                reason="canonical_id_missing_from_production",
                canonical_emp_id=canonical_emp_id,
                observed_emp_ids=observed_ids,
                production_days=days,
            ))
            continue

        legacy_ids = sorted(production_ids - {canonical_emp_id})
        if not legacy_ids:
            skipped.append(SkippedName(
                name=name,
                reason="no_historical_legacy_id",
                canonical_emp_id=canonical_emp_id,
                observed_emp_ids=observed_ids,
                production_days=days,
            ))
            continue

        for legacy_emp_id in legacy_ids:
            legacy_days = tuple(sorted({
                row["day"] for row in production_rows
                if str(row["emp_id"]) == legacy_emp_id and row.get("day") is not None
            }))
            candidates.append(AliasCandidate(
                legacy_emp_id=legacy_emp_id,
                canonical_emp_id=canonical_emp_id,
                confirmed_name=name,
                production_days=legacy_days,
            ))

    return ReconciliationResult(tuple(candidates), tuple(skipped))


def upsert_confirmed_aliases(candidates: Iterable[AliasCandidate]) -> int:
    """Persist candidates produced by :func:`find_confirmed_aliases`.

    The defensive validation keeps callers from turning this into a generic
    name-based identity-writing API.
    """
    candidates = tuple(candidates)
    if not candidates:
        return 0
    for candidate in candidates:
        if (
            not isinstance(candidate, AliasCandidate)
            or candidate.confirmed_name not in APPROVED_NAMES
            or _nonempty_id(candidate.legacy_emp_id) is None
            or _nonempty_id(candidate.canonical_emp_id) is None
            or candidate.legacy_emp_id == candidate.canonical_emp_id
        ):
            raise ValueError("candidates must come from approved-name reconciliation")

    db.execute_many(
        """
        INSERT INTO production_identity_aliases (
            legacy_emp_id, canonical_emp_id, confirmed_name, confirmed_at, source
        ) VALUES (%s, %s, %s, now(), %s)
        ON CONFLICT (legacy_emp_id) DO UPDATE SET
            canonical_emp_id = EXCLUDED.canonical_emp_id,
            confirmed_name = EXCLUDED.confirmed_name,
            confirmed_at = now(),
            source = EXCLUDED.source
        """,
        [
            (
                candidate.legacy_emp_id,
                candidate.canonical_emp_id,
                candidate.confirmed_name,
                SOURCE,
            )
            for candidate in candidates
        ],
    )
    return len(candidates)
