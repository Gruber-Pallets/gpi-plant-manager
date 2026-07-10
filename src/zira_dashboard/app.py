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
from datetime import datetime, UTC
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from . import db
from . import page_views
from .plant_day import today as plant_today
from .routes import (
    admin,
    api_layout,
    auth as auth_routes,
    changelog,
    dashboard,
    exceptions,
    feedback,
    forklift_leaderboards,
    goat_watch,
    new_leaderboard,
    recycling_leaderboard,
    timeclock,
    timeclock_time_off,
    late_report,
    missing_wc,
    missed_punch_out,
    leaderboards,
    object_api,
    past_schedules,
    people,
    rotations,
    settings,
    share,
    skills,
    staffing,
    time_off,
    time_off_approvals,
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
        today = plant_today()
        now_utc = datetime.now(UTC)
        # Run the (sync, blocking) leaderboard fetch off the event loop.
        await asyncio.to_thread(
            cached_leaderboard, _zira_client(), stations, today, now_utc
        )


async def _tick_live_cache():
    """Refresh today's attendance into live_cache and UPSERT today's
    production_daily rows (so MTD / today leaderboards see partial-day data)."""
    from . import live_cache
    today = plant_today()
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
    """Keep the staffing day view pre-rendered in the response cache so the
    first human load (including the first after a Railway deploy) is a warm hit.
    (Leaderboards were dropped from this tick 2026-07-07 — see warm_once.)"""
    from . import page_warmer
    await asyncio.to_thread(page_warmer.warm_once)


async def _tick_inbox():
    """Keep the inbox top-nav sub-caches warm. build_summary() renders the
    Inbox badge on every page (_topnav.html); its expensive sub-sources self-
    cache for 30 s but the TTL doesn't slide on hits, so a human hitting any
    page after the cache lapses pays the full cold Zira/Odoo cascade. Refresh
    below that TTL (interval 20 s) so the badge is always served warm."""
    from . import page_warmer
    await asyncio.to_thread(page_warmer.warm_inbox_once)


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
    since = datetime.now(UTC) - timedelta(days=14)
    rows = await asyncio.to_thread(odoo_client.fetch_attendances_missing_wc, since)
    await asyncio.to_thread(missing_wc.write_cache, rows)


async def _tick_missed_punch_out():
    """Close any attendance still open from a prior day at that day's midnight
    and flag it for the Missed-Punch-Out alert. Cadence doesn't affect the close
    time (it's computed from the check-in day, not 'now')."""
    from . import missed_punch_out, shift_config
    today = datetime.now(shift_config.SITE_TZ).date()
    await asyncio.to_thread(missed_punch_out.run_close, today)


# Run the one-time full-history backfill until we have at least this many days
# of demand snapshots; after that, each tick just refreshes today.
_FORKLIFT_MIN_HISTORY_DAYS = 14

# When fewer than this many days carry on-time/utilization data, the warmer
# self-heals the history by reconstructing it (~90 API calls) once per process.
_FORKLIFT_ONTIME_MIN_DAYS = 14
_forklift_ontime_reconstructed = False


async def _tick_forklift():
    """Keep forklift demand/performance snapshots fresh. On the first run(s)
    after deploy — while our stored history is sparse — pull the FULL history
    from the external completions API; once enough days are stored, just refresh
    today. No-ops gracefully (logs+swallows via _run_warmer; degrades to no data
    if FORKLIFT_API_KEY isn't set). Runs off the event loop (blocking HTTP)."""
    from . import forklift_backfill, forklift_snapshot, forklift_store
    try:
        days = await asyncio.to_thread(forklift_store.history_day_count)
    except Exception:
        days = _FORKLIFT_MIN_HISTORY_DAYS  # can't tell -> just refresh today
    if days < _FORKLIFT_MIN_HISTORY_DAYS:
        result = await asyncio.to_thread(forklift_backfill.backfill_history, None, 0)
        _log.warning("forklift warmer: backfill (had %d days) -> %s", days, result)
    else:
        result = await asyncio.to_thread(forklift_snapshot.snapshot_today, None, plant_today())
        _log.warning("forklift warmer: snapshot today (history=%d days) -> %s", days, result)
        await asyncio.to_thread(_capture_forklift_ontime)
        await _maybe_reconstruct_ontime()


async def _maybe_reconstruct_ontime() -> None:
    """Self-heal the on-time/utilization history once per process when it's
    sparse, so nobody has to run scripts/backfill_forklift_ontime.py by hand.
    Mirrors the Stage-1 self-backfill above, but for the on-time columns the
    completions feed can't supply. Runs the ~90-call reconstruction off the
    event loop. Best-effort: the guard flag flips in `finally` so a transient
    source failure won't re-trigger it every tick — it retries next process."""
    global _forklift_ontime_reconstructed
    from . import forklift_backfill, forklift_store
    if _forklift_ontime_reconstructed:
        return
    try:
        days = await asyncio.to_thread(forklift_store.ontime_history_day_count)
    except Exception:
        days = _FORKLIFT_ONTIME_MIN_DAYS  # can't tell -> assume fine, skip
    if days >= _FORKLIFT_ONTIME_MIN_DAYS:
        return
    try:
        result = await asyncio.to_thread(forklift_backfill.reconstruct_ontime_history)
        _log.warning(
            "forklift warmer: reconstructed on-time history (had %d days) -> %s",
            days, result,
        )
    except Exception as exc:  # noqa: BLE001 - best-effort, never fatal
        _log.warning("forklift warmer: on-time reconstruction failed: %s", exc)
    finally:
        _forklift_ontime_reconstructed = True


def _capture_forklift_ontime() -> None:
    """Forward-capture today's on-time/utilization from the dashboard endpoint
    into today's forklift_driver_daily row (the completions snapshot can't
    supply these). Best-effort: logs and swallows; never raises into the warmer.
    Leaves calls/avg_ms/max_ms (owned by snapshot_today) untouched."""
    from . import (
        forklift_client,
        forklift_ingest,
        forklift_snapshot,
        forklift_store,
    )
    try:
        today = plant_today()
        start_ms = forklift_snapshot.day_start_ms(today)
        dash = forklift_client.fetch_dashboard(since=start_ms)
        id_to_name = {str(d.get("id")): d.get("name")
                      for d in (forklift_client.fetch_drivers() or [])
                      if d.get("id") is not None}
        metric_rows = forklift_ingest.driver_metrics_from_dashboard(dash, id_to_name)
        for r in metric_rows:
            r["day"] = today
        n = forklift_store.upsert_driver_metrics(metric_rows)
        _log.warning("forklift warmer: captured on-time metrics -> %d drivers", n)
    except Exception as exc:  # noqa: BLE001 - best-effort, never fatal
        _log.warning("forklift warmer: on-time capture failed: %s", exc)


async def _tick_inbox_reconcile():
    """Log auto_resolved for inbox items that cleared themselves and refresh the
    open-items mirror. Skips categories whose source errored this tick, so a
    transient Odoo hiccup never mass-logs false resolutions."""
    from . import inbox_reconcile
    await asyncio.to_thread(inbox_reconcile.run_once)


async def _tick_machine_breakdown():
    """Detect newly-broken recycling machines, cap operators who've left a
    broken machine, and auto-resolve incidents whose machine resumed. Runs
    off the same cadence as the production-data warmer since it reads the
    same Zira station data."""
    from . import machine_breakdown
    await asyncio.to_thread(machine_breakdown.run_detect_tick)


async def _tick_page_usage():
    """Drain the in-memory page-view counter to Postgres in one batched upsert.
    Keeps per-request cost at a dict increment; this is the only DB work the
    feature does."""
    from . import page_views
    await asyncio.to_thread(page_views.flush)


async def _tick_calendar_conflicts():
    """Weekly Odoo calendar-conflict check. Interval is short; run_once()
    self-throttles to ~weekly via its persisted last_run_at gate."""
    from . import calendar_conflict_monitor
    await asyncio.to_thread(calendar_conflict_monitor.run_once)


async def _tick_time_off_backfill():
    """Replay locally-recorded absences (local_record rows) into Odoo once
    Odoo will accept them — e.g. after HR scopes a holiday record or fixes
    a Working Schedule. No-ops (zero RPCs) while no local records exist."""
    from . import time_off_local_backfill
    await asyncio.to_thread(time_off_local_backfill.run_once)


# (name, tick coroutine, interval seconds). `name` is used only in the
# "warmer tick failed" log line. Intervals are unchanged from the original
# per-loop functions this registry replaced.
_WARMERS = [
    ("Zira cache", _tick_zira_cache, 30),
    ("live_cache", _tick_live_cache, 45),
    ("machine breakdown", _tick_machine_breakdown, 45),
    ("kiosk sync", _tick_timeclock_sync, 60),
    ("Odoo open-attendance", _tick_odoo_attendance, 30),
    ("auto-lunch", _tick_auto_lunch, 60),
    ("time-off sync", _tick_time_off_sync, 60),
    ("time-off poll", _tick_time_off_poll, 60),
    ("time-off balance", _tick_time_off_balance, 600),
    ("staffing pages", _tick_staffing_pages, 45),
    ("inbox warm", _tick_inbox, 20),
    ("staffing stable", _tick_staffing_stable, 300),
    ("missing WC", _tick_missing_wc, 180),
    ("missed punch-out", _tick_missed_punch_out, 60),
    ("forklift snapshot", _tick_forklift, 600),
    ("Inbox reconcile", _tick_inbox_reconcile, 60),
    ("calendar conflicts", _tick_calendar_conflicts, 21600),
    ("time-off local backfill", _tick_time_off_backfill, 3600),
    ("page-usage flush", _tick_page_usage, 60),
]


async def _run_warmer(name: str, tick, interval: int, stagger: float = 0):
    """Run `tick` forever, every `interval` seconds; the first run happens
    right after the (short) `stagger` sleep, before the first long sleep.
    The stagger offsets each warmer's start so they don't all fire at boot
    and re-collide on shared interval multiples. Errors are logged and
    swallowed so a warmer can never kill itself."""
    if stagger:
        await asyncio.sleep(stagger)
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
        asyncio.create_task(_run_warmer(name, tick, interval, stagger=index * 2))
        for index, (name, tick, interval) in enumerate(_WARMERS)
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
async def _security_and_cache_headers(request, call_next):
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

    Also sets far-future cache headers on /static/ responses. The
    mtime-versioned URL (?v=<mtime>) makes browsers re-fetch when the
    file changes, so it's safe to cache aggressively when it doesn't.
    (Single middleware for all response headers — each BaseHTTPMiddleware
    layer adds per-request overhead.)
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
    if request.url.path.startswith("/static/"):
        response.headers.setdefault(
            "Cache-Control",
            "public, max-age=31536000, immutable",
        )
    # Page-usage tracking rides this same middleware (no extra layer, per the
    # note above). Cost is a dict increment; it never raises into the response.
    page_views.record_view(request)
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


# Mount each feature router. URL paths are owned by the routers themselves.
app.include_router(auth_routes.router)
app.include_router(dashboard.router)
app.include_router(exceptions.router)
app.include_router(departments.router)
app.include_router(wc_dashboard.router)
app.include_router(recycling_leaderboard.router)
app.include_router(new_leaderboard.router)
app.include_router(tv_displays.router)
app.include_router(staffing.router)
app.include_router(rotations.router)
app.include_router(late_report.router)
app.include_router(missing_wc.router)
app.include_router(missed_punch_out.router)
app.include_router(share.router)
app.include_router(skills.router)
app.include_router(people.router)
app.include_router(leaderboards.router)
app.include_router(forklift_leaderboards.router)
app.include_router(past_schedules.router)
app.include_router(time_off.router)
app.include_router(time_off_approvals.router)
app.include_router(trophies.router)
app.include_router(settings.router)
app.include_router(object_api.router)
app.include_router(api_layout.router)
app.include_router(changelog.router)
app.include_router(feedback.router)
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
