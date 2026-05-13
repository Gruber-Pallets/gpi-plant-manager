"""HTTP API for TV-dashboard layout templates.

  POST   /api/tv-templates                     save (upsert by name)
  GET    /api/tv-templates                     list
  DELETE /api/tv-templates/{template_id}       delete
  POST   /api/tv-templates/{template_id}/apply apply to one/many WCs
"""
from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from .. import tv_templates_store

router = APIRouter()


@router.post("/api/tv-templates")
async def save_template(request: Request):
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "invalid json"}, status_code=400)
    name = (body or {}).get("name")
    layout = (body or {}).get("layout") or []
    theme = (body or {}).get("theme") or "dark"
    if not isinstance(name, str) or not name.strip():
        return JSONResponse({"ok": False, "error": "name required"}, status_code=400)
    if not isinstance(layout, list):
        return JSONResponse({"ok": False, "error": "layout must be a list"}, status_code=400)
    if theme not in ("light", "dark"):
        return JSONResponse({"ok": False, "error": "theme must be light or dark"}, status_code=400)
    tv_templates_store.save(name.strip(), layout, theme=theme)
    return JSONResponse({"ok": True, "name": name.strip()})


@router.get("/api/tv-templates")
def list_templates():
    rows = tv_templates_store.list_templates()
    return JSONResponse({
        "templates": [
            {
                "id": r["id"],
                "name": r["name"],
                "theme": r["theme"],
                "updated_at": r["updated_at"].isoformat() if r.get("updated_at") else None,
            }
            for r in rows
        ]
    })


@router.delete("/api/tv-templates/{template_id}")
def delete_template(template_id: int):
    tv_templates_store.delete(template_id)
    return JSONResponse({"ok": True})


@router.post("/api/tv-templates/{template_id}/apply")
async def apply_template(template_id: int, request: Request):
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "invalid json"}, status_code=400)
    targets = (body or {}).get("targets")
    if not targets:
        return JSONResponse({"ok": False, "error": "targets required"}, status_code=400)
    result = tv_templates_store.apply_to_targets(template_id, targets)
    if result.get("error") == "template not found":
        return JSONResponse({"ok": False, "error": result["error"]}, status_code=404)
    return JSONResponse({
        "ok": True,
        "applied_count": result["applied_count"],
        "applied_pages": result["applied_pages"],
    })
