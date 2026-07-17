import pytest
from fastapi import FastAPI
from fastapi.responses import PlainTextResponse
from fastapi.testclient import TestClient

from zira_dashboard import auth


@pytest.fixture(autouse=True)
def _enforce_auth_for_this_module(monkeypatch):
    """tests/conftest.py sets AUTH_DISABLED=1 so the rest of the suite
    doesn't have to mint sessions. This module *is* the auth-gate test,
    so unset it for every test here unless a specific test re-sets it."""
    monkeypatch.delenv("AUTH_DISABLED", raising=False)


@pytest.fixture
def fixed_secret(monkeypatch):
    monkeypatch.setenv("SESSION_SECRET", "test-secret-32-bytes-of-random-data!!")


@pytest.fixture
def mini_app(fixed_secret):
    """Tiny FastAPI app with just enough routes to exercise the middleware."""
    app = FastAPI()
    app.add_middleware(auth.RequireAuthMiddleware)

    @app.get("/recycling")
    def _r(): return PlainTextResponse("ok")

    @app.get("/healthz")
    def _h(): return PlainTextResponse("ok")

    @app.get("/static/foo.css")
    def _s(): return PlainTextResponse("ok")

    @app.get("/auth/login")
    def _l(): return PlainTextResponse("login page")

    @app.get("/robots.txt")
    def _ro(): return PlainTextResponse("ok")

    @app.get("/favicon.ico")
    def _fav(): return PlainTextResponse("ok")

    @app.get("/tv/dismantler-1")
    def _tv_slug(): return PlainTextResponse("tv-slug-ok")

    @app.get("/tv/d/dismantler-1")
    def _tv_legacy(): return PlainTextResponse("tv-legacy-ok")

    return app


def test_unauthed_redirects_to_login(mini_app):
    c = TestClient(mini_app)
    r = c.get("/recycling", follow_redirects=False)
    assert r.status_code == 302
    assert "/auth/login" in r.headers["location"]
    assert "next=%2Frecycling" in r.headers["location"]


def test_unauthed_root_redirects_to_bare_login(mini_app):
    """Root path shouldn't append `?next=/` — that's noise; the default is /."""
    c = TestClient(mini_app)
    r = c.get("/", follow_redirects=False)
    # 404 if no root route — but the middleware fires first and redirects.
    assert r.status_code in (302, 404)
    if r.status_code == 302:
        assert r.headers["location"] == "/auth/login"


def test_tv_slug_path_bypasses_auth_without_device_setup(mini_app):
    c = TestClient(mini_app)
    r = c.get("/tv/dismantler-1", follow_redirects=False)
    assert r.status_code == 200
    assert r.text == "tv-slug-ok"


def test_legacy_tv_path_bypasses_auth_without_device_setup(mini_app):
    c = TestClient(mini_app)
    r = c.get("/tv/d/dismantler-1", follow_redirects=False)
    assert r.status_code == 200
    assert r.text == "tv-legacy-ok"


def test_authed_with_valid_cookie_passes_through(mini_app, fixed_secret):
    token = auth.mint_session(sub="x", upn="dale@gruberpallets.com", name="Dale")
    c = TestClient(mini_app, cookies={auth.SESSION_COOKIE_NAME: token})
    r = c.get("/recycling")
    assert r.status_code == 200
    assert r.text == "ok"


def test_authed_with_invalid_cookie_redirects(mini_app, fixed_secret):
    c = TestClient(mini_app, cookies={auth.SESSION_COOKIE_NAME: "not-a-jwt"})
    r = c.get("/recycling", follow_redirects=False)
    assert r.status_code == 302
    assert "/auth/login" in r.headers["location"]


def test_bypass_list(mini_app):
    c = TestClient(mini_app)
    assert c.get("/healthz").status_code == 200
    assert c.get("/robots.txt").status_code == 200
    assert c.get("/favicon.ico").status_code == 200
    assert c.get("/static/foo.css").status_code == 200
    assert c.get("/auth/login").status_code == 200


def test_object_api_path_bypasses_session_redirect(mini_app):
    from starlette.responses import JSONResponse

    @mini_app.get("/api/v1/object/ping")
    def _api():
        return JSONResponse({"ok": True})

    c = TestClient(mini_app)
    r = c.get("/api/v1/object/ping", follow_redirects=False)
    assert r.status_code == 200
    assert r.json() == {"ok": True}


def test_auth_disabled_env_var_bypasses_everything(mini_app, monkeypatch):
    monkeypatch.setenv("AUTH_DISABLED", "1")
    c = TestClient(mini_app)
    assert c.get("/recycling").status_code == 200


