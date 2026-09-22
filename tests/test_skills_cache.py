import os

import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="skills-matrix cache test needs a live DATABASE_URL",
)


@pytest.fixture
def client():
    from zira_dashboard import db, _http_cache
    _http_cache.invalidate_stable_cache()
    db.init_pool()
    db.bootstrap_schema()
    from zira_dashboard.app import app
    try:
        yield TestClient(app)
    finally:
        _http_cache.invalidate_stable_cache()


def test_skills_matrix_serves_from_cache(client, monkeypatch):
    # First GET renders + caches.
    r1 = client.get("/staffing/skills")
    assert r1.status_code == 200

    # Poison the roster load: a genuine second render would call it and
    # blow up. A cache hit skips it entirely.
    from zira_dashboard import staffing

    def _poison():
        raise AssertionError("load_roster called — skills matrix was not cached")

    monkeypatch.setattr(staffing, "load_roster", _poison)
    r2 = client.get("/staffing/skills")
    assert r2.status_code == 200
    assert r2.content == r1.content


def test_skills_save_invalidates_cache(client):
    from zira_dashboard import _http_cache, permissions

    key = ("staffing_skills",)
    roles = ("admin", "manager", "system")
    token = permissions.current_role.set("system")
    try:
        for role in roles:
            permissions.current_role.set(role)
            response = client.get("/staffing/skills")
            assert response.status_code == 200
            assert _http_cache.get_cached_response(key, includes_today=True, stable=True) is not None
        permissions.current_role.set("system")
        # Do not follow the redirect: it would repopulate one role's cache.
        response = client.post("/staffing/skills", data={}, follow_redirects=False)
        assert response.status_code == 303
        for role in roles:
            permissions.current_role.set(role)
            assert _http_cache.get_cached_response(key, includes_today=True, stable=True) is None
    finally:
        permissions.current_role.reset(token)
