"""Seed + DDL for department-driven rounding. Postgres-backed."""

import os

import pytest

from zira_dashboard import db

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="needs Postgres",
)


def test_seed_creates_three_systems():
    db.bootstrap_schema()
    names = {r["name"] for r in db.query("SELECT name FROM rounding_systems")}
    assert {"Plant Operator", "Supervisor", "Transportation"} <= names


def test_seed_maps_five_departments_to_named_systems():
    db.bootstrap_schema()
    rows = db.query(
        "SELECT dr.department, rs.name AS system_name "
        "FROM department_rounding dr JOIN rounding_systems rs ON rs.id = dr.system_id"
    )
    m = {r["department"]: r["system_name"] for r in rows}
    assert m["Recycled"] == "Plant Operator"
    assert m["New"] == "Plant Operator"
    assert m["Supervisor"] == "Supervisor"
    assert m["Transportation"] == "Transportation"
    assert m["Maintenance"] == "Plant Operator"
