"""Per-WC dashboard routes.

  /wc/{slug}        editor view (gridstack enabled, autosave on)
  /tv/wc/{slug}     TV view (read-only, no chrome, theme via ?theme=)

Both delegate to a single `_render_wc_dashboard` helper that composes
the data prep from `wc_dashboard_data`, looks up the saved widget
layout, and renders `wc_dashboard.html`. The helper owns the per-WC
slug lookup and the response context build.
"""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse

from .. import layout_store, wc_dashboard_data, work_centers_store
from ..deps import templates

router = APIRouter()


def _render_wc_dashboard(
    request: Request,
    *,
    slug: str,
    tv_mode: bool,
    tv_theme: str,
):
    """Shared implementation for the editor + TV routes."""
    loc = wc_dashboard_data.wc_by_slug(slug)
    if loc is None:
        return JSONResponse({"error": f"no work center matches slug {slug!r}"}, status_code=404)

    today = datetime.now(timezone.utc).date()
    wc_name = loc.name
    operators = wc_dashboard_data.assigned_operators_for_wc(wc_name, today)
    operators_display = " · ".join(operators)
    groups = work_centers_store.groups(loc) or []
    wc_group = groups[0] if groups else None

    pallets = wc_dashboard_data.pallets_banner(wc_name, today)
    daily_progress = wc_dashboard_data.daily_progress(wc_name, today)
    goat_race = wc_dashboard_data.goat_race(wc_name, today)
    ribbons = wc_dashboard_data.monthly_ribbons(wc_name, today.year, today.month)
    fifteen_min = wc_dashboard_data.fifteen_min_increments(wc_name, today)
    downtime = wc_dashboard_data.downtime_report(wc_name, today)

    layout_key = f"wc:{slug}"

    return templates.TemplateResponse(
        request,
        "wc_dashboard.html",
        {
            "slug": slug,
            "wc_name": wc_name,
            "pinned_dashboards": _pinned_for_subnav(),
            "active_dashboard_key": "wc:" + wc_name,
            "wc_group": wc_group,
            "operators": operators,
            "operators_display": operators_display,
            "today": today.isoformat(),
            "year": today.year,
            "month": today.month,
            "pallets": pallets,
            "daily_progress": daily_progress,
            "goat_race": goat_race,
            "ribbons": ribbons,
            "fifteen_min": fifteen_min,
            "downtime": downtime,
            "layout": layout_store.layout_map(layout_key),
            "layout_key": layout_key,
            "tv_mode": tv_mode,
            "tv_theme": tv_theme,
        },
    )


@router.get("/wc/{slug}", response_class=HTMLResponse)
def wc_dashboard(request: Request, slug: str):
    """Per-WC dashboard editor view. Drag / resize widgets; layout
    autosaves to `widget_layouts.page = 'wc:{slug}'`."""
    return _render_wc_dashboard(
        request,
        slug=slug,
        tv_mode=False,
        tv_theme="dark",
    )


@router.get("/tv/wc/{slug}", response_class=HTMLResponse)
def tv_wc_dashboard(
    request: Request,
    slug: str,
    theme: str | None = Query(default=None),
):
    """Per-WC TV view. Same widgets, no chrome, no drag, auto-refresh.
    `?theme=light` overrides the default dark.
    """
    tv_theme = "light" if theme == "light" else "dark"
    return _render_wc_dashboard(
        request,
        slug=slug,
        tv_mode=True,
        tv_theme=tv_theme,
    )


def _pinned_for_subnav():
    from .. import dashboard_catalog
    return dashboard_catalog.pinned_dashboards_for_subnav()
