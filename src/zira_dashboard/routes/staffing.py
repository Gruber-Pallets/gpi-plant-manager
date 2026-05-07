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
    """Wrap StratusTime attendance lookup. Returns
    {by_name, by_id, name_to_id, scheduled_ids, unscheduled_ids}.

    Returns empty dicts on any error or when attendance isn't applicable
    (not today, or before shift start). by_name keys are roster names;
    by_id keys are StratusTime EmpIdentifiers (used by late_report).

    Fetches attendance for both scheduled people AND active non-reserve
    people who weren't assigned to a WC today — so the Late/Absence
    Report can flag both groups.
    """
    empty = {
        "by_name": {}, "by_id": {}, "name_to_id": {},
        "scheduled_ids": [], "unscheduled_ids": [],
    }
    if d != today:
        return empty
    try:
        now_local = datetime.now(timezone.utc).astimezone(shift_config.SITE_TZ)
        shift_start_local = datetime.combine(
            d, shift_config.shift_start_for(d), tzinfo=shift_config.SITE_TZ
        )
        if now_local < shift_start_local:
            return empty
        name_to_id = stratustime_client.name_to_emp_id_map()
        scheduled_names: set[str] = set()
        for ops in sched.assignments.values():
            for n in (ops or []):
                if n:
                    scheduled_names.add(n)

        # Anyone with an active StratusTime time-off entry today —
        # full-day or partial — is officially excused. They don't
        # belong on the late/absence report. Drop them from both
        # scheduled and unscheduled lists before fetching attendance.
        try:
            time_off_today = set(stratustime_client.time_off_names_for_day(d))
        except Exception:
            time_off_today = set()
        scheduled_names = {n for n in scheduled_names if n not in time_off_today}

        scheduled_ids = [name_to_id[n] for n in scheduled_names if n in name_to_id]

        # Unscheduled = active non-reserve people not in scheduled_names
        # and not on time off (matches the /staffing left-rail
        # "Unscheduled" definition).
        roster = staffing.load_roster()
        unscheduled_names = [
            p.name for p in roster
            if p.active
            and not p.reserve
            and p.name not in scheduled_names
            and p.name not in time_off_today
        ]
        unscheduled_ids = [name_to_id[n] for n in unscheduled_names if n in name_to_id]

        all_ids = list({*scheduled_ids, *unscheduled_ids})
        id_to_name = {v: k for k, v in name_to_id.items()}
        attendance_by_id = stratustime_client.attendance_for_day(d, all_ids)
        by_name: dict[str, dict] = {}
        for emp_id, info in attendance_by_id.items():
            name = id_to_name.get(emp_id)
            if name:
                by_name[name] = info
        return {
            "by_name": by_name,
            "by_id": attendance_by_id,
            "name_to_id": name_to_id,
            "scheduled_ids": scheduled_ids,
            "unscheduled_ids": unscheduled_ids,
        }
    except Exception:
        return empty


