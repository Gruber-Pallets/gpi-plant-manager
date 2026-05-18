"""Authentication routes: /auth/login, /auth/callback, /auth/logout.

Login flow:
  /auth/login    → store ?next= in signed cookie → redirect to Microsoft
  /auth/callback ← Microsoft → validate token, set session cookie, redirect to next
  /auth/logout   → clear session cookie, redirect home

The `next=` redirect target is preserved across the round-trip via a
short-lived HTTP-only signed cookie (Microsoft only echoes back the
`state` param, which Authlib uses for CSRF; we don't piggyback on it).
"""
from __future__ import annotations

import logging

from authlib.integrations.base_client.errors import OAuthError
from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse
from itsdangerous import BadSignature, URLSafeSerializer

from .. import auth
from ..deps import templates

router = APIRouter()
_log = logging.getLogger(__name__)

_NEXT_COOKIE = "gpi_auth_next"
_NEXT_COOKIE_MAX_AGE = 300  # 5 minutes — round-trip to Microsoft is under a minute


def _next_serializer() -> URLSafeSerializer:
    return URLSafeSerializer(auth._session_secret(), salt="auth-next")


_SAFE_NEXT_FORBIDDEN_CHARS = ("\\", "\r", "\n", "\t")


def _safe_next(value: str | None, default: str = "/") -> str:
    """Only allow same-origin paths. Rejects:
      - empty / None
      - paths not starting with `/`
      - protocol-relative `//host/...`
      - paths containing backslashes (browsers normalize `\\` to `/`)
      - paths containing CR/LF/tab (header injection vectors)
      - anything that's been pre-trimmed leading whitespace
    """
    if not value:
        return default
    if not value.startswith("/") or value.startswith("//"):
        return default
    if any(c in value for c in _SAFE_NEXT_FORBIDDEN_CHARS):
        return default
    return value


@router.get("/auth/login")
async def auth_login(request: Request, next: str | None = None):
    """Kick off the OIDC flow. Stashes ?next= in a signed cookie so we
    can redirect there after Microsoft sends the user back."""
    target = _safe_next(next, "/")
    redirect_uri = str(request.url_for("auth_callback"))
    response = await auth.oauth_client().azure.authorize_redirect(request, redirect_uri)
    response.set_cookie(
        _NEXT_COOKIE,
        _next_serializer().dumps(target),
        max_age=_NEXT_COOKIE_MAX_AGE,
        httponly=True,
        secure=True,
        samesite="lax",
        path="/",
    )
    return response


@router.get("/auth/callback", name="auth_callback")
async def auth_callback(request: Request):
    """Microsoft redirects here after sign-in. Exchange code for tokens,
    validate the domain, set the session cookie."""
    try:
        token = await auth.oauth_client().azure.authorize_access_token(request)
    except OAuthError as e:
        # Authlib raised on token exchange or userinfo fetch — could be
        # expired auth code, network blip, etc. Surface to the user.
        _log.warning("OIDC callback failed: %s", e)
        return templates.TemplateResponse(
            request, "auth_denied.html",
            {
                "title": "Sign-in failed",
                "message": "Something went wrong on the Microsoft side. Try signing in again.",
            },
            status_code=400,
        )
    # RuntimeError from oauth_client() (missing env vars) propagates — that's
    # a server-config bug we want to see as a 500, not a generic "try again"
    # screen that masks a config error.

    userinfo = token.get("userinfo") or {}
    upn = userinfo.get("preferred_username") or userinfo.get("upn") or ""
    name = userinfo.get("name") or upn
    sub = userinfo.get("sub") or userinfo.get("oid") or ""

    if not auth.domain_ok(upn):
        # Don't log the upn — don't accumulate a list of non-GPI accounts
        # that tried to access this app.
        return templates.TemplateResponse(
            request, "auth_denied.html",
            {
                "title": "Not authorized",
                "message": "This app is restricted to GPI employees. Sign in with your @gruberpallets.com Microsoft account.",
            },
            status_code=403,
        )

    # Recover the original ?next= from the signed cookie.
    nxt = "/"
    raw = request.cookies.get(_NEXT_COOKIE)
    if raw:
        try:
            nxt = _safe_next(_next_serializer().loads(raw), "/")
        except BadSignature:
            nxt = "/"

    session_jwt = auth.mint_session(sub=sub, upn=upn, name=name)
    response = RedirectResponse(url=nxt, status_code=302)
    response.set_cookie(
        auth.SESSION_COOKIE_NAME, session_jwt,
        max_age=int(auth.SESSION_TTL.total_seconds()),
        httponly=True, secure=True, samesite="lax", path="/",
    )
    response.delete_cookie(_NEXT_COOKIE, path="/")
    return response


@router.post("/auth/logout")
async def auth_logout():
    """Clear the session cookie and redirect home. POST-only to prevent
    CSRF (an attacker's `<img src="/auth/logout">` from another site
    would otherwise log the user out). Trigger via a small form in the
    app's nav UI.

    Local logout only — the user's Microsoft SSO session in their browser
    is untouched, so signing in again is one click."""
    response = RedirectResponse(url="/", status_code=302)
    response.delete_cookie(auth.SESSION_COOKIE_NAME, path="/")
    return response
