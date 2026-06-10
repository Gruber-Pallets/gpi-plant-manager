"""Skills matrix + roster mutation routes.

Routes:
  GET  /staffing/skills          — render the skills matrix
  POST /staffing/skills          — save the skills matrix
  POST /staffing/people/add      — add a new person
  POST /staffing/people/delete   — remove a person
"""

from __future__ import annotations

import asyncio
import os

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from .. import staffing
from ..deps import templates

router = APIRouter()


@router.get("/staffing/skills", response_class=HTMLResponse)
def staffing_skills(request: Request):
    from .. import odoo_sync, skill_matrix_views_store as views_store, db
    from .. import cert_lookup, _http_cache

    # Response cache. The matrix is roster + skill-level data, which changes
    # only on roster/skill writes (each invalidates the stable bucket) and on
    # Odoo sync. Cached in the long-TTL stable bucket (600s) — the 60s today
    # bucket left the page cold 80% of the time between the skills warmer's
    # 300s ticks. On a cache hit we also skip the per-request
    # odoo_sync.sync(force=False) freshness check — fine within the TTL,
    # and the page warmer / a real miss will re-trigger it.
    response_cache_key = ("staffing_skills",)
    cached_resp = _http_cache.get_cached_response(
        response_cache_key, includes_today=True, stable=True
    )
    if cached_resp is not None:
        return cached_resp

    person_certs = cert_lookup.load_person_certs()
    sync_result = odoo_sync.sync(force=False)
    roster = staffing.load_roster()
    roster.sort(key=lambda p: (not p.active, p.name.lower()))
    active_count = sum(1 for p in roster if p.active)

    skill_rows = db.query(
        "SELECT name, skill_type FROM skills "
        "WHERE skill_type IN ('Production Skills', 'Supervisor Skills') "
        "ORDER BY skill_type, lower(name)"
    )
    columns = [r["name"] for r in skill_rows]
    type_by_skill = {r["name"]: r["skill_type"] for r in skill_rows}

    all_views = views_store.list_views()
    default_view = views_store.get_default_view()

    response = templates.TemplateResponse(
        request,
        "skills.html",
        {
            "active": "skills",
            "people": roster,
            "person_certs": person_certs,
            "skills": columns,
            "type_by_skill": type_by_skill,
            "views": all_views,
            "default_view_name": default_view["name"] if default_view else None,
            "default_view_state": default_view,
            "active_count": active_count,
            "inactive_count": len(roster) - active_count,
            "sync_ok": sync_result.ok,
            "sync_last_at": sync_result.last_sync_at.isoformat() if sync_result.last_sync_at else None,
            "sync_error": sync_result.error,
            "odoo_url": os.environ.get("ODOO_URL", "").rstrip("/"),
        },
    )
    _http_cache.set_cache_headers(response, includes_today=True)
    _http_cache.store_cached_response(
        response_cache_key, includes_today=True, response=response, stable=True
    )
    return response


@router.post("/staffing/skills")
async def staffing_skills_save(request: Request):
    form = await request.form()

    def _work():
        roster = staffing.load_roster()
        # `active` is now sourced from Odoo (read-only in the matrix UI). Only
        # the local `reserve` flag is editable; everything else round-trips
        # untouched.
        for person in roster:
            name = person.name
            if form.get(f"reserve_present__{name}"):
                person.reserve = form.get(f"reserve__{name}") in ("on", "1", "true")
        staffing.save_roster(roster)
        from .. import _http_cache
        _http_cache.invalidate_today_cache()
        _http_cache.invalidate_stable_cache()
        if (request.headers.get("accept") or "").startswith("application/json"):
            return JSONResponse({"ok": True})
        return RedirectResponse(url="/staffing/skills", status_code=303)

    return await asyncio.to_thread(_work)


@router.post("/staffing/skills/refresh")
def staffing_skills_refresh(request: Request):
    """Force-sync from Odoo. Returns JSON for AJAX clients (so the matrix
    can show progress + reload), or 303 for plain form submits.

    Sync `def` on purpose: FastAPI runs it in the threadpool, keeping the
    full Odoo sync (seconds of XML-RPC) off the event loop."""
    from .. import odoo_sync
    result = odoo_sync.sync(force=True)
    from .. import _http_cache
    _http_cache.invalidate_today_cache()
    _http_cache.invalidate_stable_cache()
    if (request.headers.get("accept") or "").startswith("application/json"):
        return JSONResponse({
            "ok": result.ok,
            "refreshed": result.refreshed,
            "employee_count": result.employee_count,
            "skill_column_count": result.skill_column_count,
            "last_sync_at": result.last_sync_at.isoformat() if result.last_sync_at else None,
            "error": result.error,
        })
    return RedirectResponse("/staffing/skills", status_code=303)


