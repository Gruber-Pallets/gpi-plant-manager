"""The saturday_schedule singleton table is created by bootstrap_schema()."""
import os
import pytest
from zira_dashboard import db

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"), reason="needs Postgres"
)


def test_saturday_schedule_table_has_expected_columns():
    db.bootstrap_schema()
    rows = db.query(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_name = 'saturday_schedule'"
    )
    cols = {r["column_name"] for r in rows}
    assert {"id", "shift_start", "shift_end", "breaks", "updated_at"} <= cols
