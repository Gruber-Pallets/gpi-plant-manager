"""Page-usage tracking.

Records which pages get used so dead pages can be found and retired. The
hot path is deliberately cheap: each request bumps an in-memory counter
(no DB work), and the counter is drained to Postgres in one batched upsert
on the existing warmer tick. This keeps request latency untouched and, by
doing zero per-request DB work, stays clear of the connection-pool
exhaustion that has taken the site down before.

One stored row == (day, route pattern, method, user) with a view count.
Storing the route *pattern* (``/staffing/people/{name}``) rather than the
concrete URL is what makes the numbers aggregable; a row per user gives
both total views and distinct-user counts exactly.
"""
from __future__ import annotations

import logging
import os
from datetime import date, datetime, timedelta, timezone
from threading import Lock

from starlette.requests import Request

from . import db, shift_config

_log = logging.getLogger(__name__)

_ENABLED_ENV = "PAGE_VIEW_TRACKING_ENABLED"


def tracking_enabled() -> bool:
    """Kill-switch. Default ON; set PAGE_VIEW_TRACKING_ENABLED to any of
    0/false/no/off to disable recording without a redeploy."""
    return os.environ.get(_ENABLED_ENV, "1").strip().lower() not in (
        "0", "false", "no", "off",
    )


def _plant_today() -> date:
    return datetime.now(timezone.utc).astimezone(shift_config.SITE_TZ).date()


def route_pattern(request: Request) -> str | None:
    """The matched route *template* for this request, e.g.
    ``/staffing/people/{name}`` — never the concrete URL. Returns ``None``
    when nothing matched (404s, unrouted paths), which callers skip."""
    route = request.scope.get("route")
    return getattr(route, "path", None)


# Traffic that says nothing about page usage: asset serving, the TV
# heartbeat, health checks, and the OIDC login handshake. Matched against
# the route *pattern*, so ``/static`` (a Mount) and ``/auth/*`` are covered
# by prefix.
_EXCLUDE_PREFIXES = ("/static", "/auth")
_EXCLUDE_EXACT = frozenset({
    "/tv/ping", "/healthz", "/favicon.ico", "/robots.txt",
    # FastAPI auto-docs (disabled in prod, but excluded here so the inventory
    # is correct even if they're ever re-enabled).
    "/docs", "/redoc", "/openapi.json", "/docs/oauth2-redirect",
})


def should_track(pattern: str) -> bool:
    """Whether a matched route pattern represents real page/feature usage
    worth counting (vs. asset/heartbeat/health/auth noise)."""
    if pattern in _EXCLUDE_EXACT:
        return False
    return not any(
        pattern == p or pattern.startswith(p + "/") for p in _EXCLUDE_PREFIXES
    )


def page_inventory(app) -> list[str]:
    """Every user-navigable page the app defines: GET routes that pass
    ``should_track`` and aren't JSON APIs (``/api/*``). This is the
    denominator for the never-hit report — diff it against observed routes to
    find pages nobody visited."""
    pages: set[str] = set()
    for route in app.routes:
        methods = getattr(route, "methods", None) or set()
        path = getattr(route, "path", None)
        if not path or "GET" not in methods:
            continue
        if path.startswith("/api") or not should_track(path):
            continue
        pages.add(path)
    return sorted(pages)


def never_hit(observed: set[str], inventory: list[str]) -> list[str]:
    """Inventory pages with no observed views, sorted — the dead-page list."""
    return sorted(p for p in inventory if p not in observed)


class PageViewCounter:
    """Thread-safe in-memory accumulator of page views.

    Keyed by ``(day, route, method, user_email)`` -> count. ``drain`` returns
    the accumulated rows and atomically resets, so the next flush starts from
    zero and views are never double-counted.
    """

    def __init__(self) -> None:
        self._counts: dict[tuple[date, str, str, str], int] = {}
        self._lock = Lock()

    def record(self, day: date, route: str, method: str, user_email: str) -> None:
        key = (day, route, method, user_email)
        with self._lock:
            self._counts[key] = self._counts.get(key, 0) + 1

    def drain(self) -> list[tuple[date, str, str, str, int]]:
        with self._lock:
            rows = [(*key, views) for key, views in self._counts.items()]
            self._counts.clear()
        return rows


# Process-global accumulator. Single uvicorn worker in prod, so one instance
# sees every request; the lock keeps it correct for sync routes served from
# the threadpool too.
_counter = PageViewCounter()


def _persist(rows: list[tuple[date, str, str, str, int]]) -> None:
    """Upsert drained view counts, adding to any existing (day, route, method,
    user) total so repeated flushes across a day accumulate correctly."""
    with db.cursor() as cur:
        db.execute_values(
            cur,
            "INSERT INTO page_views (day, route, method, user_email, views) "
            "VALUES %s "
            "ON CONFLICT (day, route, method, user_email) DO UPDATE SET "
            "  views = page_views.views + EXCLUDED.views",
            rows,
        )


def usage_report(days: int = 7) -> list[dict]:
    """Per-route usage over the last ``days`` (inclusive of today), ordered
    most-used first. Each row: ``route``, ``views`` (total), ``users``
    (distinct signed-in people; anonymous kiosk/TV views don't add here),
    ``last_day`` (most recent day seen)."""
    since = _plant_today() - timedelta(days=max(days, 1) - 1)
    return db.query(
        "SELECT route, "
        "  SUM(views)::int AS views, "
        "  COUNT(DISTINCT user_email) FILTER (WHERE user_email <> '')::int AS users, "
        "  MAX(day) AS last_day "
        "FROM page_views WHERE day >= %s "
        "GROUP BY route ORDER BY views DESC, route",
        (since,),
    )


def record_view(request: Request) -> None:
    """Record one page view for the just-served request. Called from the
    response middleware after ``call_next``. Never raises into the request
    path — a tracking failure must not affect the response."""
    if not tracking_enabled():
        return
    try:
        pattern = route_pattern(request)
        if not pattern or not should_track(pattern):
            return
        user = getattr(request.state, "user_upn", None) or ""
        _counter.record(_plant_today(), pattern, request.method, user)
    except Exception as e:  # noqa: BLE001 -- tracking must never break a request
        _log.warning("page-view record failed: %s", e)


def flush() -> None:
    """Drain the in-memory counter to Postgres. Called on the warmer tick;
    a no-op when nothing has been recorded since the last flush."""
    rows = _counter.drain()
    if rows:
        _persist(rows)
