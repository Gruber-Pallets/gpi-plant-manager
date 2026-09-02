import os
from datetime import UTC, datetime

import pytest

from zira_dashboard import db
from zira_dashboard.forklift_identity_store import (
    MappingConflict,
    audit_rows,
    list_mappings,
    mapping_ids,
    remove_mapping,
    save_mapping,
)


DB_ONLY = pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="needs DATABASE_URL")


def test_schema_declares_one_to_one_mapping_and_append_only_audit():
    from zira_dashboard._schema import SCHEMA_DDL

    assert "CREATE TABLE IF NOT EXISTS forklift_driver_identity_map" in SCHEMA_DDL
    assert "employee_odoo_id INTEGER NOT NULL REFERENCES people(odoo_id)" in SCHEMA_DDL
    assert "UNIQUE (employee_odoo_id)" in SCHEMA_DDL
    assert "CREATE TABLE IF NOT EXISTS forklift_driver_identity_audit" in SCHEMA_DDL
    assert "before_employee_odoo_id INTEGER REFERENCES" not in SCHEMA_DDL
    assert "after_employee_odoo_id INTEGER REFERENCES" not in SCHEMA_DDL


@pytest.mark.parametrize("external_driver_id", [None, "", "   "])
def test_save_mapping_rejects_blank_external_driver_id_without_database(external_driver_id):
    with pytest.raises(ValueError, match="external driver ID is required"):
        save_mapping(
            external_driver_id,
            "Sam",
            700,
            expected_version=None,
            actor_upn="manager@example.com",
            actor_name="Manager",
        )


@pytest.mark.parametrize("employee_odoo_id", [True, False, 0, -1, "not-an-id"])
def test_save_mapping_rejects_non_positive_employee_id_without_database(employee_odoo_id):
    with pytest.raises(ValueError, match="employee Odoo ID must be a positive integer"):
        save_mapping(
            "driver-7",
            "Sam",
            employee_odoo_id,
            expected_version=None,
            actor_upn="manager@example.com",
            actor_name="Manager",
        )


def test_save_mapping_rejects_blank_actor_without_database():
    with pytest.raises(ValueError, match="actor UPN is required"):
        save_mapping(
            "driver-7",
            "Sam",
            700,
            expected_version=None,
            actor_upn="  ",
            actor_name="Manager",
        )


@pytest.mark.parametrize("expected_version", [True, False, 0, -1, "not-a-version"])
def test_remove_mapping_rejects_invalid_version_without_database(expected_version):
    with pytest.raises(ValueError, match="mapping version must be a positive integer"):
        remove_mapping(
            "driver-7",
            expected_version=expected_version,
            actor_upn="manager@example.com",
            actor_name="Manager",
        )


def test_audit_rows_rejects_blank_external_driver_id_without_database():
    with pytest.raises(ValueError, match="external driver ID is required"):
        audit_rows("  ")


class _SaveCursor:
    def __init__(self):
        self.statements = []
        self._result = None

    def execute(self, sql, params):
        normalized_sql = " ".join(sql.split())
        self.statements.append((normalized_sql, params))
        if normalized_sql.startswith("SELECT external_driver_id, source_name"):
            self._result = None
        elif normalized_sql.startswith("SELECT odoo_id, name FROM people"):
            self._result = {"odoo_id": 700, "name": "Identity Test One"}
        elif normalized_sql.startswith("SELECT external_driver_id FROM"):
            self._result = None
        elif normalized_sql.startswith("INSERT INTO forklift_driver_identity_map"):
            now = datetime(2026, 9, 2, 14, 30, tzinfo=UTC)
            self._result = {
                "external_driver_id": params[0],
                "source_name": params[1],
                "employee_odoo_id": params[2],
                "version": 1,
                "created_at": now,
                "created_by_upn": params[3],
                "updated_at": now,
                "updated_by_upn": params[4],
            }
        else:
            self._result = None

    def fetchone(self):
        return self._result


class _CursorContext:
    def __init__(self, cursor):
        self.cursor = cursor

    def __enter__(self):
        return self.cursor

    def __exit__(self, exc_type, exc, traceback):
        return False


class _RemoveCursor:
    def __init__(self):
        self.statements = []
        self._result = None

    def execute(self, sql, params):
        normalized_sql = " ".join(sql.split())
        self.statements.append((normalized_sql, params))
        if normalized_sql.startswith("SELECT * FROM forklift_driver_identity_map"):
            self._result = {
                "external_driver_id": "driver-7",
                "source_name": "Sam Rivera",
                "employee_odoo_id": 700,
                "version": 2,
            }
        else:
            self._result = None

    def fetchone(self):
        return self._result


