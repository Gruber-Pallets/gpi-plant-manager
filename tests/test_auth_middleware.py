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


def test_tv_path_with_invalid_device_token_redirects(mini_app, monkeypatch):
    """Invalid (signature-bad or revoked) token: redirect to login."""
    from zira_dashboard import device_tokens as dt
    monkeypatch.setattr(dt, "lookup_active", lambda signed: None)
    from starlette.responses import PlainTextResponse
    @mini_app.get("/tv/bar")
    def _tv(): return PlainTextResponse("tv-ok")
    c = TestClient(mini_app)
    r = c.get("/tv/bar?device=garbage", follow_redirects=False)
    assert r.status_code == 302
    assert "/auth/login" in r.headers["location"]


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


def test_tv_path_without_device_token_redirects(mini_app):
    """A bare /tv/foo without ?device= must also redirect — the path
    being under /tv/ doesn't grant a free pass."""
    from starlette.responses import PlainTextResponse
    @mini_app.get("/tv/baz")
    def _tv(): return PlainTextResponse("tv-ok")
    c = TestClient(mini_app)
    r = c.get("/tv/baz", follow_redirects=False)
    assert r.status_code == 302
