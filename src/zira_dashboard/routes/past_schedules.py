"""Past schedules browser + admin actions.

Routes:
  GET  /staffing/past             — filterable history view
  POST /staffing/past/unpublish   — flip a saved day back to draft
  POST /staffing/past/delete      — hard-delete a saved day (admin password gated)
"""

from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse

from .. import staffing
from ..deps import templates

router = APIRouter()


ADMIN_PASSWORD = "4840"


@router.post("/staffing/past/unpublish")
async def staffing_past_unpublish(request: Request):
    form = await request.form()
    day_raw = (form.get("day") or "").strip()
    try:
        d = date.fromisoformat(day_raw)
    except ValueError:
        return JSONResponse({"ok": False, "error": "bad day"}, status_code=400)
    sched = staffing.load_schedule(d)
    # Snapshot the posted version so the scheduler can toggle between draft + posted.
    if sched.published and not sched.published_snapshot:
        sched.published_snapshot = staffing.snapshot_of(sched)
    sched.published = False
    staffing.save_schedule(sched)
    return JSONResponse({"ok": True, "day": d.isoformat()})


@router.post("/staffing/past/delete")
async def staffing_past_delete(request: Request):
    form = await request.form()
    day_raw = (form.get("day") or "").strip()
    pw = (form.get("admin_password") or "").strip()
    if pw != ADMIN_PASSWORD:
        return JSONResponse({"ok": False, "error": "Wrong admin password."}, status_code=401)
    try:
        d = date.fromisoformat(day_raw)
    except ValueError:
        return JSONResponse({"ok": False, "error": "bad day"}, status_code=400)
    # Hard-delete from Postgres (cascades to schedule_assignments, etc.).
    from .. import db as _db
    _db.execute("DELETE FROM schedules WHERE day = %s", (d,))
    staffing._invalidate_schedule_cache(d)
    return JSONResponse({"ok": True, "day": d.isoformat()})


@router.get("/staffing/past", response_class=HTMLResponse)
def staffing_past(
    request: Request,
    from_: str | None = Query(default=None, alias="from"),
    to: str | None = Query(default=None),
    person: str | None = Query(default=None),
    wc: str | None = Query(default=None),
    published: str | None = Query(default=None),
):
    from .. import cert_lookup
    person_certs = cert_lookup.load_person_certs()

    def _parse(s):
        try: return date.fromisoformat(s) if s else None
        except ValueError: return None
    d_from = _parse(from_)
    d_to = _parse(to)
    pub_filter = published if published in ("0", "1") else ""

    rows = []
    all_people: set[str] = set()
    all_wcs: set[str] = set()

    for day, sched in staffing.iter_saved_schedules():
        # Collect for filter dropdowns (all days)
        for loc_name, names in sched.assignments.items():
            if loc_name == staffing.TIME_OFF_KEY:
                continue
            all_wcs.add(loc_name)
            for n in names or []:
                all_people.add(n)

        if d_from and day < d_from: continue
        if d_to and day > d_to: continue
        if pub_filter == "1" and not sched.published: continue
        if pub_filter == "0" and sched.published: continue

        # Apply person + wc filters to produce filtered_assignments
        filtered = []
        person_matches = (not person)
        wc_matches = (not wc)
        for loc_name, names in sched.assignments.items():
            if loc_name == staffing.TIME_OFF_KEY: continue
            if wc and loc_name != wc: continue
            if person and person not in (names or []): continue
            filtered.append((loc_name, names or []))
            if person and person in (names or []): person_matches = True
            if wc and loc_name == wc: wc_matches = True

        if person and not person_matches: continue
        if wc and not wc_matches: continue

        people_count = sum(len(ns) for k, ns in sched.assignments.items() if k != staffing.TIME_OFF_KEY)
        wc_count = sum(1 for k, ns in sched.assignments.items() if k != staffing.TIME_OFF_KEY and ns)

        wc_notes_map = sched.wc_notes or {}
        filtered_with_notes = [(name, ppl, wc_notes_map.get(name, "")) for name, ppl in filtered]
        rows.append({
            "day": day.isoformat(),
            "weekday": day.strftime("%A"),
            "published": sched.published,
            "people_count": people_count,
            "wc_count": wc_count,
            "filtered_assignments": filtered_with_notes,
            "notes": sched.notes or "",
            "testing_day": bool(getattr(sched, "testing_day", False)),
        })

    response = templates.TemplateResponse(
        request,
        "past_schedules.html",
        {
            "active": "past",
            "rows": rows,
            "all_people": sorted(all_people, key=str.lower),
            "all_wcs": sorted(all_wcs, key=str.lower),
            "filters": {
                "from": from_ or "",
                "to": to or "",
                "person": person or "",
                "wc": wc or "",
                "published": pub_filter,
            },
            "person_certs": person_certs,
        },
    )
    from .._http_cache import set_cache_headers
    set_cache_headers(response, includes_today=False)
    return response
