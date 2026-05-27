"""Settings page + save endpoints.

Routes:
  GET  /settings                  — render the full settings page
  POST /settings/schedule         — save shift schedule + breaks
  POST /settings/work_centers     — save WC rows, group registry, group/VS overrides
  POST /settings                  — legacy global save (kept for backward compat)
"""

from __future__ import annotations

from datetime import time as _time

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from .. import schedule_store, settings_store, shift_config, staffing, work_centers_store
from ..deps import templates
from ..stations import CATEGORIES, STATIONS

router = APIRouter()


def _parse_hhmm(raw: str | None) -> _time | None:
    if not isinstance(raw, str):
        return None
    raw = raw.strip()
    if not raw:
        return None
    try:
        hh, mm = raw.split(":")
        return _time(int(hh), int(mm))
    except (ValueError, AttributeError):
        return None


def _loc_by_key(key: str):
    for loc in staffing.LOCATIONS:
        if (loc.meter_id or f"name:{loc.name}") == key:
            return loc
    return None


@router.get("/settings", response_class=HTMLResponse)
def settings_page(
    request: Request,
    saved: int = Query(default=0),
    section: str = Query(default="work_centers"),
):
    if section not in ("work_centers", "schedule", "integrations", "roster_filter", "tvs", "kiosk"):
        section = "work_centers"
    roster_filter_rows: list[dict] = []
    if section == "roster_filter":
        from .. import db
        roster_filter_rows = db.query(
            "SELECT odoo_id, name, excluded "
            "FROM people "
            "WHERE odoo_id IS NOT NULL "
            "ORDER BY lower(name)"
        )
    integration_status = None
    if section == "integrations":
        from .. import stratustime_client
        integration_status = stratustime_client.health_check()
    kiosk_recent_punches: list[dict] = []
    kiosk_recent_variances: list[dict] = []
    kiosk_sync_status: dict | None = None
    if section == "kiosk":
        from .. import db
        kiosk_recent_punches = db.query(
            "SELECT kpl.id, kpl.person_odoo_id, p.name AS person_name, "
            "kpl.action, kpl.wc_name, kpl.occurred_at, kpl.synced_to_odoo, "
            "kpl.sync_error, kpl.synced_at, kpl.odoo_attendance_id "
            "FROM kiosk_punches_log kpl "
            "LEFT JOIN people p ON p.odoo_id = kpl.person_odoo_id "
            "ORDER BY kpl.occurred_at DESC LIMIT 50"
        )
        kiosk_recent_variances = db.query(
            "SELECT ksv.id, ksv.person_odoo_id, p.name AS person_name, "
            "ksv.scheduled_wc_name, ksv.actual_wc_name, ksv.occurred_at, "
            "ksv.reviewed_at "
            "FROM kiosk_schedule_variances ksv "
            "LEFT JOIN people p ON p.odoo_id = ksv.person_odoo_id "
            "ORDER BY ksv.occurred_at DESC LIMIT 50"
        )
        status_rows = db.query(
            "SELECT "
            "COUNT(*) FILTER (WHERE synced_to_odoo = FALSE) AS unsynced, "
            "COUNT(*) AS total_7d, "
            "MAX(synced_at) AS last_sync_at, "
            "COUNT(*) FILTER (WHERE sync_error IS NOT NULL AND synced_to_odoo = FALSE) AS error_count "
            "FROM kiosk_punches_log "
            "WHERE occurred_at > now() - interval '7 days'"
        )
        kiosk_sync_status = status_rows[0] if status_rows else None
    tv_displays_rows: list[dict] = []
    all_dashboards_for_picker: list[dict] = []
    if section == "tvs":
        from .. import tv_displays_store
        tv_displays_rows = tv_displays_store.list_displays()
        all_dashboards_for_picker = [
            {"kind": "vs_recycling", "ref": "", "name": "Recycling"},
            {"kind": "vs_new", "ref": "", "name": "New"},
            {"kind": "vs_work_centers", "ref": "", "name": "Work Centers"},
        ]
        for loc in staffing.LOCATIONS:
            all_dashboards_for_picker.append(
                {"kind": "wc", "ref": loc.name, "name": loc.name}
            )
    from .. import odoo_sync
    # TTL-checked sync so /settings self-heals after a Railway redeploy
    # where the ephemeral roster.json got reset to the legacy seed.
    odoo_sync.sync(force=False)
    productive_min = shift_config.productive_minutes_per_day()

    # Active roster (objects, not just names) so we can compute per-WC skill
    # levels and reserve flags for the Default People picker.
    roster = staffing.load_roster()
    active_people_objs = [p for p in roster if p.active]
    active_people = sorted((p.name for p in active_people_objs), key=str.lower)

    # Per-work-center rows.
    wc_rows = []
    for loc in staffing.LOCATIONS:
        eff = work_centers_store.effective(loc)
        max_ops = eff["max_ops"]
        required_skills = eff["required_skills"]
        # Pool for the Default People picker, color-coded by min skill level
        # across the WC's required skills (mirrors the scheduler's logic).
        # When required_skills is empty, render at neutral lvl-2 (no scale).
        default_pool: list[dict] = []
        for p in active_people_objs:
            if required_skills:
                lvl = min((p.level(s) for s in required_skills), default=0)
            else:
                lvl = 2
            default_pool.append({"name": p.name, "level": lvl, "reserve": p.reserve})
        default_pool.sort(key=lambda r: (r["reserve"], -r["level"], r["name"].lower()))
        wc_rows.append(
            {
                "key": loc.meter_id or f"name:{loc.name}",
                "name": loc.name,
                "bay": loc.bay,
                "department": loc.department,
                "required_skills": required_skills,
                "min_ops": eff["min_ops"],
                "max_ops": max_ops if max_ops is not None else "",
                "goal": eff["goal_per_day"],
                "note": eff["note"],
                "groups": eff["groups"],
                "department": eff["department"],
                "default_people": eff["default_people"],
                "default_pool": default_pool,
            }
        )

    def _group_summary(kind: str) -> list[dict]:
        rows = []
        for name in work_centers_store.all_group_names(kind):
            members = work_centers_store.members(kind, name)
            auto = work_centers_store.group_goal_auto(kind, name)
            override = work_centers_store.group_goal_override(kind, name)
            rows.append(
                {
                    "name": name,
                    "count": len(members),
                    "auto": auto,
                    "override": "" if override is None else override,
                    "effective": work_centers_store.group_goal(kind, name),
                }
            )
        return rows

    group_rows = _group_summary("group")
    dept_rows = _group_summary("department")
    sched = schedule_store.current()
    schedule_ctx = {
        "shift_start": f"{sched.shift_start.hour:02d}:{sched.shift_start.minute:02d}",
        "shift_end":   f"{sched.shift_end.hour:02d}:{sched.shift_end.minute:02d}",
        "work_weekdays": sorted(sched.work_weekdays),
        "weekday_names": schedule_store.WEEKDAY_NAMES,
        "breaks": [
            {
                "start": f"{b.start.hour:02d}:{b.start.minute:02d}",
                "end":   f"{b.end.hour:02d}:{b.end.minute:02d}",
                "name": b.name,
            }
            for b in sched.breaks
        ],
    }
    # Skill list comes directly from the `skills` table — Odoo's
    # Production + Supervisor skill types. Production first (alphabetical),
    # then Supervisor.
    from .. import db as _db
    _skill_rows = _db.query(
        "SELECT name FROM skills "
        "WHERE skill_type IN ('Production Skills', 'Supervisor Skills') "
        "ORDER BY skill_type, lower(name)"
    )
    skills_all = [r["name"] for r in _skill_rows]
    return templates.TemplateResponse(
        request,
        "settings.html",
        {
            "wc_rows": wc_rows,
            "skills_all": skills_all,
            "departments": work_centers_store.synced_departments(),
            "groups_all": work_centers_store.registered_groups(),
            "group_rows": group_rows,
            "dept_rows": dept_rows,
            "active_people": active_people,
            "saved": bool(saved),
            "active_section": section,
            "roster_filter_rows": roster_filter_rows,
            "productive_minutes": productive_min,
            "schedule": schedule_ctx,
            "integration_status": integration_status,
            "tv_displays_rows": tv_displays_rows,
            "all_dashboards_for_picker": all_dashboards_for_picker,
            "wc_locations_for_picker": [{"name": loc.name} for loc in staffing.LOCATIONS],
            "kiosk_recent_punches": kiosk_recent_punches,
            "kiosk_recent_variances": kiosk_recent_variances,
            "kiosk_sync_status": kiosk_sync_status,
        },
    )


