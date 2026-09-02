"""Transactional external forklift-driver identity mappings and audit history."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from psycopg2.errors import UniqueViolation

from . import db


@dataclass(frozen=True)
class DriverIdentityMapping:
    external_driver_id: str
    source_name: str
    employee_odoo_id: int
    employee_name: str
    version: int
    created_at: datetime
    created_by_upn: str
    updated_at: datetime
    updated_by_upn: str


class MappingConflict(ValueError):
    """Raised when a requested mapping conflicts with current database state."""


def _required_text(value: object, label: str) -> str:
    clean = str(value or "").strip()
    if not clean:
        raise ValueError(f"{label} is required")
    return clean


def _positive_int(value: object, label: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{label} must be a positive integer")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be a positive integer") from exc
    if parsed <= 0:
        raise ValueError(f"{label} must be a positive integer")
    return parsed


def _mapping(row: dict) -> DriverIdentityMapping:
    return DriverIdentityMapping(
        external_driver_id=row["external_driver_id"],
        source_name=row["source_name"],
        employee_odoo_id=int(row["employee_odoo_id"]),
        employee_name=row["employee_name"],
        version=int(row["version"]),
        created_at=row["created_at"],
        created_by_upn=row["created_by_upn"],
        updated_at=row["updated_at"],
        updated_by_upn=row["updated_by_upn"],
    )


def _append_audit(
    cur,
    *,
    driver_id: str,
    action: str,
    before_employee_id: int | None,
    after_employee_id: int | None,
    before_source_name: str | None,
    after_source_name: str | None,
    actor_upn: str,
    actor_name: str | None,
) -> None:
    cur.execute(
        "INSERT INTO forklift_driver_identity_audit "
        "(external_driver_id, action, before_employee_odoo_id, after_employee_odoo_id, "
        "before_source_name, after_source_name, actor_upn, actor_name) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
        (
            driver_id,
            action,
            before_employee_id,
            after_employee_id,
            before_source_name,
            after_source_name,
            actor_upn,
            actor_name,
        ),
    )


def list_mappings() -> tuple[DriverIdentityMapping, ...]:
    rows = db.query(
        "SELECT m.external_driver_id, m.source_name, m.employee_odoo_id, "
        "p.name AS employee_name, m.version, m.created_at, m.created_by_upn, "
        "m.updated_at, m.updated_by_upn "
        "FROM forklift_driver_identity_map m JOIN people p ON p.odoo_id=m.employee_odoo_id "
        "ORDER BY lower(p.name), m.external_driver_id"
    )
    return tuple(_mapping(row) for row in rows)


def mapping_ids() -> dict[str, int]:
    return {
        row["external_driver_id"]: int(row["employee_odoo_id"])
        for row in db.query(
            "SELECT external_driver_id, employee_odoo_id "
            "FROM forklift_driver_identity_map"
        )
    }


def save_mapping(
    external_driver_id: str,
    source_name: str,
    employee_odoo_id: int,
    *,
    expected_version: int | None,
    actor_upn: str,
    actor_name: str | None,
) -> DriverIdentityMapping:
    driver_id = _required_text(external_driver_id, "external driver ID")
    source = str(source_name or "").strip()[:200]
    employee_id = _positive_int(employee_odoo_id, "employee Odoo ID")
    actor = _required_text(actor_upn, "actor UPN")
    try:
        with db.cursor() as cur:
            cur.execute(
                "SELECT external_driver_id, source_name, employee_odoo_id, version, "
                "created_at, created_by_upn FROM forklift_driver_identity_map "
                "WHERE external_driver_id=%s FOR UPDATE",
                (driver_id,),
            )
            before = cur.fetchone()
            if before is None and expected_version is not None:
                raise MappingConflict("This forklift identity changed. Reload and try again.")
            if before is not None and int(before["version"]) != expected_version:
                raise MappingConflict("This forklift identity changed. Reload and try again.")
            cur.execute(
                "SELECT odoo_id, name FROM people "
                "WHERE odoo_id=%s AND active=TRUE AND excluded=FALSE FOR SHARE",
                (employee_id,),
            )
            person = cur.fetchone()
            if person is None:
                raise MappingConflict("Choose an active employee.")
            cur.execute(
                "SELECT external_driver_id FROM forklift_driver_identity_map "
                "WHERE employee_odoo_id=%s AND external_driver_id<>%s FOR UPDATE",
                (employee_id, driver_id),
            )
            if cur.fetchone() is not None:
                raise MappingConflict(
                    "That employee is already mapped to another forklift identity."
                )
            if before is None:
                cur.execute(
                    "INSERT INTO forklift_driver_identity_map "
                    "(external_driver_id, source_name, employee_odoo_id, "
                    "created_by_upn, updated_by_upn) VALUES (%s,%s,%s,%s,%s) RETURNING *",
                    (driver_id, source, employee_id, actor, actor),
                )
                action = "create"
            else:
                cur.execute(
                    "UPDATE forklift_driver_identity_map SET source_name=%s, "
                    "employee_odoo_id=%s, version=version+1, updated_at=now(), "
                    "updated_by_upn=%s WHERE external_driver_id=%s RETURNING *",
                    (source, employee_id, actor, driver_id),
                )
                action = "change"
            saved = cur.fetchone()
            _append_audit(
                cur,
                driver_id=driver_id,
                action=action,
                before_employee_id=(before["employee_odoo_id"] if before else None),
                after_employee_id=employee_id,
                before_source_name=(before["source_name"] if before else None),
                after_source_name=source,
                actor_upn=actor,
                actor_name=actor_name,
            )
            return _mapping({**saved, "employee_name": person["name"]})
    except UniqueViolation as exc:
        raise MappingConflict(
            "That employee is already mapped to another forklift identity."
        ) from exc


def remove_mapping(
    external_driver_id: str,
    *,
    expected_version: int,
    actor_upn: str,
    actor_name: str | None,
) -> None:
    driver_id = _required_text(external_driver_id, "external driver ID")
    version = _positive_int(expected_version, "mapping version")
    actor = _required_text(actor_upn, "actor UPN")
    with db.cursor() as cur:
        cur.execute(
            "SELECT * FROM forklift_driver_identity_map "
            "WHERE external_driver_id=%s FOR UPDATE",
            (driver_id,),
        )
        before = cur.fetchone()
        if before is None or int(before["version"]) != version:
            raise MappingConflict("This forklift identity changed. Reload and try again.")
        cur.execute(
            "DELETE FROM forklift_driver_identity_map WHERE external_driver_id=%s",
            (driver_id,),
        )
        _append_audit(
            cur,
            driver_id=driver_id,
            action="remove",
            before_employee_id=before["employee_odoo_id"],
            after_employee_id=None,
            before_source_name=before["source_name"],
            after_source_name=None,
            actor_upn=actor,
            actor_name=actor_name,
        )


def audit_rows(external_driver_id: str) -> tuple[dict, ...]:
    return tuple(
        db.query(
            "SELECT action, before_employee_odoo_id, after_employee_odoo_id, "
            "before_source_name, after_source_name, actor_upn, actor_name, created_at "
            "FROM forklift_driver_identity_audit WHERE external_driver_id=%s "
            "ORDER BY created_at, id",
            (_required_text(external_driver_id, "external driver ID"),),
        )
    )
