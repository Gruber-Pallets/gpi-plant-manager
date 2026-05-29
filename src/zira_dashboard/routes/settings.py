"""Settings page + save endpoints.

Routes:
  GET  /settings                  — render the full settings page
  POST /settings/schedule         — save shift schedule + breaks
  POST /settings/work_centers     — save WC rows, group registry, group/VS overrides
  POST /settings                  — legacy global save (kept for backward compat)
"""

from __future__ import annotations

from datetime import time as _time

from fastapi import APIRouter, Form, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from .. import schedule_store, settings_store, shift_config, staffing, work_centers_store
from ..deps import templates
from ..stations import CATEGORIES, STATIONS

router = APIRouter()


def _odoo_configured() -> bool:
    """True when the four Odoo env vars are set so XML-RPC calls won't
    raise OdooConfigError. Used to gate the Time Off settings panel's
    leave-types fetch when running on a dev box without Odoo wiring."""
    import os
    return all(os.environ.get(k) for k in
               ("ODOO_URL", "ODOO_DB", "ODOO_LOGIN", "ODOO_API_KEY"))


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
    if section not in ("work_centers", "integrations", "roster_filter", "tvs", "kiosk", "time_off"):
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
    time_off_settings: dict | None = None
    if section == "time_off":
        from .. import db, odoo_client
        import logging as _logging
        _settings_log = _logging.getLogger(__name__)
        # Primary source: the local leave_types_cache table populated by
        # the 60s poller. This guarantees the panel mirrors what the
        # kiosk picker sees, and stays usable during an Odoo outage. We
        # map holiday_status_id -> id for the template (which iterates
        # `t.id`/`t.name`).
        leave_types: list[dict] = []
        cache_rows = db.query(
            "SELECT holiday_status_id, name, request_unit, "
            "requires_allocation, color, active "
            "FROM leave_types_cache WHERE active = TRUE "
            "ORDER BY name"
        )
        for r in cache_rows:
            leave_types.append({
                "id": r["holiday_status_id"],
                "name": r["name"],
                "request_unit": r["request_unit"],
                "requires_allocation": r["requires_allocation"],
                "color": r["color"],
                "active": r["active"],
            })
        # Fallback: if the table is empty (poller hasn't run yet on a
        # fresh box) AND Odoo is wired up, hit Odoo directly so the
        # panel isn't blank on first load.
        odoo_error: str | None = None
        odoo_error_class: str | None = None
        if not leave_types and _odoo_configured():
            try:
                leave_types = odoo_client.fetch_leave_types()
            except Exception as e:  # noqa: BLE001
                # Surface the error to the template so the user can
                # see *why* the panel is empty. We capture the exception
                # class name so the template can pick a class-specific
                # hint (config vs auth vs permission vs unknown) instead
                # of the old one-size-fits-all "lacks hr.leave.type read
                # permission" hint, which is misleading for auth failures.
                _settings_log.warning(
                    "Settings: Odoo fetch_leave_types failed: %s",
                    e, exc_info=True,
                )
                odoo_error = f"{type(e).__name__}: {e}"
                odoo_error_class = type(e).__name__
                leave_types = []
        time_off_settings = {
            "leave_types": leave_types,
            "hidden_ids": settings_store.get_hidden_leave_type_ids(),
            "show_stratustime_overlay": settings_store.get_show_stratustime_overlay(),
            "odoo_configured": _odoo_configured(),
            "odoo_error": odoo_error,
            "odoo_error_class": odoo_error_class,
        }
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
    from .. import rounding_store
    rounding_settings = rounding_store.current()
    rounding_ctx = {
        "in_before_min": rounding_settings.in_before_min,
        "in_after_min": rounding_settings.in_after_min,
        "out_before_min": rounding_settings.out_before_min,
        "out_after_min": rounding_settings.out_after_min,
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
            "rounding": rounding_ctx,
            "integration_status": integration_status,
            "tv_displays_rows": tv_displays_rows,
            "all_dashboards_for_picker": all_dashboards_for_picker,
            "wc_locations_for_picker": [{"name": loc.name} for loc in staffing.LOCATIONS],
            "kiosk_recent_punches": kiosk_recent_punches,
            "kiosk_recent_variances": kiosk_recent_variances,
            "kiosk_sync_status": kiosk_sync_status,
            "time_off_settings": time_off_settings,
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
    return RedirectResponse(url="/settings?saved=1&section=kiosk", status_code=303)


@router.post("/settings/rounding")
async def settings_save_rounding(request: Request):
    """Save the four rounding-window values. Each must be 0 <= v <= 60.
    Out-of-range or unparseable values fall back to 0 (no rounding on
    that side) rather than rejecting the whole submission."""
    from .. import rounding_store
    from ..rounding import RoundingSettings
    form = await request.form()

    def _clamp(raw) -> int:
        try:
            v = int(raw)
        except (TypeError, ValueError):
            return 0
        if v < 0:
            return 0
        if v > 60:
            return 60
        return v

    settings = RoundingSettings(
        in_before_min=_clamp(form.get("in_before_min")),
        in_after_min=_clamp(form.get("in_after_min")),
        out_before_min=_clamp(form.get("out_before_min")),
        out_after_min=_clamp(form.get("out_after_min")),
    )
    rounding_store.save(settings)
    if (request.headers.get("accept") or "").startswith("application/json"):
        return JSONResponse({"ok": True})
    return RedirectResponse(url="/settings?saved=1&section=kiosk", status_code=303)


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


# ---------- Time Off settings (2026-05-27) ----------


def _wants_json(request: Request) -> bool:
    return (request.headers.get("accept") or "").startswith("application/json")


@router.post("/api/settings/time-off/hidden-types")
async def time_off_set_hidden_types(request: Request):
    """Persist the list of leave-type ids that should be hidden from the
    kiosk picker. Posted as a multi-valued `ids` field (one per checked
    checkbox); absent => all visible."""
    form = await request.form()
    raw = form.getlist("ids")
    ids: list[int] = []
    for v in raw:
        try:
            ids.append(int(v))
        except (TypeError, ValueError):
            continue
    settings_store.set_hidden_leave_type_ids(ids)
    if _wants_json(request):
        return JSONResponse({"ok": True})
    return RedirectResponse(url="/settings?saved=1&section=time_off",
                            status_code=303)


@router.post("/api/settings/time-off/overlay")
async def time_off_set_overlay(request: Request,
                               enabled: str = Form(default="off")):
    """Toggle the StratusTime overlay on the admin calendar. Checkbox
    posts `enabled=on` when checked and omits the field entirely when
    unchecked, which Form(default='off') turns into off."""
    settings_store.set_show_stratustime_overlay(enabled == "on")
    if _wants_json(request):
        return JSONResponse({"ok": True})
    return RedirectResponse(url="/settings?saved=1&section=time_off",
                            status_code=303)


@router.post("/api/settings/time-off/refresh-now")
def time_off_refresh_now(request: Request):
    """One-shot admin action — runs the Odoo leaves poller synchronously
    so the next page render sees a fresh local mirror. Swallows
    exceptions so the redirect still works when Odoo is down.

    Busts the in-process leave-types cache first so the poller's call
    to ``fetch_leave_types`` actually hits Odoo instead of returning the
    cached (possibly empty) list — that's the whole point of clicking
    Refresh.
    """
    from .. import odoo_client, time_off_sync
    # Force the next fetch_leave_types() to hit Odoo, not the 10-min
    # cache. If a previous call returned [] silently (e.g. due to an
    # earlier XML-RPC permission error), the cache would otherwise hold
    # that empty list and the Refresh button would be a no-op.
    odoo_client._leave_types_cache = None
    try:
        time_off_sync.poll_odoo_leaves()
    except Exception:
        pass
    if _wants_json(request):
        return JSONResponse({"ok": True})
    return RedirectResponse(url="/settings?saved=1&section=time_off",
                            status_code=303)


@router.get("/api/settings/time-off/diagnostics")
def time_off_diagnostics(request: Request):
    """Read-only diagnostic for the kiosk balance panel.

    Compares the local ``leave_types_cache`` against a *live* Odoo pull so
    we can see exactly which ``requires_allocation`` / ``request_unit`` the
    app holds vs. what Odoo reports. Built to diagnose the kiosk showing
    "No allocation tracked" while Odoo itself has correct balances — the
    smoking gun is a cached ``requires_allocation='no'`` for a type that is
    ``'yes'`` in Odoo, plus any row that would fail the cache CHECK
    constraint (which is what aborts the poller's refresh).

    No writes except busting the in-process leave-types cache so the live
    pull is genuinely live.
    """
    from .. import db, odoo_client

    allowed_units = {"day", "half_day", "hour"}
    allowed_req = {"yes", "no"}

    cache_rows = db.query(
        "SELECT holiday_status_id, name, request_unit, requires_allocation, "
        "active, last_pulled_at FROM leave_types_cache ORDER BY name"
    )
    cache = [
        {
            "id": r["holiday_status_id"],
            "name": r["name"],
            "request_unit": r["request_unit"],
            "requires_allocation": r["requires_allocation"],
            "active": r["active"],
            "last_pulled_at": str(r["last_pulled_at"]),
        }
        for r in cache_rows
    ]

    live = None
    live_error = None
    would_fail_check = []
    if _odoo_configured():
        try:
            odoo_client._leave_types_cache = None  # force a real Odoo round-trip
            raw = odoo_client.fetch_leave_types()
            live = []
            for t in raw:
                live.append({
                    "id": t.get("id"),
                    "name": t.get("name"),
                    "request_unit": t.get("request_unit"),
                    "requires_allocation": t.get("requires_allocation"),
                    "active": t.get("active"),
                    "color": t.get("color"),
                })
                reasons = []
                if t.get("request_unit") not in allowed_units:
                    reasons.append(
                        f"request_unit={t.get('request_unit')!r} not in "
                        f"{sorted(allowed_units)}")
                if t.get("requires_allocation") not in allowed_req:
                    reasons.append(
                        f"requires_allocation={t.get('requires_allocation')!r} "
                        f"not in {sorted(allowed_req)}")
                color = t.get("color")
                if isinstance(color, bool) or not isinstance(color, (int, type(None))):
                    reasons.append(f"color={color!r} not int/None")
                if reasons:
                    would_fail_check.append(
                        {"id": t.get("id"), "name": t.get("name"),
                         "reasons": reasons})
        except Exception as e:  # noqa: BLE001
            live_error = repr(e)
    else:
        live_error = "Odoo env not configured on this host"

    mismatches = []
    if live is not None:
        cache_by_id = {c["id"]: c for c in cache}
        for lt in live:
            ct = cache_by_id.get(lt["id"])
            if ct is None:
                mismatches.append(
                    {"id": lt["id"], "name": lt["name"],
                     "issue": "present in Odoo, missing from local cache"})
                continue
            for f in ("requires_allocation", "request_unit", "active"):
                if str(ct.get(f)) != str(lt.get(f)):
                    mismatches.append(
                        {"id": lt["id"], "name": lt["name"], "field": f,
                         "cache": ct.get(f), "odoo": lt.get(f)})

    # Optional per-employee balance probe. Pass ?person=<name substr> or
    # ?employee_odoo_id=<n> to see (a) what's cached in time_off_balances and
    # (b) a LIVE fetch_balances_for() with any Odoo error surfaced — this is
    # how we catch a renamed/changed Odoo field that throws and leaves the
    # balance cache empty (kiosk then shows "—").
    balances_probe = None
    person_q = request.query_params.get("person")
    emp_id_q = request.query_params.get("employee_odoo_id")
    odoo_id = None
    matched_name = None
    if emp_id_q:
        try:
            odoo_id = int(emp_id_q)
        except ValueError:
            odoo_id = None
    elif person_q:
        prow = db.query(
            "SELECT odoo_id, name FROM people WHERE name ILIKE %s "
            "AND odoo_id IS NOT NULL ORDER BY name LIMIT 1",
            (f"%{person_q}%",),
        )
        if prow:
            odoo_id = prow[0]["odoo_id"]
            matched_name = prow[0]["name"]
    if odoo_id is not None:
        cached = db.query(
            "SELECT holiday_status_id, unit, allocated_total, taken, pending, "
            "available, available_practical, last_pulled_at "
            "FROM time_off_balances WHERE person_odoo_id = %s "
            "ORDER BY holiday_status_id",
            (odoo_id,),
        )
        live_bal = None
        live_bal_error = None
        try:
            live_bal = odoo_client.fetch_balances_for(odoo_id)
        except Exception as e:  # noqa: BLE001
            live_bal_error = repr(e)
        balances_probe = {
            "employee_odoo_id": odoo_id,
            "matched_name": matched_name,
            "cached_balances": [
                {
                    "holiday_status_id": r["holiday_status_id"],
                    "unit": r["unit"],
                    "allocated_total": float(r["allocated_total"]),
                    "taken": float(r["taken"]),
                    "pending": float(r["pending"]),
                    "available": float(r["available"]),
                    "available_practical": float(r["available_practical"]),
                    "last_pulled_at": str(r["last_pulled_at"]),
                }
                for r in cached
            ],
            "live_fetch_balances_for": live_bal,
            "live_fetch_balances_error": live_bal_error,
        }

    return JSONResponse({
        "ok": True,
        "odoo_configured": _odoo_configured(),
        "cache": cache,
        "live": live,
        "live_error": live_error,
        "rows_that_would_fail_cache_check": would_fail_check,
        "cache_vs_odoo_mismatches": mismatches,
        "balances_probe": balances_probe,
    })
