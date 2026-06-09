"""FastAPI app: station status + leaderboard UI + JSON endpoint.

Routes are organised into feature-specific modules under ``routes/``.
This file just instantiates the FastAPI app, mounts each feature router,
and provides the ``main()`` entry point used by the console script.
"""

from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from . import db
from .routes import (
    admin,
    api_layout,
    auth as auth_routes,
    changelog,
    dashboard,
    goat_watch,
    timeclock,
    timeclock_time_off,
    late_report,
    missing_wc,
    missed_punch_out,
    leaderboards,
    past_schedules,
    people,
    settings,
    share,
    skills,
    staffing,
    time_off,
    trophies,
    tv_displays,
    departments,
    wc_dashboard,
)


_log = logging.getLogger(__name__)


def _zira_client():
    """Lazy import of the shared Zira client to avoid import cycles."""
    from .deps import client
    return client


# --- Background warmers --------------------------------------------------
# Each warmer runs its tick coroutine forever on a fixed interval; the first
# run happens immediately, before the first sleep. _run_warmer owns the
# while/try/except/sleep skeleton so the ticks stay tiny. The refresh_*
# helpers already log+swallow internally; the try in _run_warmer is the
# backstop that keeps a warmer alive across an unexpected bad tick.


async def _tick_zira_cache():
    """Warm today's Zira leaderboard cache so /recycling never pays the
    cold-cache penalty. Past days are cached on first read, not here."""
    from .leaderboard import cached_leaderboard
    from .stations import recycling_stations
    stations = recycling_stations()
    if stations:
        today = datetime.now(timezone.utc).date()
        now_utc = datetime.now(timezone.utc)
        # Run the (sync, blocking) leaderboard fetch off the event loop.
        await asyncio.to_thread(
            cached_leaderboard, _zira_client(), stations, today, now_utc
        )


async def _tick_live_cache():
    """Refresh today's attendance into live_cache and UPSERT today's
    production_daily rows (so MTD / today leaderboards see partial-day data)."""
    from . import live_cache
    today = datetime.now(timezone.utc).date()
    await asyncio.to_thread(live_cache.refresh_attendance, today)
    await asyncio.to_thread(live_cache.refresh_production, today, _zira_client())


async def _tick_timeclock_sync():
    """Retry any timeclock_punches_log rows still flagged unsynced to Odoo."""
    from . import timeclock_sync
    await asyncio.to_thread(timeclock_sync.retry_unsynced_punches)


async def _tick_odoo_attendance():
    """Mirror Odoo's open hr.attendance into odoo_open_attendance_cache so the
    punch screen reflects out-of-band Odoo edits without an XML-RPC on the tap."""
    from . import live_cache
    await asyncio.to_thread(live_cache.refresh_odoo_open_attendance)


async def _tick_auto_lunch():
    """Drive the auto-lunch worker. No-ops while the feature is disabled
    (auto_lunch_settings.enabled defaults to FALSE)."""
    from . import auto_lunch
    await asyncio.to_thread(auto_lunch.run_tick)


async def _tick_time_off_sync():
    """Retry any time_off_requests rows still flagged unsynced to Odoo hr.leave."""
    from . import time_off_sync
    await asyncio.to_thread(time_off_sync.retry_unsynced_requests)


async def _tick_time_off_poll():
    """Pull hr.leave state changes back from Odoo so the local mirror picks up
    manager approvals/refusals and cascades them into the staffing scheduler."""
    from . import time_off_sync
    await asyncio.to_thread(time_off_sync.poll_odoo_leaves)


async def _tick_time_off_balance():
    """Refresh stale time_off_balances rows from Odoo (older than 10 min) so
    kiosk balance reads don't each pay a per-employee Odoo round-trip."""
    from . import time_off_balances
    await asyncio.to_thread(time_off_balances.refresh_stale, 600)


async def _tick_staffing_pages():
    """Keep today's hot staffing pages pre-rendered in the response cache so the
    first human load (including the first after a Railway deploy) is a warm hit."""
    from . import page_warmer
    await asyncio.to_thread(page_warmer.warm_once)


