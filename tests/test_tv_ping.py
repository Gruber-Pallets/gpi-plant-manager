"""GET /tv/ping — the tiny probe target for tv-refresh.js.

The TVs probe before reloading; the probe must be (a) nearly free — a
bodyless 204 instead of the full dashboard document — and (b) behind the
auth middleware, so an expired/missing session still produces the
redirect-to-login signal (opaqueredirect => resp.ok === false) that
tv-refresh.js treats as "don't reload".
"""

from pathlib import Path

from fastapi.testclient import TestClient

from zira_dashboard.app import app

STATIC_DIR = Path(__file__).resolve().parents[1] / "src" / "zira_dashboard" / "static"


def test_tv_ping_returns_204_no_body():
    # conftest sets AUTH_DISABLED=1, so this exercises the route itself.
    c = TestClient(app)
    r = c.get("/tv/ping")
    assert r.status_code == 204
    assert r.content == b""


def test_tv_ping_is_not_in_auth_bypass_list(monkeypatch):
    """With auth enforced and no session, /tv/ping must redirect to login —
    NOT return 204. tv-refresh.js relies on that redirect to detect a dead
    session and hold the current frame instead of reloading into a login
    page."""
    monkeypatch.delenv("AUTH_DISABLED", raising=False)
    c = TestClient(app)
    r = c.get("/tv/ping", follow_redirects=False)
    assert r.status_code == 302
    assert "/auth/login" in r.headers["location"]


def test_tv_ping_allows_ip_allowlisted_tvs(monkeypatch):
    """Shop-floor TVs auth via TV_ALLOWED_IPS on /tv/* paths; the probe
    lives under /tv/ precisely so it authenticates like the dashboards."""
    monkeypatch.delenv("AUTH_DISABLED", raising=False)
    monkeypatch.setenv("TV_ALLOWED_IPS", "127.0.0.1")
    c = TestClient(app, client=("127.0.0.1", 12345))
    r = c.get("/tv/ping")
    assert r.status_code == 204


def test_tv_ping_allows_device_token_tvs(monkeypatch):
    """Off-network TVs carry a ?device= token; tv-refresh.js copies it from
    the page URL onto the probe."""
    monkeypatch.delenv("AUTH_DISABLED", raising=False)
    from zira_dashboard import device_tokens as dt
    monkeypatch.setattr(
        dt, "lookup_active",
        lambda signed: {"id": 1, "name": "Bay 3"} if signed == "fake.signed" else None,
    )
    c = TestClient(app)
    assert c.get("/tv/ping?device=fake.signed").status_code == 204
    r = c.get("/tv/ping?device=garbage", follow_redirects=False)
    assert r.status_code == 302


def test_tv_refresh_js_probes_tv_ping():
    """Keep tv-refresh.js and the endpoint in sync: the script must probe
    /tv/ping (not re-download the whole page) and still use the manual-
    redirect login detection."""
    js = (STATIC_DIR / "tv-refresh.js").read_text(encoding="utf-8")
    assert "/tv/ping" in js
    assert 'redirect: "manual"' in js
    assert "fetch(PROBE_URL" in js
    # The old probe re-downloaded the entire page just to throw it away.
    assert "fetch(window.location.href" not in js
