import os

import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="skills-matrix cache test needs a live DATABASE_URL",
)


@pytest.fixture
def client():
    from zira_dashboard import db
    db.init_pool()
    db.bootstrap_schema()
    from zira_dashboard.app import app
    return TestClient(app)


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
    from zira_dashboard import _http_cache

    client.get("/staffing/skills")  # populate cache
    # The skills matrix lives in the long-TTL stable bucket (600s), not the
    # 60s today bucket — writes invalidate it directly.
    assert _http_cache._RESPONSE_CACHE_STABLE.peek(("staffing_skills",)) is not None
    # A roster save must clear it so edits show immediately. Don't follow
    # the 303 redirect — the redirected GET would just repopulate the cache.
    client.post("/staffing/skills", data={}, follow_redirects=False)
    assert _http_cache._RESPONSE_CACHE_STABLE.peek(("staffing_skills",)) is None
