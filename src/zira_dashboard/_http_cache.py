"""HTTP Cache-Control helpers for dashboard GET handlers.

Conventions:
- Past-day pages: long cache. Data is immutable.
- Today-or-future pages: short cache. Data changes throughout the shift.
- All caches are `private` (don't cache at shared proxies — content
  may include user-specific widget customizations down the line).

Usage from a handler:
    from .._http_cache import set_cache_headers
    response = templates.TemplateResponse(...)
    set_cache_headers(response, includes_today=is_today)
    return response
"""

from __future__ import annotations

from datetime import date

# 15s is short enough that an in-progress shift feels live, long
# enough that rapid back-and-forth between pages doesn't re-fetch.
_TODAY_MAX_AGE = 15
# 1 hour. Past data is immutable in principle but capping at 1h keeps
# the browser cache from holding stale templates after a code deploy.
_PAST_MAX_AGE = 3600


def set_cache_headers(response, *, includes_today: bool) -> None:
    """Apply Cache-Control to a response based on data freshness."""
    max_age = _TODAY_MAX_AGE if includes_today else _PAST_MAX_AGE
    response.headers["Cache-Control"] = f"private, max-age={max_age}"


def includes_today(d: date, today: date) -> bool:
    return d >= today


def range_includes_today(end_d: date, today: date) -> bool:
    """For range-based pages (leaderboards): the range's end determines
    whether the data is still moving."""
    return end_d >= today


from starlette.responses import HTMLResponse, Response
from ._cache import TTLCache

# Server-side response cache. Keyed by route + query state.
# Today's pages: 15s — matches the existing browser Cache-Control short
# window so we never serve content older than what the browser would
# fetch on its own. Past pages: 5 minutes (server-side; the browser side
# caches longer via Cache-Control).
_RESPONSE_CACHE_TODAY = TTLCache(ttl_seconds=15.0, max_entries=64)
_RESPONSE_CACHE_PAST = TTLCache(ttl_seconds=300.0, max_entries=128)


def get_cached_response(cache_key, *, includes_today: bool) -> Response | None:
    """Return a cached HTMLResponse for the given key, or None on miss.

    The returned response is a fresh HTMLResponse built from cached bytes
    + content_type. We re-apply Cache-Control headers so the browser side
    of the cache stays in sync.
    """
    cache = _RESPONSE_CACHE_TODAY if includes_today else _RESPONSE_CACHE_PAST
    cached = cache.peek(cache_key)
    if cached is None:
        return None
    body, content_type = cached
    response = HTMLResponse(content=body, media_type=content_type)
    set_cache_headers(response, includes_today=includes_today)
    return response


def store_cached_response(cache_key, *, includes_today: bool, response: Response) -> None:
    """Cache a response's body bytes + content_type for future serve.

    Safe to call after `set_cache_headers` — we capture the body only,
    not the headers (those are re-applied on serve)."""
    cache = _RESPONSE_CACHE_TODAY if includes_today else _RESPONSE_CACHE_PAST
    # Force Starlette's TemplateResponse to render its template if it
    # hasn't already; .body is the rendered bytes.
    body = response.body if hasattr(response, "body") else b""
    if isinstance(body, memoryview):
        body = bytes(body)
    cache.set(cache_key, (body, response.media_type or "text/html; charset=utf-8"))
