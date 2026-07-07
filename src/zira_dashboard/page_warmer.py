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
    """Render the staffing day view so its handler repopulates the response
    cache. A failure must never crash the caller (the warmer loop must
    never die).

    /staffing/leaderboards was warmed here too until 2026-07-07: a week of
    page-usage data showed ~2 human views against ~13k warm renders, and
    its 60s response-cache TTL means no slower cadence could keep it warm.
    The rare visitor now pays the ~2s cold render; the page's data
    sub-caches stay fresh via the Zira/live_cache warmer ticks."""
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


def warm_inbox_once() -> None:
    """Force-refresh the inbox top-nav sub-caches (assignments-todo +
    late-report). ``build_summary()`` renders these into the Inbox badge on
    EVERY page via _topnav.html; their 30 s in-process TTL doesn't slide on
    hits, so without this a human repeatedly pays the cold Zira/Odoo cascade
    just to draw the nav. Run on a cadence below the 30 s TTL (see _tick_inbox)
    so the badge is always served warm. Each source refreshes independently;
    a failure must never bubble (the warmer loop must never die)."""
    from .routes.staffing import assignments_todo_payload, late_report_payload
    try:
        assignments_todo_payload(force=True)
    except Exception as e:  # noqa: BLE001 — warmer must never bubble
        _log.warning("page_warmer: assignments inbox warm failed: %s", e)
    try:
        late_report_payload(force=True)
    except Exception as e:  # noqa: BLE001 — warmer must never bubble
        _log.warning("page_warmer: late-report inbox warm failed: %s", e)


def warm_skills_once() -> None:
    """Warm the skills matrix. Separate from warm_once() because roster /
    skill data changes rarely (and writes invalidate the cache directly),
    so this runs on a relaxed cadence — and warming it triggers
    odoo_sync.sync(force=False), which we don't want to fire every 45s."""
    try:
        from .routes.skills import staffing_skills
        staffing_skills(_synthetic_get_request("/staffing/skills"))
    except Exception as e:  # noqa: BLE001 — warmer must never bubble
        _log.warning("page_warmer: skills warm failed: %s", e)