def test_save_mapping_normalizes_driver_source_and_actor_values_without_database(
    monkeypatch,
):
    cursor = _SaveCursor()
    monkeypatch.setattr(db, "cursor", lambda: _CursorContext(cursor))

    saved = save_mapping(
        "  driver-7  ",
        "  Sam Rivera  ",
        700,
        expected_version=None,
        actor_upn="  manager@example.com  ",
        actor_name="  Floor Manager  ",
    )

    assert saved.external_driver_id == "driver-7"
    assert saved.source_name == "Sam Rivera"
    assert saved.created_by_upn == "manager@example.com"
    mapping_insert = next(
        params
        for sql, params in cursor.statements
        if sql.startswith("INSERT INTO forklift_driver_identity_map")
    )
    audit_insert = next(
        params
        for sql, params in cursor.statements
        if sql.startswith("INSERT INTO forklift_driver_identity_audit")
    )
    assert mapping_insert == (
        "driver-7",
        "Sam Rivera",
        700,
        "manager@example.com",
        "manager@example.com",
    )
    assert audit_insert == (
        "driver-7",
        "create",
        None,
        700,
        None,
        "Sam Rivera",
        "manager@example.com",
        "Floor Manager",
    )


def test_remove_mapping_normalizes_driver_and_actor_values_without_database(
    monkeypatch,
):
    cursor = _RemoveCursor()
    monkeypatch.setattr(db, "cursor", lambda: _CursorContext(cursor))

    remove_mapping(
        "  driver-7  ",
        expected_version=2,
        actor_upn="  manager@example.com  ",
        actor_name="  Floor Manager  ",
    )

    delete_params = next(
        params
        for sql, params in cursor.statements
        if sql.startswith("DELETE FROM forklift_driver_identity_map")
    )
    audit_insert = next(
        params
        for sql, params in cursor.statements
        if sql.startswith("INSERT INTO forklift_driver_identity_audit")
    )
    assert delete_params == ("driver-7",)
    assert audit_insert == (
        "driver-7",
        "remove",
        700,
        None,
        "Sam Rivera",
        None,
        "manager@example.com",
        "Floor Manager",
    )


def test_list_mappings_keeps_a_mapping_when_its_employee_row_is_missing(monkeypatch):
    now = datetime(2026, 9, 2, 14, 30, tzinfo=UTC)
    captured = {}

    def query(sql, params=None):
        captured.update(sql=" ".join(sql.split()), params=params)
        return [{
            "external_driver_id": "driver-missing",
            "source_name": "Former Driver",
            "employee_odoo_id": 799,
            "employee_name": None,
            "employee_active": None,
            "employee_excluded": None,
            "version": 3,
            "created_at": now,
            "created_by_upn": "manager@example.com",
            "updated_at": now,
            "updated_by_upn": "manager@example.com",
        }]

    monkeypatch.setattr(db, "query", query)

    mappings = list_mappings()

    assert "LEFT JOIN people" in captured["sql"]
    assert mappings[0].external_driver_id == "driver-missing"
    assert mappings[0].employee_name is None
    assert mappings[0].employee_active is None
    assert mappings[0].employee_excluded is None


@pytest.fixture
def identity_people():
    driver_ids = ("driver-7", "driver-8", "driver-9")
    employee_ids = (700, 701, 702, 703)
    db.bootstrap_schema()
    db.execute(
        "DELETE FROM forklift_driver_identity_map WHERE external_driver_id = ANY(%s)",
        (list(driver_ids),),
    )
    db.execute(
        "DELETE FROM forklift_driver_identity_audit WHERE external_driver_id = ANY(%s)",
        (list(driver_ids),),
    )
    db.execute("DELETE FROM people WHERE odoo_id = ANY(%s)", (list(employee_ids),))
    db.execute(
        "INSERT INTO people (odoo_id, name, active, excluded) VALUES "
        "(700, 'Identity Test One', TRUE, FALSE), "
        "(701, 'Identity Test Two', TRUE, FALSE), "
        "(702, 'Identity Test Inactive', FALSE, FALSE), "
        "(703, 'Identity Test Excluded', TRUE, TRUE)"
    )
    yield
    db.execute(
        "DELETE FROM forklift_driver_identity_map WHERE external_driver_id = ANY(%s)",
        (list(driver_ids),),
    )
    db.execute(
        "DELETE FROM forklift_driver_identity_audit WHERE external_driver_id = ANY(%s)",
        (list(driver_ids),),
    )
    db.execute("DELETE FROM people WHERE odoo_id = ANY(%s)", (list(employee_ids),))


