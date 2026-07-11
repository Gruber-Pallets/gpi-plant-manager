"""Skills matrix + roster mutation routes.

Routes:
  GET  /staffing/skills          — render the skills matrix
  POST /staffing/skills          — save the skills matrix
  POST /staffing/people/add      — add a new person
  POST /staffing/people/delete   — remove a person
"""

from __future__ import annotations

import asyncio
import logging
import os

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from .. import staffing, skill_levels, rotation_store
from .. import _http_cache, db
from ..deps import templates

router = APIRouter()
log = logging.getLogger(__name__)


@router.get("/staffing/skills", response_class=HTMLResponse)
def staffing_skills(request: Request):
    from .. import odoo_sync, skill_matrix_views_store as views_store, db
    from .. import cert_lookup

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
        "SELECT name, odoo_id, skill_type FROM skills "
        "WHERE skill_type IN ('Production Skills', 'Supervisor Skills') "
        "ORDER BY skill_type, lower(name)"
    )
    columns = [
        {"name": r["name"], "odoo_id": r["odoo_id"], "skill_type": r["skill_type"]}
        for r in skill_rows
    ]
    skill_names = [r["name"] for r in skill_rows]
    type_by_skill = {r["name"]: r["skill_type"] for r in skill_rows}

    all_views = views_store.list_views()
    default_view = views_store.get_default_view()

    # Recycled Rotation editor data. Each load is guarded so a DB hiccup degrades
    # to an empty editor rather than 500ing the whole matrix (mirrors the
    # safe-defaults discipline in the staffing route). Saved preferences/blocks
    # refresh here on the next load because the Task 4 endpoints invalidate the
    # stable bucket this response is cached in.
    rotation_groups = list(rotation_store.ROTATION_GROUPS)
    rotation_preference_options = list(rotation_store.PREFERENCES)
    try:
        rotation_preferences = rotation_store.load_preferences_by_name()
    except Exception:
        log.exception("Skills matrix: failed to load rotation preferences")
        rotation_preferences = {}
    try:
        active_training_blocks = [
            {
                "id": b.id,
                "trainee": b.trainee_name,
                "trainer": b.trainer_name,
                "group": b.skill,
                "skill": b.skill,
                "start_day": b.start_day.isoformat(),
                "planned_attended_days": b.planned_attended_days,
                "status": b.status,
            }
            for b in rotation_store.active_blocks()
        ]
    except Exception:
        log.exception("Skills matrix: failed to load active training blocks")
        active_training_blocks = []
    # Per-person Recycled skill levels drive the level-0-only training-block form
    # (which groups a trainee is eligible for) and the level-3 trainer picker.
    rotation_levels = {
        p.name: {g: int(p.skills.get(g, 0)) for g in rotation_groups} for p in roster
    }
    rotation_active_people = [p.name for p in roster if p.active]

    response = templates.TemplateResponse(
        request,
        "skills.html",
        {
            "active": "skills",
            "people": roster,
            "person_certs": person_certs,
            "skills": columns,
            "skill_names": skill_names,
            "type_by_skill": type_by_skill,
            "views": all_views,
            "default_view_name": default_view["name"] if default_view else None,
            "default_view_state": default_view,
            "rotation_groups": rotation_groups,
            "rotation_preference_options": rotation_preference_options,
            "rotation_preferences": rotation_preferences,
            "active_training_blocks": active_training_blocks,
            "rotation_levels": rotation_levels,
            "rotation_active_people": rotation_active_people,
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
        _http_cache.invalidate_today_cache()
        _http_cache.invalidate_stable_cache()
        if (request.headers.get("accept") or "").startswith("application/json"):
            return JSONResponse({"ok": True})
        return RedirectResponse(url="/staffing/skills", status_code=303)

    return await asyncio.to_thread(_work)


def _skill_cell_error(message: str, status_code: int) -> JSONResponse:
    return JSONResponse({"ok": False, "error": message}, status_code=status_code)


def _strict_json_int(body: dict, field: str) -> int:
    value = body.get(field)
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(field)
    return value


def _level_label(level: int) -> str:
    return {0: "not trained", 1: "practicing", 2: "competent", 3: "proficient"}[level]


@router.post("/staffing/skills/cell")
async def staffing_skill_cell_update(request: Request):
    try:
        body = await request.json()
    except Exception:
        return _skill_cell_error("Invalid JSON body.", 400)

    try:
        if not isinstance(body, dict):
            raise ValueError("body")
        person_odoo_id = _strict_json_int(body, "person_odoo_id")
        skill_odoo_id = _strict_json_int(body, "skill_odoo_id")
        level = _strict_json_int(body, "level")
    except ValueError:
        return _skill_cell_error("person_odoo_id, skill_odoo_id, and level are required.", 400)

    if person_odoo_id <= 0 or skill_odoo_id <= 0:
        return _skill_cell_error("person_odoo_id and skill_odoo_id must be positive integers.", 400)
    if level not in (0, 1, 2, 3):
        return _skill_cell_error("level must be 0, 1, 2, or 3.", 400)

    def _work():
        person_rows = db.query(
            "SELECT id, odoo_id, name FROM people "
            "WHERE odoo_id = %s AND NOT excluded",
            (person_odoo_id,),
        )
        if not person_rows:
            return _skill_cell_error("Person not found. Refresh from Odoo and try again.", 404)

        skill_rows = db.query(
            "SELECT id, odoo_id, name, skill_type FROM skills WHERE odoo_id = %s",
            (skill_odoo_id,),
        )
        if not skill_rows:
            return _skill_cell_error("Skill not found. Refresh from Odoo and try again.", 404)

        skill = skill_rows[0]
        if skill["skill_type"] not in ("Production Skills", "Supervisor Skills"):
            return _skill_cell_error("Skill is not editable in the People Matrix.", 400)

        # One shared promotion path (also used by training-block completion):
        # Odoo first, then the local mirror + cache invalidation. A rejected
        # Odoo write raises SkillSyncError (no local write); a later local/cache
        # failure surfaces as any other exception.
        try:
            skill_levels.set_person_skill_level(
                int(person_rows[0]["id"]), int(skill["id"]), level
            )
        except skill_levels.SkillSyncError as exc:
            return _skill_cell_error(f"Odoo save failed: {exc}", 502)
        except Exception:
            log.exception("Odoo skill save succeeded but local mirror/cache refresh failed")
            return JSONResponse({
                "ok": True,
                "level": level,
                "label": _level_label(level),
                "warning": (
                    "Saved in Odoo, but the local matrix did not refresh. "
                    "Use Refresh from Odoo if it looks stale."
                ),
            }, status_code=202)
        return JSONResponse({
            "ok": True,
            "level": level,
            "label": _level_label(level),
        })

    return await asyncio.to_thread(_work)


@router.post("/staffing/skills/refresh")
def staffing_skills_refresh(request: Request):
    """Force-sync from Odoo. Returns JSON for AJAX clients (so the matrix
    can show progress + reload), or 303 for plain form submits.

    Sync `def` on purpose: FastAPI runs it in the threadpool, keeping the
    full Odoo sync (seconds of XML-RPC) off the event loop."""
    from .. import odoo_sync
    result = odoo_sync.sync(force=True)
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
        _http_cache.invalidate_today_cache()
        _http_cache.invalidate_stable_cache()
        return JSONResponse({"ok": True, "view": view})

    return await asyncio.to_thread(_work)


@router.delete("/staffing/skills/views/default")
def staffing_skills_view_clear_default():
    from .. import skill_matrix_views_store as views_store
    views_store.set_default(None)
    _http_cache.invalidate_today_cache()
    _http_cache.invalidate_stable_cache()
    return JSONResponse({"ok": True})


@router.delete("/staffing/skills/views/{name}")
def staffing_skills_view_delete(name: str):
    from .. import skill_matrix_views_store as views_store
    views_store.delete_view(name)
    _http_cache.invalidate_today_cache()
    _http_cache.invalidate_stable_cache()
    return JSONResponse({"ok": True})


@router.post("/staffing/skills/views/{name}/default")
def staffing_skills_view_set_default(name: str):
    from .. import skill_matrix_views_store as views_store
    if views_store.get_view(name) is None:
        return JSONResponse({"ok": False, "error": "not found"}, status_code=404)
    views_store.set_default(name)
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
        _http_cache.invalidate_today_cache()
        _http_cache.invalidate_stable_cache()
        if (request.headers.get("accept") or "").startswith("application/json"):
            return JSONResponse({"ok": True, "removed": name})
        return RedirectResponse(url="/staffing/skills", status_code=303)

    return await asyncio.to_thread(_work)
