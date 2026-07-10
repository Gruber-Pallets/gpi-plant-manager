from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from zira_dashboard import odoo_client
from zira_dashboard.routes.skills import router


def _client():
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def test_skill_cell_update_writes_odoo_then_mirrors_local(monkeypatch):
    from zira_dashboard.routes import skills as skills_routes

    calls = []

    def fake_query(sql, params=None):
        calls.append(("query", sql, params))
        if "FROM people" in sql:
            return [{"id": 1, "odoo_id": 77, "name": "Maria Garcia"}]
        if "FROM skills" in sql:
            return [{"id": 2, "odoo_id": 88, "name": "Repair", "skill_type": "Production Skills"}]
        return []

    class FakeCursor:
        def execute(self, sql, params=None):
            calls.append(("execute", sql, params))

    class FakeCursorContext:
        def __enter__(self):
            return FakeCursor()

        def __exit__(self, exc_type, exc, tb):
            return False

    monkeypatch.setattr(skills_routes.db, "query", fake_query)
    monkeypatch.setattr(skills_routes.db, "cursor", lambda: FakeCursorContext())
    monkeypatch.setattr(
        odoo_client,
        "set_employee_skill_level",
        lambda employee_id, skill_id, level: calls.append(("odoo", employee_id, skill_id, level)),
    )
    monkeypatch.setattr(skills_routes.staffing, "_invalidate_roster_cache", lambda: calls.append(("roster_cache",)))
    monkeypatch.setattr(skills_routes._http_cache, "invalidate_today_cache", lambda: calls.append(("today_cache",)))
    monkeypatch.setattr(skills_routes._http_cache, "invalidate_stable_cache", lambda: calls.append(("stable_cache",)))

    response = _client().post(
        "/staffing/skills/cell",
        json={"person_odoo_id": 77, "skill_odoo_id": 88, "level": 3},
    )

    assert response.status_code == 200
    assert response.json() == {"ok": True, "level": 3, "label": "proficient"}
    assert ("odoo", 77, 88, 3) in calls
    upserts = [c for c in calls if c[0] == "execute" and "INSERT INTO person_skills" in c[1]]
    assert upserts
    assert calls.index(("odoo", 77, 88, 3)) < calls.index(upserts[0])
    assert ("roster_cache",) in calls
    assert ("today_cache",) in calls
    assert ("stable_cache",) in calls


def test_skill_cell_update_zero_deletes_local_after_odoo_success(monkeypatch):
    from zira_dashboard.routes import skills as skills_routes

    calls = []

    def fake_query(sql, params=None):
        if "FROM people" in sql:
            return [{"id": 1, "odoo_id": 77, "name": "Maria Garcia"}]
        if "FROM skills" in sql:
            return [{"id": 2, "odoo_id": 88, "name": "Repair", "skill_type": "Production Skills"}]
        return []

    class FakeCursor:
        def execute(self, sql, params=None):
            calls.append((sql, params))

    class FakeCursorContext:
        def __enter__(self):
            return FakeCursor()

        def __exit__(self, exc_type, exc, tb):
            return False

    monkeypatch.setattr(skills_routes.db, "query", fake_query)
    monkeypatch.setattr(skills_routes.db, "cursor", lambda: FakeCursorContext())
    monkeypatch.setattr(odoo_client, "set_employee_skill_level", lambda *args: None)
    monkeypatch.setattr(skills_routes.staffing, "_invalidate_roster_cache", lambda: None)
    monkeypatch.setattr(skills_routes._http_cache, "invalidate_today_cache", lambda: None)
    monkeypatch.setattr(skills_routes._http_cache, "invalidate_stable_cache", lambda: None)

    response = _client().post(
        "/staffing/skills/cell",
        json={"person_odoo_id": 77, "skill_odoo_id": 88, "level": 0},
    )

    assert response.status_code == 200
    assert any("DELETE FROM person_skills" in sql for sql, _params in calls)


def test_skill_cell_update_rejects_invalid_level():
    response = _client().post(
        "/staffing/skills/cell",
        json={"person_odoo_id": 77, "skill_odoo_id": 88, "level": 4},
    )

    assert response.status_code == 400
    assert response.json()["ok"] is False
    assert "level" in response.json()["error"]