def _late_emp_ids(d, today, attendance_pkg) -> set[str]:
    """Compute the set of currently-late StratusTime EmpIdentifiers for `d`.

    Uses the same threshold + filtering as the Late/Absence Report so the
    scheduler highlight stays in sync with the global modal.
    """
    if d != today or not attendance_pkg.get("by_id"):
        return set()
    try:
        from .. import late_report
        now_local = datetime.now(timezone.utc).astimezone(shift_config.SITE_TZ)
        shift_start_local = datetime.combine(
            d, shift_config.shift_start_for(d), tzinfo=shift_config.SITE_TZ
        )
        late = late_report.late_people_for_day(
            d,
            attendance_pkg.get("scheduled_ids") or [],
            attendance_pkg.get("by_id") or {},
            now_local,
            shift_start_local,
        )
        return {r["emp_id"] for r in late}
    except Exception:
        return set()


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

    # Server-side response cache: 15 s for today, 5 min for past days.
    # Most pageviews — including the reload after a clear-partial click —
    # serve from cache and never pay the StratusTime/Zira/DB chain.
    # Mutations (POST /staffing, /api/staffing/attribute, clear-partial,
    # declare-absent, etc.) all call invalidate_today_cache() so saves
    # show up on the next reload regardless of TTL.
    is_today = d >= today
    view_mode_normalized = view if view in ("draft", "posted") else "draft"
    response_cache_key = (
        "staffing", d.isoformat(), view_mode_normalized, int(publish_blocked or 0)
    )
    cached_resp = _http_cache.get_cached_response(response_cache_key, includes_today=is_today)
    if cached_resp is not None:
        return cached_resp

    # One pool fans out everything that doesn't depend on the schedule:
    # 3 DB reads (certs, roster, schedule) + StratusTime time-off. The
    # attendance fetch is fired AFTER the schedule resolves (it needs
    # `sched.assignments`) but still runs concurrently with the rest of
    # the page-prep work.
    def _safe_assignments_todo():
        try:
            from .. import wc_attributions
            from ..deps import client as zira_client
            return wc_attributions.unattributed_for_day(d, zira_client)
        except Exception:
            return []

    def _safe_assignments_done():
        try:
            from .. import wc_attributions
            return wc_attributions.for_day(d)
        except Exception:
            return []

    with ThreadPoolExecutor(max_workers=8) as pool:
        with _Phase(phases, "db"):
            f_certs = pool.submit(cert_lookup.load_person_certs)
            f_roster = pool.submit(staffing.load_roster)
            f_sched = pool.submit(staffing.load_schedule, d)
            f_time_off_entries = pool.submit(_safe_time_off_entries, d)
            # Independent of schedule/roster — fire immediately.
            f_assignments_todo = pool.submit(_safe_assignments_todo)
            f_assignments_done = pool.submit(_safe_assignments_done)
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
            attendance_pkg = f_attendance.result()
            attendance_by_name = attendance_pkg.get("by_name") or {}

    # Resolve the late-emp-id set to roster names for the template highlight.
    late_emp_ids = _late_emp_ids(d, today, attendance_pkg)
    id_to_name = {v: k for k, v in (attendance_pkg.get("name_to_id") or {}).items()}
    late_names_set = {id_to_name[e] for e in late_emp_ids if e in id_to_name}

    time_off_names = [e["name"] for e in time_off_entries]
    time_off_set = set(time_off_names)

    active_people = [p for p in roster if p.active]
    all_by_name = {p.name: p for p in roster}

    # Drain the parallel-pool futures for the two assignment lists.
    site_tz = shift_config.SITE_TZ
    assignments_todo: list[dict] = []
    try:
        for item in (f_assignments_todo.result() or []):
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

    assignments_done: list[dict] = []
    attributions_by_wc: dict[str, list[dict]] = {}
    try:
        for r in (f_assignments_done.result() or []):
            s_local = r["start_utc"].astimezone(site_tz)
            e_local = r["end_utc"].astimezone(site_tz)
            entry = {
                "id": r["id"],
                "wc_name": r["wc_name"],
                "person_name": r["person_name"],
                "first_label": s_local.strftime("%I:%M %p").lstrip("0"),
                "last_label": e_local.strftime("%I:%M %p").lstrip("0"),
                "time_range": stratustime_client._fmt_time_range(
                    s_local.isoformat(), e_local.isoformat()
                ),
            }
            assignments_done.append(entry)
            attributions_by_wc.setdefault(r["wc_name"], []).append(entry)
    except Exception:
        assignments_done = []
        attributions_by_wc = {}
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
    # Per-partial clear key. Every partial gets a × button; the value
    # carries either a request_id (StratusTime time-off request path)
    # or an emp_id (StratusTime non-work-shift path) so the JS can hit
    # the right backend route. Derived/manual absences are full-day
    # and don't appear here.
    partial_clear_by_name: dict[str, dict] = {}
    for e in time_off_entries:
        if e.get("hours") is None or not (0 < e["hours"] < 8):
            continue
        key: dict = {}
        if e.get("request_id"):
            key["request_id"] = int(e["request_id"])
        elif e.get("emp_id"):
            key["emp_id"] = str(e["emp_id"])
        if key:
            partial_clear_by_name[e["name"]] = key
    # Build the "Cleared today" footer list (request_id → person/range)
    # so the user can restore a mis-clicked clear.
    cleared_partials_today: list[dict] = []
    try:
        from .. import late_report as _lr
        if d == today:
            roster_map = stratustime_client._emp_id_to_roster_name_map()
            full_map = stratustime_client._employee_id_to_name_map()
            # Cleared StratusTime time-off requests
            cleared_rows = _lr.cleared_partials_for_day(d)
            if cleared_rows:
                from datetime import timedelta as _td
                req_window = stratustime_client.get_time_off_requests(
                    d - _td(days=3), d + _td(days=3)
                )
                req_by_id = {int(r["ID"]): r for r in req_window if r.get("ID")}
                for row in cleared_rows:
                    rid = int(row["request_id"])
                    r = req_by_id.get(rid)
                    if not r:
                        continue
                    emp_id = str(r.get("EmpIdentifier") or "")
                    nm = roster_map.get(emp_id) or full_map.get(emp_id) or f"Unknown ({emp_id})"
                    start_str = r.get("StartDateTimeSchema") or ""
                    end_str = r.get("EndDateTimeSchema") or ""
                    tr = ""
                    if start_str[:10] == end_str[:10] and start_str:
                        tr = stratustime_client._fmt_time_range(start_str, end_str)
                    cleared_partials_today.append({
                        "request_id": rid,
                        "emp_id": emp_id,
                        "name": nm,
                        "time_range": tr,
                    })
            # Cleared StratusTime non-work shifts
            cleared_nw_rows = _lr.cleared_non_work_for_day(d)
            for row in cleared_nw_rows:
                emp_id = str(row["emp_id"])
                nm = roster_map.get(emp_id) or full_map.get(emp_id) or f"Unknown ({emp_id})"
                cleared_partials_today.append({
                    "request_id": None,
                    "emp_id": emp_id,
                    "name": nm,
                    "time_range": "",
                })
            # Cleared by-name (catch-all path — primary going forward)
            for row in _lr.cleared_partial_names_today_list(d):
                cleared_partials_today.append({
                    "request_id": None,
                    "emp_id": None,
                    "name": row["name"],
                    "time_range": "",
                })
    except Exception:
        cleared_partials_today = []

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
                "partial_clear_by_name": partial_clear_by_name,
                "cleared_partials_today": cleared_partials_today,
                "attendance_by_name": attendance_by_name,
                "late_names_set": late_names_set,
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
                "attributions_by_wc": attributions_by_wc,
                "all_active_people": all_active_people,
            },
        )

    # Past-day staffing pages are immutable, so the browser can cache them
    # for a long time. Today / future days get the short cache (so edits
    # appear immediately on reload).
    _http_cache.set_cache_headers(response, includes_today=is_today)

    phases["total"] = (time.perf_counter() - _total_t0) * 1000.0
    response.headers["Server-Timing"] = _server_timing_header(phases)
    # Stash in the server-side response cache. Mutations bust this via
    # invalidate_today_cache; non-today buckets live for 5 min.
    _http_cache.store_cached_response(
        response_cache_key, includes_today=is_today, response=response
    )
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
        _http_cache.invalidate_today_cache()
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
    # Bust the today response cache so the next GET sees fresh data.
    _http_cache.invalidate_today_cache()

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
        _http_cache.invalidate_today_cache()
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
    _http_cache.invalidate_today_cache()
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
    # Drop cached dashboard responses so the next load reflects the change.
    from .._http_cache import invalidate_today_cache
    invalidate_today_cache()
    return JSONResponse({"ok": True, "id": new_id})