def test_auth_disabled_logs_on_first_request(mini_app, monkeypatch, caplog):
    """Verify the AUTH_DISABLED warning re-fires inside dispatch (not just
    at boot) so accidental prod bypass is detectable from request logs."""
    import logging
    monkeypatch.setenv("AUTH_DISABLED", "1")
    # Reset the class counter so this test is deterministic regardless of
    # what other tests in this file ran first.
    auth.RequireAuthMiddleware._auth_disabled_request_count = 0
    c = TestClient(mini_app)
    with caplog.at_level(logging.ERROR, logger="zira_dashboard.auth"):
        r = c.get("/recycling")
    assert r.status_code == 200
    assert any("AUTH_DISABLED is set" in rec.message for rec in caplog.records)


def test_sliding_refresh_reissues_cookie_when_near_expiry(mini_app, fixed_secret, monkeypatch):
    """When the session is within SESSION_REFRESH_AT of expiry, a successful
    request should set a fresh Set-Cookie header re-issuing the session."""
    from datetime import timedelta
    monkeypatch.setattr(auth, "SESSION_TTL", timedelta(seconds=120))
    monkeypatch.setattr(auth, "SESSION_REFRESH_AT", timedelta(seconds=180))  # always-refresh
    token = auth.mint_session(sub="x", upn="dale@gruberpallets.com", name="Dale")
    c = TestClient(mini_app, cookies={auth.SESSION_COOKIE_NAME: token})
    r = c.get("/recycling")
    assert r.status_code == 200
    assert auth.SESSION_COOKIE_NAME in r.headers.get("set-cookie", "")


def test_no_sliding_refresh_when_fresh(mini_app, fixed_secret, monkeypatch):
    """Sessions that aren't near expiry shouldn't get a Set-Cookie header
    on every request (avoids churn)."""
    from datetime import timedelta
    monkeypatch.setattr(auth, "SESSION_TTL", timedelta(days=7))
    monkeypatch.setattr(auth, "SESSION_REFRESH_AT", timedelta(days=6))  # never-refresh on fresh
    token = auth.mint_session(sub="x", upn="dale@gruberpallets.com", name="Dale")
    c = TestClient(mini_app, cookies={auth.SESSION_COOKIE_NAME: token})
    r = c.get("/recycling")
    assert r.status_code == 200
    assert auth.SESSION_COOKIE_NAME not in r.headers.get("set-cookie", "")


def test_tv_path_with_valid_device_token_passes(mini_app, monkeypatch):
    """Valid device token on /tv/* path: pass through, no cookie set."""
    from zira_dashboard import device_tokens as dt
    monkeypatch.setattr(dt, "lookup_active",
        lambda signed: {"id": 1, "name": "Bay 3"} if signed == "fake.signed" else None,
    )
    from starlette.responses import PlainTextResponse
    @mini_app.get("/tv/foo")
    def _tv(): return PlainTextResponse("tv-ok")
    c = TestClient(mini_app)
    r = c.get("/tv/foo?device=fake.signed")
    assert r.status_code == 200
    assert r.text == "tv-ok"


def test_tv_path_with_invalid_device_token_still_bypasses_auth(mini_app, monkeypatch):
    """TV routes are anonymous even when a stale device token is present."""
    from zira_dashboard import device_tokens as dt
    monkeypatch.setattr(dt, "lookup_active", lambda signed: None)
    from starlette.responses import PlainTextResponse
    @mini_app.get("/tv/bar")
    def _tv(): return PlainTextResponse("tv-ok")
    c = TestClient(mini_app)
    r = c.get("/tv/bar?device=garbage", follow_redirects=False)
    assert r.status_code == 200
    assert r.text == "tv-ok"


def test_non_tv_path_with_device_token_still_redirects(mini_app, monkeypatch):
    """Token on a non-/tv/* path must NOT work — token is scoped strictly
    to TV paths."""
    from zira_dashboard import device_tokens as dt
    monkeypatch.setattr(dt, "lookup_active",
        lambda signed: {"id": 1, "name": "Bay 3"} if signed == "fake.signed" else None,
    )
    c = TestClient(mini_app)
    # /recycling is NOT under /tv/, so the token must NOT work.
    r = c.get("/recycling?device=fake.signed", follow_redirects=False)
    assert r.status_code == 302
    assert "/auth/login" in r.headers["location"]


def test_tv_path_without_device_token_bypasses_auth(mini_app):
    """A bare /tv/foo is anonymous without a device token."""
    from starlette.responses import PlainTextResponse
    @mini_app.get("/tv/baz")
    def _tv(): return PlainTextResponse("tv-ok")
    c = TestClient(mini_app)
    r = c.get("/tv/baz", follow_redirects=False)
    assert r.status_code == 200
    assert r.text == "tv-ok"


def test_session_sets_request_state(mini_app, fixed_secret):
    """Cookie-authed requests get user_upn + user_name on request.state."""
    token = auth.mint_session(sub="x", upn="dale@gruberpallets.com", name="Dale")

    from starlette.requests import Request
    from starlette.responses import JSONResponse
    @mini_app.get("/whoami")
    async def _w(request: Request):
        return JSONResponse({
            "upn": getattr(request.state, "user_upn", None),
            "name": getattr(request.state, "user_name", None),
        })

    c = TestClient(mini_app, cookies={auth.SESSION_COOKIE_NAME: token})
    r = c.get("/whoami")
    assert r.status_code == 200
    assert r.json() == {"upn": "dale@gruberpallets.com", "name": "Dale"}


