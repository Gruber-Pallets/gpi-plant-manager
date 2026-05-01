"""Main staffing scheduler page: GET /staffing and POST /staffing."""

from __future__ import annotations

import time
from datetime import date, datetime, timedelta, timezone

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from .. import _http_cache, schedule_store, shift_config, staffing, stratustime_client, work_centers_store
from ..deps import templates

router = APIRouter()


class _Phase:
    """Tiny context manager that records milliseconds elapsed under a name.

    Used to build a Server-Timing header so the GET /staffing response
    exposes phase durations (db, stratustime, render, total) directly in
    browser devtools' Network → Timing tab.
    """

    __slots__ = ("store", "name", "_t0")

    def __init__(self, store: dict, name: str) -> None:
        self.store = store
        self.name = name

    def __enter__(self) -> "_Phase":
        self._t0 = time.perf_counter()
        return self

    def __exit__(self, *_args) -> None:
        self.store[self.name] = (time.perf_counter() - self._t0) * 1000.0


def _server_timing_header(phases: dict) -> str:
    """Format a Server-Timing value: 'db;dur=42.1, stratustime;dur=320.4, ...'."""
    return ", ".join(f"{name};dur={dur:.1f}" for name, dur in phases.items())


def _safe_time_off_entries(d):
    """Wrap stratustime_client.time_off_entries_for_day so a StratusTime
    outage never breaks /staffing rendering."""
    try:
        return stratustime_client.time_off_entries_for_day(d)
    except Exception:
        return []


def _safe_attendance(d, sched, today):
    """Wrap StratusTime attendance lookup. Returns {} on any error or
    when attendance isn't applicable (not today, or before shift start).
    """
    if d != today:
        return {}
    try:
        now_local = datetime.now(timezone.utc).astimezone(shift_config.SITE_TZ)
        shift_start_local = datetime.combine(
            d, shift_config.shift_start_for(d), tzinfo=shift_config.SITE_TZ
        )
        if now_local < shift_start_local:
            return {}
        name_to_id = stratustime_client.name_to_emp_id_map()
        scheduled_names: set[str] = set()
        for ops in sched.assignments.values():
            for n in (ops or []):
                if n:
                    scheduled_names.add(n)
        scheduled_ids = [name_to_id[n] for n in scheduled_names if n in name_to_id]
        id_to_name = {v: k for k, v in name_to_id.items()}
        attendance_by_id = stratustime_client.attendance_for_day(d, scheduled_ids)
        out: dict[str, dict] = {}
        for emp_id, info in attendance_by_id.items():
            name = id_to_name.get(emp_id)
            if name:
                out[name] = info
        return out
    except Exception:
        return {}


def _next_working_day(d: date) -> date:
    """Return the next date after `d` that is a work-day per the shift schedule."""
    wd = schedule_store.current().work_weekdays or frozenset({0, 1, 2, 3, 4})
    nxt = d + timedelta(days=1)
    for _ in range(14):
        if nxt.weekday() in wd:
            return nxt
        nxt += timedelta(days=1)
    return d + timedelta(days=1)