@router.post("/staffing/skills/views")
async def staffing_skills_view_create(request: Request):
    from .. import skill_matrix_views_store as views_store
    body = await request.json()

    def _work():
        name = (body.get("name") or "").strip()
        if not name:
            return JSONResponse({"ok": False, "error": "name required"}, status_code=400)
        if views_store.get_view(name) is not None:
            return JSONResponse({"ok": False, "error": "name already exists"}, status_code=409)
        view = views_store.create_view(name, body)
        from .. import _http_cache
        _http_cache.invalidate_today_cache()
        _http_cache.invalidate_stable_cache()
        return JSONResponse({"ok": True, "view": view})

    return await asyncio.to_thread(_work)


@router.put("/staffing/skills/views/{name}")
async def staffing_skills_view_update(name: str, request: Request):
    from .. import skill_matrix_views_store as views_store
    body = await request.json()

    def _work():
        if views_store.get_view(name) is None:
            return JSONResponse({"ok": False, "error": "not found"}, status_code=404)
        view = views_store.update_view(name, body)
        from .. import _http_cache
        _http_cache.invalidate_today_cache()
        _http_cache.invalidate_stable_cache()
        return JSONResponse({"ok": True, "view": view})

    return await asyncio.to_thread(_work)


@router.delete("/staffing/skills/views/default")
def staffing_skills_view_clear_default():
    from .. import skill_matrix_views_store as views_store
    views_store.set_default(None)
    from .. import _http_cache
    _http_cache.invalidate_today_cache()
    _http_cache.invalidate_stable_cache()
    return JSONResponse({"ok": True})


@router.delete("/staffing/skills/views/{name}")
def staffing_skills_view_delete(name: str):
    from .. import skill_matrix_views_store as views_store
    views_store.delete_view(name)
    from .. import _http_cache
    _http_cache.invalidate_today_cache()
    _http_cache.invalidate_stable_cache()
    return JSONResponse({"ok": True})


@router.post("/staffing/skills/views/{name}/default")
def staffing_skills_view_set_default(name: str):
    from .. import skill_matrix_views_store as views_store
    if views_store.get_view(name) is None:
        return JSONResponse({"ok": False, "error": "not found"}, status_code=404)
    views_store.set_default(name)
    from .. import _http_cache
    _http_cache.invalidate_today_cache()
    _http_cache.invalidate_stable_cache()
    return JSONResponse({"ok": True})


@router.post("/staffing/people/add")
async def staffing_person_add(request: Request):
    form = await request.form()

    def _work():
        name = (form.get("name") or "").strip()[:80]
        if not name:
            return JSONResponse({"ok": False, "error": "name required"}, status_code=400)
        roster = staffing.load_roster()
        if any(p.name.lower() == name.lower() for p in roster):
            return JSONResponse({"ok": False, "error": f"'{name}' already exists"}, status_code=409)
        skills = {s: 0 for s in staffing.SKILLS}
        roster.append(staffing.Person(name=name, active=True, skills=skills))
        staffing.save_roster(roster)
        from .. import _http_cache
        _http_cache.invalidate_today_cache()
        _http_cache.invalidate_stable_cache()
        if (request.headers.get("accept") or "").startswith("application/json"):
            return JSONResponse({"ok": True, "name": name})
        return RedirectResponse(url="/staffing/skills", status_code=303)

    return await asyncio.to_thread(_work)


@router.post("/staffing/people/delete")
async def staffing_person_delete(request: Request):
    form = await request.form()

    def _work():
        name = (form.get("name") or "").strip()
        if not name:
            return JSONResponse({"ok": False, "error": "name required"}, status_code=400)
        roster = staffing.load_roster()
        before = len(roster)
        roster = [p for p in roster if p.name != name]
        if len(roster) == before:
            return JSONResponse({"ok": False, "error": f"'{name}' not found"}, status_code=404)
        staffing.save_roster(roster)
        from .. import _http_cache
        _http_cache.invalidate_today_cache()
        _http_cache.invalidate_stable_cache()
        if (request.headers.get("accept") or "").startswith("application/json"):
            return JSONResponse({"ok": True, "removed": name})
        return RedirectResponse(url="/staffing/skills", status_code=303)

    return await asyncio.to_thread(_work)
