"""Custom-dashboard pages + CRUD API.

Pages:
  GET /dashboards                       index
  GET /dashboards/{slug}                editor (gridstack enabled, palette visible)
  GET /tv/dashboards/{slug}             TV view (no chrome, TV header on top)

API:
  POST   /api/dashboards                add/update (body {id?, name, scope_kind, scope_value, theme})
  DELETE /api/dashboards/{id}           delete (cascades placements)
  POST   /api/dashboards/{id}/placements   add placement
  PATCH  /api/placements/{id}              update position/overrides
  DELETE /api/placements/{id}              remove placement
  POST   /api/dashboards/{id}/layout    gridstack bulk-save (list of {id, x, y, w, h})
"""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse

from .. import (
    custom_dashboards_store,
    widget_data,
    widget_definitions_store,
    widget_types,
)
from ..deps import templates

router = APIRouter()


@router.get("/dashboards", response_class=HTMLResponse)
def dashboards_index(request: Request):
    from .. import dashboard_catalog
    all_d = dashboard_catalog.all_dashboards()
    builtin = [d for d in all_d if d["kind"] in ("vs_recycling", "vs_new", "wc")]
    custom = [d for d in all_d if d["kind"] == "custom"]
    return templates.TemplateResponse(
        request, "dashboards.html",
        {
            "builtin_dashboards": builtin,
            "custom_dashboards": custom,
            "wcs": _wc_options(),
            "groups": _group_options(),
            "pinned_dashboards": _pinned_for_subnav(),
            "active_dashboard_key": "meta:dashboards",
        },
    )


@router.get("/dashboards/{slug}", response_class=HTMLResponse)
def dashboard_editor(request: Request, slug: str):
    return _render_dashboard(request, slug=slug, tv_mode=False, tv_theme=None)


@router.get("/tv/dashboards/{slug}", response_class=HTMLResponse)
def dashboard_tv(request: Request, slug: str, theme: str | None = None):
    return _render_dashboard(request, slug=slug, tv_mode=True, tv_theme=theme)


def _render_dashboard(request: Request, *, slug: str, tv_mode: bool, tv_theme: str | None):
    dash = custom_dashboards_store.get_dashboard(slug)
    if dash is None:
        return HTMLResponse(
            f"<h1>Dashboard not found: {slug}</h1>"
            f"<p><a href=\"/dashboards\">Back to dashboards</a></p>",
            status_code=404,
        )
    placements = custom_dashboards_store.list_placements(dash["id"])
    today = datetime.now(timezone.utc).date()

    # Resolve each placement's data via its type's resolver.
    for p in placements:
        entry = widget_types.get(p["type"])
        if entry is None:
            p["data"] = {}
            continue
        resolver = getattr(widget_data, entry["resolver"], None)
        if resolver is None:
            p["data"] = {}
            continue
        try:
            p["data"] = resolver(p["effective_data"], day=today) or {}
        except Exception:
            p["data"] = {}

    if tv_mode:
        tv_header_right = _operators_for_scope(dash["scope_kind"], dash["scope_value"], today)
    else:
        tv_header_right = None

    resolved_theme = tv_theme if tv_theme in ("light", "dark") else dash["theme"]

    return templates.TemplateResponse(
        request, "custom_dashboard.html",
        {
            "dashboard": dash,
            "pinned_dashboards": _pinned_for_subnav(),
            "active_dashboard_key": "custom:" + dash["slug"],
            "placements": placements,
            "definitions": widget_definitions_store.list_definitions(),
            "tv_mode": tv_mode,
            "tv_theme": resolved_theme,
            "tv_header_right": tv_header_right,
            "today": today.isoformat(),
        },
    )


def _operators_for_scope(scope_kind: str, scope_value: str, day) -> str:
    from .. import work_centers_store
    from ..wc_dashboard_data import assigned_operators_for_wc
    if scope_kind == "wc":
        ops = assigned_operators_for_wc(scope_value, day)
    elif scope_kind == "group":
        members = work_centers_store.members("group", scope_value) or []
        seen: list[str] = []
        for loc in members:
            for op in assigned_operators_for_wc(loc.name, day):
                if op not in seen:
                    seen.append(op)
        ops = seen
    else:
        ops = []
    return " · ".join(ops) if ops else "(unassigned)"


def _wc_options():
    from .. import staffing
    return [{"name": loc.name} for loc in staffing.LOCATIONS]


def _group_options():
    from .. import work_centers_store
    return [{"name": g} for g in work_centers_store.all_group_names("group")]