@router.post("/settings/schedule")
async def settings_save_schedule(request: Request):
    form = await request.form()
    current = schedule_store.current()
    shift_s = _parse_hhmm(form.get("shift_start")) or current.shift_start
    shift_e = _parse_hhmm(form.get("shift_end")) or current.shift_end
    if shift_e <= shift_s:
        shift_e = current.shift_end
    weekday_set = set()
    for i in range(7):
        if form.get(f"weekday_{i}"):
            weekday_set.add(i)
    if not weekday_set:
        weekday_set = set(current.work_weekdays)
    # Collect breaks from indexed form fields (start_N, end_N, name_N).
    breaks_new: list[schedule_store.Break] = []
    idx = 0
    while True:
        bs = _parse_hhmm(form.get(f"break_start_{idx}"))
        be = _parse_hhmm(form.get(f"break_end_{idx}"))
        bn = (form.get(f"break_name_{idx}") or "").strip() or "Break"
        if bs is None and be is None and not form.get(f"break_name_{idx}"):
            # No form fields at this index → stop scanning.
            if idx > 50:
                break
            idx += 1
            if idx > 50:
                break
            continue
        if bs and be and be > bs:
            breaks_new.append(schedule_store.Break(bs, be, bn[:40]))
        idx += 1
        if idx > 50:
            break
    breaks_new.sort(key=lambda b: b.start)
    schedule_store.save(schedule_store.Schedule(
        shift_start=shift_s,
        shift_end=shift_e,
        work_weekdays=frozenset(weekday_set),
        breaks=tuple(breaks_new),
    ))
    if (request.headers.get("accept") or "").startswith("application/json"):
        return JSONResponse({"ok": True})
    return RedirectResponse(url="/settings?saved=1&section=schedule", status_code=303)


