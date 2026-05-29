"""Pre-render the hot staffing pages into the HTTP response cache.

The day-view and leaderboards GET handlers already render AND call
``_http_cache.store_cached_response()`` themselves. This module simply
invokes them on a background tick (from ``app.py``'s lifespan loops) so
the response cache is populated proactively — a human never pays the
~1.9s cold render; they hit the warm <1ms cached bytes instead.

Calling the handlers as plain functions (the ``share.py`` pattern)
bypasses the ASGI middleware stack entirely, so no auth is involved. The
handlers only touch ``request`` to pass it to
``templates.TemplateResponse``; the staffing-section templates never
dereference ``request.session`` / ``url_for`` / ``request.url`` (verified),
so a minimal synthetic Request renders byte-identical HTML.
"""
from __future__ import annotations

import logging

from starlette.requests import Request

_log = logging.getLogger(__name__)


def _synthetic_get_request(path: str, query_string: bytes = b"") -> Request:
    """Build a minimal ASGI GET ``Request`` for calling a page handler
    outside the request cycle. Enough scope for Starlette's
    ``TemplateResponse``; no ``app``/``session`` needed because the
    staffing templates don't use ``url_for`` or ``request.session``."""
    async def _receive() -> dict:
        return {"type": "http.request", "body": b"", "more_body": False}

    scope = {
        "type": "http",
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "server": ("127.0.0.1", 80),
        "client": ("127.0.0.1", 0),
        "root_path": "",
        "path": path,
        "raw_path": path.encode("utf-8"),
        "query_string": query_string,
        "headers": [],
    }
    return Request(scope, receive=_receive)


def warm_once() -> None:
    """Render today's hot, frequently-changing staffing pages so their
    handlers repopulate the response cache. Each page is warmed
    independently; a failure in one must never block the others or crash
    the caller (the warmer loop must never die)."""
    # Day-view: a bare /staffing nav resolves day=None -> next working day,
    # view="draft", publish_blocked=0. Pass them explicitly (not via Query
    # defaults) so the handler sees real values, reproducing the exact
    # cache key a human's bare navigation produces.
    try:
        from .routes.staffing import staffing_page
        staffing_page(
            _synthetic_get_request("/staffing"),
            day=None,
            publish_blocked=0,
            view="draft",
        )
    except Exception as e:  # noqa: BLE001 — warmer must never bubble
        _log.warning("page_warmer: day-view warm failed: %s", e)

    # Leaderboards: bare nav -> window="week", metric="pct".
    try:
        from .routes.leaderboards import staffing_leaderboards
        staffing_leaderboards(
            _synthetic_get_request("/staffing/leaderboards"),
            window="week",
            metric="pct",
            start=None,
            end=None,
        )
    except Exception as e:  # noqa: BLE001 — warmer must never bubble
        _log.warning("page_warmer: leaderboards warm failed: %s", e)