@router.get("/staffing", response_class=HTMLResponse)
def staffing_page(
    request: Request,
    day: str | None = Query(default=None),
    publish_blocked: int = Query(default=0),
    view: str = Query(default="draft"),
):
    from concurrent.futures import ThreadPoolExecutor
    from .. import cert_lookup
    phases: dict[str, float] = {}
    _total_t0 = time.perf_counter()
    today = datetime.now(timezone.utc).date()
    # Default to the next working day (Dale plans the day before; skip weekends).
    try:
        d = date.fromisoformat(day) if day else _next_working_day(today)
    except ValueError:
        d = _next_working_day(today)
    # One pool fans out everything that doesn't depend on the schedule:
    # 3 DB reads (certs, roster, schedule) + StratusTime time-off. The
    # attendance fetch is fired AFTER the schedule resolves (it needs
    # `sched.assignments`) but still runs concurrently with the rest of
    # the page-prep work.
    with ThreadPoolExecutor(max_workers=5) as pool:
        with _Phase(phases, "db"):
            f_certs = pool.submit(cert_lookup.load_person_certs)
            f_roster = pool.submit(staffing.load_roster)
            f_sched = pool.submit(staffing.load_schedule, d)
            f_time_off_entries = pool.submit(_safe_time_off_entries, d)
            person_certs = f_certs.result()
            roster = f_roster.result()
            sched = f_sched.result()
        # If this day has both a current draft and a posted snapshot, the user may want
        # to view the posted version. Swap the visible fields in from the snapshot.
        has_snapshot = bool(sched.published_snapshot) and not sched.published
        view_mode = view if view in ("draft", "posted") else "draft"
        viewing_posted = has_snapshot and view_mode == "posted"
        if viewing_posted:
            snap = sched.published_snapshot or {}
            sched.assignments = {k: list(v) for k, v in (snap.get("assignments") or {}).items()}
            sched.notes = str(snap.get("notes") or "")
            sched.wc_notes = dict(snap.get("wc_notes") or {})
            sched.testing_day = bool(snap.get("testing_day", False))
        # If the day has no saved assignments, pre-fill from per-work-center defaults.
        if not sched.assignments:
            seeded: dict[str, list[str]] = {}
            for loc in staffing.LOCATIONS:
                dp = work_centers_store.default_people(loc)
                if dp:
                    seeded[loc.name] = list(dp)
            if not seeded:  # fallback for first-run: legacy CSV defaults
                seeded = staffing.default_assignments()
            sched.assignments = seeded

        # Now that the schedule is in hand, kick off attendance in parallel
        # with our render-prep work below.
        f_attendance = pool.submit(_safe_attendance, d, sched, today)

        # Collect StratusTime time-off (already fetched in parallel above).
        with _Phase(phases, "stratustime"):
            time_off_entries = f_time_off_entries.result()
            attendance_by_name = f_attendance.result()

    time_off_names = [e["name"] for e in time_off_entries]
    time_off_set = set(time_off_names)

    active_people = [p for p in roster if p.active]
    all_by_name = {p.name: p for p in roster}

    # Retro WC attributions ("Assignments to Do"). Lists metered WCs that
    # produced units today but have no one scheduled. Wrapped in try/except
    # so a transient StratusTime / Zira / Postgres failure doesn't break the
    # scheduler page.
    assignments_todo: list[dict] = []
    try:
        from .. import wc_attributions
        from ..deps import client as zira_client
        todo = wc_attributions.unattributed_for_day(d, zira_client)
        site_tz = shift_config.SITE_TZ
        for item in todo:
            first = item["first_sample_utc"].astimezone(site_tz)
            last = item["last_sample_utc"].astimezone(site_tz)
            assignments_todo.append({
                "wc_name": item["wc_name"],
                "units": item["units"],
                "first_label": first.strftime("%I:%M %p").lstrip("0"),
                "last_label": last.strftime("%I:%M %p").lstrip("0"),
                "first_iso": item["first_sample_utc"].isoformat(),
                "last_iso": item["last_sample_utc"].isoformat(),
            })
    except Exception:
        assignments_todo = []

    # Saved-today list: existing attributions for the modal's "Saved today"
    # sub-list with delete buttons. Same try/except guard as the todo list.
    assignments_done: list[dict] = []
    try:
        from .. import wc_attributions
        site_tz = shift_config.SITE_TZ
        for r in wc_attributions.for_day(d):
            s_local = r["start_utc"].astimezone(site_tz)
            e_local = r["end_utc"].astimezone(site_tz)
            assignments_done.append({
                "id": r["id"],
                "wc_name": r["wc_name"],
                "person_name": r["person_name"],
                "first_label": s_local.strftime("%I:%M %p").lstrip("0"),
                "last_label": e_local.strftime("%I:%M %p").lstrip("0"),
            })
    except Exception:
        assignments_done = []
    all_active_people = sorted(p.name for p in active_people)

    # Per-person hours-off-today (for partial entries) so the scheduler
    # can show a badge next to their name.
    partial_hours_by_name: dict[str, float] = {
        e["name"]: e["hours"]
        for e in time_off_entries
        if e.get("hours") is not None and e["hours"] < 8 and e["hours"] > 0
    }
    partial_range_by_name: dict[str, str] = {
        e["name"]: e["time_range"]
        for e in time_off_entries
        if e.get("time_range") and e.get("hours") is not None and e["hours"] < 8
    }

    _options_cache: dict[tuple[str, ...], list[dict]] = {}

    def options_for(required: tuple[str, ...]) -> list[dict]:
        """All active people, tagged with trained = (level >= 1 in ALL required skills).
        Untrained people are hidden client-side unless the WC's per-row Training
        checkbox is ticked. Reserves are tagged so they can be split into a
        secondary picker section (office/manager pool, only used when short).

        Memoized within this request — many WCs share the same `required`
        tuple, so we compute each unique skill set only once."""
        cached = _options_cache.get(required)
        if cached is not None:
            return cached
        rows = []
        for p in active_people:
            levels = [p.level(s) for s in required] if required else []
            min_lvl = min(levels) if levels else 0
            trained = bool(levels) and all(l >= 1 for l in levels)
            rows.append({
                "name": p.name,
                "level": min_lvl,
                "color": staffing.skill_color(min_lvl),
                "trained": trained,
                "reserve": p.reserve,
            })
        _options_cache[required] = rows
        return rows

    # Build a location-level render model and group by bay (preserving LOCATIONS order).
    bays: list[dict] = []
    current_bay: str | None = None
    for loc in staffing.LOCATIONS:
        required = tuple(work_centers_store.required_skills(loc))
        min_ops = work_centers_store.min_ops(loc)
        max_ops = work_centers_store.max_ops(loc)
        assigned_names = sched.assignments.get(loc.name, [])
        assigned = []
        for n in assigned_names:
            p = all_by_name.get(n)
            # Color by the lowest level across required skills.
            lvl = min((p.level(s) for s in required), default=0) if p else 0
            assigned.append({"name": n, "level": lvl, "color": staffing.skill_color(lvl)})
        # Filter out anyone in Time Off — they shouldn't appear in any WC's
        # picker. The "currently-assigned safety net" below re-adds anyone
        # already historically assigned to this WC, so dirty data won't be
        # silently dropped.
        pool = [r for r in options_for(required) if r["name"] not in time_off_set]
        # Reserves go last so the template can split them into the bottom group.
        pool.sort(key=lambda r: (r["reserve"], -r["level"], r["name"].lower()))
        assigned_set = {a["name"] for a in assigned}
        # Ensure currently-assigned people appear in pool even if below the filter.
        # (Assigned names are already in the pool since options_for returns everyone,
        # but inactive/deleted people might have been assigned historically.)
        for a in assigned:
            if not any(r["name"] == a["name"] for r in pool):
                pool.append({"name": a["name"], "level": a["level"], "color": a["color"], "trained": a["level"] >= 1, "reserve": False})
        pool.sort(key=lambda r: (r["reserve"], -r["level"], r["name"].lower()))
        # Headcount status
        count = len(assigned)
        hc_status = "ok"
        if count == 0:
            hc_status = "empty"
        elif count < min_ops:
            hc_status = "under"
        elif max_ops is not None and count > max_ops:
            hc_status = "over"
        # Default people for this WC (editable inline in the scheduler).
        defaults_list = work_centers_store.default_people(loc)
        default_set = set(defaults_list)
        # Auto-open the picker's reserves group when a reserve is currently chosen there.
        has_selected_reserve = any(r["reserve"] and r["name"] in assigned_set for r in pool)
        has_default_reserve = any(r["reserve"] and r["name"] in default_set for r in pool)
        row = {
            "loc": loc,
            "assigned": assigned,
            "pool": pool,
            "assigned_set": assigned_set,
            "min_ops": min_ops,
            "max_ops": max_ops,
            "max_ops_label": ("∞" if max_ops is None else str(max_ops)),
            "required_skills": list(required),
            "default_people": defaults_list,
            "default_set": default_set,
            "has_selected_reserve": has_selected_reserve,
            "has_default_reserve": has_default_reserve,
            "hc_status": hc_status,
            "hc_badge": (
                "needs " + str(min_ops) if hc_status == "under"
                else ("max " + str(max_ops) if hc_status == "over" else "")
            ),
            "wc_note": (sched.wc_notes or {}).get(loc.name, ""),
        }
        if loc.bay != current_bay:
            bays.append({"name": loc.bay, "subtitle": staffing.BAY_SUBTITLES.get(loc.bay, ""), "rows": [row]})
            current_bay = loc.bay
        else:
            bays[-1]["rows"].append(row)

    # Only populate block reasons if we just came back from a failed publish attempt.
    publish_block_reasons = []
    if publish_blocked:
        for bay in bays:
            for r in bay["rows"]:
                if r["hc_status"] == "under" and r["min_ops"] >= 2:
                    publish_block_reasons.append(
                        f"{r['loc'].name} requires {r['min_ops']} operators — currently {len(r['assigned'])}."
                    )

    # Per-WC default operators (set in Settings → Work Centers). Exposed as a
    # plain dict for the scheduler's "Reset to defaults" button.
    defaults_by_loc = {
        loc.name: list(work_centers_store.default_people(loc))
        for loc in staffing.LOCATIONS
    }

    # Unscheduled = active non-reserve people with no station and not on time off.
    # Reserves (office staff / managers) live in their own list regardless of state.
    assigned_today = {
        n
        for key, names in sched.assignments.items()
        if key != staffing.TIME_OFF_KEY
        for n in names
    }
    unassigned = [
        p.name
        for p in active_people
        if not p.reserve and p.name not in assigned_today and p.name not in time_off_set
    ]
    reserves = [p.name for p in active_people if p.reserve and p.name not in time_off_set]

    eff_start = shift_config.shift_start_for(d)
    eff_end   = shift_config.shift_end_for(d)
    eff_breaks = [
        {"start": b.start.strftime("%H:%M"),
         "end":   b.end.strftime("%H:%M"),
         "name":  b.name}
        for b in shift_config.breaks_for(d)
    ]
    has_custom_hours = sched.custom_hours is not None
    eff_hours_label = f"{eff_start.strftime('%H:%M')}–{eff_end.strftime('%H:%M')}"

    with _Phase(phases, "render"):
        response = templates.TemplateResponse(
            request,
            "staffing.html",
            {
                "active": "plant",
                "day": d.isoformat(),
                "day_short": d.strftime("%m/%d/%y"),
                "day_pretty": f"{d.strftime('%A, %B')} {d.day}, {d.year}",
                "tomorrow": _next_working_day(today).isoformat(),
                "today": today.isoformat(),
                "published": sched.published,
                "bays": bays,
                "notes": sched.notes or "",
                "testing_day": bool(sched.testing_day),
                "publish_block_reasons": publish_block_reasons,
                "time_off_names": sorted(time_off_names),
                "time_off_entries": sorted(time_off_entries, key=lambda e: e["name"].lower()),
                "partial_hours_by_name": partial_hours_by_name,
                "partial_range_by_name": partial_range_by_name,
                "attendance_by_name": attendance_by_name,
                "unassigned": sorted(unassigned),
                "reserves": sorted(reserves),
                # JS uses this to route auto-removed people back to the right
                # left-rail list (Unscheduled vs Reserves) on uncheck/X.
                "people_meta": {p.name: {"reserve": p.reserve} for p in active_people},
                "defaults_by_loc": defaults_by_loc,
                "skill_labels": staffing.SKILL_LABELS,
                "has_snapshot": has_snapshot,
                "viewing_posted": viewing_posted,
                "view_mode": view_mode,
                "eff_hours_start": eff_start.strftime("%H:%M"),
                "eff_hours_end": eff_end.strftime("%H:%M"),
                "eff_breaks": eff_breaks,
                "has_custom_hours": has_custom_hours,
                "eff_hours_label": eff_hours_label,
                "person_certs": person_certs,
                "assignments_todo": assignments_todo,
                "assignments_done": assignments_done,
                "all_active_people": all_active_people,
            },
        )

    # Past-day staffing pages are immutable, so the browser can cache them
    # for a long time. Today / future days get the short cache (so edits
    # appear immediately on reload).
    _http_cache.set_cache_headers(response, includes_today=(d >= today))

    phases["total"] = (time.perf_counter() - _total_t0) * 1000.0
    response.headers["Server-Timing"] = _server_timing_header(phases)
    return response


