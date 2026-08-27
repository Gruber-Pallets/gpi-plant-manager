from zira_dashboard._schema import SCHEMA_DDL


def test_schema_has_private_deduplicated_celebration_queue():
    assert "CREATE TABLE IF NOT EXISTS employee_celebrations" in SCHEMA_DDL
    assert "UNIQUE (person_odoo_id, kind, event_day)" in SCHEMA_DDL
    assert "employee_celebrations_unack" in SCHEMA_DDL
    assert "WHERE acknowledged_at IS NULL" in SCHEMA_DDL
