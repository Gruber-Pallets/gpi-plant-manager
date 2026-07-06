import time
from datetime import timedelta

import pytest

from zira_dashboard import auth


def test_session_round_trip(monkeypatch):
    monkeypatch.setattr(auth, "_session_secret", lambda: "test-secret-32-bytes-of-random-data!!")
    token = auth.mint_session(sub="oid-abc", upn="dale@gruberpallets.com", name="Dale")
    payload = auth.verify_session(token)
    assert payload is not None
    assert payload["sub"] == "oid-abc"
    assert payload["upn"] == "dale@gruberpallets.com"
    assert payload["name"] == "Dale"
    assert payload["exp"] > time.time()


def test_session_rejects_bad_signature(monkeypatch):
    monkeypatch.setattr(auth, "_session_secret", lambda: "test-secret-32-bytes-of-random-data!!")
    token = auth.mint_session(sub="oid-abc", upn="x@y.z", name="X")
    monkeypatch.setattr(auth, "_session_secret", lambda: "different-secret-aaaaaaaaaaaaaaaaa")
    assert auth.verify_session(token) is None


def test_session_rejects_expired(monkeypatch):
    monkeypatch.setattr(auth, "_session_secret", lambda: "test-secret-32-bytes-of-random-data!!")
    monkeypatch.setattr(auth, "SESSION_TTL", timedelta(seconds=-1))
    token = auth.mint_session(sub="oid-abc", upn="x@y.z", name="X")
    assert auth.verify_session(token) is None


def test_session_rejects_malformed(monkeypatch):
    monkeypatch.setattr(auth, "_session_secret", lambda: "test-secret-32-bytes-of-random-data!!")
    assert auth.verify_session(None) is None
    assert auth.verify_session("") is None
    assert auth.verify_session("not-a-jwt") is None


def test_session_rejects_when_secret_unset(monkeypatch):
    monkeypatch.delenv("SESSION_SECRET", raising=False)
    assert auth.verify_session("any-token-value-here") is None


def test_needs_refresh_when_close_to_expiry(monkeypatch):
    monkeypatch.setattr(auth, "_session_secret", lambda: "test-secret-32-bytes-of-random-data!!")
    monkeypatch.setattr(auth, "SESSION_TTL", timedelta(days=7))
    monkeypatch.setattr(auth, "SESSION_REFRESH_AT", timedelta(days=6))
    token = auth.mint_session(sub="x", upn="x@y.z", name="X")
    payload = auth.verify_session(token)
    assert auth.needs_refresh(payload) is False
    payload_near_expiry = {**payload, "exp": int(time.time()) + 60}
    assert auth.needs_refresh(payload_near_expiry) is True


def test_auth_disabled_reads_env(monkeypatch):
    monkeypatch.delenv("AUTH_DISABLED", raising=False)
    assert auth.auth_disabled() is False
    monkeypatch.setenv("AUTH_DISABLED", "1")
    assert auth.auth_disabled() is True
    monkeypatch.setenv("AUTH_DISABLED", "true")
    assert auth.auth_disabled() is True
    monkeypatch.setenv("AUTH_DISABLED", "yes")
    assert auth.auth_disabled() is True
    monkeypatch.setenv("AUTH_DISABLED", "0")
    assert auth.auth_disabled() is False
    monkeypatch.setenv("AUTH_DISABLED", "")
    assert auth.auth_disabled() is False


def test_super_admin_upns_default_to_dale(monkeypatch):
    monkeypatch.delenv("SUPER_ADMIN_UPNS", raising=False)
    assert auth.is_super_admin_upn("dale@gruberpallets.com") is True
    assert auth.is_super_admin_upn("DALE@GRUBERPALLETS.COM") is True
    assert auth.is_super_admin_upn("ian@gruberpallets.com") is False


def test_super_admin_upns_can_be_configured(monkeypatch):
    monkeypatch.setenv(
        "SUPER_ADMIN_UPNS",
        "dale@gruberpallets.com, e@gruberpallets.com",
    )
    assert auth.is_super_admin_upn("e@gruberpallets.com") is True
    assert auth.is_super_admin_upn("ian@gruberpallets.com") is False


def test_session_rejects_alg_none_token(monkeypatch):
    """Regression: a JWT with `alg: none` (unsigned) must be rejected
    even if the rest of the payload looks valid. authlib's HS256
    decode-with-secret should refuse to validate unsigned tokens."""
    monkeypatch.setattr(auth, "_session_secret", lambda: "test-secret-32-bytes-of-random-data!!")
    # Manually construct an unsigned JWT: base64url(header).base64url(payload).<empty>
    import base64
    import json
    def b64u(d):
        raw = json.dumps(d, separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")
    header = b64u({"alg": "none", "typ": "JWT"})
    payload = b64u({"sub": "x", "upn": "evil@gruberpallets.com", "name": "Evil", "exp": 9999999999})
    unsigned = f"{header}.{payload}."
    assert auth.verify_session(unsigned) is None


@pytest.mark.parametrize("upn,expected", [
    # Happy paths
    ("dale@gruberpallets.com", True),
    ("DALE@GRUBERPALLETS.COM", True),  # case-insensitive
    ("dale.gruber+tag@gruberpallets.com", True),  # plus-addressing
    ("@gruberpallets.com", True),  # empty local part — Entra IDP gates this separately
    # Wrong domains
    ("attacker@evil.com", False),
    ("dale@gruberpallets.com.evil.com", False),  # suffix attack
    ("dale@notgruberpallets.com", False),  # near-miss
    ("dalegruberpallets.com", False),  # no @
    # Multi-@ pathological
    ("user@evil@gruberpallets.com", False),
    ("@@gruberpallets.com", False),
    ("a@b@c@gruberpallets.com", False),
    # Empty / None
    ("", False),
    (None, False),
])
def test_domain_ok(upn, expected):
    assert auth.domain_ok(upn) is expected


def test_oauth_client_requires_env(monkeypatch):
    monkeypatch.delenv("MS_TENANT_ID", raising=False)
    monkeypatch.delenv("MS_CLIENT_ID", raising=False)
    monkeypatch.delenv("MS_CLIENT_SECRET", raising=False)
    auth.reset_oauth_client_for_tests()
    with pytest.raises(RuntimeError, match="MS_TENANT_ID"):
        auth.oauth_client()


def test_oauth_client_lists_all_missing_vars(monkeypatch):
    monkeypatch.setenv("MS_TENANT_ID", "ok")
    monkeypatch.delenv("MS_CLIENT_ID", raising=False)
    monkeypatch.delenv("MS_CLIENT_SECRET", raising=False)
    auth.reset_oauth_client_for_tests()
    with pytest.raises(RuntimeError, match="MS_CLIENT_ID.*MS_CLIENT_SECRET"):
        auth.oauth_client()


def test_oauth_client_memoizes(monkeypatch):
    monkeypatch.setenv("MS_TENANT_ID", "00000000-0000-0000-0000-000000000000")
    monkeypatch.setenv("MS_CLIENT_ID", "11111111-1111-1111-1111-111111111111")
    monkeypatch.setenv("MS_CLIENT_SECRET", "fake-secret")
    auth.reset_oauth_client_for_tests()
    a = auth.oauth_client()
    b = auth.oauth_client()
    assert a is b