@router.delete("/api/staffing/attribute/{attribution_id}")
def staffing_attribute_delete(attribution_id: int):
    """Remove one retro WC attribution row by id."""
    from .. import wc_attributions
    try:
        wc_attributions.delete(attribution_id)
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)
    from .._http_cache import invalidate_today_cache
    invalidate_today_cache()
    return JSONResponse({"ok": True})


@router.get("/api/assignments-todo")
def assignments_todo_json():
    """JSON snapshot for the global "Assignments to Do" nav badge + modal.

    Always for today. Returns count, items (pending), saved (already
    attributed today), and the active-people roster.
    """
    from .. import staffing as _staffing, wc_attributions
    from ..deps import client as _client
    today = datetime.now(timezone.utc).date()
    out: dict = {"count": 0, "today": today.isoformat(), "items": [], "saved": [], "people": []}
    try:
        site_tz = shift_config.SITE_TZ
        for item in wc_attributions.unattributed_for_day(today, _client):
            first = item["first_sample_utc"].astimezone(site_tz)
            last = item["last_sample_utc"].astimezone(site_tz)
            out["items"].append({
                "wc_name": item["wc_name"],
                "units": item["units"],
                "first_label": first.strftime("%I:%M %p").lstrip("0"),
                "last_label": last.strftime("%I:%M %p").lstrip("0"),
                "first_iso": item["first_sample_utc"].isoformat(),
                "last_iso": item["last_sample_utc"].isoformat(),
            })
        for r in wc_attributions.for_day(today):
            s_local = r["start_utc"].astimezone(site_tz)
            e_local = r["end_utc"].astimezone(site_tz)
            out["saved"].append({
                "id": r["id"],
                "wc_name": r["wc_name"],
                "person_name": r["person_name"],
                "first_label": s_local.strftime("%I:%M %p").lstrip("0"),
                "last_label": e_local.strftime("%I:%M %p").lstrip("0"),
            })
        roster = _staffing.load_roster()
        out["people"] = sorted((p.name for p in roster if p.active), key=str.lower)
        out["count"] = len(out["items"])
    except Exception:
        pass
    return JSONResponse(out)


