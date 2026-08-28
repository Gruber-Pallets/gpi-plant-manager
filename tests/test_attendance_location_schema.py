"""Static contract tests for the durable attendance-location schema."""

from zira_dashboard._schema import SCHEMA_DDL


def test_attendance_location_schema_is_idempotent_and_versioned():
    ddl = SCHEMA_DDL
    assert "ADD COLUMN IF NOT EXISTS requires_work_center" in ddl
    assert "ADD COLUMN IF NOT EXISTS requires_work_center_explicit" in ddl
    assert "CREATE TABLE IF NOT EXISTS odoo_attendance_mirror" in ddl
    assert "odoo_attendance_id BIGINT PRIMARY KEY" in ddl
    assert "CREATE TABLE IF NOT EXISTS odoo_attendance_sync_state" in ddl
    assert "CREATE TABLE IF NOT EXISTS attendance_recalc_queue" in ddl
    assert "CREATE TABLE IF NOT EXISTS attendance_strict_days" in ddl
    assert "CREATE TABLE IF NOT EXISTS attendance_correction_jobs" in ddl
    assert "CREATE TABLE IF NOT EXISTS attendance_correction_job_events" in ddl
    assert "CREATE TABLE IF NOT EXISTS attendance_department_repairs" in ddl
