"""Authentication helpers: session JWT mint/verify + config + domain check.

Import-safe even when Microsoft env vars are missing — the OIDC client
is constructed lazily inside `oauth_client()` (added in Task 4). Tests
that only exercise JWT helpers don't need any Microsoft config.
"""
from __future__ import annotations

import os
import time
from datetime import timedelta
from typing import Any

from joserfc import jwt
from joserfc.errors import JoseError
from joserfc.jwk import OctKey
from joserfc.jwt import JWTClaimsRegistry

SESSION_COOKIE_NAME = "gpi_session"
SESSION_TTL = timedelta(days=7)
SESSION_REFRESH_AT = timedelta(days=6)
_JWT_ALG = "HS256"

ALLOWED_DOMAIN = "gruberpallets.com"
DEFAULT_SUPER_ADMIN_UPNS = ("dale@gruberpallets.com",)


def _session_secret() -> str:
    """Read SESSION_SECRET from env. Raises at use time, not import time."""
    secret = os.environ.get("SESSION_SECRET")
    if not secret:
        raise RuntimeError(
            "SESSION_SECRET env var is not set. Generate one via "
            "`python -c \"import secrets; print(secrets.token_urlsafe(32))\"` "
            "and add it to your environment."
        )
    return secret


def _jwt_key(secret: str) -> OctKey:
    return OctKey.import_key(secret)


def auth_disabled() -> bool:
    """True when AUTH_DISABLED=1/true/yes (local dev or staged rollout)."""
    return os.environ.get("AUTH_DISABLED", "").strip().lower() in ("1", "true", "yes")


def super_admin_upns() -> set[str]:
    """Configured UPNs allowed to manage sensitive admin-only surfaces."""
    raw = os.environ.get("SUPER_ADMIN_UPNS", "").strip()
    if not raw:
        return set(DEFAULT_SUPER_ADMIN_UPNS)
    return {part.strip().lower() for part in raw.split(",") if part.strip()}


def is_super_admin_upn(upn: str | None) -> bool:
    if not upn:
        return False
    return upn.strip().lower() in super_admin_upns()


def request_is_super_admin(request) -> bool:
    """True for a signed-in configured super admin."""
    return is_super_admin_upn(getattr(request.state, "user_upn", None))


def mint_session(*, sub: str, upn: str, name: str) -> str:
    """Sign a 7-day JWT with the user's Microsoft OID + UPN + display name."""
    now = int(time.time())
    payload = {
        "sub": sub,
        "upn": upn,
        "name": name,
        "iat": now,
        "exp": now + int(SESSION_TTL.total_seconds()),
    }
    return jwt.encode({"alg": _JWT_ALG}, payload, _jwt_key(_session_secret()))


def verify_session(token: str | None) -> dict[str, Any] | None:
    """Decode + verify a session JWT. Returns the payload or None for
    any failure (missing/malformed/bad-signature/expired/secret-unset).
    Never raises."""
    if not token:
        return None
    try:
        secret = _session_secret()
    except RuntimeError:
        return None
    try:
        token_obj = jwt.decode(token, _jwt_key(secret), algorithms=[_JWT_ALG])
        JWTClaimsRegistry().validate(token_obj.claims)  # checks exp/nbf/iat if present
        return dict(token_obj.claims)
    except JoseError:
        return None
    except (ValueError, TypeError):
        # authlib raises ValueError on malformed JWTs and TypeError on
        # non-str inputs that slip past the early None/empty check.
        return None


def needs_refresh(payload: dict[str, Any] | None) -> bool:
    """True when remaining lifetime is below SESSION_REFRESH_AT."""
    if not payload or "exp" not in payload:
        return False
    remaining = int(payload["exp"]) - int(time.time())
    return remaining < int(SESSION_REFRESH_AT.total_seconds())


def domain_ok(upn_or_email: str | None) -> bool:
    """Allow only single-@ identities whose domain part exactly matches
    ALLOWED_DOMAIN. Rejects multi-@ inputs that would slip past a naive
    .endswith() check."""
    if not upn_or_email or upn_or_email.count("@") != 1:
        return False
    domain = upn_or_email.split("@", 1)[1].lower()
    return domain == ALLOWED_DOMAIN.lower()


