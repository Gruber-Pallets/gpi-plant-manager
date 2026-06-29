"""Read-only REST client for the GPI Forklift app (gpiforklift.com).

The forklift app runs a call-and-dispatch queue; this client pulls demand and
driver-performance data into the Plant Manager. GET-only — we never write.

Config (read per-call, so importing this module has no side effects):
  FORKLIFT_API_KEY   - the internal endpoints accept it as the X-API-Key header
                       (best-effort; they currently serve reads without auth).
                       The external completions feed REQUIRES it as a
                       `Authorization: Bearer <key>` header.
  FORKLIFT_BASE_URL  - defaults to https://www.gpiforklift.com
"""
from __future__ import annotations

import os
from typing import Any

import requests

DEFAULT_BASE_URL = "https://www.gpiforklift.com"
_TIMEOUT = 15


class ForkliftError(Exception):
    """Raised on any forklift API failure."""


def _base_url() -> str:
    return (os.environ.get("FORKLIFT_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")


def _get(path: str) -> Any:
    """GET {base}{path} with the API key header; return parsed JSON.
    Wraps any transport/HTTP error in ForkliftError."""
    url = f"{_base_url()}{path}"
    headers = {}
    key = os.environ.get("FORKLIFT_API_KEY")
    if key:
        headers["X-API-Key"] = key
    try:
        r = requests.get(url, headers=headers, timeout=_TIMEOUT)
        r.raise_for_status()
        return r.json()
    except Exception as e:  # noqa: BLE001 - normalize to one error type
        raise ForkliftError(f"GET {path} failed: {e}") from e


def fetch_dashboard() -> dict:
    """Today's precomputed analytics: driverLeaderboard, hourlyClaimAvgs, etc."""
    return _get("/api/dashboard")


def fetch_queue_history() -> list[dict]:
    """Today's call records (the API only exposes 'today')."""
    return _get("/api/queue/history")


def fetch_drivers() -> list[dict]:
    """Forklift drivers: {id, name, isOverloadResponder, skills}."""
    return _get("/api/drivers")


def fetch_weekly_trends() -> dict:
    """8-week aggregate trends (cold-start demand source)."""
    return _get("/api/report/weekly-trends")


def _external_get(path: str, params: dict) -> dict:
    """GET an authenticated external API endpoint (Bearer). Raises ForkliftError
    if the key is missing or the call fails."""
    key = os.environ.get("FORKLIFT_API_KEY")
    if not key:
        raise ForkliftError("FORKLIFT_API_KEY required for the external API")
    url = f"{_base_url()}{path}"
    try:
        r = requests.get(url, headers={"Authorization": f"Bearer {key}"},
                         params=params, timeout=_TIMEOUT)
        r.raise_for_status()
        return r.json()
    except ForkliftError:
        raise
    except Exception as e:  # noqa: BLE001 - normalize to one error type
        raise ForkliftError(f"external GET {path} failed: {e}") from e


def fetch_completions(since: int = 0, limit: int = 500, max_pages: int = 400) -> list[dict]:
    """All completed calls from the external API since `since` (epoch ms),
    walking nextCursor. max_pages is a safety cap."""
    out: list[dict] = []
    cursor = None
    for _ in range(max_pages):
        params = {"since": since, "limit": limit}
        if cursor:
            params["cursor"] = cursor
        data = _external_get("/api/external/v1/completions", params)
        out.extend(data.get("items") or [])
        cursor = data.get("nextCursor")
        if not cursor:
            break
    return out
