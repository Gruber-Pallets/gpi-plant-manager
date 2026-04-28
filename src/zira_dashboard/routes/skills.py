"""Skills matrix + roster mutation routes.

Routes:
  GET  /staffing/skills          — render the skills matrix
  POST /staffing/skills          — save the skills matrix
  POST /staffing/people/add      — add a new person
  POST /staffing/people/delete   — remove a person
"""

from __future__ import annotations

import os

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from .. import staffing
from ..deps import templates

router = APIRouter()


@router.get("/staffing/skills", response_class=HTMLResponse)
def staffing_skills(request: Request):
    from .. import odoo_sync, skill_filter_store
    import json
    sync_result = odoo_sync.sync(force=False)
    roster = staffing.load_roster()
    roster.sort(key=lambda p: (not p.active, p.name.lower()))
    active_count = sum(1 for p in roster if p.active)
    # Skill columns come from the synced roster — every person has the same
    # keys (sync writes a uniform skills dict per Odoo column). Falls back to
    # legacy SKILLS only if roster is empty (e.g., first run before any sync).
    if roster and roster[0].skills:
        columns = list(roster[0].skills.keys())
    else:
        columns = list(staffing.SKILLS)
    # Type metadata for filter UI grouping.
    columns_meta = []
    if odoo_sync.SKILL_META_PATH.exists():
        try:
            columns_meta = json.loads(odoo_sync.SKILL_META_PATH.read_text())
        except (json.JSONDecodeError, OSError):
            columns_meta = []
    type_by_skill = {c["name"]: c.get("type", "") for c in columns_meta if isinstance(c, dict) and "name" in c}
    hidden = set(skill_filter_store.load_hidden())
    return templates.TemplateResponse(
        request,
        "skills.html",
        {
            "active": "skills",
            "people": roster,
            "skills": columns,
            "type_by_skill": type_by_skill,
            "hidden_skills": hidden,
            "active_count": active_count,
            "inactive_count": len(roster) - active_count,
            "sync_ok": sync_result.ok,
            "sync_last_at": sync_result.last_sync_at.isoformat() if sync_result.last_sync_at else None,
            "sync_error": sync_result.error,
            "odoo_url": os.environ.get("ODOO_URL", "").rstrip("/"),
        },
    )


@router.post("/staffing/skills/filter")
async def staffing_skills_filter(request: Request):
    from .. import skill_filter_store
    body = await request.json()
    hidden = body.get("hidden", []) if isinstance(body, dict) else []
    if not isinstance(hidden, list):
        return JSONResponse({"ok": False, "error": "hidden must be a list"}, status_code=400)
    skill_filter_store.save_hidden([str(x) for x in hidden])
    return JSONResponse({"ok": True})


@router.post("/staffing/skills")
async def staffing_skills_save(request: Request):
    form = await request.form()
    roster = staffing.load_roster()
    for person in roster:
        name = person.name
        if form.get(f"active_present__{name}"):
            person.active = form.get(f"active__{name}") in ("on", "1", "true")
        if form.get(f"reserve_present__{name}"):
            person.reserve = form.get(f"reserve__{name}") in ("on", "1", "true")
    staffing.save_roster(roster)
    if (request.headers.get("accept") or "").startswith("application/json"):
        return JSONResponse({"ok": True})
    return RedirectResponse(url="/staffing/skills", status_code=303)


@router.post("/staffing/skills/refresh")
def staffing_skills_refresh():
    from .. import odoo_sync
    odoo_sync.sync(force=True)
    return RedirectResponse("/staffing/skills", status_code=303)


@router.post("/staffing/people/add")
async def staffing_person_add(request: Request):
    form = await request.form()
    name = (form.get("name") or "").strip()[:80]
    if not name:
        return JSONResponse({"ok": False, "error": "name required"}, status_code=400)
    roster = staffing.load_roster()
    if any(p.name.lower() == name.lower() for p in roster):
        return JSONResponse({"ok": False, "error": f"'{name}' already exists"}, status_code=409)
    skills = {s: 0 for s in staffing.SKILLS}
    roster.append(staffing.Person(name=name, active=True, skills=skills))
    staffing.save_roster(roster)
    if (request.headers.get("accept") or "").startswith("application/json"):
        return JSONResponse({"ok": True, "name": name})
    return RedirectResponse(url="/staffing/skills", status_code=303)


@router.post("/staffing/people/delete")
async def staffing_person_delete(request: Request):
    form = await request.form()
    name = (form.get("name") or "").strip()
    if not name:
        return JSONResponse({"ok": False, "error": "name required"}, status_code=400)
    roster = staffing.load_roster()
    before = len(roster)
    roster = [p for p in roster if p.name != name]
    if len(roster) == before:
        return JSONResponse({"ok": False, "error": f"'{name}' not found"}, status_code=404)
    staffing.save_roster(roster)
    if (request.headers.get("accept") or "").startswith("application/json"):
        return JSONResponse({"ok": True, "removed": name})
    return RedirectResponse(url="/staffing/skills", status_code=303)
