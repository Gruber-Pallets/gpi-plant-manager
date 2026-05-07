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


def test_toggle_endpoint_400_when_odoo_id_missing():
    from fastapi.testclient import TestClient
    from zira_dashboard.app import app
    client = TestClient(app)
    r = client.post(
        "/api/settings/roster-filter/toggle",
        json={"excluded": True},
    )
    assert r.status_code == 400


def test_toggle_endpoint_400_when_odoo_id_not_int():
    from fastapi.testclient import TestClient
    from zira_dashboard.app import app
    client = TestClient(app)
    r = client.post(
        "/api/settings/roster-filter/toggle",
        json={"odoo_id": "not-an-int", "excluded": True},
    )
    assert r.status_code == 400


def test_toggle_endpoint_400_when_excluded_not_bool():
    from fastapi.testclient import TestClient
    from zira_dashboard.app import app
    client = TestClient(app)
    r = client.post(
        "/api/settings/roster-filter/toggle",
        json={"odoo_id": 123, "excluded": "yes"},
    )
    assert r.status_code == 400


def test_toggle_endpoint_writes_excluded_flag(monkeypatch):
    """Mock db.execute and assert it gets called with (excluded, odoo_id).
    Verifies the SQL shape without needing DATABASE_URL."""
    from unittest.mock import MagicMock
    from fastapi.testclient import TestClient
    from zira_dashboard.app import app
    from zira_dashboard import db, staffing

    spy = MagicMock()
    monkeypatch.setattr(db, "execute", spy)
    monkeypatch.setattr(staffing, "_invalidate_roster_cache", MagicMock())

    client = TestClient(app)
    r = client.post(
        "/api/settings/roster-filter/toggle",
        json={"odoo_id": 1234, "excluded": True},
    )
    assert r.status_code == 200
    assert r.json() == {"ok": True}
    spy.assert_called_once()
    args = spy.call_args.args
    assert "UPDATE people SET excluded" in args[0]
    assert args[1] == (True, 1234)
