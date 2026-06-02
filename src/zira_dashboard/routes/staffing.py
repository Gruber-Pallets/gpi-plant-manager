"""Main staffing scheduler page: GET /staffing and POST /staffing."""

from __future__ import annotations

import time
from datetime import date, datetime, timedelta, timezone

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from .. import _http_cache, schedule_store, shift_config, staffing, time_format, work_centers_store
from .._http_cache import invalidate_today_cache
from ..deps import templates

router = APIRouter()


class _Phase:
    """Tiny context manager that records milliseconds elapsed under a name.

    Used to build a Server-Timing header so the GET /staffing response
    exposes phase durations (db, attendance, render, total) directly in
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
    """Format a Server-Timing value: 'db;dur=42.1, attendance;dur=320.4, ...'."""
    return ", ".join(f"{name};dur={dur:.1f}" for name, dur in phases.items())


def _live_or_fallback(day, *, read, refresh, fallback, transform):
    """Cold-start safety valve for live_cache lookups.

    Reads `read(day)`; if missing or stale, calls `refresh(day)` and re-reads;
    if still empty, returns `fallback()` (caller-provided direct fetch).
    Otherwise returns `transform(payload)`. The fallback already returns the
    caller's final shape, so it's not passed through `transform`.
    """
    from .. import live_cache
    payload, refreshed_at = read(day)
    if payload is None or live_cache.is_stale(refreshed_at):
        try:
            refresh(day)
            payload, _ = read(day)
        except Exception:
            payload = None
        if payload is None:
            return fallback()
    return transform(payload)


def _safe_time_off_entries(d):
    """Time-off entries for the scheduler, sourced from the Odoo-backed
    time_off_requests mirror (approved + pending). Never raises — a query
    failure degrades to an empty panel rather than a 500."""
    from .. import scheduler_time_off
    try:
        return scheduler_time_off.time_off_entries_for_day(d)
    except Exception:  # noqa: BLE001 — empty panel beats a broken scheduler
        return []


def _attendance_with_fallback(day, ids):
    """Return today's per-id Odoo punch dict, filtered to `ids`.

    The cache holds punches for ALL employees; we filter here so callers
    get exactly the subset they asked for. Keys are str(person_odoo_id);
    values are {first_check_in, currently_open}. _safe_attendance turns
    these into a status dict via attendance.compute_status.
    """
    from .. import live_cache, attendance
    wanted = {str(i) for i in ids}
    return _live_or_fallback(
        day,
        read=live_cache.read_attendance,
        refresh=live_cache.refresh_attendance,
        fallback=lambda: attendance.punches_for_day(day),
        transform=lambda payload: {
            sid: info for sid, info in payload.items() if sid in wanted
        },
    )


def _timeoff_names_with_fallback(day):
    """Set of names off on ``day`` (full-day OR partial), from the Odoo-backed
    time_off_requests mirror. Used by _safe_attendance to excuse these people
    from the late/absence report — a partial (e.g. an approved late arrival)
    must still count as excused, so this returns ALL off names, not just the
    full-day ones the scheduler pool excludes."""
    from .. import scheduler_time_off
    try:
        return {
            e["name"]
            for e in scheduler_time_off.time_off_entries_for_day(day)
            if e.get("name")
        }
    except Exception:  # noqa: BLE001 — degrade to "nobody excused" rather than 500
        return set()


def _safe_attendance(d, sched, today):
    """Wrap the Odoo attendance/status lookup. Returns
    {by_name, by_id, name_to_id, scheduled_ids, unscheduled_ids}.

    Returns empty dicts on any error or when attendance isn't applicable
    (not today, or before shift start). by_name keys are roster names;
    by_id keys are str(person_odoo_id) (used by late_report).

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
        from .. import attendance
        name_to_id = attendance.name_to_person_id()
        scheduled_names: set[str] = set()
        for ops in sched.assignments.values():
            for n in (ops or []):
                if n:
                    scheduled_names.add(n)

        # Anyone with an active Odoo time-off entry today —
        # full-day or partial — is officially excused. They don't
        # belong on the late/absence report. Drop them from both
        # scheduled and unscheduled lists before fetching attendance.
        try:
            time_off_today = _timeoff_names_with_fallback(d)
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
        punches = _attendance_with_fallback(d, all_ids)
        attendance_by_id = attendance.compute_status(
            punches, all_ids, now_local, shift_start_local
        )
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
    """Compute the set of currently-late person ids for `d`.

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
        # Same eligibility filter as the report (GET /api/late-report): only
        # hourly, fixed-schedule people. Keeps the scheduler highlight in sync
        # with the modal — flex/salaried people no longer light up red.
        eligible = late_report.report_eligible_emp_ids(
            staffing.load_roster(), attendance_pkg.get("name_to_id") or {}
        )
        scheduled_ids = [
            e for e in (attendance_pkg.get("scheduled_ids") or []) if e in eligible
        ]
        late = late_report.late_people_for_day(
            d,
            scheduled_ids,
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
    # serve from cache and never pay the Odoo/Zira/DB chain.
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
    # 3 DB reads (certs, roster, schedule) + Odoo time-off. The
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

        # Collect Odoo time-off (already fetched in parallel above).
        with _Phase(phases, "attendance"):
            time_off_entries = f_time_off_entries.result()
            attendance_pkg = f_attendance.result()
            attendance_by_name = attendance_pkg.get("by_name") or {}

    # Resolve the late-emp-id set to roster names for the template highlight.
    late_emp_ids = _late_emp_ids(d, today, attendance_pkg)
    id_to_name = {v: k for k, v in (attendance_pkg.get("name_to_id") or {}).items()}
    late_names_set = {id_to_name[e] for e in late_emp_ids if e in id_to_name}

    # Full-day absences drive BOTH the Time Off panel and the roster-availability
    # exclusion. Partial-day people are deliberately NOT treated as "in the Time
    # Off section": they stay in the assignable pool / Unscheduled list (badged
    # with their off-window) so they can still be scheduled around their partial.
    # Full-day entries have hours=None; partials carry a numeric off-span
    # (see scheduler_time_off).
    full_day_entries = [e for e in time_off_entries if e.get("hours") is None]
    time_off_set = {e["name"] for e in full_day_entries}

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
                "time_range": time_format.fmt_time_range(
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
            # By-name is the only clear path now. The legacy StratusTime
            # request-id / non-work-shift clears are retired (StratusTime is
            # off) — and fetching them is exactly what used to blank this
            # whole footer once StratusTime stopped responding.
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
            if required:
                levels = [p.level(s) for s in required]
                min_lvl = min(levels)
                trained = all(l >= 1 for l in levels)
                color = staffing.skill_color(min_lvl)
            else:
                # No required skills → don't color-code; everyone is a
                # valid option. lvl-2 CSS class renders as a neutral pill.
                min_lvl = 2
                trained = True
                color = "neutral"
            rows.append({
                "name": p.name,
                "level": min_lvl,
                "color": color,
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
            if not required:
                # Blank required → render at neutral lvl-2, no color scale.
                lvl = 2
                color = "neutral"
            elif p:
                lvl = min(p.level(s) for s in required)
                color = staffing.skill_color(lvl)
            else:
                lvl = 0
                color = staffing.skill_color(0)
            assigned.append({"name": n, "level": lvl, "color": color})
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
        # Full-day-off / absent people stay assigned in the saved data (picker
        # checkbox + form input below), but are pulled from the station's
        # display and headcount so the slot reads as needing coverage.
        present_assigned = staffing.present_operators(assigned, time_off_set)
        # Headcount status
        count = len(present_assigned)
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
            "present_assigned": present_assigned,
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
                        f"{r['loc'].name} requires {r['min_ops']} operators — currently {len(r['present_assigned'])}."
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

    eff_start = shift_config.configured_shift_start_for(d)
    eff_end   = shift_config.configured_shift_end_for(d)
    eff_breaks = [
        {"start": b.start.strftime("%H:%M"),
         "end":   b.end.strftime("%H:%M"),
         "name":  b.name}
        for b in shift_config.configured_breaks_for(d)
    ]
    hours_source = shift_config.scheduler_hours_source(d, sched.custom_hours is not None)
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
                # Time Off panel + the client-side __timeOffNames set are
                # FULL-DAY only. Partial-day people live in Unscheduled/Reserves
                # (with an off-window badge); listing them here would let the
                # left-rail "defensive sweep" pull them back out of Unscheduled.
                "time_off_names": sorted(e["name"] for e in full_day_entries),
                "time_off_entries": sorted(full_day_entries, key=lambda e: e["name"].lower()),
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
                "hours_source": hours_source,
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
    # Time-off is sourced from the Odoo mirror. The scheduler UI
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

    # Notes-only update on a published schedule. Lets supervisors edit the
    # day's notes (or per-WC notes) after publishing without dropping the
    # schedule back to draft. Preserves assignments, published_snapshot,
    # testing_day, and custom_hours — only `notes` and `wc_notes` change.
    if action == "save_notes":
        staffing.save_schedule(staffing.Schedule(
            day=d,
            published=existing.published,
            assignments={k: list(v) for k, v in existing.assignments.items()},
            notes=notes,
            wc_notes=wc_notes,
            testing_day=existing.testing_day,
            published_snapshot=existing.published_snapshot,
            custom_hours=existing.custom_hours,
        ))
        _http_cache.invalidate_today_cache()
        if (request.headers.get("accept") or "").startswith("application/json"):
            return JSONResponse({"ok": True, "published": existing.published, "notes_only": True})
        return RedirectResponse(f"/staffing?day={d.isoformat()}", status_code=303)

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
    # Department transfer side-effect: if the person physically moved to this
    # WC's department, reflect it in Odoo. Never let an Odoo hiccup fail the
    # attribution write — the credit is the source of truth.
    from .. import staffing_transfer
    try:
        transfer = staffing_transfer.decide_and_apply(person, wc, start_utc)
    except Exception as e:  # noqa: BLE001
        transfer = {"transfer": "error", "error": str(e)}
    invalidate_today_cache()
    return JSONResponse({"ok": True, "id": new_id, "transfer": transfer})


@router.delete("/api/staffing/attribute/{attribution_id}")
def staffing_attribute_delete(attribution_id: int):
    """Remove one retro WC attribution row by id."""
    from .. import wc_attributions
    try:
        wc_attributions.delete(attribution_id)
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)
    invalidate_today_cache()
    return JSONResponse({"ok": True})


@router.post("/api/staffing/attribute-with-testing")
async def staffing_attribute_with_testing(request: Request):
    """Carve a no-credit testing window out of sensed production, then
    optionally attribute the remainder to a real person.

    Body (JSON):
      day, wc_name, testing_start_utc, testing_end_utc,
      sensed_end_utc (optional remainder end; defaults to testing_end_utc),
      remainder_person (optional).
    """
    from datetime import date as _date, datetime as _dt
    from .. import wc_attributions, staffing_transfer
    body = await request.json()
    try:
        day = _date.fromisoformat(body["day"])
        wc = str(body["wc_name"]).strip()
        t_start = _dt.fromisoformat(body["testing_start_utc"])
        t_end = _dt.fromisoformat(body["testing_end_utc"])
    except (KeyError, TypeError, ValueError) as e:
        return JSONResponse({"ok": False, "error": f"bad body: {e}"}, status_code=400)
    if not wc or t_end <= t_start:
        return JSONResponse({"ok": False, "error": "missing/invalid fields"}, status_code=400)

    ids: list[int] = []
    ids.append(wc_attributions.add(
        day, wc, wc_attributions.TESTING_PERSON, t_start, t_end,
        source=wc_attributions.TESTING_SOURCE))

    transfer = {"transfer": "none"}
    remainder = str(body.get("remainder_person") or "").strip()
    if remainder:
        try:
            rem_end = _dt.fromisoformat(body["sensed_end_utc"])
        except (KeyError, TypeError, ValueError):
            rem_end = t_end
        if rem_end > t_end:
            ids.append(wc_attributions.add(day, wc, remainder, t_end, rem_end))
            try:
                transfer = staffing_transfer.decide_and_apply(remainder, wc, t_end)
            except Exception as e:  # noqa: BLE001
                transfer = {"transfer": "error", "error": str(e)}

    invalidate_today_cache()
    return JSONResponse({"ok": True, "ids": ids, "transfer": transfer})


@router.post("/api/staffing/transfer/undo")
async def staffing_transfer_undo(request: Request):
    """Reverse an Odoo department transfer created by an assignment.
    Body (JSON): {closed_id: int|null, new_id: int}."""
    from .. import odoo_client
    body = await request.json()
    try:
        new_id = int(body["new_id"])
        raw_closed = body.get("closed_id")
        closed_id = int(raw_closed) if raw_closed else None
    except (KeyError, TypeError, ValueError) as e:
        return JSONResponse({"ok": False, "error": f"bad body: {e}"}, status_code=400)
    try:
        odoo_client.undo_transfer(closed_id, new_id)
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)
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

            # Eligibility filter: the report applies only to hourly people on
            # a FIXED schedule. Salaried/unknown wage_type (managers) and
            # flexible-schedule people are dropped from all three sections.
            # Source of truth is Odoo (wage_type + Schedule Type), synced into
            # people.wage_type / people.is_flexible.
            name_to_id = attendance_pkg.get("name_to_id") or {}
            eligible_emp_ids = late_report.report_eligible_emp_ids(
                staffing.load_roster(), name_to_id
            )
            scheduled_ids = [e for e in (attendance_pkg.get("scheduled_ids") or []) if e in eligible_emp_ids]
            unscheduled_ids = [e for e in (attendance_pkg.get("unscheduled_ids") or []) if e in eligible_emp_ids]

            sections = late_report.late_people_for_day_v2(
                day=today,
                scheduled_emp_ids=scheduled_ids,
                unscheduled_emp_ids=unscheduled_ids,
                attendance=by_id,
                now_local=now_local,
                shift_start_local=shift_start_local,
                absent_ids=absent_ids,
                snoozed_ids=snoozed_ids,
                already_recorded_late_ids=already_recorded_late_ids,
            )

            id_to_name = {v: k for k, v in (attendance_pkg.get("name_to_id") or {}).items()}

            def _resolve(emp_id):
                # id_to_name covers all active people (Odoo). No StratusTime fallback.
                return id_to_name.get(emp_id) or f"Unknown ({emp_id})"

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
    declare-absent, snooze, attribute, etc.). Drops the late-report
    response cache and the today bucket of the response cache."""
    _bust_late_report_cache()
    _http_cache.invalidate_today_cache()


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


