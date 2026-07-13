"""Schema migrations for dynamic scheduling preference targets."""

from zira_dashboard._schema import SCHEMA_DDL


def test_rotation_preferences_accept_dynamic_target_keys():
    """Qualified standalone centers such as Chop/Notch must be persistable."""
    table_ddl = SCHEMA_DDL.split(
        "CREATE TABLE IF NOT EXISTS person_rotation_preferences", 1
    )[1].split("CREATE TABLE IF NOT EXISTS rotation_training_blocks", 1)[0]

    assert "rotation_group TEXT NOT NULL CHECK" not in table_ddl
    normalized_ddl = " ".join(SCHEMA_DDL.split())
    assert (
        "ALTER TABLE person_rotation_preferences DROP CONSTRAINT IF EXISTS "
        "person_rotation_preferences_rotation_group_check;"
    ) in normalized_ddl