# ---------- OIDC client (lazy) ----------

_oauth_singleton: Any = None


def oauth_client():
    """Construct and memoize the Authlib OAuth client for Microsoft Entra ID.

    Lazy because the env vars may not be present at module import time
    (tests, AUTH_DISABLED=1 dev runs). Raises a clear RuntimeError when
    called without the required env vars set."""
    global _oauth_singleton
    if _oauth_singleton is not None:
        return _oauth_singleton

    tenant = os.environ.get("MS_TENANT_ID")
    client_id = os.environ.get("MS_CLIENT_ID")
    client_secret = os.environ.get("MS_CLIENT_SECRET")
    missing = [k for k, v in (
        ("MS_TENANT_ID", tenant),
        ("MS_CLIENT_ID", client_id),
        ("MS_CLIENT_SECRET", client_secret),
    ) if not v]
    if missing:
        raise RuntimeError(
            f"Microsoft Entra ID env vars not set: {', '.join(missing)}. "
            "See docs/superpowers/specs/2026-05-18-microsoft-auth-design.md for setup."
        )

    from authlib.integrations.starlette_client import OAuth
    oauth = OAuth()
    oauth.register(
        name="azure",
        server_metadata_url=f"https://login.microsoftonline.com/{tenant}/v2.0/.well-known/openid-configuration",
        client_id=client_id,
        client_secret=client_secret,
        client_kwargs={"scope": "openid profile email"},
    )
    _oauth_singleton = oauth
    return oauth


def reset_oauth_client_for_tests() -> None:
    """Reset the memoized client. Tests that monkeypatch env vars should
    call this between tests; production never calls this."""
    global _oauth_singleton
    _oauth_singleton = None


# ---------- ASGI middleware ----------

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import RedirectResponse

# Paths (or prefixes) that bypass auth entirely. Keep this list tight —
# every entry is a potential bypass for an attacker probing for an
# unauthenticated endpoint.
_BYPASS_PREFIXES = (
    "/auth/",      # login + callback + logout
    "/static/",    # CSS/JS/images
    "/api/v1/object/",  # server-to-server bearer-auth API owns its own auth
)
_BYPASS_EXACT = frozenset({
    "/healthz",
    "/robots.txt",
    "/favicon.ico",
})


def _is_bypass_path(path: str) -> bool:
    if path in _BYPASS_EXACT:
        return True
    return any(path.startswith(p) for p in _BYPASS_PREFIXES)


# Memoized parse of TV_ALLOWED_IPS: (raw env string, parsed entries). Keyed
# on the raw string (not parse-once-forever) so tests that monkeypatch the
# env var still see their value; production parses once per process.
_tv_allowlist_memo: tuple[str, tuple] | None = None


def _ip_allowlisted(request) -> bool:
    """True when the request's client IP matches any entry in the
    `TV_ALLOWED_IPS` env var (comma-separated single IPs and/or CIDR
    ranges). Used to grant /tv/* paths zero-config access from the
    shop floor — typing a device token URL on a TV remote is
    impractical, but the shop's public IP is stable.

    Honors `X-Forwarded-For` because uvicorn runs with `--proxy-headers`
    (set in the Dockerfile), so `request.client.host` reflects the real
    client IP, not Railway's proxy.

    Returns False when the env var is unset (no IPs allowlisted) or
    when the client IP can't be determined.
    """
    import ipaddress
    import os
    global _tv_allowlist_memo
    allowed_raw = os.environ.get("TV_ALLOWED_IPS", "").strip()
    if not allowed_raw:
        return False
    if _tv_allowlist_memo is None or _tv_allowlist_memo[0] != allowed_raw:
        entries = []
        for entry in allowed_raw.split(","):
            entry = entry.strip()
            if not entry:
                continue
            try:
                if "/" in entry:
                    entries.append(ipaddress.ip_network(entry, strict=False))
                else:
                    entries.append(ipaddress.ip_address(entry))
            except ValueError:
                # Malformed env var entry — skip it, log nothing (env var is
                # operator config, not user input; a typo here is a deploy issue).
                continue
        _tv_allowlist_memo = (allowed_raw, tuple(entries))
    client_ip = request.client.host if request.client else None
    if not client_ip:
        return False
    try:
        client_addr = ipaddress.ip_address(client_ip)
    except ValueError:
        return False
    for entry in _tv_allowlist_memo[1]:
        if isinstance(entry, (ipaddress.IPv4Network, ipaddress.IPv6Network)):
            if client_addr in entry:
                return True
        elif client_addr == entry:
            return True
    return False