_LATE_REPORT_CACHE: dict = {"value": None, "expires_at": 0.0}


@router.get("/api/late-report")
def late_report_json():
    """JSON snapshot for the global Late/Absence Report badge + modal.

    Always for today. Returns four sections:
      scheduled_late:   scheduled people who haven't punched in past threshold
      unscheduled_late: active non-reserve people not assigned today + no_punch
      needs_reason:     people who punched in past threshold + no late_arrivals
                        record yet — manager fills in reason and saves
      snoozed:          silenced rows (no reason field; transient)

    `late` is an alias for `scheduled_late` for legacy clients.
    `count` is the badge number = sum of the three actionable sections.

    Cached in-process for 30 s. Polled by every page footer every 60 s.
    """
    from .. import late_report
    now_ts = time.time()
    cached = _LATE_REPORT_CACHE.get("value")
    if cached is not None and now_ts < _LATE_REPORT_CACHE.get("expires_at", 0):
        return JSONResponse(cached)

    today = datetime.now(timezone.utc).date()
    out: dict = {
        "count": 0,
        "today": today.isoformat(),
        "scheduled_late": [],
        "unscheduled_late": [],
        "needs_reason": [],
        "late": [],  # alias for scheduled_late
        "snoozed": [],
    }
    try:
        sched = staffing.load_schedule(today)
        attendance_pkg = _safe_attendance(today, sched, today)
        by_id = attendance_pkg.get("by_id") or {}
        if by_id:
            now_local = datetime.now(timezone.utc).astimezone(shift_config.SITE_TZ)
            shift_start_local = datetime.combine(
                today, shift_config.shift_start_for(today), tzinfo=shift_config.SITE_TZ
            )
            absent_ids = late_report.absent_emp_ids_for_day(today)
            snoozed_ids = {s["emp_id"] for s in late_report.active_snoozes(today)}
            already_recorded_late_ids = late_report.late_arrivals_for_day(today)

            sections = late_report.late_people_for_day_v2(
                day=today,
                scheduled_emp_ids=attendance_pkg.get("scheduled_ids") or [],
                unscheduled_emp_ids=attendance_pkg.get("unscheduled_ids") or [],
                attendance=by_id,
                now_local=now_local,
                shift_start_local=shift_start_local,
                absent_ids=absent_ids,
                snoozed_ids=snoozed_ids,
                already_recorded_late_ids=already_recorded_late_ids,
            )

            id_to_name = {v: k for k, v in (attendance_pkg.get("name_to_id") or {}).items()}
            full_map = stratustime_client._employee_id_to_name_map()

            def _resolve(emp_id):
                return id_to_name.get(emp_id) or full_map.get(emp_id) or f"Unknown ({emp_id})"

            for r in sections["scheduled_late"]:
                out["scheduled_late"].append({
                    "emp_id": r["emp_id"],
                    "name": _resolve(r["emp_id"]),
                    "minutes_late": r["minutes_late"],
                })
            for r in sections["unscheduled_late"]:
                out["unscheduled_late"].append({
                    "emp_id": r["emp_id"],
                    "name": _resolve(r["emp_id"]),
                })
            for r in sections["needs_reason"]:
                out["needs_reason"].append({
                    "emp_id": r["emp_id"],
                    "name": _resolve(r["emp_id"]),
                    "minutes_late": r["minutes_late"],
                })
            out["late"] = list(out["scheduled_late"])  # legacy alias

        # Snoozed list (independent of attendance).
        now_utc = datetime.now(timezone.utc)
        for s in late_report.active_snoozes(today):
            until = s["until_utc"]
            mins_remaining = max(0, int((until - now_utc).total_seconds() // 60))
            out["snoozed"].append({
                "emp_id": s["emp_id"],
                "name": s["name"],
                "until_iso": until.isoformat(),
                "mins_remaining": mins_remaining,
            })
        out["count"] = (
            len(out["scheduled_late"])
            + len(out["unscheduled_late"])
            + len(out["needs_reason"])
        )
    except Exception:
        pass
    _LATE_REPORT_CACHE["value"] = out
    _LATE_REPORT_CACHE["expires_at"] = now_ts + 30.0
    return JSONResponse(out)


def _bust_late_report_cache() -> None:
    _LATE_REPORT_CACHE["value"] = None
    _LATE_REPORT_CACHE["expires_at"] = 0.0


def _bust_after_mutation() -> None:
    """Drop every cache that could now be stale after a write.

    Called from POST endpoints that mutate Postgres state (clear-partial,
    declare-absent, snooze, attribute, etc.). Drops the StratusTime
    in-process cache, the late-report response cache, and the today
    bucket of the response cache."""
    stratustime_client.cache_clear()
    _bust_late_report_cache()
    _http_cache.invalidate_today_cache()


@router.post("/api/late-report/declare-absent")
async def late_report_declare_absent(request: Request):
    """Mark a person as Absent for today.

    Body (JSON): {emp_id, name, reason}

    Reason is REQUIRED — no manual_absences row gets written until
    a non-empty reason is captured. Side effects: writes to
    manual_absences; clears any pending snooze; busts caches.
    """
    from .. import late_report
    body = await request.json()
    emp_id = str(body.get("emp_id") or "").strip()
    name = str(body.get("name") or "").strip()
    reason_raw = body.get("reason")
    reason = str(reason_raw).strip() if reason_raw is not None else ""
    if not emp_id or not name:
        return JSONResponse({"ok": False, "error": "emp_id and name required"}, status_code=400)
    if not reason:
        return JSONResponse(
            {"ok": False, "error": "reason required — no record posts until a reason is given"},
            status_code=400,
        )
    today = datetime.now(timezone.utc).date()
    try:
        late_report.declare_absent(today, emp_id, name, reason=reason)
        from .. import db as _db
        _db.execute(
            "DELETE FROM late_snoozes WHERE day = %s AND emp_id = %s",
            (today, emp_id),
        )
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)
    _bust_after_mutation()
    return JSONResponse({"ok": True})


@router.post("/api/late-report/save-late-arrival")
async def late_report_save_late_arrival(request: Request):
    """Record a late-arrival event for today.

    Body (JSON): {emp_id, name, reason}

    Reason is REQUIRED — no late_arrivals row gets written until a
    non-empty reason is captured. Side effects: writes to
    late_arrivals; busts the report cache so the row drops out of
    needs_reason on the next poll.
    """
    from .. import late_report
    body = await request.json()
    emp_id = str(body.get("emp_id") or "").strip()
    name = str(body.get("name") or "").strip()
    reason_raw = body.get("reason")
    reason = str(reason_raw).strip() if reason_raw is not None else ""
    if not emp_id or not name:
        return JSONResponse({"ok": False, "error": "emp_id and name required"}, status_code=400)
    if not reason:
        return JSONResponse(
            {"ok": False, "error": "reason required — no record posts until a reason is given"},
            status_code=400,
        )
    today = datetime.now(timezone.utc).date()
    try:
        late_report.save_late_arrival(today, emp_id, name, reason=reason)
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)
    _bust_after_mutation()
    return JSONResponse({"ok": True})


