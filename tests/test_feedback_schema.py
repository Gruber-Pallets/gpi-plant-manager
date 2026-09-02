from zira_dashboard._schema import SCHEMA_DDL


def test_schema_defines_feedback_table():
    assert "CREATE TABLE IF NOT EXISTS feedback" in SCHEMA_DDL
    for col in (
        "id",
        "created_at",
        "submitter",
        "page_url",
        "category",
        "message",
        "task_type",
        "odoo_task_id",
    ):
        assert col in SCHEMA_DDL, f"missing column {col}"


def test_feedback_schema_keeps_exact_submitter_employee_id():
    assert "submitter_employee_odoo_id BIGINT" in SCHEMA_DDL
    assert "ADD COLUMN IF NOT EXISTS submitter_employee_odoo_id BIGINT" in SCHEMA_DDL
    assert "feedback_submitter_employee_odoo_id_check" in SCHEMA_DDL
    assert "submitter_employee_odoo_id IS NULL OR submitter_employee_odoo_id > 0" in (
        " ".join(SCHEMA_DDL.split())
    )


def test_people_schema_keeps_normalized_work_email_for_feedback_identity():
    assert "ALTER TABLE people ADD COLUMN IF NOT EXISTS work_email TEXT" in SCHEMA_DDL
    assert "CREATE INDEX IF NOT EXISTS people_active_work_email_idx" in SCHEMA_DDL
    assert "ON people (work_email) WHERE active = TRUE AND work_email IS NOT NULL" in (
        " ".join(SCHEMA_DDL.split())
    )


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


def test_feedback_schema_has_exact_append_only_pre_attempt_release_audit():
    ddl = " ".join(SCHEMA_DDL.split())
    assert (
        "CREATE TABLE IF NOT EXISTS feedback_odoo_pre_attempt_releases ( "
        "id BIGSERIAL PRIMARY KEY, "
        "feedback_id BIGINT NOT NULL REFERENCES feedback(id), "
        "projection_version BIGINT NOT NULL CHECK (projection_version > 0), "
        "quarantine_reason TEXT NOT NULL CHECK ( quarantine_reason = "
        "'target_identity_or_contract_mismatch' ), "
        "quarantined_at TIMESTAMPTZ NOT NULL, "
        "reviewer TEXT NOT NULL CHECK (btrim(reviewer) <> ''), "
        "created_at TIMESTAMPTZ NOT NULL DEFAULT now(), "
        "UNIQUE (feedback_id, projection_version, quarantined_at) );"
    ) in ddl


def test_schema_has_owner_task_delivery_outbox():
    ddl = " ".join(SCHEMA_DDL.split())
    assert "CREATE TABLE IF NOT EXISTS feedback_task_delivery" in ddl
    assert "feedback_id BIGINT PRIMARY KEY REFERENCES feedback(id)" in ddl
    assert "state TEXT NOT NULL DEFAULT 'pending'" in ddl
    assert "state IN ('pending', 'in_flight', 'attention', 'delivered', 'blocked')" in ddl
    assert "claim_token UUID" in ddl
    assert "odoo_task_id BIGINT" in ddl
    assert "before_attachment_id BIGINT" in ddl
    assert "desired_version BIGINT NOT NULL DEFAULT 1" in ddl
    assert "last_synced_version BIGINT NOT NULL DEFAULT 0" in ddl
    assert "desired_status TEXT NOT NULL DEFAULT 'requested'" in ddl
    assert "last_synced_version <= desired_version" in ddl
    assert "ADD COLUMN IF NOT EXISTS desired_version BIGINT NOT NULL DEFAULT 1" in ddl
    assert "desired_contract_version BIGINT NOT NULL DEFAULT 2" in ddl
    assert "last_synced_contract_version BIGINT NOT NULL DEFAULT 0" in ddl
    assert (
        "ADD COLUMN IF NOT EXISTS desired_contract_version BIGINT NOT NULL DEFAULT 1"
        in ddl
    )
    assert "ALTER COLUMN desired_contract_version SET DEFAULT 2" in ddl
    assert "last_synced_contract_version <= desired_contract_version" in ddl



def test_feedback_remote_ids_use_signed_64_bit_columns_and_idempotent_migration():
    ddl = " ".join(SCHEMA_DDL.split())
    migration = ddl.split("DO $feedback_remote_ids_bigint$", 1)[1].split(
        "$feedback_remote_ids_bigint$;", 1
    )[0]
    assert "odoo_improvement_id BIGINT" in ddl
    assert "remote_id BIGINT" in ddl
    assert (
        "ALTER TABLE feedback_odoo_sync ALTER COLUMN odoo_improvement_id "
        "TYPE BIGINT USING odoo_improvement_id::BIGINT"
    ) in ddl
    assert (
        "ALTER TABLE feedback_odoo_attempts ALTER COLUMN remote_id "
        "TYPE BIGINT USING remote_id::BIGINT"
    ) in ddl
    assert "attrelid = 'feedback_odoo_sync'::regclass" in ddl
    assert "attrelid = 'feedback_odoo_attempts'::regclass" in ddl
    assert ddl.count("atttypid = 'integer'::regtype") >= 2
    assert migration.count("ALTER TABLE") == 2
    assert "attempt_count" not in migration
    assert "before_byte_length" not in migration


def test_feedback_local_origin_requires_non_null_status():
    ddl = " ".join(SCHEMA_DDL.split())
    assert "lifecycle_origin IS DISTINCT FROM 'local' OR ( status IS NOT NULL" in ddl


def test_feedback_active_attempt_belongs_to_same_feedback():
    ddl = " ".join(SCHEMA_DDL.split())
    assert "UNIQUE (feedback_id, attempt_id)" in ddl
    assert (
        "FOREIGN KEY (feedback_id, active_attempt_id) REFERENCES "
        "feedback_odoo_attempts(feedback_id, attempt_id)" in ddl
    )


def test_feedback_constraint_guards_are_table_scoped():
    ddl = " ".join(SCHEMA_DDL.split())
    assert ddl.count("AND conrelid = 'feedback'::regclass") == 4
    assert "AND conrelid = 'feedback_odoo_sync'::regclass" in ddl
