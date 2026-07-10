"""HTTP routes for the TV display registry.

  GET    /tv/{slug}                         resolve display -> dispatch
  GET    /tv/d/{slug}                       legacy alias -> 302 to /tv/{slug}
  POST   /api/tv-displays                   add/update
  POST   /api/tv-displays/{id}/theme        theme toggle
  DELETE /api/tv-displays/{id}              delete
"""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from .. import tv_displays_store
from ..wc_dashboard_data import slug_for_wc

router = APIRouter()


@router.get("/tv/d/{slug}")
def tv_display_legacy(request: Request, slug: str):
    """Backward-compat redirect for the old /tv/d/{slug} URL pattern.

    Preserves the query string (e.g. ?theme=light) so older TVs that
    bookmarked the long URL keep working without manual reconfiguration.
    """
    qs = f"?{request.url.query}" if request.url.query else ""
    return RedirectResponse(url=f"/tv/{slug}{qs}", status_code=302)


@router.get("/tv/{slug}", response_class=HTMLResponse)
def tv_display(request: Request, slug: str, theme: str | None = Query(default=None)):
    row = tv_displays_store.by_slug(slug)
    if row is None:
        return HTMLResponse(
            _not_configured_html(slug),
            status_code=404,
        )
    tv_theme = "light" if theme == "light" else ("dark" if theme == "dark" else row["theme"])
    kind = row["kind"]
    if kind == "vs_recycling":
        from .departments import _render_recycling
        return _render_recycling(
            request, window="today", start=None, end=None,
            tv_mode=True, tv_theme=tv_theme,
        )
    if kind == "vs_new":
        from .departments import _render_new_dept
        return _render_new_dept(
            request, day=None, tv_mode=True, tv_theme=tv_theme,
        )
    if kind == "vs_recycling_leaderboard":
        from . import recycling_leaderboard
        return recycling_leaderboard.render_recycling_leaderboard_tv(
            request,
            tv_theme=tv_theme,
        )
    if kind == "wc":
        from .. import staffing
        wc_name = row["wc_name"]
        valid = any(loc.name == wc_name for loc in staffing.LOCATIONS)
        if not valid:
            return HTMLResponse(
                _wc_removed_html(row["name"], wc_name),
                status_code=404,
            )
        from .wc_dashboard import _dashboard_day, _render_wc_dashboard
        return _render_wc_dashboard(
            request, slug=slug_for_wc(wc_name), day=_dashboard_day(None),
            tv_mode=True, tv_theme=tv_theme,
        )
    return JSONResponse(
        {"error": f"unknown kind: {kind}"}, status_code=500,
    )


def _not_configured_html(slug: str) -> str:
    return (
        f"<!doctype html><html><head><title>Display not configured</title>"
        f"<style>body{{font-family:system-ui;padding:3rem;text-align:center}}"
        f"a{{color:#16a34a}}</style></head><body>"
        f"<h1>Display \"{slug}\" isn't configured</h1>"
        f"<p>Add it on the <a href=\"/settings?section=tvs\">TVs settings page</a>.</p>"
        f"</body></html>"
    )


def _wc_removed_html(display_name: str, wc_name: str | None) -> str:
    return (
        f"<!doctype html><html><head><title>Work center removed</title>"
        f"<style>body{{font-family:system-ui;padding:3rem;text-align:center}}"
        f"a{{color:#16a34a}}</style></head><body>"
        f"<h1>Work center removed</h1>"
        f"<p>The display \"{display_name}\" was pointing at \"{wc_name}\", which is no longer in Settings.</p>"
        f"<p><a href=\"/settings?section=tvs\">Go to TVs settings</a></p>"
        f"</body></html>"
    )


@router.post("/api/tv-displays")
async def post_display(request: Request):
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "invalid json"}, status_code=400)
    body = body or {}
    name = body.get("name")
    kind = body.get("kind")
    wc_name = body.get("wc_name") or None
    theme = body.get("theme") or "dark"
    row_id = body.get("id")
    if not isinstance(name, str) or not name.strip():
        return JSONResponse({"ok": False, "error": "name required"}, status_code=400)
    if kind not in ("vs_recycling", "vs_new", "vs_recycling_leaderboard", "wc"):
        return JSONResponse({"ok": False, "error": "kind invalid"}, status_code=400)
    if kind == "wc":
        from .. import staffing
        if not isinstance(wc_name, str) or not wc_name.strip():
            return JSONResponse({"ok": False, "error": "wc_name required when kind=wc"}, status_code=400)
        if not any(loc.name == wc_name for loc in staffing.LOCATIONS):
            return JSONResponse({"ok": False, "error": f"unknown work center: {wc_name}"}, status_code=400)
    else:
        wc_name = None
    if theme not in ("light", "dark"):
        return JSONResponse({"ok": False, "error": "theme must be light or dark"}, status_code=400)
    saved = await asyncio.to_thread(
        tv_displays_store.save,
        name=name.strip(), kind=kind, wc_name=wc_name, theme=theme,
        id=int(row_id) if row_id is not None else None,
    )
    return JSONResponse({
        "ok": True,
        "id": saved["id"],
        "slug": saved["slug"],
        "url": f"/tv/{saved['slug']}",
    })


@router.post("/api/tv-displays/{display_id}/theme")
async def post_theme(display_id: int, request: Request):
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "invalid json"}, status_code=400)
    theme = (body or {}).get("theme")
    if theme not in ("light", "dark"):
        return JSONResponse({"ok": False, "error": "theme must be light or dark"}, status_code=400)
    await asyncio.to_thread(tv_displays_store.set_theme, display_id, theme)
    return JSONResponse({"ok": True})


@router.delete("/api/tv-displays/{display_id}")
def delete_display(display_id: int):
    tv_displays_store.delete(display_id)
    return JSONResponse({"ok": True})
