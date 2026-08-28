"""Department-policy preservation at the Odoo sync insertion boundary."""

import os

import pytest

from zira_dashboard import db, odoo_sync


class _DepartmentCursor:
    def __init__(self):
        self.rows = {
            "Supervisor": {
                "requires_work_center": True,
                "requires_work_center_explicit": True,
            }
        }

    def execute(self, sql, params):
        assert "INSERT INTO departments (name, requires_work_center)" in sql
        assert "ON CONFLICT (name) DO NOTHING" in sql
        name, required = params
        self.rows.setdefault(
            name,
            {
                "requires_work_center": required,
                "requires_work_center_explicit": False,
            },
        )


def test_department_sync_defaults_fresh_exempt_rows_without_overwriting_admin_choice():
    cur = _DepartmentCursor()

    odoo_sync._upsert_departments(
        cur,
        ["Maintenance", "Supervisor", "Assembly"],
    )

    assert cur.rows == {
        "Maintenance": {
            "requires_work_center": False,
            "requires_work_center_explicit": False,
        },
        "Supervisor": {
            "requires_work_center": True,
            "requires_work_center_explicit": True,
        },
        "Assembly": {
            "requires_work_center": True,
            "requires_work_center_explicit": False,
        },
    }


@pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="needs Postgres")
def test_department_sync_store_preserves_explicit_choice_in_one_real_transaction():
    class RollBackTestRows(Exception):
        pass

    names = ["Maintenance", "Supervisor", "Attendance Policy Assembly Test"]
    try:
        with db.cursor() as cur:
            cur.execute("DELETE FROM departments WHERE name = ANY(%s)", (names,))
            cur.execute(
                "INSERT INTO departments "
                "(name, requires_work_center, requires_work_center_explicit) "
                "VALUES ('Supervisor', TRUE, TRUE)"
            )

            odoo_sync._upsert_departments(cur, names)

            cur.execute(
                "SELECT name, requires_work_center, requires_work_center_explicit "
                "FROM departments WHERE name = ANY(%s) ORDER BY name",
                (names,),
            )
            assert list(cur.fetchall()) == [
                {
                    "name": "Attendance Policy Assembly Test",
                    "requires_work_center": True,
                    "requires_work_center_explicit": False,
                },
                {
                    "name": "Maintenance",
                    "requires_work_center": False,
                    "requires_work_center_explicit": False,
                },
                {
                    "name": "Supervisor",
                    "requires_work_center": True,
                    "requires_work_center_explicit": True,
                },
            ]
            raise RollBackTestRows
    except RollBackTestRows:
        pass
