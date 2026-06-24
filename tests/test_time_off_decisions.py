from zira_dashboard._schema import SCHEMA_DDL


def test_schema_defines_time_off_decisions_table():
    assert "CREATE TABLE IF NOT EXISTS time_off_decisions" in SCHEMA_DDL
    for col in (
        "request_id", "odoo_leave_id", "person_odoo_id", "person_name",
        "leave_type", "date_from", "date_to", "action", "result_state",
        "reason", "actor_upn", "actor_name", "source", "decided_at",
    ):
        assert col in SCHEMA_DDL, f"missing column {col}"
    assert "action IN ('approve','deny')" in SCHEMA_DDL
    assert "time_off_decisions_decided_at_idx" in SCHEMA_DDL