@DB_ONLY
def test_save_change_remove_round_trip_records_each_audit(identity_people):
    first = save_mapping(
        " driver-7 ",
        " Sam ",
        700,
        expected_version=None,
        actor_upn=" manager@example.com ",
        actor_name="Manager",
    )

    assert first.external_driver_id == "driver-7"
    assert first.source_name == "Sam"
    assert first.employee_odoo_id == 700
    assert first.employee_name == "Identity Test One"
    assert first.version == 1
    assert first.created_by_upn == "manager@example.com"
    assert first.updated_by_upn == "manager@example.com"
    assert mapping_ids()["driver-7"] == 700

    changed = save_mapping(
        "driver-7",
        "Samuel",
        701,
        expected_version=first.version,
        actor_upn="manager@example.com",
        actor_name="Manager",
    )

    assert changed.version == 2
    assert changed.employee_odoo_id == 701
    remove_mapping(
        "driver-7",
        expected_version=changed.version,
        actor_upn="manager@example.com",
        actor_name="Manager",
    )
    assert not any(row.external_driver_id == "driver-7" for row in list_mappings())
    assert "driver-7" not in mapping_ids()
    rows = audit_rows("driver-7")
    assert [row["action"] for row in rows] == ["create", "change", "remove"]
    assert rows[0]["before_employee_odoo_id"] is None
    assert rows[0]["after_employee_odoo_id"] == 700
    assert rows[1]["before_employee_odoo_id"] == 700
    assert rows[1]["after_employee_odoo_id"] == 701
    assert rows[2]["before_employee_odoo_id"] == 701
    assert rows[2]["after_employee_odoo_id"] is None


@DB_ONLY
@pytest.mark.parametrize("employee_odoo_id", [702, 703])
def test_save_mapping_rejects_inactive_or_excluded_employee(identity_people, employee_odoo_id):
    with pytest.raises(MappingConflict, match="Choose an active employee"):
        save_mapping(
            "driver-7",
            "Sam",
            employee_odoo_id,
            expected_version=None,
            actor_upn="manager@example.com",
            actor_name="Manager",
        )
    assert audit_rows("driver-7") == ()


@DB_ONLY
def test_two_driver_ids_cannot_claim_one_employee(identity_people):
    save_mapping(
        "driver-7",
        "Sam",
        700,
        expected_version=None,
        actor_upn="manager@example.com",
        actor_name="Manager",
    )

    with pytest.raises(
        MappingConflict,
        match="That employee is already mapped to another forklift identity",
    ):
        save_mapping(
            "driver-8",
            "Samuel",
            700,
            expected_version=None,
            actor_upn="manager@example.com",
            actor_name="Manager",
        )
    assert audit_rows("driver-8") == ()


@DB_ONLY
def test_save_mapping_rejects_stale_expected_version(identity_people):
    saved = save_mapping(
        "driver-7",
        "Sam",
        700,
        expected_version=None,
        actor_upn="manager@example.com",
        actor_name="Manager",
    )

    with pytest.raises(MappingConflict, match="changed. Reload and try again"):
        save_mapping(
            "driver-7",
            "Samuel",
            701,
            expected_version=saved.version + 1,
            actor_upn="manager@example.com",
            actor_name="Manager",
        )
    assert mapping_ids()["driver-7"] == 700
    assert [row["action"] for row in audit_rows("driver-7")] == ["create"]


@DB_ONLY
def test_save_mapping_rejects_expected_version_for_missing_mapping(identity_people):
    with pytest.raises(MappingConflict, match="changed. Reload and try again"):
        save_mapping(
            "driver-9",
            "Sam",
            700,
            expected_version=1,
            actor_upn="manager@example.com",
            actor_name="Manager",
        )
    assert audit_rows("driver-9") == ()


@DB_ONLY
def test_remove_mapping_rejects_stale_version_without_removing(identity_people):
    saved = save_mapping(
        "driver-7",
        "Sam",
        700,
        expected_version=None,
        actor_upn="manager@example.com",
        actor_name="Manager",
    )

    with pytest.raises(MappingConflict, match="changed. Reload and try again"):
        remove_mapping(
            "driver-7",
            expected_version=saved.version + 1,
            actor_upn="manager@example.com",
            actor_name="Manager",
        )
    assert mapping_ids()["driver-7"] == 700
    assert [row["action"] for row in audit_rows("driver-7")] == ["create"]


@DB_ONLY
def test_remove_mapping_rejects_missing_mapping(identity_people):
    with pytest.raises(MappingConflict, match="changed. Reload and try again"):
        remove_mapping(
            "driver-9",
            expected_version=1,
            actor_upn="manager@example.com",
            actor_name="Manager",
        )
    assert audit_rows("driver-9") == ()