@router.post("/api/dashboards")
async def post_dashboard(request: Request):
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "invalid json"}, status_code=400)
    body = body or {}
    name = body.get("name")
    scope_kind = body.get("scope_kind")
    scope_value = body.get("scope_value")
    theme = body.get("theme") or "dark"
    row_id = body.get("id")
    if not isinstance(name, str) or not name.strip():
        return JSONResponse({"ok": False, "error": "name required"}, status_code=400)
    if scope_kind not in ("wc", "group"):
        return JSONResponse({"ok": False, "error": "scope_kind must be wc or group"}, status_code=400)
    if not isinstance(scope_value, str) or not scope_value.strip():
        return JSONResponse({"ok": False, "error": "scope_value required"}, status_code=400)
    if theme not in ("light", "dark"):
        return JSONResponse({"ok": False, "error": "theme must be light or dark"}, status_code=400)
    saved = custom_dashboards_store.save_dashboard(
        name=name.strip(), scope_kind=scope_kind, scope_value=scope_value.strip(),
        theme=theme, id=int(row_id) if row_id is not None else None,
    )
    return JSONResponse({"ok": True, "dashboard": saved})


@router.delete("/api/dashboards/{dashboard_id}")
def delete_dashboard(dashboard_id: int):
    custom_dashboards_store.delete_dashboard(dashboard_id)
    return JSONResponse({"ok": True})


@router.post("/api/dashboards/{dashboard_id}/placements")
async def post_placement(dashboard_id: int, request: Request):
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "invalid json"}, status_code=400)
    body = body or {}
    widget_def_id = body.get("widget_def_id")
    if not isinstance(widget_def_id, int):
        return JSONResponse({"ok": False, "error": "widget_def_id required (int)"}, status_code=400)
    placement = custom_dashboards_store.add_placement(
        dashboard_id=dashboard_id,
        widget_def_id=widget_def_id,
        x=int(body.get("x", 0)),
        y=int(body.get("y", 0)),
        w=int(body.get("w", 4)),
        h=int(body.get("h", 4)),
        data_overrides=body.get("data_overrides") or {},
    )
    return JSONResponse({"ok": True, "placement": placement})


@router.patch("/api/placements/{placement_id}")
async def patch_placement(placement_id: int, request: Request):
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "invalid json"}, status_code=400)
    body = body or {}
    custom_dashboards_store.update_placement(
        placement_id,
        x=body.get("x"),
        y=body.get("y"),
        w=body.get("w"),
        h=body.get("h"),
        data_overrides=body.get("data_overrides"),
    )
    return JSONResponse({"ok": True})


@router.delete("/api/placements/{placement_id}")
def delete_placement(placement_id: int):
    custom_dashboards_store.delete_placement(placement_id)
    return JSONResponse({"ok": True})


@router.post("/api/dashboards/{dashboard_id}/layout")
async def post_layout(dashboard_id: int, request: Request):
    """Bulk-save layout. Body is a list of {id, x, y, w, h}."""
    try:
        items = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "invalid json"}, status_code=400)
    if not isinstance(items, list):
        return JSONResponse({"ok": False, "error": "expected list"}, status_code=400)
    for it in items:
        if not isinstance(it, dict) or "id" not in it:
            continue
        custom_dashboards_store.update_placement(
            int(it["id"]),
            x=it.get("x"), y=it.get("y"), w=it.get("w"), h=it.get("h"),
        )
    return JSONResponse({"ok": True, "count": len(items)})


@router.post("/api/pinned-dashboards")
async def post_pinned_dashboard(request: Request):
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "invalid json"}, status_code=400)
    body = body or {}
    kind = body.get("kind")
    ref = body.get("ref")
    pinned = body.get("pinned")
    if kind not in ("vs_recycling", "vs_new", "vs_work_centers", "wc", "custom"):
        return JSONResponse({"ok": False, "error": "invalid kind"}, status_code=400)
    if not isinstance(ref, str):
        return JSONResponse({"ok": False, "error": "ref must be string"}, status_code=400)
    if not isinstance(pinned, bool):
        return JSONResponse({"ok": False, "error": "pinned must be bool"}, status_code=400)
    if kind == "wc":
        from .. import staffing
        if not any(loc.name == ref for loc in staffing.LOCATIONS):
            return JSONResponse(
                {"ok": False, "error": f"unknown work center: {ref}"},
                status_code=400,
            )
    if kind == "custom":
        if custom_dashboards_store.get_dashboard(ref) is None:
            return JSONResponse(
                {"ok": False, "error": f"unknown custom dashboard slug: {ref}"},
                status_code=400,
            )
    from .. import pinned_dashboards_store
    if pinned:
        pinned_dashboards_store.pin(kind, ref)
    else:
        pinned_dashboards_store.unpin(kind, ref)
    return JSONResponse({"ok": True, "pinned": pinned})


def _pinned_for_subnav():
    from .. import dashboard_catalog
    return dashboard_catalog.pinned_dashboards_for_subnav()