@pytest.mark.parametrize(
    "payload",
    [
        [],
        {"person_odoo_id": True, "skill_odoo_id": 88, "level": 2},
        {"person_odoo_id": 77.5, "skill_odoo_id": 88, "level": 2},
        {"person_odoo_id": "77", "skill_odoo_id": 88, "level": 2},
        {"person_odoo_id": 77, "skill_odoo_id": 88, "level": False},
    ],
)
def test_skill_cell_update_rejects_malformed_payloads(payload):
    response = _client().post("/staffing/skills/cell", json=payload)

    assert response.status_code == 400
    assert response.json()["ok"] is False
    assert "required" in response.json()["error"]


def test_skill_cell_update_rejects_missing_person(monkeypatch):
    from zira_dashboard.routes import skills as skills_routes

    monkeypatch.setattr(skills_routes.db, "query", lambda sql, params=None: [])

    response = _client().post(
        "/staffing/skills/cell",
        json={"person_odoo_id": 77, "skill_odoo_id": 88, "level": 2},
    )

    assert response.status_code == 404
    assert response.json() == {"ok": False, "error": "Person not found. Refresh from Odoo and try again."}


def test_skill_cell_update_rejects_non_matrix_skill(monkeypatch):
    from zira_dashboard.routes import skills as skills_routes

    def fake_query(sql, params=None):
        if "FROM people" in sql:
            return [{"id": 1, "odoo_id": 77, "name": "Maria Garcia"}]
        if "FROM skills" in sql:
            return [{"id": 2, "odoo_id": 88, "name": "Spanish", "skill_type": "Languages"}]
        return []

    monkeypatch.setattr(skills_routes.db, "query", fake_query)

    response = _client().post(
        "/staffing/skills/cell",
        json={"person_odoo_id": 77, "skill_odoo_id": 88, "level": 2},
    )

    assert response.status_code == 400
    assert response.json() == {"ok": False, "error": "Skill is not editable in the People Matrix."}


def test_skill_cell_update_leaves_local_state_when_odoo_fails(monkeypatch):
    from zira_dashboard.routes import skills as skills_routes

    calls = []

    def fake_query(sql, params=None):
        if "FROM people" in sql:
            return [{"id": 1, "odoo_id": 77, "name": "Maria Garcia"}]
        if "FROM skills" in sql:
            return [{"id": 2, "odoo_id": 88, "name": "Repair", "skill_type": "Production Skills"}]
        return []

    def fail_odoo(*args):
        raise RuntimeError("odoo down")

    class FakeCursor:
        def execute(self, sql, params=None):
            calls.append((sql, params))

    class FakeCursorContext:
        def __enter__(self):
            return FakeCursor()

        def __exit__(self, exc_type, exc, tb):
            return False

    monkeypatch.setattr(skills_routes.db, "query", fake_query)
    monkeypatch.setattr(skills_routes.db, "cursor", lambda: FakeCursorContext())
    monkeypatch.setattr(odoo_client, "set_employee_skill_level", fail_odoo)

    response = _client().post(
        "/staffing/skills/cell",
        json={"person_odoo_id": 77, "skill_odoo_id": 88, "level": 3},
    )

    assert response.status_code == 502
    assert response.json() == {"ok": False, "error": "Odoo save failed: odoo down"}
    assert calls == []