@router.post("/api/late-report/snooze")
async def late_report_snooze(request: Request):
    """Silence a person from the Late/Absence Report for `minutes` (default 30).

    Body (JSON): {emp_id, name, minutes?}
    """
    from .. import late_report
    body = await request.json()
    emp_id = str(body.get("emp_id") or "").strip()
    name = str(body.get("name") or "").strip()
    try:
        minutes = int(body.get("minutes") or late_report.DEFAULT_SNOOZE_MINUTES)
    except (TypeError, ValueError):
        minutes = late_report.DEFAULT_SNOOZE_MINUTES
    minutes = max(1, min(minutes, 8 * 60))
    if not emp_id or not name:
        return JSONResponse({"ok": False, "error": "emp_id and name required"}, status_code=400)
    today = datetime.now(timezone.utc).date()
    try:
        late_report.snooze(today, emp_id, name, minutes)
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)
    return JSONResponse({"ok": True, "minutes": minutes})


@router.post("/api/late-report/undo-absent")
async def late_report_undo_absent(request: Request):
    """Reverse a declared absence (e.g., manager mis-clicked).

    Body (JSON): {emp_id}
    """
    from .. import late_report
    body = await request.json()
    emp_id = str(body.get("emp_id") or "").strip()
    if not emp_id:
        return JSONResponse({"ok": False, "error": "emp_id required"}, status_code=400)
    today = datetime.now(timezone.utc).date()
    try:
        late_report.undo_absent(today, emp_id)
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)
    _bust_after_mutation()
    return JSONResponse({"ok": True})


