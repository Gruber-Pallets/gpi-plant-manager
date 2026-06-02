"""Sync orchestration tests against live Postgres.

Skips when DATABASE_URL is unset. The sync module now writes to the
people / skills / person_skills tables directly; tests stub the four
fetch_* helpers on odoo_client and assert against the resulting rows.
"""

import json
import os
from datetime import datetime, timezone

import pytest

from zira_dashboard import odoo_sync


pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="needs Postgres",
)


@pytest.fixture(autouse=True)
def _clean_sync_state():
    from zira_dashboard import db
    # Wipe sync state + the test odoo_id rows so each test starts fresh.
    db.execute("DELETE FROM app_settings WHERE key = 'odoo_last_sync'")
    db.execute("DELETE FROM person_skills WHERE person_id IN (SELECT id FROM people WHERE odoo_id BETWEEN 99000 AND 99999)")
    db.execute("DELETE FROM people WHERE odoo_id BETWEEN 99000 AND 99999")
    db.execute("DELETE FROM skills WHERE name IN ('TestRepair', 'TestDismantler')")
    yield
    db.execute("DELETE FROM person_skills WHERE person_id IN (SELECT id FROM people WHERE odoo_id BETWEEN 99000 AND 99999)")
    db.execute("DELETE FROM people WHERE odoo_id BETWEEN 99000 AND 99999")
    db.execute("DELETE FROM skills WHERE name IN ('TestRepair', 'TestDismantler')")


def _stub_client(monkeypatch, employees, skills_for, columns_meta, buckets):
    monkeypatch.setattr(odoo_sync.odoo_client, "fetch_employees", lambda: employees)
    monkeypatch.setattr(odoo_sync.odoo_client, "fetch_skills_for", lambda ids: skills_for)
    monkeypatch.setattr(odoo_sync.odoo_client, "fetch_skill_columns_with_types", lambda: columns_meta)
    monkeypatch.setattr(odoo_sync.odoo_client, "fetch_skill_level_buckets", lambda: buckets)
    monkeypatch.setattr(odoo_sync.odoo_client, "fetch_work_schedules", lambda: [])


def test_sync_skips_when_within_ttl(monkeypatch):
    from zira_dashboard import db
    db.execute(
        "INSERT INTO app_settings (key, value, updated_at) "
        "VALUES ('odoo_last_sync', %s::jsonb, now())",
        (json.dumps(datetime.now(timezone.utc).isoformat()),),
    )
    monkeypatch.setattr(odoo_sync.odoo_client, "fetch_employees",
                        lambda: (_ for _ in ()).throw(AssertionError("should not call")))
    result = odoo_sync.sync(force=False)
    assert result.refreshed is False
    assert result.ok is True


def test_sync_force_refreshes_even_within_ttl(monkeypatch):
    _stub_client(
        monkeypatch,
        employees=[{"id": 99001, "name": "TestAlice", "active": True, "work_email": False}],
        skills_for={99001: [{"skill_id": 10, "skill_name": "TestRepair", "level_id": 103}]},
        columns_meta=[
            {"name": "TestRepair", "type": "Production Skills"},
            {"name": "TestDismantler", "type": "Production Skills"},
        ],
        buckets={103: 3},
    )
    result = odoo_sync.sync(force=True)
    assert result.refreshed is True
    assert result.employee_count == 1
    assert result.skill_column_count == 2
    from zira_dashboard import db
    rows = db.query(
        "SELECT pe.name, ps.level, sk.name AS skill_name "
        "FROM people pe JOIN person_skills ps ON ps.person_id = pe.id "
        "JOIN skills sk ON sk.id = ps.skill_id WHERE pe.odoo_id = 99001"
    )
    assert rows == [{"name": "TestAlice", "level": 3, "skill_name": "TestRepair"}]