@router.post("/staffing")
async def staffing_save(
    request: Request,
    day: str = Query(...),
    auto: int = Query(default=0),
):
    try:
        d = date.fromisoformat(day)
    except ValueError:
        return RedirectResponse("/staffing", status_code=303)
    form = await request.form()
    assignments: dict[str, list[str]] = {}
    for loc in staffing.LOCATIONS:
        picked = form.getlist(f"loc__{loc.name}")
        clean = [n.strip() for n in picked if n and n.strip()]
        if clean:
            assignments[loc.name] = clean
        # Default-people per WC: only persist when the JS marks this WC dirty
        # (i.e., the user actually touched its Defaults picker). Otherwise
        # scheduled-only autosaves would re-write defaults from form state on
        # every keystroke, risking accidental clears.
        if form.get(f"defaults_dirty__{loc.name}") == "1":
            picked_defaults = form.getlist(f"default__{loc.name}")
            clean_defaults = [n.strip() for n in picked_defaults if n and n.strip()]
            work_centers_store.save_one(loc, {"default_people": clean_defaults})
    # Time-off is now sourced from StratusTime (sub-project #2). The scheduler UI
    # no longer collects time-off entries via form fields, so we ignore any
    # `loc____time_off` values that a stale tab might still be posting.

    action = (form.get("action") or "save").strip().lower()
    override = (form.get("override") or "").strip() == "1"
    notes = (form.get("notes") or "").strip()[:2000]
    wc_notes: dict[str, str] = {}
    for loc in staffing.LOCATIONS:
        v = (form.get(f"wc_note__{loc.name}") or "").strip()[:500]
        if v:
            wc_notes[loc.name] = v
    testing_day = (form.get("testing_day") or "").strip() in ("1", "on", "true")

    # Publish-only block: only when action=publish, not overridden, and any min-≥2 work center is partially staffed.
    publish_block: list[str] = []
    if action == "publish" and not override:
        for loc in staffing.LOCATIONS:
            min_required = work_centers_store.min_ops(loc)
            if min_required < 2:
                continue
            count = len(assignments.get(loc.name, []))
            if 0 < count < min_required:
                publish_block.append(
                    f"{loc.name} requires {min_required} operators — currently {count}."
                )

    existing = staffing.load_schedule(d)

    # Discard-draft action: restore the posted snapshot, clear it, and re-publish.
    if action == "discard_draft" and existing.published_snapshot:
        snap = existing.published_snapshot
        restored = staffing.Schedule(
            day=d,
            published=True,
            assignments={k: list(v) for k, v in (snap.get("assignments") or {}).items()},
            notes=str(snap.get("notes") or ""),
            wc_notes=dict(snap.get("wc_notes") or {}),
            testing_day=bool(snap.get("testing_day", False)),
            published_snapshot=None,
            # Discard-draft only reverts the schedule grid; custom_hours are
            # managed independently via the Hours editor and persist.
            custom_hours=existing.custom_hours,
        )
        staffing.save_schedule(restored)
        if (request.headers.get("accept") or "").startswith("application/json"):
            return JSONResponse({"ok": True, "published": True, "discarded": True})
        return RedirectResponse(f"/staffing?day={d.isoformat()}", status_code=303)

    # Determine published state. If publish is blocked, save as draft with existing published state.
    if publish_block:
        published = existing.published
    elif action == "publish":
        published = True
    elif action == "unpublish":
        published = False
    else:
        published = existing.published

    # If the existing day was posted and we're now saving an edit (not publishing),
    # capture a one-time snapshot of the posted version so the user can toggle back.
    published_snapshot = existing.published_snapshot
    if action == "publish" and not publish_block:
        # Re-publish clears any prior snapshot.
        published_snapshot = None
    elif existing.published and action != "publish" and published_snapshot is None:
        # First edit of a posted day: snapshot before overwriting, flip to draft.
        published_snapshot = staffing.snapshot_of(existing)
        published = False
    staffing.save_schedule(staffing.Schedule(
        day=d,
        published=published,
        assignments=assignments,
        notes=notes,
        wc_notes=wc_notes,
        testing_day=testing_day,
        published_snapshot=published_snapshot,
        # Custom hours live alongside the day's schedule and are managed by
        # the dedicated /staffing/hours route. Preserve them through every
        # publish / save / unpublish so the user's overrides aren't dropped.
        custom_hours=existing.custom_hours,
    ))

    # Auto-save (fetch with ?auto=1) → JSON, no redirect.
    if auto or (request.headers.get("accept") or "").startswith("application/json"):
        return JSONResponse({"ok": True, "published": published, "testing_day": testing_day})

    # If publish was blocked, bounce back to the same day with a flag so the UI can show the alert.
    if publish_block:
        return RedirectResponse(f"/staffing?day={d.isoformat()}&publish_blocked=1", status_code=303)

    # Successful publish: advance to next working day and pre-fill with defaults.
    if action == "publish" and published:
        next_day = _next_working_day(d)
        next_sched = staffing.load_schedule(next_day)
        if not next_sched.assignments:
            defaults: dict[str, list[str]] = {}
            for loc in staffing.LOCATIONS:
                dp = work_centers_store.default_people(loc)
                if dp:
                    defaults[loc.name] = list(dp)
            if defaults:
                staffing.save_schedule(staffing.Schedule(day=next_day, published=False, assignments=defaults))
        return RedirectResponse(f"/staffing?day={d.isoformat()}", status_code=303)

    return RedirectResponse(f"/staffing?day={d.isoformat()}", status_code=303)


