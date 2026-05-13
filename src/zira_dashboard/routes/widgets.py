"""Widget Workshop routes.

Pages:
  GET  /widgets                      workshop UI

API:
  GET    /api/widgets/types          type registry (read-only)
  GET    /api/widgets/options/{kind} resolve options_from at render time
  GET    /api/widget-defs            list all definitions
  POST   /api/widget-defs            create or update (body {id?, name, type, visual, default_data})
  DELETE /api/widget-defs/{id}       delete (409 if in use)
  POST   /api/widget-defs/{id}/duplicate  clone def, name " (copy)" appended
"""
from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse

from .. import widget_definitions_store, widget_types
from ..deps import templates

router = APIRouter()


@router.get("/widgets", response_class=HTMLResponse)
def widgets_page(request: Request):
    return templates.TemplateResponse(
        request, "widgets.html",
        {
            "definitions": widget_definitions_store.list_definitions(),
            "types": widget_types.all_types(),
            "pinned_dashboards": _pinned_for_subnav(),
            "active_dashboard_key": "meta:widgets",
        },
    )


@router.get("/api/widgets/types")
def get_types():
    return JSONResponse({"types": widget_types.all_types()})


@router.get("/api/widgets/options/{kind}")
def get_options(kind: str):
    if kind == "groups":
        from .. import work_centers_store
        return JSONResponse({"options": [
            {"value": g, "label": g} for g in work_centers_store.all_group_names("group")
        ]})
    if kind == "value_streams":
        from .. import work_centers_store
        return JSONResponse({"options": [
            {"value": g, "label": g} for g in work_centers_store.all_group_names("value_stream")
        ]})
    if kind == "wcs":
        from .. import staffing
        return JSONResponse({"options": [
            {"value": loc.name, "label": loc.name} for loc in staffing.LOCATIONS
        ]})
    return JSONResponse({"ok": False, "error": f"unknown kind: {kind}"}, status_code=400)


@router.get("/api/widget-defs")
def list_defs():
    return JSONResponse({"definitions": widget_definitions_store.list_definitions()})


@router.post("/api/widget-defs")
async def save_def(request: Request):
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "invalid json"}, status_code=400)
    body = body or {}
    name = body.get("name")
    type_ = body.get("type")
    visual = body.get("visual") or {}
    default_data = body.get("default_data") or {}
    row_id = body.get("id")
    if not isinstance(name, str) or not name.strip():
        return JSONResponse({"ok": False, "error": "name required"}, status_code=400)
    if widget_types.get(type_) is None:
        return JSONResponse({"ok": False, "error": f"unknown type: {type_}"}, status_code=400)
    if not isinstance(visual, dict) or not isinstance(default_data, dict):
        return JSONResponse({"ok": False, "error": "visual and default_data must be objects"}, status_code=400)
    saved = widget_definitions_store.save(
        name=name.strip(), type=type_,
        visual=visual, default_data=default_data,
        id=int(row_id) if row_id is not None else None,
    )
    return JSONResponse({"ok": True, "definition": saved})


@router.delete("/api/widget-defs/{def_id}")
def delete_def(def_id: int):
    n = widget_definitions_store.usage_count(def_id)
    if n > 0:
        return JSONResponse(
            {"ok": False, "error": f"in use by {n} placement(s)"},
            status_code=409,
        )
    widget_definitions_store.delete(def_id)
    return JSONResponse({"ok": True})


@router.post("/api/widget-defs/{def_id}/duplicate")
def duplicate_def(def_id: int):
    try:
        dup = widget_definitions_store.duplicate(def_id)
    except LookupError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=404)
    return JSONResponse({"ok": True, "definition": dup})


def _pinned_for_subnav():
    from .. import dashboard_catalog
    return dashboard_catalog.pinned_dashboards_for_subnav()