def _tv_accessible_path(path: str) -> bool:
    """Paths reachable via TV auth (IP allowlist or device token).

    Beyond the literal `/tv/*` dashboard routes, this also includes the
    GOAT-alert dismiss endpoint so any TV showing the banner can clear
    it without a human session. Scope is intentionally narrow: dismiss
    only flips a boolean on a celebration row — no security implication.
    """
    if path.startswith("/tv/"):
        return True
    if path.startswith("/api/goat-alerts/") and path.endswith("/dismiss"):
        return True
    return False


class RequireAuthMiddleware(BaseHTTPMiddleware):
    """Gate every request behind a valid session cookie.

    Device-token support for /tv/* paths is added in a later task (subclass
    behavior or extend dispatch). Bypass list + AUTH_DISABLED logic stays
    the same.
    """

    # Class-level counter for the periodic AUTH_DISABLED warning. Logged
    # every Nth request (not every request, so we don't blow up log storage,
    # but often enough to surface accidental production bypass after the
    # boot-time log has scrolled out of the buffer).
    _AUTH_DISABLED_LOG_INTERVAL = 500
    _auth_disabled_request_count = 0

    async def dispatch(self, request, call_next):
        if auth_disabled():
            type(self)._auth_disabled_request_count += 1
            if type(self)._auth_disabled_request_count % type(self)._AUTH_DISABLED_LOG_INTERVAL == 1:
                import logging
                logging.getLogger(__name__).error(
                    "AUTH_DISABLED is set — every route is unauthenticated. "
                    "Unset this env var to enforce authentication. "
                    "(Re-logged every %d requests.)",
                    type(self)._AUTH_DISABLED_LOG_INTERVAL,
                )
            return await call_next(request)

        path = request.url.path
        if _is_bypass_path(path):
            return await call_next(request)

        cookie = request.cookies.get(SESSION_COOKIE_NAME)
        payload = verify_session(cookie)
        if payload is not None:
            request.state.user_upn = payload.get("upn")
            request.state.user_name = payload.get("name")
            response = await call_next(request)
            # Sliding-window refresh: if cookie is close to expiry, re-issue.
            if needs_refresh(payload):
                fresh = mint_session(sub=payload["sub"], upn=payload["upn"], name=payload["name"])
                response.set_cookie(
                    SESSION_COOKIE_NAME, fresh,
                    max_age=int(SESSION_TTL.total_seconds()),
                    httponly=True, secure=True, samesite="lax", path="/",
                )
            return response

        # No session cookie — try a device token, but ONLY on TV-accessible
        # paths (/tv/* plus the benign GOAT-alert dismiss endpoint, so a
        # shop-floor TV can clear its own celebration banner).
        if _tv_accessible_path(path):
            # Path 1: IP allowlist — shop-floor TVs, zero typing.
            if _ip_allowlisted(request):
                client_ip = request.client.host if request.client else "unknown"
                request.state.user_upn = f"ip:{client_ip}"
                request.state.user_name = "TV (allowlisted IP)"
                return await call_next(request)
            # Path 2: device token — off-network TVs or one-off setups.
            from . import device_tokens as _dt
            signed = request.query_params.get("device")
            row = _dt.lookup_active(signed) if signed else None
            if row is not None:
                # `device:` prefix marks the request as TV-not-human so any
                # downstream audit can distinguish humans from TVs at a glance.
                request.state.user_upn = f"device:{row['name']}"
                request.state.user_name = row["name"]
                return await call_next(request)

        # No valid auth — redirect to login.
        from urllib.parse import urlencode
        qs = urlencode({"next": path}) if path != "/" else ""
        target = "/auth/login" + (("?" + qs) if qs else "")
        return RedirectResponse(url=target, status_code=302)