@router.post("/api/staffing/clear-partial")
async def staffing_clear_partial(request: Request):
    """Hide a partial-day time-off entry from the scheduler for one day.

    Primary path: clear by NAME. The user thinks in roster names ('Jose
    Luis'), and that's the most reliable key — works regardless of
    whether the underlying StratusTime entry has a request_id, emp_id,
    or neither.

    Body: {day: ISO date, name: str}

    Back-compat: also still accepts {request_id} or {emp_id} (those
    paths write to their dedicated cleared tables) so old client code
    keeps working until the page reloads with new JS.
    """
    from datetime import date as _date
    from .. import late_report
    body = await request.json()
    try:
        day = _date.fromisoformat(body["day"])
    except (KeyError, TypeError, ValueError) as e:
        return JSONResponse({"ok": False, "error": f"bad day: {e}"}, status_code=400)
    name = (body.get("name") or "").strip()
    request_id = body.get("request_id")
    emp_id = body.get("emp_id")
    if not name and not request_id and not emp_id:
        return JSONResponse(
            {"ok": False, "error": "name (preferred), request_id, or emp_id required"},
            status_code=400,
        )
    try:
        if name:
            late_report.clear_partial_by_name(day, name)
        elif request_id:
            late_report.clear_time_off_request(day, int(request_id))
        else:
            late_report.clear_non_work_shift(day, str(emp_id))
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)
    _bust_after_mutation()
    return JSONResponse({"ok": True})


@router.post("/api/staffing/restore-partial")
async def staffing_restore_partial(request: Request):
    """Undo clear-partial. Same body shape as clear-partial."""
    from datetime import date as _date
    from .. import late_report
    body = await request.json()
    try:
        day = _date.fromisoformat(body["day"])
    except (KeyError, TypeError, ValueError) as e:
        return JSONResponse({"ok": False, "error": f"bad day: {e}"}, status_code=400)
    name = (body.get("name") or "").strip()
    request_id = body.get("request_id")
    emp_id = body.get("emp_id")
    if not name and not request_id and not emp_id:
        return JSONResponse(
            {"ok": False, "error": "name, request_id, or emp_id required"},
            status_code=400,
        )
    try:
        if name:
            late_report.restore_partial_by_name(day, name)
        elif request_id:
            late_report.restore_time_off_request(day, int(request_id))
        else:
            late_report.restore_non_work_shift(day, str(emp_id))
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)
    _bust_after_mutation()
    return JSONResponse({"ok": True})


