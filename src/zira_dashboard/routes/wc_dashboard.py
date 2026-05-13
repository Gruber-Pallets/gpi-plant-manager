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

from .. import layout_store, wc_dashboard_data, widget_data, work_centers_store
from ..deps import templates

router = APIRouter()


def _render_wc_dashboard(
    request: Request,
    *,
    slug: str,
    tv_mode: bool,
    tv_theme: str,
):
    """Shared implementation for the editor + TV routes.

    Widgets are rendered via the workshop's widget partials, so the
    per-WC dashboard looks identical to /recycling (same markup, same
    CSS classes). Scope per widget:
      - pallets banner / 15-min progress / cumulative / downtime → this WC only
      - GOAT race / monthly ribbons → this WC's group
    """
    loc = wc_dashboard_data.wc_by_slug(slug)
    if loc is None:
        return JSONResponse({"error": f"no work center matches slug {slug!r}"}, status_code=404)

    today = datetime.now(timezone.utc).date()
    wc_name = loc.name
    operators = wc_dashboard_data.assigned_operators_for_wc(wc_name, today)
    operators_display = " · ".join(operators)
    groups = work_centers_store.groups(loc) or []
    wc_group = groups[0] if groups else None

    wc_scope = {"wcs": [wc_name]}
    group_scope = {"group": wc_group} if wc_group else {}

    widgets_data = {
        "pallets_banner": wc_dashboard_data.pallets_banner(wc_name, today),
        "fifteen_min": widget_data._resolve_daily_progress(wc_scope, day=today),
        "cumulative":   widget_data._resolve_cumulative(wc_scope, day=today),
        "goat_race":    widget_data._resolve_goat_race(group_scope, day=today) if wc_group else None,
        "ribbons":      widget_data._resolve_ribbons(group_scope, day=today) if wc_group else None,
        "downtime":     widget_data._resolve_downtime(wc_scope, day=today),
    }

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
            "widgets": widgets_data,
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