def test_sync_preserves_local_reserve_flag(monkeypatch):
    from zira_dashboard import db
    # Pre-create the person with reserve=True locally.
    db.execute(
        "INSERT INTO people (odoo_id, name, active, reserve) VALUES (99002, 'TestBob', TRUE, TRUE)"
    )
    _stub_client(
        monkeypatch,
        employees=[{"id": 99002, "name": "TestBob", "active": True, "work_email": False}],
        skills_for={99002: []},
        columns_meta=[{"name": "TestRepair", "type": "Production Skills"}],
        buckets={},
    )
    odoo_sync.sync(force=True)
    rows = db.query("SELECT reserve FROM people WHERE odoo_id = 99002")
    assert rows[0]["reserve"] is True


def test_sync_returns_error_on_odoo_failure(monkeypatch):
    monkeypatch.setattr(odoo_sync.odoo_client, "fetch_employees",
                        lambda: (_ for _ in ()).throw(odoo_sync.odoo_client.OdooAuthError("nope")))
    result = odoo_sync.sync(force=True)
    assert result.ok is False
    assert "nope" in (result.error or "")
    assert result.refreshed is False


def test_sync_upsert_does_not_clear_excluded_flag():
    """The Odoo sync's INSERT … ON CONFLICT (odoo_id) DO UPDATE clause
    only names (name, active, last_pulled_at) — local-only columns
    like reserve and excluded must survive across syncs.

    Validate by simulating a sync's UPSERT against a row that's
    already marked excluded, and checking the flag is preserved.
    """
    from datetime import datetime, timezone
    from zira_dashboard import db

    # Seed a row with excluded=TRUE.
    db.execute(
        "INSERT INTO people (odoo_id, name, active, excluded, last_pulled_at) "
        "VALUES (%s, %s, %s, %s, %s) "
        "ON CONFLICT (odoo_id) DO UPDATE SET name = EXCLUDED.name, "
        "  active = EXCLUDED.active, excluded = EXCLUDED.excluded, "
        "  last_pulled_at = EXCLUDED.last_pulled_at",
        (999995, "EXCLUDED Sync Test", True, True, datetime.now(timezone.utc)),
    )

    # Simulate the sync's UPSERT (matches odoo_sync.sync()'s SQL exactly).
    db.execute(
        "INSERT INTO people (odoo_id, name, active, last_pulled_at) "
        "VALUES (%s, %s, %s, %s) "
        "ON CONFLICT (odoo_id) DO UPDATE SET name = EXCLUDED.name, "
        "active = EXCLUDED.active, last_pulled_at = EXCLUDED.last_pulled_at",
        (999995, "EXCLUDED Sync Test (renamed)", True, datetime.now(timezone.utc)),
    )

    rows = db.query(
        "SELECT excluded FROM people WHERE odoo_id = %s", (999995,)
    )
    assert rows[0]["excluded"] is True

    # Cleanup.
    db.execute("DELETE FROM people WHERE odoo_id = %s", (999995,))


def test_sync_deactivates_employees_missing_from_odoo_response(monkeypatch):
    """When Odoo archives an employee, fetch_employees() (which filters
    on active=True) stops returning them. The sync must then flip the
    local people.active to FALSE so they drop out of the scheduler."""
    from zira_dashboard import db

    # Seed two people: one will be in the next sync, one won't (archived).
    cols = [
        {"name": "TestRepair", "type": "skill"},
        {"name": "TestDismantler", "type": "skill"},
    ]
    _stub_client(
        monkeypatch,
        employees=[
            {"id": 99100, "name": "Still Active", "active": True},
            {"id": 99101, "name": "About To Be Archived", "active": True},
        ],
        skills_for={},
        columns_meta=cols,
        buckets={},
    )
    odoo_sync.sync(force=True)
    rows = db.query("SELECT active FROM people WHERE odoo_id IN (99100, 99101) ORDER BY odoo_id")
    assert [r["active"] for r in rows] == [True, True]

    # Second sync: only 99100 comes back. 99101 should flip to inactive.
    _stub_client(
        monkeypatch,
        employees=[{"id": 99100, "name": "Still Active", "active": True}],
        skills_for={},
        columns_meta=cols,
        buckets={},
    )
    odoo_sync.sync(force=True)
    rows = db.query("SELECT odoo_id, active FROM people WHERE odoo_id IN (99100, 99101) ORDER BY odoo_id")
    by_id = {r["odoo_id"]: r["active"] for r in rows}
    assert by_id[99100] is True
    assert by_id[99101] is False, "archived-in-Odoo person must be deactivated locally"