@router.post("/settings/groups/add")
async def settings_add_group(request: Request):
    """Quick-add endpoint for the Groups section's Enter-to-add UX. Saves
    just the named group without touching WC rows, value-stream overrides,
    or schedule fields, so power-typing groups doesn't clobber other
    in-progress edits on the page."""
    form = await request.form()
    name = (form.get("name") or "").strip()[:80]
    if not name:
        return JSONResponse({"ok": False, "error": "name required"}, status_code=400)
    if name in set(work_centers_store.registered_groups()):
        return JSONResponse({"ok": False, "error": "already exists", "name": name}, status_code=409)
    work_centers_store.add_group(name)
    return JSONResponse({"ok": True, "name": name})


@router.post("/settings/work_centers")
async def settings_save_work_centers(request: Request):
    """Bulk save: group registry edits, WC rows, group/VS overrides."""
    form = await request.form()

    # 1. Group registry (delete, rename, add) — do first so WC save sees updated names.
    for name in list(work_centers_store.registered_groups()):
        if form.get(f"group_delete__{name}"):
            work_centers_store.delete_group(name)
    for name in list(work_centers_store.registered_groups()):
        new_name = (form.get(f"group_rename__{name}") or "").strip()
        if new_name and new_name != name:
            work_centers_store.rename_group(name, new_name)
    new_group = (form.get("group_new") or "").strip()
    if new_group:
        work_centers_store.add_group(new_group)

    # 2. Work-center rows.
    for loc in staffing.LOCATIONS:
        key = loc.meter_id or f"name:{loc.name}"
        prefix = f"wc__{key}__"
        updates: dict = {}
        for field in ("goal_per_day", "min_ops", "max_ops", "department"):
            name = prefix + field
            if name in form:
                updates[field] = form.get(name) or ""
        # Multi-valued: required_skills (checkbox list). The hidden
        # required_skills_present marker (settings.html) lets us
        # distinguish "no checkboxes posted" (form didn't include this
        # section — leave DB alone) from "explicitly cleared" (form
        # did include it but no skills checked — save the empty list).
        if (prefix + "required_skills_present") in form:
            updates["required_skills"] = form.getlist(prefix + "required_skills")
        # Single-value Group select (stored internally as a 1-element list in `groups`).
        group_field = prefix + "group"
        if group_field in form:
            v = (form.get(group_field) or "").strip()
            updates["groups"] = [v] if v else []
        # Multi-select Default People checkbox list.
        dp_present = prefix + "default_people_present"
        if dp_present in form:
            updates["default_people"] = form.getlist(prefix + "default_people")
        if updates:
            work_centers_store.save_one(loc, updates)

    # 3. Group + VS overrides.
    for kind in work_centers_store.GROUP_KINDS:
        for name in work_centers_store.all_group_names(kind):
            field = f"group_override__{kind}__{name}"
            if field in form:
                work_centers_store.save_group_override(kind, name, form.get(field) or "")
    if (request.headers.get("accept") or "").startswith("application/json"):
        return JSONResponse({"ok": True})
    return RedirectResponse(url="/settings?saved=1&section=work_centers", status_code=303)


