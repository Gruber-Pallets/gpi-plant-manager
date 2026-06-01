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


def test_split_roster_rows_separates_active_and_inactive():
    """Active rows (active truthy) and inactive rows are split, order preserved."""
    from zira_dashboard.routes.settings import _split_roster_rows

    rows = [
        {"odoo_id": 1, "name": "Ana", "excluded": False, "active": True},
        {"odoo_id": 2, "name": "Zed", "excluded": False, "active": False},
        {"odoo_id": 3, "name": "Cara", "excluded": True, "active": True},
    ]
    active, inactive = _split_roster_rows(rows)
    assert [r["name"] for r in active] == ["Ana", "Cara"]
    assert [r["name"] for r in inactive] == ["Zed"]


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


def test_roster_filter_lists_queries_active_and_splits(monkeypatch):
    """_roster_filter_lists() selects the `active` column and returns the
    rows split into (active, inactive)."""
    from zira_dashboard import db
    from zira_dashboard.routes import settings as settings_route

    captured = {}

    def fake_query(sql, *args):
        captured["sql"] = sql
        return [
            {"odoo_id": 1, "name": "Ana", "excluded": False, "active": True},
            {"odoo_id": 2, "name": "Zed", "excluded": False, "active": False},
        ]

    monkeypatch.setattr(db, "query", fake_query)
    active, inactive = settings_route._roster_filter_lists()

    assert "active" in captured["sql"].lower()
    assert "where odoo_id is not null" in captured["sql"].lower()
    assert [r["name"] for r in active] == ["Ana"]
    assert [r["name"] for r in inactive] == ["Zed"]


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