@router.get("/api/debug/staffing-diag")
def staffing_diag(names: str = Query(default="jesus,porfirio")):
    """One-shot diagnostic for late/absent debugging. Returns the full
    chain of name → emp_id → schedule → attendance → derived absence →
    time-off-set for a comma-separated list of first-name prefixes.

    Hit /api/debug/staffing-diag?names=jesus,porfirio to dump everything
    we know about those people for today.
    """
    from datetime import timedelta
    today = datetime.now(timezone.utc).date()
    prefixes = tuple(p.strip().lower() for p in names.split(",") if p.strip())
    out: dict = {"today": today.isoformat(), "prefixes": list(prefixes)}
    if not prefixes:
        return JSONResponse(out)
    matches = lambda s: any(s.lower().startswith(p) for p in prefixes)
    try:
        emps = stratustime_client.list_employees()
        out["matching_emps"] = [
            {
                "emp_id": str(e.get("EmpIdentifier") or ""),
                "first": (e.get("FirstName") or "").strip(),
                "last": (e.get("LastName") or "").strip(),
                "status": e.get("Status"),
            }
            for e in emps
            if matches(e.get("FirstName") or "")
        ]
        nm = stratustime_client.name_to_emp_id_map()
        out["name_to_id_map_subset"] = {n: i for n, i in nm.items() if matches(n)}
        from .. import staffing as _s
        roster = _s.load_roster()
        out["roster_subset"] = [
            {"name": p.name, "active": p.active, "reserve": p.reserve}
            for p in roster if matches(p.name)
        ]
        sched = _s.load_schedule(today)
        out["schedule_assignments_subset"] = {
            wc: ops for wc, ops in (sched.assignments or {}).items()
            if any(matches(n) for n in (ops or []))
        }
        emp_ids_to_check = sorted({
            str(e.get("EmpIdentifier") or "")
            for e in emps if matches(e.get("FirstName") or "")
        })
        emp_ids_to_check = [e for e in emp_ids_to_check if e]
        out["attendance_for_matching_emps"] = stratustime_client.attendance_for_day(
            today, emp_ids_to_check
        )
        try:
            out["derived_absences_subset"] = [
                d for d in stratustime_client.derived_absences_for_day(today)
                if matches(d.get("name") or "")
            ]
        except Exception as e:
            out["derived_absences_error"] = str(e)
        try:
            toe = stratustime_client.time_off_entries_for_day(today)
            out["time_off_entries_subset"] = [e for e in toe if matches(e.get("name") or "")]
        except Exception as e:
            out["time_off_entries_error"] = str(e)
        try:
            start_ms = stratustime_client._epoch_ms(today)
            end_ms = stratustime_client._epoch_ms(today + timedelta(days=1))
            s, p = stratustime_client.authenticated_post("GetUserSchedule", {
                "StartDate": stratustime_client._wcf_date(start_ms),
                "EndDate": stratustime_client._wcf_date(end_ms),
                "DateTimeSchema": 0,
                "DataAction": {"Name": "SELECT-ALL", "Values": []},
            })
            target_iso = today.isoformat()
            scheduled_ids: list[str] = []
            if isinstance(p, dict):
                results = p.get("Results") or []
                for r in results:
                    eid = str(r.get("EmpIdentifier") or "")
                    if (r.get("StartDateTimeSchema") or "")[:10] == target_iso and eid in emp_ids_to_check:
                        scheduled_ids.append(eid)
            out["stratustime_scheduled_today_subset"] = sorted(set(scheduled_ids))
        except Exception as e:
            out["stratustime_schedule_error"] = str(e)
    except Exception as e:
        out["error"] = str(e)
    return JSONResponse(out)


@router.get("/api/stratustime/refresh")
def stratustime_refresh(back: str | None = Query(default=None)):
    """Bust the StratusTime in-process cache, then redirect back.

    Triggered by a plain `<a>` link from scheduler / time-off pages.
    """
    _bust_after_mutation()
    target = back or "/staffing"
    # Basic safety: only allow same-origin paths.
    if not target.startswith("/"):
        target = "/staffing"
    return RedirectResponse(target, status_code=303)
