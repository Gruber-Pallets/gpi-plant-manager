from zira_dashboard._schema import SCHEMA_DDL


def test_auto_lunch_setting_event_schema_is_append_only_and_attributed():
    assert "CREATE TABLE IF NOT EXISTS auto_lunch_setting_events" in SCHEMA_DDL
    for column in (
        "before_enabled", "before_observe_only", "before_flex_after_hours",
        "before_flex_minutes", "after_enabled", "after_observe_only",
        "after_flex_after_hours", "after_flex_minutes", "actor_upn",
        "actor_name", "source", "changed_at",
    ):
        assert column in SCHEMA_DDL
    assert "CHECK (source IN ('settings','external','baseline'))" in SCHEMA_DDL
    assert "auto_lunch_setting_events_changed_at_idx" in SCHEMA_DDL


def test_auto_lunch_department_continuity_columns_are_idempotent():
    assert (
        "ALTER TABLE timeclock_punches_log\n"
        "  ADD COLUMN IF NOT EXISTS odoo_department_id BIGINT;"
    ) in SCHEMA_DDL
    assert (
        "ALTER TABLE auto_lunch_runs\n"
        "  ADD COLUMN IF NOT EXISTS odoo_department_id BIGINT;"
    ) in SCHEMA_DDL
    assert (
        "ALTER TABLE auto_lunch_runs\n"
        "  ADD COLUMN IF NOT EXISTS odoo_department_name TEXT;"
    ) in SCHEMA_DDL
