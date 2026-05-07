import os
import pytest

requires_db = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set; this test needs Postgres.",
)


@requires_db
def test_load_roster_skips_excluded_people():
    """staffing.load_roster() returns only NOT excluded rows."""
    from zira_dashboard import db, staffing

    db.execute(
        "INSERT INTO people (odoo_id, name, active, excluded) "
        "VALUES (%s, %s, %s, %s) "
        "ON CONFLICT (odoo_id) DO UPDATE SET excluded = EXCLUDED.excluded, "
        "  name = EXCLUDED.name, active = EXCLUDED.active",
        (999991, "EXCLUDED Test Person", True, True),
    )
    db.execute(
        "INSERT INTO people (odoo_id, name, active, excluded) "
        "VALUES (%s, %s, %s, %s) "
        "ON CONFLICT (odoo_id) DO UPDATE SET excluded = EXCLUDED.excluded, "
        "  name = EXCLUDED.name, active = EXCLUDED.active",
        (999992, "VISIBLE Test Person", True, False),
    )
    staffing._invalidate_roster_cache()

    roster = staffing.load_roster()
    names = {p.name for p in roster}
    assert "VISIBLE Test Person" in names
    assert "EXCLUDED Test Person" not in names

    db.execute("DELETE FROM people WHERE odoo_id IN (%s, %s)", (999991, 999992))
    staffing._invalidate_roster_cache()


@requires_db
def test_load_roster_includes_inactive_but_not_excluded():
    """Inactive people are still returned (sorted to bottom). Excluded
    are dropped regardless of active status."""
    from zira_dashboard import db, staffing

    db.execute(
        "INSERT INTO people (odoo_id, name, active, excluded) "
        "VALUES (%s, %s, %s, %s) "
        "ON CONFLICT (odoo_id) DO UPDATE SET excluded = EXCLUDED.excluded, "
        "  name = EXCLUDED.name, active = EXCLUDED.active",
        (999993, "INACTIVE Test", False, False),
    )
    db.execute(
        "INSERT INTO people (odoo_id, name, active, excluded) "
        "VALUES (%s, %s, %s, %s) "
        "ON CONFLICT (odoo_id) DO UPDATE SET excluded = EXCLUDED.excluded, "
        "  name = EXCLUDED.name, active = EXCLUDED.active",
        (999994, "INACTIVE+EXCLUDED Test", False, True),
    )
    staffing._invalidate_roster_cache()

    roster = staffing.load_roster()
    names = {p.name for p in roster}
    assert "INACTIVE Test" in names
    assert "INACTIVE+EXCLUDED Test" not in names

    db.execute("DELETE FROM people WHERE odoo_id IN (%s, %s)", (999993, 999994))
    staffing._invalidate_roster_cache()