@router.post("/staffing/hours")
async def staffing_hours_save(request: Request):
    """Persist a per-day shift override (or clear it via reset=1).

    Body fields (multipart/form-data):
      day:          ISO date (required)
      reset:        "1" -> clear custom_hours and exit
      start, end:   "HH:MM" shift bookends
      break_start, break_end, break_name: parallel lists, one entry per break
    """
    form = await request.form()
    day_raw = (form.get("day") or "").strip()
    try:
        d = date.fromisoformat(day_raw)
    except ValueError:
        return JSONResponse({"ok": False, "error": "bad day"}, status_code=400)

    sched = staffing.load_schedule(d)

    if form.get("reset") == "1":
        sched.custom_hours = None
        staffing.save_schedule(sched)
        return JSONResponse({"ok": True, "reset": True})

    start_s = (form.get("start") or "").strip()
    end_s = (form.get("end") or "").strip()
    if not start_s or not end_s or start_s >= end_s:
        return JSONResponse({"ok": False, "error": "shift start must be before end"}, status_code=400)

    starts = form.getlist("break_start")
    ends   = form.getlist("break_end")
    names  = form.getlist("break_name")
    breaks_out: list[dict] = []
    for bs, be, bn in zip(starts, ends, names):
        bs, be = bs.strip(), be.strip()
        if not bs or not be or bs >= be:
            return JSONResponse({"ok": False, "error": f"bad break: {bs}-{be}"}, status_code=400)
        if bs < start_s or be > end_s:
            return JSONResponse({"ok": False, "error": f"break {bs}-{be} outside shift"}, status_code=400)
        breaks_out.append({"start": bs, "end": be, "name": (bn or "Break").strip()[:40]})

    sched.custom_hours = {"start": start_s, "end": end_s, "breaks": breaks_out}
    staffing.save_schedule(sched)
    return JSONResponse({"ok": True})


