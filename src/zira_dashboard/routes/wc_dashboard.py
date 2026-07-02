"""Operator dashboard routes.

  /wc/{slug}        editor view (gridstack enabled, WC picker visible)
  /tv/wc/{slug}     TV view (chrome stripped, picker hidden)
  /operator         redirect to the first WC's /wc/{slug}

The /wc/{slug} dashboard mirrors /recycling's visual style — same CSS
classes, same widget markup — scoped to a single WC. A picker at the
top lets the user switch which WC. Layout + per-widget customizations
are shared across every WC under page='operator'.
"""
from __future__ import annotations

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from .. import (
    layout_store,
    shift_config,
    wc_dashboard_data,
    widget_customizer,
    work_centers_store,
)
from ..deps import templates
from ..plant_day import today as plant_today, now as plant_now

router = APIRouter()


def _shift_start_label(day) -> str:
    """`HH:MM` for the day's shift start, or "" if unavailable."""
    try:
        t = shift_config.shift_start_for(day)
    except Exception:
        return ""
    return f"{t.hour:02d}:{t.minute:02d}"


def _now_label(day) -> str:
    """Current local time `HH:MM` if `day` is today (in SITE_TZ); empty otherwise."""
    today_local = plant_today()
    if day != today_local:
        return ""
    now_local = plant_now()
    return f"{now_local.hour:02d}:{now_local.minute:02d}"


def _render_wc_dashboard(
    request: Request,
    *,
    slug: str,
    tv_mode: bool,
    tv_theme: str,
):
    """Render the Operator dashboard for one WC."""
    from .. import staffing
    loc = wc_dashboard_data.wc_by_slug(slug)
    if loc is None:
        return JSONResponse({"error": f"no work center matches slug {slug!r}"}, status_code=404)

    today = plant_today()

    # Server-side HTML response cache — 15s on today's data, same pattern
    # /recycling uses. TVs auto-refresh every 30-60s so this is the
    # difference between rebuilding the full page on every refresh vs.
    # serving cached bytes from RAM. `today` in the key keeps the cache
    # day-boundary-safe.
    from .._http_cache import get_cached_response, set_cache_headers, store_cached_response
    cache_key = ("wc_dashboard", slug, today.isoformat(), tv_mode, tv_theme)
    cached = get_cached_response(cache_key, includes_today=True)
    if cached is not None:
        return cached

    wc_name = loc.name
    operators = wc_dashboard_data.assigned_operators_for_wc(wc_name, today)
    operators_display = " · ".join(operators)
    groups = work_centers_store.groups(loc) or []
    wc_group = groups[0] if groups else None

    pallets = wc_dashboard_data.pallets_banner(wc_name, today)
    # Nobody scheduled AND nothing produced yet → a calm "not staffed" view,
    # not red zeros and a "-246 BEHIND" GOAT delta against an empty station.
    no_activity = not operators and int(pallets.get("units_today") or 0) == 0
    progress = wc_dashboard_data.fifteen_min_progress_buckets(wc_name, today)
    kpi = wc_dashboard_data.kpi_tiles(wc_name, today)
    report = wc_dashboard_data.downtime_report(wc_name, today) or {}
    down_min = int(report.get("total_minutes", 0))
    elapsed_min = int(kpi["hours_elapsed"] * 60)
    working_min = max(0, elapsed_min - down_min)
    denom = elapsed_min if elapsed_min else 1
    downtime_row = {
        "name": wc_name,
        "who": operators_display or None,
        "working": working_min,
        "down": down_min,
        "working_pct": working_min / denom * 100.0,
        "down_pct": down_min / denom * 100.0,
    }
    goat = wc_dashboard_data.goat_race(wc_name, today) if wc_group else None
    ribbons = wc_dashboard_data.monthly_ribbons(wc_name, today.year, today.month) if wc_group else None

    layout_key = "operator"

    # Pallets-banner axis-tick position: prorated target as % of full-day goal.
    full_day = int(pallets.get("target_full_day") or 0)
    today_target = int(pallets.get("target_today") or 0)
    banner_now_pct = (today_target / full_day * 100.0) if full_day > 0 else 0.0

    response = templates.TemplateResponse(
        request,
        "wc_dashboard.html",
        {
            "slug": slug,
            "wc_name": wc_name,
            "wc_group": wc_group,
            "operators": operators,
            "operators_display": operators_display,
            "no_activity": no_activity,
            "today": today.isoformat(),
            "year": today.year,
            "month": today.month,
            "wc_options": [
                {"name": l.name, "slug": wc_dashboard_data.slug_for_wc(l.name)}
                for l in staffing.LOCATIONS
            ],
            "pallets": pallets,
            "progress_buckets": progress["buckets"],
            "progress_bucket_target": progress["bucket_target"],
            "kpi": kpi,
            "downtime_row": downtime_row,
            "downtime_elapsed_minutes": elapsed_min,
            "goat_race": goat,
            "ribbons": ribbons,
            "active_dashboard_key": "wc:" + wc_name,
            "layout": layout_store.layout_map(layout_key),
            "layout_key": layout_key,
            "customs": widget_customizer.load_all(layout_key),
            "shift_start_label": _shift_start_label(today),
            "now_label": _now_label(today),
            "banner_now_pct": banner_now_pct,
            "tv_mode": tv_mode,
            "tv_theme": tv_theme,
            # NEW GOAT alerts surface on every dashboard so a record-breaker
            # is celebrated plant-wide. Live contenders stay on /recycling
            # only — they're a per-group projection, not a per-WC stat.
            "goat_alerts_active": _goat_watch_active_alerts(today),
            "goat_contenders": [],
        },
    )
    set_cache_headers(response, includes_today=True)
    store_cached_response(cache_key, includes_today=True, response=response)
    return response


def _goat_watch_active_alerts(today):
    try:
        from .. import goat_watch
        return goat_watch.active_alerts(today)
    except Exception:
        return []


@router.get("/wc/{slug}", response_class=HTMLResponse)
def wc_dashboard(request: Request, slug: str):
    return _render_wc_dashboard(request, slug=slug, tv_mode=False, tv_theme="dark")


@router.get("/tv/wc/{slug}", response_class=HTMLResponse)
def tv_wc_dashboard(
    request: Request,
    slug: str,
    theme: str | None = Query(default=None),
):
    tv_theme = "light" if theme == "light" else "dark"
    return _render_wc_dashboard(request, slug=slug, tv_mode=True, tv_theme=tv_theme)


@router.get("/operator")
def operator_default():
    """Entry point for the Operator dashboard sub-tab.

    Redirects to the first work center's /wc/{slug} URL. Order is
    staffing.LOCATIONS order — usually alphabetical by name.
    """
    from .. import staffing
    if not staffing.LOCATIONS:
        return JSONResponse(
            {"error": "no work centers configured — set them up in Settings"},
            status_code=404,
        )
    # Prefer the first WC that actually has someone scheduled today, so the
    # operator doesn't land on an empty "(unassigned)" station by default.
    # Fall back to the first WC when nobody is scheduled anywhere yet.
    today = plant_today()
    target = next(
        (l for l in staffing.LOCATIONS
         if wc_dashboard_data.assigned_operators_for_wc(l.name, today)),
        staffing.LOCATIONS[0],
    )
    return RedirectResponse(url=f"/wc/{wc_dashboard_data.slug_for_wc(target.name)}", status_code=302)
