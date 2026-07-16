"""Schema contract for persisted Saturday-work recruiting."""

import os
from dataclasses import FrozenInstanceError

import pytest

from zira_dashboard import db, saturday_recruiting_store as store
from zira_dashboard._schema import SCHEMA_DDL


def test_schema_defines_saturday_recruiting_tables_and_notification_key():
    for table in (
        "saturday_recruitments",
        "saturday_recruitment_openings",
        "saturday_work_responses",
    ):
        assert f"CREATE TABLE IF NOT EXISTS {table}" in SCHEMA_DDL
    assert "saturday_day DATE" in SCHEMA_DDL
    assert "employee_notifications_saturday_dedupe" in SCHEMA_DDL
    assert "(person_odoo_id, saturday_day, kind)" in SCHEMA_DDL


def test_lifecycle_rows_are_immutable_value_objects_without_database():
    position = store.AvailablePosition(4, "Repair", ("Repair",))
    with pytest.raises(FrozenInstanceError):
        position.wc_name = "Different"  # type: ignore[misc]


def test_lifecycle_rejects_empty_or_nonpositive_opening_counts_without_database():
    with pytest.raises(store.LifecycleConflict):
        store._normalize_counts({})
    with pytest.raises(store.LifecycleConflict):
        store._normalize_counts({4: 0})


@pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="needs Postgres")
def test_recruiting_tables_and_notification_key_exist():
    db.bootstrap_schema()
    rows = db.query(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_name = ANY(%s)",
        (["saturday_recruitments", "saturday_recruitment_openings", "saturday_work_responses"],),
    )
    assert {row["table_name"] for row in rows} == {
        "saturday_recruitments",
        "saturday_recruitment_openings",
        "saturday_work_responses",
    }
