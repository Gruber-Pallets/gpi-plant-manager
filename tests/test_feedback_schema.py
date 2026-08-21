from zira_dashboard._schema import SCHEMA_DDL


def test_schema_defines_feedback_table():
    assert "CREATE TABLE IF NOT EXISTS feedback" in SCHEMA_DDL
    for col in (
        "id", "created_at", "submitter", "page_url", "category", "message",
        "task_type", "odoo_task_id",
    ):
        assert col in SCHEMA_DDL, f"missing column {col}"


def test_schema_has_idempotent_alters_for_new_feedback_columns():
    assert "ADD COLUMN IF NOT EXISTS task_type" in SCHEMA_DDL
    assert "ADD COLUMN IF NOT EXISTS odoo_task_id" in SCHEMA_DDL


def test_feedback_schema_has_local_lifecycle_and_version():
    ddl = " ".join(SCHEMA_DDL.split())
    assert "ADD COLUMN IF NOT EXISTS status TEXT" in ddl
    assert "ADD COLUMN IF NOT EXISTS lifecycle_origin TEXT" in ddl
    assert "ADD COLUMN IF NOT EXISTS finished_at TIMESTAMPTZ" in ddl
    assert "ADD COLUMN IF NOT EXISTS finished_by TEXT" in ddl
    assert "ADD COLUMN IF NOT EXISTS resolution_note TEXT" in ddl
    assert "ADD COLUMN IF NOT EXISTS projection_version BIGINT NOT NULL DEFAULT 1" in ddl
    assert "feedback_local_terminal_fields_check" in ddl


def test_feedback_schema_has_durable_odoo_outbox_and_immutable_manifest():
    ddl = " ".join(SCHEMA_DDL.split())
    assert "CREATE TABLE IF NOT EXISTS feedback_images" in ddl
    assert "PRIMARY KEY (feedback_id, role)" in ddl
    assert "CREATE TABLE IF NOT EXISTS feedback_odoo_sync" in ddl
    assert "state TEXT NOT NULL DEFAULT 'idle'" in ddl
    assert "claim_token UUID" in ddl
    assert "CREATE TABLE IF NOT EXISTS feedback_odoo_attempts" in ddl
    assert "manifest JSONB NOT NULL" in ddl
    assert "manifest_digest TEXT NOT NULL" in ddl
    assert "CREATE TRIGGER feedback_odoo_attempts_immutable_manifest" in ddl
    assert "CREATE TRIGGER feedback_odoo_attempts_reject_delete" in ddl
    assert "CREATE TRIGGER feedback_odoo_attempts_reject_truncate" in ddl
    assert "CREATE TABLE IF NOT EXISTS feedback_odoo_warnings" in ddl
    assert "CREATE TABLE IF NOT EXISTS feedback_odoo_operator_actions" in ddl
    assert "CREATE TABLE IF NOT EXISTS feedback_odoo_backfill_state" in ddl


def test_feedback_local_origin_requires_non_null_status():
    ddl = " ".join(SCHEMA_DDL.split())
    assert (
        "lifecycle_origin IS DISTINCT FROM 'local' OR ( status IS NOT NULL"
        in ddl
    )


def test_feedback_active_attempt_belongs_to_same_feedback():
    ddl = " ".join(SCHEMA_DDL.split())
    assert "UNIQUE (feedback_id, attempt_id)" in ddl
    assert (
        "FOREIGN KEY (feedback_id, active_attempt_id) REFERENCES "
        "feedback_odoo_attempts(feedback_id, attempt_id)"
        in ddl
    )


def test_feedback_constraint_guards_are_table_scoped():
    ddl = " ".join(SCHEMA_DDL.split())
    assert ddl.count("AND conrelid = 'feedback'::regclass") == 3
    assert "AND conrelid = 'feedback_odoo_sync'::regclass" in ddl