def test_tv_bypass_does_not_set_request_state_from_device_token(mini_app, monkeypatch):
    """Anonymous TV routes bypass device-token parsing and request state."""
    from zira_dashboard import device_tokens as dt
    monkeypatch.setattr(dt, "lookup_active",
        lambda signed: {"id": 1, "name": "Bay 3 TV"} if signed == "fake.signed" else None,
    )

    from starlette.requests import Request
    from starlette.responses import JSONResponse
    @mini_app.get("/tv/whoami")
    async def _w(request: Request):
        return JSONResponse({
            "upn": getattr(request.state, "user_upn", None),
            "name": getattr(request.state, "user_name", None),
        })

    c = TestClient(mini_app)
    r = c.get("/tv/whoami?device=fake.signed")
    assert r.status_code == 200
    assert r.json() == {"upn": None, "name": None}


def test_tv_path_with_ip_allowlist_passes(mini_app, monkeypatch):
    """Shop-floor IP in TV_ALLOWED_IPS bypasses auth on /tv/* paths
    without needing a device token."""
    monkeypatch.setenv("TV_ALLOWED_IPS", "127.0.0.1")
    from starlette.responses import PlainTextResponse
    @mini_app.get("/tv/floor")
    def _tv(): return PlainTextResponse("tv-ok")
    # TestClient defaults client.host to "testclient" (not an IP), so
    # override it to a real IP that matches the env var.
    c = TestClient(mini_app, client=("127.0.0.1", 12345))
    r = c.get("/tv/floor")
    assert r.status_code == 200
    assert r.text == "tv-ok"


def test_tv_path_with_ip_allowlist_cidr_passes(mini_app, monkeypatch):
    """CIDR ranges in TV_ALLOWED_IPS match correctly."""
    monkeypatch.setenv("TV_ALLOWED_IPS", "10.0.0.0/8")
    from starlette.responses import PlainTextResponse
    @mini_app.get("/tv/cidr")
    def _tv(): return PlainTextResponse("tv-ok")
    c = TestClient(mini_app, client=("10.5.5.5", 12345))
    r = c.get("/tv/cidr")
    assert r.status_code == 200
    assert r.text == "tv-ok"


def test_tv_path_with_ip_allowlist_miss_bypasses_auth(mini_app, monkeypatch):
    """TV routes are anonymous even from IPs outside the allowlist."""
    monkeypatch.setenv("TV_ALLOWED_IPS", "10.20.30.40")
    from starlette.responses import PlainTextResponse
    @mini_app.get("/tv/miss")
    def _tv(): return PlainTextResponse("tv-ok")
    # client.host = "8.8.8.8" — not in the allowlist
    c = TestClient(mini_app, client=("8.8.8.8", 12345))
    r = c.get("/tv/miss", follow_redirects=False)
    assert r.status_code == 200
    assert r.text == "tv-ok"


def test_ip_allowlist_does_not_grant_non_tv_paths(mini_app, monkeypatch):
    """Allowlisted IP must NOT bypass auth on /recycling or other
    non-/tv/* paths. The shop floor's IP is a TV-only convenience,
    not a blanket auth bypass."""
    monkeypatch.setenv("TV_ALLOWED_IPS", "127.0.0.1")
    c = TestClient(mini_app, client=("127.0.0.1", 12345))
    r = c.get("/recycling", follow_redirects=False)
    assert r.status_code == 302
    assert "/auth/login" in r.headers["location"]


def test_tv_path_bypasses_auth_when_ip_allowlist_is_unset(mini_app, monkeypatch):
    """TV routes are anonymous when no TV_ALLOWED_IPS value is configured."""
    monkeypatch.delenv("TV_ALLOWED_IPS", raising=False)
    from starlette.responses import PlainTextResponse
    @mini_app.get("/tv/none")
    def _tv(): return PlainTextResponse("tv-ok")
    c = TestClient(mini_app, client=("127.0.0.1", 12345))
    r = c.get("/tv/none", follow_redirects=False)
    assert r.status_code == 200
    assert r.text == "tv-ok"


def test_ip_allowlist_malformed_entry_falls_back(mini_app, monkeypatch):
    """A typo in the env var (e.g. 'not-an-ip') doesn't break the
    middleware — it just doesn't match anything, and the request falls
    through to the device-token check."""
    monkeypatch.setenv("TV_ALLOWED_IPS", "not-an-ip, 127.0.0.1")
    from starlette.responses import PlainTextResponse
    @mini_app.get("/tv/typo")
    def _tv(): return PlainTextResponse("tv-ok")
    c = TestClient(mini_app, client=("127.0.0.1", 12345))
    r = c.get("/tv/typo")
    # Should still pass because 127.0.0.1 is the valid match
    assert r.status_code == 200
    assert r.text == "tv-ok"