def test_skill_cell_update_reports_odoo_saved_when_local_mirror_fails(monkeypatch):
    from zira_dashboard.routes import skills as skills_routes

    def fake_query(sql, params=None):
        if "FROM people" in sql:
            return [{"id": 1, "odoo_id": 77, "name": "Maria Garcia"}]
        if "FROM skills" in sql:
            return [{"id": 2, "odoo_id": 88, "name": "Repair", "skill_type": "Production Skills"}]
        return []

    # Odoo succeeds, but the shared writer's local mirror fails: the cursor
    # blows up when it tries to upsert person_skills. This exercises the real
    # refactored path (a non-SkillSyncError after a successful Odoo write ->
    # 202), not a mocked-out mirror.
    class FailingCursor:
        def execute(self, sql, params=None):
            raise RuntimeError("db unavailable")

    class FailingCursorContext:
        def __enter__(self):
            return FailingCursor()

        def __exit__(self, exc_type, exc, tb):
            return False

    monkeypatch.setattr(skills_routes.db, "query", fake_query)
    monkeypatch.setattr(skills_routes.db, "cursor", lambda: FailingCursorContext())
    monkeypatch.setattr(odoo_client, "set_employee_skill_level", lambda *args: None)
    monkeypatch.setattr(skills_routes.staffing, "_invalidate_roster_cache", lambda: None)
    monkeypatch.setattr(skills_routes._http_cache, "invalidate_today_cache", lambda: None)
    monkeypatch.setattr(skills_routes._http_cache, "invalidate_stable_cache", lambda: None)

    response = _client().post(
        "/staffing/skills/cell",
        json={"person_odoo_id": 77, "skill_odoo_id": 88, "level": 3},
    )

    assert response.status_code == 202
    assert response.json()["ok"] is True
    assert response.json()["level"] == 3
    assert "Saved in Odoo" in response.json()["warning"]


def test_skill_cell_update_delegates_to_shared_skill_writer(monkeypatch):
    """The matrix endpoint resolves the local person/skill ids, then hands the
    write to the one shared promotion path with LOCAL ids and the level."""
    from zira_dashboard.routes import skills as skills_routes

    def fake_query(sql, params=None):
        if "FROM people" in sql:
            return [{"id": 1, "odoo_id": 77, "name": "Maria Garcia"}]
        if "FROM skills" in sql:
            return [{"id": 2, "odoo_id": 88, "name": "Repair", "skill_type": "Production Skills"}]
        return []

    calls: list[tuple] = []
    monkeypatch.setattr(skills_routes.db, "query", fake_query)
    monkeypatch.setattr(
        skills_routes.skill_levels,
        "set_person_skill_level",
        lambda person_id, skill_id, level: calls.append((person_id, skill_id, level)),
    )

    response = _client().post(
        "/staffing/skills/cell",
        json={"person_odoo_id": 77, "skill_odoo_id": 88, "level": 3},
    )

    assert response.status_code == 200
    assert response.json() == {"ok": True, "level": 3, "label": "proficient"}
    assert calls == [(1, 2, 3)]


def test_skill_cell_update_resolution_db_failure_returns_502_not_202(monkeypatch):
    """A DB blip while the writer resolves Odoo ids happens *before* Odoo is
    called, so it must surface as 502 (Odoo not saved), never the 202
    "Saved in Odoo" branch. No local write should occur."""
    from zira_dashboard.routes import skills as skills_routes

    def fake_query(sql, params=None):
        # The writer's Odoo-id resolution -- simulate a transient DB failure.
        if sql.strip().startswith("SELECT odoo_id FROM"):
            raise RuntimeError("db blip during resolution")
        # The endpoint's own lookups succeed so we get past the 404 checks.
        if "FROM people" in sql:
            return [{"id": 1, "odoo_id": 77, "name": "Maria Garcia"}]
        if "FROM skills" in sql:
            return [{"id": 2, "odoo_id": 88, "name": "Repair", "skill_type": "Production Skills"}]
        return []

    cursor_used: list[tuple] = []

    class FakeCursor:
        def execute(self, sql, params=None):
            cursor_used.append((sql, params))

    class FakeCursorContext:
        def __enter__(self):
            return FakeCursor()

        def __exit__(self, exc_type, exc, tb):
            return False

    odoo_calls: list[tuple] = []
    monkeypatch.setattr(skills_routes.db, "query", fake_query)
    monkeypatch.setattr(skills_routes.db, "cursor", lambda: FakeCursorContext())
    monkeypatch.setattr(
        odoo_client, "set_employee_skill_level", lambda *args: odoo_calls.append(args)
    )

    response = _client().post(
        "/staffing/skills/cell",
        json={"person_odoo_id": 77, "skill_odoo_id": 88, "level": 3},
    )

    assert response.status_code == 502
    assert response.json()["ok"] is False
    assert odoo_calls == []  # Odoo never reached
    assert cursor_used == []  # no local mirror write
