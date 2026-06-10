"""JSON endpoints for the dashboard's per-page layout + per-widget customizer.

Routes:
  GET    /api/layout/{page}
  POST   /api/layout/{page}
  GET    /api/widget/{page}/{widget_id}
  POST   /api/widget/{page}/{widget_id}
  DELETE /api/widget/{page}/{widget_id}
  GET    /healthz
"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from .. import _http_cache, layout_store, widget_customizer

router = APIRouter()


@router.get("/api/layout/{page}")
def get_layout(page: str):
    return JSONResponse({"page": page, "items": layout_store.load(page)})


@router.post("/api/layout/{page}")
async def save_layout(page: str, request: Request):
    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "invalid json"}, status_code=400)
    if not isinstance(data, list):
        return JSONResponse({"ok": False, "error": "expected list"}, status_code=400)
    await asyncio.to_thread(layout_store.save, page, data)
    return JSONResponse({"ok": True, "count": len(data)})


@router.get("/api/widget/{page}/{widget_id}")
def get_widget(page: str, widget_id: str):
    return JSONResponse({"page": page, "id": widget_id, "config": widget_customizer.load_one(page, widget_id)})


@router.post("/api/widget/{page}/{widget_id}")
async def save_widget(page: str, widget_id: str, request: Request):
    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "invalid json"}, status_code=400)
    if not isinstance(data, dict):
        return JSONResponse({"ok": False, "error": "expected object"}, status_code=400)

    def _work():
        saved = widget_customizer.save_one(page, widget_id, data)
        # Customs apply to every rendered page regardless of date range, so we
        # drop ALL cache buckets — otherwise the JS reload after saving a title
        # serves the still-cached old HTML until the TTL (15s today / 5min past)
        # expires, and the edit looks like it didn't take.
        _http_cache.invalidate_all_cache()
        return JSONResponse({"ok": True, "config": saved})

    return await asyncio.to_thread(_work)


@router.delete("/api/widget/{page}/{widget_id}")
def reset_widget(page: str, widget_id: str):
    widget_customizer.reset_one(page, widget_id)
    _http_cache.invalidate_all_cache()
    return JSONResponse({"ok": True})


@router.get("/healthz")
def healthz():
    return {"ok": True}