async def _tick_staffing_stable():
    """Warm the slow-changing staffing pages (the skills matrix). Roster/skill
    data rarely changes and writes invalidate the cache directly, so 5 min is
    plenty -- and it avoids triggering odoo_sync.sync(force=False) every 45s."""
    from . import page_warmer
    await asyncio.to_thread(page_warmer.warm_skills_once)


async def _tick_missing_wc():
    """Refresh the cache of Odoo hr.attendance lacking a work-center tag (last
    14 days) for the Missing-Work-Center alert. No-ops (logs once) if the Odoo
    kiosk WC field isn't configured."""
    from datetime import timedelta
    from . import missing_wc, odoo_client
    since = datetime.now(timezone.utc) - timedelta(days=14)
    rows = await asyncio.to_thread(odoo_client.fetch_attendances_missing_wc, since)
    await asyncio.to_thread(missing_wc.write_cache, rows)


async def _tick_missed_punch_out():
    """Close any attendance still open from a prior day at that day's midnight
    and flag it for the Missed-Punch-Out alert. Cadence doesn't affect the close
    time (it's computed from the check-in day, not 'now')."""
    from . import missed_punch_out, shift_config
    today = datetime.now(shift_config.SITE_TZ).date()
    await asyncio.to_thread(missed_punch_out.run_close, today)


# (name, tick coroutine, interval seconds). `name` is used only in the
# "warmer tick failed" log line. Intervals are unchanged from the original
# per-loop functions this registry replaced.
_WARMERS = [
    ("Zira cache", _tick_zira_cache, 30),
    ("live_cache", _tick_live_cache, 45),
    ("kiosk sync", _tick_timeclock_sync, 60),
    ("Odoo open-attendance", _tick_odoo_attendance, 30),
    ("auto-lunch", _tick_auto_lunch, 60),
    ("time-off sync", _tick_time_off_sync, 60),
    ("time-off poll", _tick_time_off_poll, 60),
    ("time-off balance", _tick_time_off_balance, 600),
    ("staffing pages", _tick_staffing_pages, 45),
    ("staffing stable", _tick_staffing_stable, 300),
    ("missing WC", _tick_missing_wc, 180),
    ("missed punch-out", _tick_missed_punch_out, 60),
]


