from zira_dashboard._schema import SCHEMA_DDL


def test_auto_salaried_runs_schema():
    assert "CREATE TABLE IF NOT EXISTS auto_salaried_runs" in SCHEMA_DDL
    for column in (
        "morning_in_punch_id", "lunch_out_punch_id", "lunch_in_punch_id",
        "day_out_punch_id", "skipped", "skip_reason", "lunch_dept_id",
        "lunch_dept_name", "dept_patch_state", "reverted", "flagged",
    ):
        assert column in SCHEMA_DDL
    assert "CHECK (dept_patch_state IN ('none','pending','done','failed'))" in SCHEMA_DDL


def test_auto_salaried_flags_schema():
    assert "CREATE TABLE IF NOT EXISTS auto_salaried_flags" in SCHEMA_DDL
    assert "UNIQUE (person_odoo_id, day, reason)" in SCHEMA_DDL