@router.post("/settings")
async def settings_save(request: Request):
    """Save the Per-Group overrides. Work-center rows post to /settings/work_center/{key}."""
    form = await request.form()
    group_targets: dict[str, int] = {}
    # Keep the legacy station_targets dict empty; goals are now stored in work_centers.json.
    station_targets: dict[str, int] = {}
    for s in STATIONS:
        raw = (form.get(f"station_{s.meter_id}") or "").strip()
        if raw:
            try:
                station_targets[s.meter_id] = max(0, int(raw))
            except ValueError:
                pass
    for c in CATEGORIES:
        raw = (form.get(f"group_{c}") or "").strip()
        if raw:
            try:
                group_targets[c] = max(0, int(raw))
            except ValueError:
                pass
    settings_store.save(station_targets, group_targets)
    if (request.headers.get("accept") or "").startswith("application/json"):
        return JSONResponse({"ok": True})
    return RedirectResponse(url="/settings?saved=1", status_code=303)


@router.post("/api/settings/roster-filter/toggle")
async def roster_filter_toggle(request: Request):
    """Flip the `excluded` flag on a single person.

    Body (JSON): {odoo_id: int, excluded: bool}
    Side effects: UPDATE people SET excluded = $excluded WHERE odoo_id = $odoo_id;
    invalidate the roster cache so the next /staffing render picks up
    the change.
    """
    from .. import db, staffing
    body = await request.json()
    odoo_id_raw = body.get("odoo_id")
    excluded_raw = body.get("excluded")
    try:
        odoo_id = int(odoo_id_raw)
    except (TypeError, ValueError):
        return JSONResponse({"ok": False, "error": "odoo_id required (int)"}, status_code=400)
    if not isinstance(excluded_raw, bool):
        return JSONResponse({"ok": False, "error": "excluded must be true or false"}, status_code=400)
    db.execute(
        "UPDATE people SET excluded = %s WHERE odoo_id = %s",
        (excluded_raw, odoo_id),
    )
    staffing._invalidate_roster_cache()
    return JSONResponse({"ok": True})
