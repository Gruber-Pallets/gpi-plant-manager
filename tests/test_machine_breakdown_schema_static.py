"""Static migration contracts that must run even without Postgres."""

from zira_dashboard._schema import SCHEMA_DDL


def test_breakdown_attribution_duplicates_are_reconciled_before_unique_indexes():
    dedupe = SCHEMA_DDL.index("breakdown attribution duplicate reconciliation")
    first_unique = SCHEMA_DDL.index(
        "CREATE UNIQUE INDEX IF NOT EXISTS wc_time_attributions_breakdown_odoo_open_uniq"
    )

    assert dedupe < first_unique
    block = SCHEMA_DDL[dedupe:first_unique]
    assert "DELETE FROM wc_time_attributions" in block
    assert "employee_odoo_id" in block
    assert "start_utc" in block
    assert "LEAST" in block
    assert "RAISE EXCEPTION" in block
    assert "ambiguous breakdown attribution duplicates" in block