async def _run_warmer(name: str, tick, interval: int):
    """Run `tick` forever, every `interval` seconds; the first run is immediate
    (before the first sleep). Errors are logged and swallowed so a warmer can
    never kill itself."""
    while True:
        try:
            await tick()
        except Exception as e:  # noqa: BLE001 -- a warmer must never die
            _log.warning("%s warmer tick failed: %s", name, e)
        await asyncio.sleep(interval)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialise the Postgres pool, bootstrap schema, then start the background
    warmers. On shutdown, cancel the warmers and shut the pool.

    If ``DATABASE_URL`` is missing or the pool can't be created,
    ``db.init_pool()`` raises with a clear message -- fail fast rather than
    silently fall back to JSON storage.
    """
    db.init_pool()
    db.bootstrap_schema()
    from . import tv_displays_store
    tv_displays_store.seed_defaults_if_empty()
    warmer_tasks = [
        asyncio.create_task(_run_warmer(name, tick, interval))
        for (name, tick, interval) in _WARMERS
    ]
    try:
        yield
    finally:
        for t in warmer_tasks:
            t.cancel()
            try:
                await t
            except asyncio.CancelledError:
                pass
        db.shutdown_pool()


app = FastAPI(
    title="Zira Station Dashboard",
    lifespan=lifespan,
    # No public API docs — this is an internal app; the auto-mounted
    # /docs, /redoc, /openapi.json would expose the route surface to
    # unauthenticated probes (they'd 302 to login per the middleware,
    # but disabling is cleaner than gating).
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)
app.add_middleware(GZipMiddleware, minimum_size=1024, compresslevel=6)
# Authlib's OIDC flow stashes state nonce in request.session (Starlette's
# session backend) for CSRF validation on the callback. Distinct from
# our JWT user-session cookie — this one just carries the OIDC state.
app.add_middleware(
    SessionMiddleware,
    secret_key=os.environ.get("SESSION_SECRET", ""),
    session_cookie="gpi_oidc_state",
    same_site="lax",
    https_only=True,
)

_STATIC_DIR = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")

from . import cert_icons
from .deps import templates

templates.env.globals["cert_icon_svg"] = cert_icons.icon_for
templates.env.globals["cert_icon_slug"] = cert_icons.slug_for
templates.env.globals["cert_icon_data"] = cert_icons.all_data

from . import awards
templates.env.globals["goat_holders"] = awards.goat_holders_map

import calendar as _calendar
templates.env.globals["month_name"] = lambda m: _calendar.month_name[m]


def _static_v(filename: str) -> str:
    """Return a stable cache-busting token (the file's mtime as int)
    so browsers re-fetch when the file changes but cache aggressively
    when it doesn't."""
    try:
        return str(int((_STATIC_DIR / filename).stat().st_mtime))
    except FileNotFoundError:
        return "0"


templates.env.globals["static_v"] = _static_v


@app.middleware("http")
async def _security_headers(request, call_next):
    """Tell browsers to remember this site is HTTPS-only and lock down a
    few common attack surfaces. The HSTS max-age is one year with
    includeSubDomains so www. and any future subdomains inherit. Do not
    add `preload` until this is verified on a stable apex + www setup —
    HSTS preload is hard to undo.

    Also tells search engines: no indexing, no following links, no
    archiving, no snippets. This site is an internal manufacturing tool
    that surfaces employee names + production data — never appropriate
    for public discovery. The X-Robots-Tag header is the authoritative
    signal (Google obeys the most restrictive directive across header +
    meta tag) and applies to every response, including non-HTML.
    """
    response = await call_next(request)
    response.headers.setdefault(
        "Strict-Transport-Security",
        "max-age=31536000; includeSubDomains",
    )
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    response.headers.setdefault(
        "X-Robots-Tag",
        "noindex, nofollow, noarchive, nosnippet",
    )
    return response


@app.get("/robots.txt", include_in_schema=False)
async def _robots_txt():
    """Disallow every crawler from every path. Backstop to the
    X-Robots-Tag header — search engines fetch /robots.txt before
    crawling, and a Disallow rule here stops them before they ever
    request the rest of the site."""
    from fastapi.responses import PlainTextResponse
    return PlainTextResponse("User-agent: *\nDisallow: /\n")


# Auth gate — every request not in the bypass list must have a valid
# session cookie. Device-token support for /tv/* paths is added in a
# follow-up task. AUTH_DISABLED=1 env var short-circuits this gate
# entirely (used in local dev and during the staged production rollout).
from .auth import RequireAuthMiddleware, auth_disabled
app.add_middleware(RequireAuthMiddleware)
if auth_disabled():
    logging.getLogger(__name__).error(
        "AUTH_DISABLED is set — every route is unauthenticated. "
        "Unset this env var to enforce authentication. "
        "(Repeated every 500 requests by RequireAuthMiddleware.)"
    )


@app.middleware("http")
async def _static_cache_headers(request, call_next):
    """Set far-future cache headers on /static/ responses. The
    mtime-versioned URL (?v=<mtime>) makes browsers re-fetch when the
    file changes, so it's safe to cache aggressively when it doesn't."""
    response = await call_next(request)
    if request.url.path.startswith("/static/"):
        response.headers.setdefault(
            "Cache-Control",
            "public, max-age=31536000, immutable",
        )
    return response


# Mount each feature router. URL paths are owned by the routers themselves.
app.include_router(auth_routes.router)
app.include_router(dashboard.router)
app.include_router(departments.router)
app.include_router(wc_dashboard.router)
app.include_router(tv_displays.router)
app.include_router(staffing.router)
app.include_router(late_report.router)
app.include_router(missing_wc.router)
app.include_router(missed_punch_out.router)
app.include_router(share.router)
app.include_router(skills.router)
app.include_router(people.router)
app.include_router(leaderboards.router)
app.include_router(past_schedules.router)
app.include_router(time_off.router)
app.include_router(trophies.router)
app.include_router(settings.router)
app.include_router(api_layout.router)
app.include_router(changelog.router)
app.include_router(admin.router)
app.include_router(goat_watch.router)
app.include_router(timeclock.router)
app.include_router(timeclock_time_off.router)


def main() -> None:
    import uvicorn

    uvicorn.run(
        "zira_dashboard.app:app",
        host=os.environ.get("HOST", "0.0.0.0"),
        port=int(os.environ.get("PORT", "8000")),
        reload=False,
    )
