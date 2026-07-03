from fastapi.testclient import TestClient

from zira_dashboard import api_keys
from zira_dashboard.app import app


client = TestClient(app)


def test_execute_requires_bearer_key(monkeypatch):
    monkeypatch.delenv("AUTH_DISABLED", raising=False)
    r = client.post("/api/v1/object/execute", json={"model": "plant.person", "method": "search_read"})
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "auth_required"


def test_execute_rejects_invalid_key(monkeypatch):
    monkeypatch.delenv("AUTH_DISABLED", raising=False)
    monkeypatch.setattr(api_keys, "verify_key", lambda token: None)
    r = client.post(
        "/api/v1/object/execute",
        headers={"Authorization": "Bearer gpi_live_bad"},
        json={"model": "plant.person", "method": "search_read"},
    )
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "invalid_api_key"


def test_models_lists_registered_models(monkeypatch):
    monkeypatch.setattr(
        api_keys,
        "verify_key",
        lambda token: {"id": 1, "name": "Test", "scopes": ["admin:*"], "allowed_ips": []},
    )
    r = client.get("/api/v1/object/models", headers={"Authorization": "Bearer gpi_live_good"})
    assert r.status_code == 200
    assert any(model["model"] == "plant.person" for model in r.json()["models"])


def test_execute_search_read_dispatches_and_audits(monkeypatch):
    calls = []
    monkeypatch.setattr(
        api_keys,
        "verify_key",
        lambda token: {"id": 1, "name": "CRM", "scopes": ["admin:*"], "allowed_ips": []},
    )
    monkeypatch.setattr(
        "zira_dashboard.object_models.PersonModel.all_records",
        lambda self, ctx: [{"id": 1, "name": "Dale", "active": True}],
    )
    monkeypatch.setattr(
        "zira_dashboard.object_api.db.execute",
        lambda sql, params=None: calls.append((sql, params)),
    )
    r = client.post(
        "/api/v1/object/execute",
        headers={"Authorization": "Bearer gpi_live_good"},
        json={
            "model": "plant.person",
            "method": "search_read",
            "args": [[["active", "=", True]]],
            "kwargs": {"fields": ["id", "name"]},
            "context": {"actor": "Dale"},
        },
    )
    assert r.status_code == 200
    assert r.json()["result"] == [{"id": 1, "name": "Dale"}]
    assert calls and "api_audit_log" in calls[0][0]


def test_write_denied_without_scope(monkeypatch):
    monkeypatch.setattr(
        api_keys,
        "verify_key",
        lambda token: {"id": 1, "name": "Reader", "scopes": ["object:read"], "allowed_ips": []},
    )
    r = client.post(
        "/api/v1/object/execute",
        headers={"Authorization": "Bearer gpi_live_good"},
        json={"model": "plant.person", "method": "write", "args": [[1], {"active": False}]},
    )
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "access_denied"


def test_cookie_does_not_authenticate_object_api():
    cookie_client = TestClient(app, cookies={"gpi_session": "not-used"})
    r = cookie_client.get("/api/v1/object/models")
    assert r.status_code == 401


def test_ip_allowlist_rejects_unlisted_client(monkeypatch):
    monkeypatch.setattr(
        api_keys,
        "verify_key",
        lambda token: {
            "id": 1,
            "name": "Locked",
            "scopes": ["admin:*"],
            "allowed_ips": ["10.0.0.1"],
        },
    )
    r = client.get("/api/v1/object/models", headers={"Authorization": "Bearer gpi_live_good"})
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "ip_not_allowed"


def test_https_required_in_production(monkeypatch):
    monkeypatch.setenv("REQUIRE_API_HTTPS", "1")
    monkeypatch.setattr(
        api_keys,
        "verify_key",
        lambda token: {"id": 1, "name": "CRM", "scopes": ["admin:*"], "allowed_ips": []},
    )
    r = client.get(
        "/api/v1/object/models",
        headers={"Authorization": "Bearer gpi_live_good", "x-forwarded-proto": "http"},
    )
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "https_required"
