from zira_dashboard._schema import SCHEMA_DDL


def test_schema_defines_employee_notifications_table():
    assert "CREATE TABLE IF NOT EXISTS employee_notifications" in SCHEMA_DDL
    for col in (
        "person_odoo_id", "kind", "time_off_request_id", "odoo_leave_id",
        "title", "body", "leave_date_from", "leave_date_to",
        "created_at", "acknowledged_at",
    ):
        assert col in SCHEMA_DDL, f"missing column {col}"


def test_schema_has_employee_notifications_indexes():
    # Hard dedupe backstop: one notification per (request, kind). Uniqueness
    # is the whole point of this index, so assert it explicitly.
    assert "CREATE UNIQUE INDEX IF NOT EXISTS employee_notifications_dedupe" in SCHEMA_DDL
    assert "(time_off_request_id, kind)" in SCHEMA_DDL
    # Fast unacknowledged lookup at sign-in.
    assert "employee_notifications_unack" in SCHEMA_DDL
    assert "WHERE acknowledged_at IS NULL" in SCHEMA_DDL


def test_schema_adds_anniversary_pto_audit_fields():
    for column in (
        "anniversary_date DATE",
        "balance_amount NUMERIC(8,2)",
        "balance_unit TEXT",
        "presented_at TIMESTAMPTZ",
    ):
        assert column in SCHEMA_DDL
    assert "employee_notifications_anniversary_pto_dedupe" in SCHEMA_DDL
    assert "(person_odoo_id, anniversary_date, kind)" in SCHEMA_DDL
    assert "anniversary_date IS NOT NULL" in SCHEMA_DDL
