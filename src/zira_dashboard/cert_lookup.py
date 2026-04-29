"""Per-request lookup: who has which certifications.

Reads from the local Postgres tables that the Odoo sync populates:
- `skills` rows with skill_type='Certifications' are the cert master list.
- `person_skills` rows link a person to a cert. Any link counts as
  'has this cert' — the level value is ignored (binary semantics).

Cheap single indexed query. Call once per request from any route that
renders names; pass the result into the template context as
`person_certs`.
"""

from __future__ import annotations

from . import db


def load_person_certs() -> dict[str, list[str]]:
    """Return {person_name: [cert_name, ...]} for everyone with at least
    one certification record. Cert lists are alphabetical."""
    sql = """
        SELECT p.name AS person, s.name AS cert
        FROM person_skills ps
        JOIN skills s ON s.id = ps.skill_id
        JOIN people p ON p.id = ps.person_id
        WHERE s.skill_type = 'Certifications'
        ORDER BY p.name, lower(s.name)
    """
    out: dict[str, list[str]] = {}
    for row in db.query(sql):
        out.setdefault(row["person"], []).append(row["cert"])
    return out
