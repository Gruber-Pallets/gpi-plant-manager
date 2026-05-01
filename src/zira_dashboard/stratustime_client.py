"""Client for the StratusTime time-clock web services API.

Auth flow (confirmed by live probe):
  1. POST /CreateToken with {CustomerAlias, SharedKey, UserName, UserPass}
     → returns a base64 token (JSON-quoted string).
  2. POST /<Method> with {"AuthToken": <token>, ...method-specific fields...}.

Required env vars:
  STRATUSTIME_SHARED_KEY      — UUID from Inbound Services admin page
  STRATUSTIME_WS_PASSWORD     — wsuser password from same page
  STRATUSTIME_CUSTOMER_ALIAS  — tenant alias (e.g., "gruberpallets")
  STRATUSTIME_WS_USERNAME     — defaults to "wsuser"

Module-level token cache keeps the same token in memory across calls within
one process for TOKEN_TTL_SECONDS. Callers can force refresh.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request

BASE_URL = "https://stratustime.centralservers.com/service/ws-json/2.0"
TIMEOUT_SECONDS = 30
TOKEN_TTL_SECONDS = 60 * 30  # refresh well before any reasonable expiry


def _config() -> dict:
    return {
        "shared_key": os.environ.get("STRATUSTIME_SHARED_KEY"),
        "ws_password": os.environ.get("STRATUSTIME_WS_PASSWORD"),
        "customer_alias": os.environ.get("STRATUSTIME_CUSTOMER_ALIAS"),
        "ws_username": os.environ.get("STRATUSTIME_WS_USERNAME") or "wsuser",
    }


def _is_configured(cfg: dict) -> bool:
    return bool(cfg["shared_key"] and cfg["ws_password"] and cfg["customer_alias"])


# Module-level token cache: (token, expires_at_epoch_seconds).
_token_cache: tuple[str, float] | None = None


def _post(path: str, body: dict, timeout: int = TIMEOUT_SECONDS) -> tuple[int, str]:
    """Raw POST to a service endpoint. Returns (status, body_text)."""
    url = f"{BASE_URL}/{path.lstrip('/')}"
    payload = json.dumps(body).encode()
    req = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        try:
            body_text = e.read().decode("utf-8", errors="replace")
        except Exception:
            body_text = str(e)
        return e.code, body_text
    except urllib.error.URLError as e:
        return 0, f"network error: {e.reason}"
    except Exception as e:
        return 0, f"error: {e}"


def ping() -> tuple[int, str]:
    """Unauthenticated ping. Returns (status, body)."""
    return _post("PingTest", {})


def _create_token() -> tuple[str | None, str]:
    """Request a fresh token. Returns (token, error_message)."""
    cfg = _config()
    if not _is_configured(cfg):
        return None, "Missing env vars (need SHARED_KEY, WS_PASSWORD, CUSTOMER_ALIAS)"
    body = {
        "CustomerAlias": cfg["customer_alias"],
        "CustomerAliasExternal": "",
        "SharedKey": cfg["shared_key"],
        "UserName": cfg["ws_username"],
        "UserPass": cfg["ws_password"],
    }
    status, resp = _post("CreateToken", body)
    if not (200 <= status < 300):
        return None, f"HTTP {status}: {resp[:200]}"
    try:
        token = json.loads(resp)
    except json.JSONDecodeError:
        return None, f"Invalid JSON token response: {resp[:200]}"
    if not isinstance(token, str) or not token:
        return None, f"Unexpected token shape: {repr(resp)[:200]}"
    return token, ""


def get_token(force_refresh: bool = False) -> tuple[str | None, str]:
    """Cached token getter. Returns (token, error_message)."""
    global _token_cache
    now = time.time()
    if not force_refresh and _token_cache is not None:
        token, expires_at = _token_cache
        if expires_at > now:
            return token, ""
    token, err = _create_token()
    if token:
        _token_cache = (token, now + TOKEN_TTL_SECONDS)
    return token, err


def _now_wcf_date() -> str:
    """Current time formatted as Microsoft WCF date string: /Date(epoch_ms+0000)/."""
    ms = int(time.time() * 1000)
    return f"/Date({ms}+0000)/"


def authenticated_post(method: str, body: dict | None = None) -> tuple[int, dict | str]:
    """POST a method with an injected AuthToken. Returns (status, parsed_json_or_text)."""
    token, err = get_token()
    if not token:
        return 0, err or "No token"
    full_body = dict(body or {})
    full_body["AuthToken"] = token
    status, resp_text = _post(method, full_body)
    if 200 <= status < 300:
        try:
            return status, json.loads(resp_text)
        except json.JSONDecodeError:
            return status, resp_text
    return status, resp_text


def health_check() -> dict:
    """Verify connectivity + auth.

    Returns:
      {
        "ok": bool,                      # ping_ok AND token_ok
        "configured": bool,              # all three required env vars present
        "ping_ok": bool,                 # /PingTest returned 2xx
        "ping_status": int,
        "token_ok": bool,                # /CreateToken returned a token
        "token_error": str,              # only set when token_ok is False
        "endpoint": str,                 # base URL we used
      }
    """
    cfg = _config()
    if not _is_configured(cfg):
        missing = [
            n for n, v in [
                ("STRATUSTIME_SHARED_KEY", cfg["shared_key"]),
                ("STRATUSTIME_WS_PASSWORD", cfg["ws_password"]),
                ("STRATUSTIME_CUSTOMER_ALIAS", cfg["customer_alias"]),
            ] if not v
        ]
        return {
            "ok": False,
            "configured": False,
            "ping_ok": False,
            "ping_status": 0,
            "token_ok": False,
            "token_error": f"Set on Railway: {', '.join(missing)}.",
            "endpoint": BASE_URL,
        }
    ping_status, _ = ping()
    ping_ok = 200 <= ping_status < 300
    token, token_err = get_token(force_refresh=True)
    token_ok = token is not None
    return {
        "ok": ping_ok and token_ok,
        "configured": True,
        "ping_ok": ping_ok,
        "ping_status": ping_status,
        "token_ok": token_ok,
        "token_error": token_err if not token_ok else "",
        "endpoint": BASE_URL,
    }


def list_employees() -> list[dict]:
    """Smoke fetch via GetUserBasic (DataAction SELECT-ALL).

    Returns a list of employee dicts with keys like:
      Badge, Email, EmpIdentifier, FirstName, LastName, Phone1/2/3,
      Status, TimeZoneDisplayName, ...
    Returns [] on failure (caller should display health_check details first).
    """
    status, parsed = authenticated_post("GetUserBasic", {
        "EffectiveDate": _now_wcf_date(),
        "DataAction": {"Name": "SELECT-ALL", "Values": []},
    })
    if status < 200 or status >= 300:
        return []
    if isinstance(parsed, dict):
        results = parsed.get("Results")
        if isinstance(results, list):
            return results
    return []