def test_sync_deactivation_skips_when_no_employees_returned(monkeypatch):
    """Defensive: if Odoo returns an empty employee list (likely an
    upstream bug, not a real mass-archive), we must NOT wipe out every
    local active flag. The deactivation step is gated on at least one
    employee in the response."""
    from datetime import datetime, timezone
    from zira_dashboard import db

    db.execute(
        "INSERT INTO people (odoo_id, name, active, last_pulled_at) "
        "VALUES (%s, %s, TRUE, %s) "
        "ON CONFLICT (odoo_id) DO UPDATE SET active = EXCLUDED.active",
        (99200, "Should Stay Active", datetime.now(timezone.utc)),
    )
    cols = [{"name": "TestRepair", "type": "skill"}]
    _stub_client(
        monkeypatch,
        employees=[],
        skills_for={},
        columns_meta=cols,
        buckets={},
    )
    odoo_sync.sync(force=True)
    rows = db.query("SELECT active FROM people WHERE odoo_id = %s", (99200,))
    assert rows[0]["active"] is True, "empty Odoo response must not deactivate existing people"

    # Cleanup.
    db.execute("DELETE FROM people WHERE odoo_id = %s", (99200,))


def test_sync_inserts_certification_at_level_3_regardless_of_bucket(monkeypatch):
    """Odoo skill types with a single level bucket to 0 (see
    fetch_skill_level_buckets). Certifications must override that
    and insert at level=3 so cert_lookup finds them and staffing
    colors CDL drivers green."""
    from zira_dashboard import db
    db.execute("DELETE FROM skills WHERE name = 'TestDOTCert'")
    _stub_client(
        monkeypatch,
        employees=[{"id": 99010, "name": "TestCDLDriver", "active": True, "work_email": False}],
        skills_for={99010: [{"skill_id": 50, "skill_name": "TestDOTCert", "level_id": 500}]},
        columns_meta=[
            {"name": "TestDOTCert", "type": "Certifications"},
        ],
        buckets={500: 0},  # single-level cert type buckets to 0
    )
    result = odoo_sync.sync(force=True)
    assert result.refreshed is True
    rows = db.query(
        "SELECT pe.name, ps.level, sk.name AS skill_name, sk.skill_type "
        "FROM people pe JOIN person_skills ps ON ps.person_id = pe.id "
        "JOIN skills sk ON sk.id = ps.skill_id WHERE pe.odoo_id = 99010"
    )
    assert rows == [{
        "name": "TestCDLDriver", "level": 3,
        "skill_name": "TestDOTCert", "skill_type": "Certifications",
    }]
    db.execute("DELETE FROM person_skills WHERE person_id IN (SELECT id FROM people WHERE odoo_id = 99010)")
    db.execute("DELETE FROM people WHERE odoo_id = 99010")
    db.execute("DELETE FROM skills WHERE name = 'TestDOTCert'")


def test_sync_production_skill_still_skips_when_level_0(monkeypatch):
    """Sanity: the cert override must NOT change the existing behavior
    for non-cert skill types. A production skill with level<=0 still
    gets skipped."""
    from zira_dashboard import db
    db.execute("DELETE FROM skills WHERE name = 'TestProdSkillSkip'")
    _stub_client(
        monkeypatch,
        employees=[{"id": 99011, "name": "TestProdPerson", "active": True, "work_email": False}],
        skills_for={99011: [{"skill_id": 51, "skill_name": "TestProdSkillSkip", "level_id": 501}]},
        columns_meta=[
            {"name": "TestProdSkillSkip", "type": "Production Skills"},
        ],
        buckets={501: 0},
    )
    odoo_sync.sync(force=True)
    rows = db.query(
        "SELECT * FROM person_skills ps "
        "JOIN people pe ON pe.id = ps.person_id "
        "WHERE pe.odoo_id = 99011"
    )
    assert rows == []
    db.execute("DELETE FROM people WHERE odoo_id = 99011")
    db.execute("DELETE FROM skills WHERE name = 'TestProdSkillSkip'")