@router.post("/api/staffing/clear-testing-day")
async def staffing_clear_testing_day(request: Request):
    """Flip a schedule's testing_day flag back to False without touching
    anything else (assignments, notes, published state, custom_hours).
    Powers the × on the Testing Day pill at the top of the staffing page.

    The regular save path requires Edit mode on a published schedule, and
    `save_notes` deliberately preserves testing_day so editing notes
    doesn't accidentally undo a Testing Day override. This endpoint is
    the explicit clear path — idempotent, JSON-only, no Edit mode needed.

    Body: {day: ISO date}
    """
    from datetime import date as _date
    body = await request.json()
    try:
        d = _date.fromisoformat(body["day"])
    except (KeyError, TypeError, ValueError) as e:
        return JSONResponse({"ok": False, "error": f"bad day: {e}"}, status_code=400)
    existing = staffing.load_schedule(d)
    if not existing.testing_day:
        return JSONResponse({"ok": True, "no_op": True})
    staffing.save_schedule(staffing.Schedule(
        day=d,
        published=existing.published,
        assignments={k: list(v) for k, v in existing.assignments.items()},
        notes=existing.notes,
        wc_notes=dict(existing.wc_notes),
        testing_day=False,
        published_snapshot=existing.published_snapshot,
        custom_hours=existing.custom_hours,
    ))
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
