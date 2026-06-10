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
# 5 minutes. Past data is immutable in principle, but a code or data
# fix that changes how a past day renders (e.g. the Saturday in_shift_on
# fix) shouldn't take up to an hour to reach a browser that already
# loaded the page. Matches the server-side _RESPONSE_CACHE_PAST TTL, so
# the server's still doing the heavy lifting for repeated views; browsers
# just revalidate every 5 min instead of every hour.
_PAST_MAX_AGE = 300


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
# 60s, not 15s: the staffing page-warmer re-renders today's hot pages
# every 45s, so a 60s TTL keeps a comfortable margin and the cache never
# goes cold between ticks. Mutations still call invalidate_today_cache()
# so saves appear immediately regardless of TTL. (Browser-side
# Cache-Control stays at _TODAY_MAX_AGE=15s — the browser revalidates
# every 15s and hits this warm server cache, so revalidation is ~free.)
_RESPONSE_CACHE_TODAY = TTLCache(ttl_seconds=60.0, max_entries=64)
_RESPONSE_CACHE_PAST = TTLCache(ttl_seconds=300.0, max_entries=128)
# "Stable" bucket: pages whose data changes only on explicit writes (which
# invalidate this bucket directly) rather than continuously through the day —
# e.g. the skills matrix. 600s TTL so the page stays warm between the skills
# warmer's 300s ticks instead of going cold like the 60s today bucket.
# Mutations call invalidate_stable_cache() so edits still show immediately.
_RESPONSE_CACHE_STABLE = TTLCache(ttl_seconds=600.0, max_entries=32)


def _bucket_for(*, includes_today: bool, stable: bool) -> TTLCache:
    if stable:
        return _RESPONSE_CACHE_STABLE
    return _RESPONSE_CACHE_TODAY if includes_today else _RESPONSE_CACHE_PAST


def get_cached_response(cache_key, *, includes_today: bool, stable: bool = False) -> Response | None:
    """Return a cached HTMLResponse for the given key, or None on miss.

    The returned response is a fresh HTMLResponse built from cached bytes
    + content_type. We re-apply Cache-Control headers so the browser side
    of the cache stays in sync. `stable=True` selects the long-TTL bucket
    (`includes_today` still drives the browser Cache-Control headers).
    """
    cache = _bucket_for(includes_today=includes_today, stable=stable)
    cached = cache.peek(cache_key)
    if cached is None:
        return None
    body, content_type = cached
    response = HTMLResponse(content=body, media_type=content_type)
    set_cache_headers(response, includes_today=includes_today)
    return response


def invalidate_today_cache() -> None:
    """Drop every cached response in the today bucket. Call this from write
    paths (e.g., saving a retro WC attribution) so the next dashboard load
    reflects the new data immediately rather than waiting up to 15s."""
    _RESPONSE_CACHE_TODAY.invalidate()


def invalidate_stable_cache() -> None:
    """Drop every cached response in the stable bucket. Call this from the
    write paths of pages cached with stable=True (e.g. skills-matrix /
    roster mutations) so edits show on the next load despite the long TTL."""
    _RESPONSE_CACHE_STABLE.invalidate()


def invalidate_all_cache() -> None:
    """Drop every cached response in ALL buckets (today, past, stable). Use
    this for writes that affect rendering regardless of date range — e.g.,
    widget customizations (title, color, alignment) apply to every cached
    page of the same type, so a save on today must also invalidate the
    cached week, month, and custom-range views."""
    _RESPONSE_CACHE_TODAY.invalidate()
    _RESPONSE_CACHE_PAST.invalidate()
    _RESPONSE_CACHE_STABLE.invalidate()


def store_cached_response(cache_key, *, includes_today: bool, response: Response, stable: bool = False) -> None:
    """Cache a response's body bytes + content_type for future serve.

    Safe to call after `set_cache_headers` — we capture the body only,
    not the headers (those are re-applied on serve)."""
    cache = _bucket_for(includes_today=includes_today, stable=stable)
    # Force Starlette's TemplateResponse to render its template if it
    # hasn't already; .body is the rendered bytes.
    body = response.body if hasattr(response, "body") else b""
    if isinstance(body, memoryview):
        body = bytes(body)
    cache.set(cache_key, (body, response.media_type or "text/html; charset=utf-8"))
