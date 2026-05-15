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

from . import db
from .routes import (
    admin,
    api_layout,
    changelog,
    dashboard,
    goat_watch,
    late_report,
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
    value_streams,
    wc_dashboard,
)


_log = logging.getLogger(__name__)
_WARMER_INTERVAL_SECONDS = 30


async def _warm_stratustime_loop():
    """Re-warm today's StratusTime caches every ~3 minutes so we never
    pay the cold-cache penalty mid-shift. The underlying caches expire
    at 5 min (employee directory, time-off requests) or 60 s (attendance,
    GetUserSchedule); ticking at 3 min keeps everything fresh.

    Errors are logged and swallowed."""
    from datetime import datetime as _dt, timezone as _tz
    from . import stratustime_client
    while True:
        try:
            today = _dt.now(_tz.utc).date()
            await asyncio.to_thread(stratustime_client._employee_id_to_name_map)
            await asyncio.to_thread(stratustime_client.name_to_emp_id_map)
            await asyncio.to_thread(stratustime_client.time_off_entries_for_day, today)
        except Exception as e:  # noqa: BLE001 — never let warmer kill itself
            _log.warning("StratusTime warmer tick failed: %s", e)
        await asyncio.sleep(180)  # 3 min — well under the 5 min cache TTL


async def _warm_live_cache_loop():
    """Refresh today's attendance, time-off, and production into the
    live_cache tables every 45 s. Each source is wrapped independently so
    one outage doesn't block the others. The loop itself never raises —
    a hard failure logs and the next tick tries again.

    The production refresh also UPSERTs today's `production_daily` rows
    so MTD / today leaderboards see today's partial-day data without a
    separate query path."""
    from . import live_cache
    while True:
        try:
            today = datetime.now(timezone.utc).date()
            await asyncio.to_thread(live_cache.refresh_attendance, today)
            await asyncio.to_thread(live_cache.refresh_timeoff, today)
            await asyncio.to_thread(
                live_cache.refresh_production, today, _zira_client()
            )
        except Exception as e:  # noqa: BLE001 — warmer must never die
            _log.warning("live_cache warmer tick failed: %s", e)
        await asyncio.sleep(45)


async def _warm_zira_cache_loop():
    """Periodically warm today's Zira leaderboard cache so user
    requests on /recycling never pay the cold-cache penalty.

    Runs every 30s. Each tick re-fetches today's leaderboard via
    the existing cached_leaderboard helper, which writes through
    the in-process TODAY cache. Past days are not touched here —
    they're cached forever-ish on first read.

    Errors are logged and swallowed; one bad Zira call shouldn't
    kill the warmer."""
    from .leaderboard import cached_leaderboard
    from .stations import recycling_stations
    while True:
        try:
            stations = recycling_stations()
            if stations:
                today = datetime.now(timezone.utc).date()
                now_utc = datetime.now(timezone.utc)
                # Run the (sync, blocking) leaderboard fetch off the event
                # loop so we don't stall request handling.
                await asyncio.to_thread(
                    cached_leaderboard,
                    _zira_client(),
                    stations,
                    today,
                    now_utc,
                )
        except Exception as e:  # noqa: BLE001 — never let warmer kill itself
            _log.warning("Zira warmer tick failed: %s", e)
        await asyncio.sleep(_WARMER_INTERVAL_SECONDS)


def _zira_client():
    """Lazy import of the shared Zira client to avoid import cycles."""
    from .deps import client
    return client


def _prewarm_stratustime() -> None:
    """Fire StratusTime token + employee directory once on app boot.

    The first /staffing request after a Railway redeploy otherwise pays
    for the cold-cache walk (token CreateToken + GetUserBasic SELECT-ALL).
    Doing it on a daemon thread at startup means the first user gets a
    warm cache. Wrapped in try/except — a StratusTime outage at boot
    must never crash the app.
    """
    import threading

    def _warm() -> None:
        try:
            from datetime import datetime as _dt, timezone as _tz
            from . import stratustime_client
            # Warm employee directory + the two name maps that derive from it.
            stratustime_client._employee_id_to_name_map()
            stratustime_client.name_to_emp_id_map()
            # Warm today's time-off-entries chain (requests, non-work shifts,
            # derived absences). This is what /staffing and /api/late-report
            # ultimately gate on, so the first user gets a warm cache.
            today = _dt.now(_tz.utc).date()
            stratustime_client.time_off_entries_for_day(today)
        except Exception as e:  # noqa: BLE001 — pre-warm must never bubble
            _log.warning("StratusTime pre-warm failed: %s", e)

    threading.Thread(target=_warm, daemon=True, name="stratustime-prewarm").start()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialise the Postgres pool, start the Zira cache warmer,
    bootstrap schema. On shutdown, cancel the warmer and shut the pool.

    If ``DATABASE_URL`` is missing or the pool can't be created,
    ``db.init_pool()`` raises with a clear message — fail fast rather than
    silently fall back to JSON storage.
    """
    db.init_pool()
    db.bootstrap_schema()
    from . import tv_displays_store
    tv_displays_store.seed_defaults_if_empty()
    _prewarm_stratustime()
    warmer_task = asyncio.create_task(_warm_zira_cache_loop())
    st_warmer_task = asyncio.create_task(_warm_stratustime_loop())
    live_cache_task = asyncio.create_task(_warm_live_cache_loop())
    try:
        yield
    finally:
        for t in (warmer_task, st_warmer_task, live_cache_task):
            t.cancel()
            try:
                await t
            except asyncio.CancelledError:
                pass
        db.shutdown_pool()


app = FastAPI(title="Zira Station Dashboard", lifespan=lifespan)
app.add_middleware(GZipMiddleware, minimum_size=1024, compresslevel=6)

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
    HSTS preload is hard to undo."""
    response = await call_next(request)
    response.headers.setdefault(
        "Strict-Transport-Security",
        "max-age=31536000; includeSubDomains",
    )
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    return response


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
app.include_router(dashboard.router)
app.include_router(value_streams.router)
app.include_router(wc_dashboard.router)
app.include_router(tv_displays.router)
app.include_router(staffing.router)
app.include_router(late_report.router)
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


def main() -> None:
    import uvicorn

    uvicorn.run(
        "zira_dashboard.app:app",
        host=os.environ.get("HOST", "0.0.0.0"),
        port=int(os.environ.get("PORT", "8000")),
        reload=False,
    )