@router.post("/api/staffing/attribute")
async def staffing_attribute(request: Request):
    """Insert one retro WC attribution row.

    Body (JSON):
      day:         ISO date
      wc_name:     work center name
      person_name: person to credit
      start_utc:   ISO datetime (UTC)
      end_utc:     ISO datetime (UTC)
    """
    from datetime import date as _date, datetime as _dt
    from .. import wc_attributions
    body = await request.json()
    try:
        day = _date.fromisoformat(body["day"])
        wc = str(body["wc_name"]).strip()
        person = str(body["person_name"]).strip()
        start_utc = _dt.fromisoformat(body["start_utc"])
        end_utc = _dt.fromisoformat(body["end_utc"])
    except (KeyError, TypeError, ValueError) as e:
        return JSONResponse({"ok": False, "error": f"bad body: {e}"}, status_code=400)
    if not (wc and person and end_utc > start_utc):
        return JSONResponse({"ok": False, "error": "missing/invalid fields"}, status_code=400)
    new_id = wc_attributions.add(day, wc, person, start_utc, end_utc)
    return JSONResponse({"ok": True, "id": new_id})


@router.delete("/api/staffing/attribute/{attribution_id}")
def staffing_attribute_delete(attribution_id: int):
    """Remove one retro WC attribution row by id."""
    from .. import wc_attributions
    try:
        wc_attributions.delete(attribution_id)
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)
    return JSONResponse({"ok": True})


@router.get("/api/stratustime/refresh")
def stratustime_refresh(back: str | None = Query(default=None)):
    """Bust the StratusTime in-process cache, then redirect back.

    Triggered by a plain `<a>` link from scheduler / time-off pages.
    """
    stratustime_client.cache_clear()
    target = back or "/staffing"
    # Basic safety: only allow same-origin paths.
    if not target.startswith("/"):
        target = "/staffing"
    return RedirectResponse(target, status_code=303)
