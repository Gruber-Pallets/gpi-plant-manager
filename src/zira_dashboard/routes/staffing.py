"""Main staffing scheduler page: GET /staffing and POST /staffing."""

from __future__ import annotations

import asyncio
import copy
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, UTC

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from .. import _http_cache, app_settings, attendance, db, rotation_store, rotation_suggestions, rotation_training, schedule_store, shift_config, staffing, staffing_view, time_format, work_centers_store
from .._http_cache import invalidate_today_cache
from ..deps import templates
from ..plant_day import today as plant_today, now as plant_now
from ..staffing_attendance import _late_emp_ids, _safe_attendance, _safe_time_off_entries

log = logging.getLogger(__name__)

router = APIRouter()

# Persistent fan-out pool for the staffing page render. Module-level so a
# cache-missed request reuses warm threads instead of paying thread spin-up
# for a fresh ThreadPoolExecutor on every render.
_PAGE_POOL = ThreadPoolExecutor(max_workers=8, thread_name_prefix="staffing-page")


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

    def __enter__(self) -> _Phase:
        self._t0 = time.perf_counter()
        return self

    def __exit__(self, *_args) -> None:
        self.store[self.name] = (time.perf_counter() - self._t0) * 1000.0


def _server_timing_header(phases: dict) -> str:
    """Format a Server-Timing value: 'db;dur=42.1, attendance;dur=320.4, ...'."""
    return ", ".join(f"{name};dur={dur:.1f}" for name, dur in phases.items())


def _next_working_day(d: date) -> date:
    """Return the next date after `d` that is a work-day per the shift schedule."""
    wd = schedule_store.current().work_weekdays or frozenset({0, 1, 2, 3, 4})
    nxt = d + timedelta(days=1)
    for _ in range(14):
        if nxt.weekday() in wd:
            return nxt
        nxt += timedelta(days=1)
    return d + timedelta(days=1)


FORKLIFT_TABLETS_WC = "Tablets"
FORKLIFT_LOADING_WC = "Loading/Jockeying"


def _forklift_scheduled_counts(assignments, overload_responders, wc_names):
    """Derive forklift-driver coverage counts from the draft schedule.
    - tablets: unique people assigned to the configured driver work centers
      (the queue drivers we size against -- NOT merely certified people).
      `wc_names` is a tuple of WC names to count as scheduled drivers
      (e.g. ("Tablets",) or ("Tablets", "Loading/Jockeying")).
    - backups: scheduled people flagged as overload responders (can jump in).
    """
    drivers = set()
    for wc in wc_names:
        for n in (assignments.get(wc, []) or []):
            drivers.add(n)
    scheduled = {n for names in assignments.values() for n in (names or [])}
    backups = {n for n in scheduled if n in overload_responders}
    return {"tablets": len(drivers), "backups": len(backups)}


# Recycled rotation is scoped to the three groups the design covers; the daily
# development cap defaults to the engine's own default (two) for Task 4.
_RECYCLED_TRAINING_CAP = 2
AUTO_SCHEDULE_WC_SETTING = "rotation_auto_enabled_work_centers"
AUTO_SCHEDULE_HISTORY_DAYS = 28

# Short per-mode help line for the Staffing schedule-goal control.
# A generic one-liner per mode — no settings/config surface, just enough to
# explain what clicking each button optimizes for.
_ROTATION_MODE_HELP = {
    "optimized": "Optimized favors the strongest coverage on auto work centers.",
    "normal": "Normal balances coverage, preferences, and fair rotation.",
    "training": "Training develops level-1/2 operators while protecting coverage.",
}


def _rotation_mode_help(mode: str) -> str:
    return _ROTATION_MODE_HELP.get(mode or "normal", _ROTATION_MODE_HELP["normal"])


def _recycled_wc_names() -> list[str]:
    """Flat list of Recycled work-center names (Dismantler/Repair/Trim Saw).

    Derived from ``staffing.LOCATIONS`` (no I/O). Lets the Staffing page scope
    the mode-driven rebuild apply to Recycled centers only, so a rebuild never
    touches non-Recycled selections the user may have edited.
    """
    groups = rotation_suggestions._default_group_locations()
    return [center for centers in groups.values() for center in centers]


def _location_order() -> dict[str, int]:
    return {loc.name: i for i, loc in enumerate(staffing.LOCATIONS)}


def _known_work_center_names() -> set[str]:
    return {loc.name for loc in staffing.LOCATIONS}


def _ordered_work_center_names(names) -> list[str]:
    known = _known_work_center_names()
    order = _location_order()
    unique = {str(name).strip() for name in (names or []) if str(name or "").strip() in known}
    return sorted(unique, key=lambda name: order.get(name, 1_000_000))


def _recently_used_work_centers(d: date) -> list[str]:
    """Work centers with scheduled people in the recent past.

    This is the first-run initializer for the global auto-schedule toggle set:
    it looks back four weeks from the viewed schedule day and keeps centers the
    plant has actually used. Once saved, the explicit setting wins.
    """
    start = d - timedelta(days=AUTO_SCHEDULE_HISTORY_DAYS)
    rows = db.query(
        "SELECT DISTINCT wc.name "
        "FROM schedule_assignments sa "
        "JOIN schedules s ON s.day = sa.day "
        "JOIN work_centers wc ON wc.id = sa.wc_id "
        "WHERE s.day < %s "
        "  AND s.day >= %s "
        "  AND COALESCE((s.published_snapshot->>'testing_day')::boolean, s.testing_day, FALSE) = FALSE",
        (d, start),
    )
    return _ordered_work_center_names(row.get("name") for row in rows)


def _enabled_auto_work_centers(d: date) -> set[str]:
    saved = app_settings.get_setting(AUTO_SCHEDULE_WC_SETTING)
    if isinstance(saved, list):
        return set(_ordered_work_center_names(saved))
    enabled = _recently_used_work_centers(d)
    app_settings.set_setting(AUTO_SCHEDULE_WC_SETTING, enabled)
    return set(enabled)


def _save_enabled_auto_work_centers(names) -> list[str]:
    enabled = _ordered_work_center_names(names)
    app_settings.set_setting(AUTO_SCHEDULE_WC_SETTING, enabled)
    return enabled


def _auto_group_maps(
    enabled_work_centers,
) -> tuple[dict[str, tuple[str, ...]], dict[str, tuple[str, ...]]]:
    enabled = set(_ordered_work_center_names(enabled_work_centers))
    locations = {}
    required_skills = {}
    for target in staffing.scheduling_preference_targets():
        centers = tuple(center for center in target.centers if center in enabled)
        if centers:
            locations[target.key] = centers
            required_skills[target.key] = target.required_skills
    return locations, required_skills


def _auto_history_group_locations() -> dict[str, tuple[str, ...]]:
    """Return all canonical Auto groups, including currently disabled centers."""
    return {
        target.key: target.centers
        for target in staffing.scheduling_preference_targets()
    }


def _roster_minus_full_day_off(roster, time_off_entries):
    """Drop people on a full-day absence so the pure engine never seats them.

    The engine is absence-agnostic by design (it's pure); absence is enforced by
    pruning the candidate roster here. Manual locks and training-block people are
    placed by name inside the engine and are unaffected by this filter.
    """
    unavailable = rotation_suggestions._full_day_time_off_names(time_off_entries or [])
    if not unavailable:
        return roster
    return [p for p in roster if p.name not in unavailable]


def _manual_locks_from_sources(assignment_sources, assignments=None):
    """Return ``{wc: [names]}`` for entries whose source is ``manual``.

    A rebuild preserves exactly these; every other Recycled slot is regenerated.
    Order follows the current assignment list when available so locks come back
    in their on-screen order.
    """
    locks: dict[str, list[str]] = {}
    for wc, sources in (assignment_sources or {}).items():
        manual = [name for name, src in (sources or {}).items() if src == "manual"]
        if not manual:
            continue
        current = (assignments or {}).get(wc)
        if current:
            manual_set = set(manual)
            ordered = [n for n in current if n in manual_set]
            ordered += [n for n in manual if n not in ordered]
            manual = ordered
        locks[wc] = manual
    return locks


def _protected_locks(assignment_sources, assignments=None, *, allowed_centers=None):
    """Manual locks plus saved default people, optionally scoped by WC name."""
    allowed = set(allowed_centers) if allowed_centers is not None else None
    locks = _manual_locks_from_sources(assignment_sources, assignments)
    for loc in staffing.LOCATIONS:
        if allowed is not None and loc.name not in allowed:
            continue
        try:
            defaults = work_centers_store.default_people(loc)
        except Exception:
            defaults = []
        if not defaults:
            continue
        existing = locks.setdefault(loc.name, [])
        for name in defaults:
            clean = str(name or "").strip()
            if clean and clean not in existing:
                existing.append(clean)
    if allowed is not None:
        locks = {wc: names for wc, names in locks.items() if wc in allowed}
    return locks


def _absence_by_day_for_block(block, d: date):
    """Full-day-off names per date across the block's bounded planning window.

    ``rotation_training.effect_for_day`` re-derives the block's planned days over
    this map to decide day-one vs. later pairing, so it must cover the window up
    to ``d``. The end is capped at the same horizon ``planned_block_days`` scans
    to (``start_day + planned_attended_days + _MAX_SCAN_DAYS``): past that point
    the effect is empty, so any further per-day lookups are pure waste. Without
    the cap, a block left ``active`` with an old ``start_day`` (extended trainee
    leave, an abandoned block) would fire O(days) serial DB queries on every
    uncached staffing render — the hottest page in the app.
    """
    from .. import scheduler_time_off

    horizon = block.start_day + timedelta(
        days=block.planned_attended_days + rotation_training._MAX_SCAN_DAYS
    )
    end = min(d, horizon)
    absence_by_day: dict[date, set[str]] = {}
    cursor = block.start_day
    while cursor <= end:
        try:
            names = scheduler_time_off.full_day_off_names(cursor)
        except Exception:
            names = set()
        if names:
            absence_by_day[cursor] = set(names)
        cursor += timedelta(days=1)
    return absence_by_day


def _gather_recycled_inputs(d: date, time_off_entries):
    """Reconcile completed blocks, then read the pure engine's inputs.

    Returns ``(preferences, history, block_effects, active_blocks)``. Impure —
    reads preferences, bounded history, and active blocks (with their per-day
    absences). A preferences read failure falls back to ``{}``, which treats
    everyone as Regular; history, reconciliation, and block read failures still
    propagate to the callers' safe defaults.
    """
    try:
        preferences = rotation_store.load_preferences_by_name()
    except Exception:
        log.exception("Could not load Recycled preferences for %s; using regular preferences", d)
        preferences = {}
    history = rotation_suggestions._load_recycled_history(
        d,
        group_locations=_auto_history_group_locations(),
    )
    rotation_training.reconcile_blocks(plant_today())
    active_blocks = rotation_store.active_blocks_for_day(d)
    block_effects = []
    for block in active_blocks:
        absence_by_day = _absence_by_day_for_block(block, d)
        block_effects.append(
            rotation_training.effect_for_day(block, d, absence_by_day=absence_by_day)
        )
    return preferences, history, block_effects, active_blocks


def _recycled_suggestion_for_day(
    d: date, roster, mode: str, base_assignments, locked_assignments, time_off_entries,
    enabled_work_centers=None,
):
    """Compute the pure Recycled suggestion for ``d``, or ``None`` on any failure.

    Loads preferences/history/block effects, prunes absent people, and runs
    ``suggest_recycled_assignments``. Every read is behind one try/except so the
    scheduler always has a stored-defaults fallback (see ``_smart_defaults_for_day``).
    """
    try:
        preferences, history, block_effects, _blocks = _gather_recycled_inputs(d, time_off_entries)
        available = _roster_minus_full_day_off(roster, time_off_entries)
        enabled = set(
            _ordered_work_center_names(
                enabled_work_centers
                if enabled_work_centers is not None
                else _enabled_auto_work_centers(d)
            )
        )
        group_locations, group_required_skills = _auto_group_maps(enabled)
        scoped_locks = {
            wc: list(names or [])
            for wc, names in (locked_assignments or {}).items()
            if wc in enabled
        }
        return rotation_suggestions.suggest_recycled_assignments(
            day=d,
            mode=mode,
            roster=available,
            preferences=preferences,
            base_assignments=base_assignments,
            group_locations=group_locations,
            group_required_skills=group_required_skills,
            history=history,
            locked_assignments=scoped_locks,
            block_effects=block_effects,
            training_cap=_RECYCLED_TRAINING_CAP,
        )
    except Exception:
        log.exception("Recycled suggestion failed for %s; falling back to stored defaults", d)
        return None


def _merge_recycled_assignments(defaults, suggestion) -> dict[str, list[str]]:
    """Overlay the engine's Recycled centers onto a copy of ``defaults``.

    Managed (Recycled) centers are replaced wholesale by the engine's output;
    non-Recycled centers are left exactly as the defaults had them.
    """
    merged = {k: list(v) for k, v in (defaults or {}).items()}
    managed = {c for centers in suggestion.group_locations.values() for c in centers}
    for center in managed:
        merged.pop(center, None)
    for center, names in suggestion.assignments.items():
        merged[center] = list(names)
    return merged


def _training_blocks_context(active_blocks, d: date):
    """Render active blocks into template-friendly dicts with remaining days."""
    out = []
    for block in active_blocks:
        try:
            attended = sum(
                1 for rec in rotation_store.resolved_days(block.id) if rec.status == "attended"
            )
        except Exception:
            attended = 0
        out.append({
            "id": block.id,
            "trainee": block.trainee_name,
            "trainer": block.trainer_name,
            "group": block.skill,
            "skill": block.skill,
            "start_day": block.start_day.isoformat(),
            "planned_attended_days": block.planned_attended_days,
            "remaining_attended_days": max(0, block.planned_attended_days - attended),
        })
    return out


def _recycled_context_for_day(
    d: date, roster, mode: str, base_assignments, locked_assignments, time_off_entries,
    enabled_work_centers=None,
):
    """Recycled template context: mode, per-assignment reasons, warnings, blocks.

    Computed once per GET and derived from a single engine run. Any failure
    degrades to safe empty defaults so the staffing page never 500s on a
    recommendation-data problem.
    """
    ctx = {
        "recycled_rotation_mode": mode or "normal",
        "rotation_reasons": {},
        "rotation_warnings": [],
        "active_training_blocks": [],
    }
    try:
        preferences, history, block_effects, active_blocks = _gather_recycled_inputs(
            d, time_off_entries
        )
        available = _roster_minus_full_day_off(roster, time_off_entries)
        enabled = set(
            _ordered_work_center_names(
                enabled_work_centers
                if enabled_work_centers is not None
                else _enabled_auto_work_centers(d)
            )
        )
        group_locations, group_required_skills = _auto_group_maps(enabled)
        scoped_locks = {
            wc: list(names or [])
            for wc, names in (locked_assignments or {}).items()
            if wc in enabled
        }
        suggestion = rotation_suggestions.suggest_recycled_assignments(
            day=d,
            mode=mode,
            roster=available,
            preferences=preferences,
            base_assignments=base_assignments,
            group_locations=group_locations,
            group_required_skills=group_required_skills,
            history=history,
            locked_assignments=scoped_locks,
            block_effects=block_effects,
            training_cap=_RECYCLED_TRAINING_CAP,
        )
        ctx["rotation_reasons"] = {wc: dict(r) for wc, r in suggestion.reasons.items()}
        ctx["rotation_warnings"] = list(suggestion.warnings)
        ctx["active_training_blocks"] = _training_blocks_context(active_blocks, d)
    except Exception:
        log.exception("Recycled context failed for %s; degrading to empty defaults", d)
    return ctx


def _smart_defaults_for_day(
    d: date,
    roster,
    defaults: dict[str, list[str]],
    time_off_entries,
    mode: str = "normal",
    enabled_work_centers=None,
):
    """Merge the Recycled rotation suggestion into the per-WC default map.

    Overlays the engine's Recycled centers onto ``defaults`` (non-Recycled
    centers untouched). On any failure reading recommendation data, returns the
    raw stored defaults — the same safe fallback the Trim Saw seeding had.
    """
    try:
        enabled = (
            _ordered_work_center_names(
                enabled_work_centers
                if enabled_work_centers is not None
                else _enabled_auto_work_centers(d)
            )
        )
        locks = _protected_locks({}, defaults, allowed_centers=enabled)
    except Exception:
        return {k: list(v) for k, v in (defaults or {}).items()}

    suggestion = _recycled_suggestion_for_day(
        d,
        roster,
        mode,
        base_assignments=defaults,
        locked_assignments=locks,
        time_off_entries=time_off_entries,
        enabled_work_centers=enabled,
    )
    if suggestion is None:
        return {k: list(v) for k, v in (defaults or {}).items()}
    try:
        return _merge_recycled_assignments(defaults, suggestion)
    except Exception:
        return {k: list(v) for k, v in (defaults or {}).items()}


@router.get("/staffing", response_class=HTMLResponse)
def staffing_page(
    request: Request,
    day: str | None = Query(default=None),
    publish_blocked: int = Query(default=0),
    view: str = Query(default="draft"),
):
    from .. import cert_lookup
    phases: dict[str, float] = {}
    _total_t0 = time.perf_counter()
    today = plant_today()
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

    pool = _PAGE_POOL
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
        time_off_entries = f_time_off_entries.result()
    # If this day has both a current draft and a posted snapshot, the user may want
    # to view the posted version. Swap the visible fields in from the snapshot.
    has_snapshot = bool(sched.published_snapshot) and not sched.published
    view_mode = view if view in ("draft", "posted") else "draft"
    viewing_posted = has_snapshot and view_mode == "posted"
    if viewing_posted:
        # ``load_schedule`` returns an in-process cached draft. The posted view
        # is display-only, so never swap snapshot fields into that shared object.
        sched = copy.deepcopy(sched)
        snap = sched.published_snapshot or {}
        sched.assignments = {k: list(v) for k, v in (snap.get("assignments") or {}).items()}
        sched.notes = str(snap.get("notes") or "")
        sched.wc_notes = dict(snap.get("wc_notes") or {})
        sched.testing_day = bool(snap.get("testing_day", False))
        sched.rotation_mode = str(snap.get("rotation_mode") or "normal")
        sched.assignment_sources = {
            wc_name: dict(sources or {})
            for wc_name, sources in (snap.get("assignment_sources") or {}).items()
        }
    try:
        enabled_auto_work_centers = _ordered_work_center_names(_enabled_auto_work_centers(d))
    except Exception:
        log.exception("Could not load auto-schedule work-center settings for %s", d)
        enabled_auto_work_centers = []
    # If the day has no saved assignments, pre-fill from per-work-center defaults.
    seeded_from_defaults = False
    if not sched.assignments:
        seeded: dict[str, list[str]] = {}
        for loc in staffing.LOCATIONS:
            dp = work_centers_store.default_people(loc)
            if dp:
                seeded[loc.name] = list(dp)
        if not seeded:  # fallback for first-run: legacy CSV defaults
            seeded = staffing.default_assignments()
        sched.assignments = _smart_defaults_for_day(
            d,
            roster,
            seeded,
            time_off_entries,
            enabled_work_centers=enabled_auto_work_centers,
        )
        seeded_from_defaults = True

    # Now that the schedule is in hand, kick off attendance in parallel
    # with our render-prep work below.
    f_attendance = pool.submit(_safe_attendance, d, sched, today)

    # Collect Odoo time-off (already fetched in parallel above).
    with _Phase(phases, "attendance"):
        attendance_pkg = f_attendance.result()
        attendance_by_name = attendance_pkg.get("by_name") or {}

    # Resolve the late-emp-id set to roster names for the template highlight.
    late_emp_ids = _late_emp_ids(d, today, attendance_pkg)
    id_to_name = attendance.person_id_to_name(attendance_pkg.get("name_to_id") or {})
    late_names_set = {id_to_name[e] for e in late_emp_ids if e in id_to_name}

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
            e_raw = r["end_utc"]
            e_local = e_raw.astimezone(site_tz) if e_raw is not None else None
            entry = {
                "id": r["id"],
                "wc_name": r["wc_name"],
                "person_name": r["person_name"],
                "first_label": s_local.strftime("%I:%M %p").lstrip("0"),
                "last_label": (e_local.strftime("%I:%M %p").lstrip("0")
                               if e_local is not None else "open"),
                "time_range": (
                    time_format.fmt_time_range(s_local.isoformat(), e_local.isoformat())
                    if e_local is not None
                    else s_local.strftime("%I:%M %p").lstrip("0") + " – open"
                ),
            }
            assignments_done.append(entry)
            attributions_by_wc.setdefault(r["wc_name"], []).append(entry)
    except Exception:
        assignments_done = []
        attributions_by_wc = {}

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
            cleared_partials_today.extend(
                {
                    "request_id": None,
                    "emp_id": None,
                    "name": row["name"],
                    "time_range": "",
                }
                for row in _lr.cleared_partial_names_today_list(d)
            )
    except Exception:
        cleared_partials_today = []

    # Per-work-center render model + left-rail lists. Pure derivation over the
    # already-fetched roster / schedule / Odoo time-off — no I/O. Returns the
    # bands-A+B context keys (bays, publish_block_reasons, defaults_by_loc,
    # unassigned, reserves, time_off_names/entries, partial_*_by_name,
    # people_meta, all_active_people), merged into the template context below.
    bay_model = staffing_view.build_staffing_bays(
        roster=roster,
        sched=sched,
        time_off_entries=time_off_entries,
        publish_blocked=publish_blocked,
    )
    raw_defaults_by_loc = bay_model.get("defaults_by_loc") or {}
    if seeded_from_defaults:
        smart_defaults_by_loc = {k: list(v) for k, v in sched.assignments.items()}
        for loc_name, names in raw_defaults_by_loc.items():
            smart_defaults_by_loc.setdefault(loc_name, list(names))
    else:
        # A saved day keeps its stored mode so the empty-slot fill hints agree
        # with the reason badges/warnings (which also use sched.rotation_mode).
        smart_defaults_by_loc = _smart_defaults_for_day(
            d,
            roster,
            {k: list(v) for k, v in raw_defaults_by_loc.items()},
            time_off_entries,
            mode=sched.rotation_mode or "normal",
            enabled_work_centers=enabled_auto_work_centers,
        )

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

    # Forklift demand advisor (read-only; never blocks scheduling).
    try:
        from .. import app_settings, forklift_advisor, forklift_settings
        _overload = set(app_settings.get_setting("forklift_overload_responders") or [])
        try:
            _fcfg = forklift_settings.current()
        except Exception:
            # A settings-table hiccup must not hide the advisor entirely; fall
            # back to defaults so coverage still counts Tablets drivers.
            _fcfg = forklift_settings.DEFAULT
        _wc_names = (
            (FORKLIFT_TABLETS_WC, FORKLIFT_LOADING_WC)
            if _fcfg.include_loading_jockeying else (FORKLIFT_TABLETS_WC,)
        )
        _counts = _forklift_scheduled_counts(sched.assignments, _overload, _wc_names)
        forklift_advisor_model = forklift_advisor.build_advisor(
            target_day=d, scheduled=_counts["tablets"], backups=_counts["backups"],
        )
        forklift_live_model = dict(
            forklift_advisor_model.get("live_model") or {"available": False}
        )
        if forklift_live_model.get("available"):
            forklift_live_model["driver_wc_names"] = list(_wc_names)
    except Exception:
        forklift_advisor_model = {"available": False}
        forklift_live_model = {"available": False}

    # Recycled rotation context: effective mode, per-assignment reasons,
    # warnings, and active training blocks. Computed once from a single engine
    # run; failures degrade to safe empty defaults so the page never 500s.
    recycled_ctx = _recycled_context_for_day(
        d,
        roster,
        sched.rotation_mode or "normal",
        base_assignments=sched.assignments,
        locked_assignments=_protected_locks(
            sched.assignment_sources,
            sched.assignments,
            allowed_centers=enabled_auto_work_centers,
        ),
        time_off_entries=time_off_entries,
        enabled_work_centers=enabled_auto_work_centers,
    )

    with _Phase(phases, "render"):
        response = templates.TemplateResponse(
            request,
            "staffing.html",
            {
                "active": "plant",
                **recycled_ctx,
                "rotation_mode_help": _rotation_mode_help(
                    recycled_ctx["recycled_rotation_mode"]
                ),
                "auto_schedule_enabled_wc_names": enabled_auto_work_centers,
                "auto_schedule_available_wc_names": [loc.name for loc in staffing.LOCATIONS],
                "recycled_wc_names": _recycled_wc_names(),
                "day": d.isoformat(),
                "day_short": d.strftime("%m/%d/%y"),
                "day_pretty": f"{d.strftime('%A, %B')} {d.day}, {d.year}",
                "tomorrow": _next_working_day(today).isoformat(),
                "today": today.isoformat(),
                "published": sched.published,
                "notes": sched.notes or "",
                "testing_day": bool(sched.testing_day),
                # Pure per-WC render model + left-rail lists (bays,
                # publish_block_reasons, defaults_by_loc, unassigned, reserves,
                # time_off_names/entries, partial_*_by_name, people_meta,
                # all_active_people). See staffing_view.build_staffing_bays.
                **bay_model,
                "smart_defaults_by_loc": smart_defaults_by_loc,
                "cleared_partials_today": cleared_partials_today,
                "attendance_by_name": attendance_by_name,
                "late_names_set": late_names_set,
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
                "forklift_advisor": forklift_advisor_model,
                "forklift_live_model": forklift_live_model,
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
    # All remaining work is blocking (Postgres reads/writes) — run it in a
    # worker thread so the autosave never stalls the event loop (and the TV
    # dashboard polls sharing it).
    return await asyncio.to_thread(_staffing_save_work, request, d, auto, form)


def _staffing_save_work(request: Request, d: date, auto: int, form):
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
    # testing_day, custom_hours, and rotation metadata — only `notes` and
    # `wc_notes` change.
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
            rotation_mode=existing.rotation_mode,
            assignment_sources={
                wc_name: dict(sources or {})
                for wc_name, sources in existing.assignment_sources.items()
            },
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
            rotation_mode=str(snap.get("rotation_mode") or "normal"),
            assignment_sources={
                wc_name: dict(sources or {})
                for wc_name, sources in (snap.get("assignment_sources") or {}).items()
            },
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
        rotation_mode=existing.rotation_mode,
        assignment_sources={
            wc_name: dict(sources or {})
            for wc_name, sources in existing.assignment_sources.items()
        },
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
                try:
                    next_roster = staffing.load_roster()
                    next_time_off = _safe_time_off_entries(next_day)
                    next_enabled = _ordered_work_center_names(
                        _enabled_auto_work_centers(next_day)
                    )
                    smart_defaults = _smart_defaults_for_day(
                        next_day,
                        next_roster,
                        defaults,
                        next_time_off,
                        enabled_work_centers=next_enabled,
                    )
                except Exception:
                    smart_defaults = {k: list(v) for k, v in defaults.items()}
                staffing.save_schedule(staffing.Schedule(
                    day=next_day,
                    published=False,
                    assignments=smart_defaults,
                    rotation_mode=next_sched.rotation_mode,
                    assignment_sources={
                        wc_name: dict(sources or {})
                        for wc_name, sources in next_sched.assignment_sources.items()
                    },
                ))
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

    def _work():
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
        for bs, be, bn in zip(starts, ends, names, strict=False):
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

    return await asyncio.to_thread(_work)


@router.post("/api/staffing/attribute")
async def staffing_attribute(request: Request):
    """Insert one retro WC attribution row.

    Body (JSON):
      day:         ISO date
      wc_name:     work center name
      person_name: person to credit
      start_utc:   ISO datetime (UTC)
      end_utc:     ISO datetime (UTC), optional -- omit/empty => open-ended
    """
    from datetime import date as _date, datetime as _dt
    from .. import wc_attributions
    body = await request.json()
    try:
        day = _date.fromisoformat(body["day"])
        wc = str(body["wc_name"]).strip()
        person = str(body["person_name"]).strip()
        start_utc = _dt.fromisoformat(body["start_utc"])
        raw_end = body.get("end_utc")
        end_utc = _dt.fromisoformat(raw_end) if raw_end else None  # None/"" => open
    except (KeyError, TypeError, ValueError) as e:
        return JSONResponse({"ok": False, "error": f"bad body: {e}"}, status_code=400)
    if not (wc and person):
        return JSONResponse({"ok": False, "error": "missing/invalid fields"}, status_code=400)
    if end_utc is not None and end_utc <= start_utc:
        return JSONResponse({"ok": False, "error": "end must be after start"}, status_code=400)

    from .. import inbox_keys, inbox_log
    actor_upn, actor_name = inbox_log.actor_from(request)
    source = body.get("source")

    def _work():
        new_id = wc_attributions.add(day, wc, person, start_utc, end_utc)
        # Department transfer side-effect: if the person physically moved to this
        # WC's department, reflect it in Odoo. Never let an Odoo hiccup fail the
        # attribution write — the credit is the source of truth.
        from .. import staffing_transfer
        try:
            transfer = staffing_transfer.decide_and_apply(person, wc, start_utc)
        except Exception as e:  # noqa: BLE001
            transfer = {"transfer": "error", "error": str(e)}
        if source == "inbox":
            inbox_log.log_event_safe(
                item_kind="assignment",
                item_key=inbox_keys.assignment(wc, body["start_utc"]),
                person_name=person,
                category_label="Assignments To Do",
                action="assign",
                outcome="Credited to " + person,
                after_value=person,
                actor_upn=actor_upn,
                actor_name=actor_name,
                source="inbox",
                reversible=False,
            )
        invalidate_today_cache()
        _bust_assignments_todo_cache()
        return JSONResponse({"ok": True, "id": new_id, "transfer": transfer})

    return await asyncio.to_thread(_work)


@router.delete("/api/staffing/attribute/{attribution_id}")
def staffing_attribute_delete(attribution_id: int):
    """Remove one retro WC attribution row by id."""
    from .. import wc_attributions
    try:
        wc_attributions.delete(attribution_id)
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)
    invalidate_today_cache()
    _bust_assignments_todo_cache()
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

    def _work():
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
        _bust_assignments_todo_cache()
        return JSONResponse({"ok": True, "ids": ids, "transfer": transfer})

    return await asyncio.to_thread(_work)


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

    def _work():
        try:
            odoo_client.undo_transfer(closed_id, new_id)
        except Exception as e:  # noqa: BLE001
            return JSONResponse({"ok": False, "error": str(e)}, status_code=500)
        invalidate_today_cache()
        return JSONResponse({"ok": True})

    return await asyncio.to_thread(_work)


_ASSIGNMENTS_TODO_CACHE: dict = {"value": None, "expires_at": 0.0}


def assignments_todo_payload(force: bool = False) -> dict:
    """Snapshot for the global "Assignments to Do" nav badge + modal.

    Always for today. Returns count, items (pending), saved (already
    attributed today), and the active-people roster.

    Cached in-process for 30 s (same pattern as the late-report cache
    below). Polled by every page load's footer and every /tv/new reload;
    each cold call pays schedule + attribution + roster + Zira-cache work.
    Attribution writes bust it via _bust_assignments_todo_cache.

    ``force=True`` skips the cache read and recomputes, resetting the TTL.
    The inbox warmer (page_warmer.warm_inbox_once) calls it this way on a
    cadence below the 30 s TTL so build_summary()'s nav badge — rendered on
    every page via _topnav.html — never pays this cold cascade on a human
    request.
    """
    from .. import staffing as _staffing, wc_attributions
    from ..deps import client as _client
    now_ts = time.time()
    cached = _ASSIGNMENTS_TODO_CACHE.get("value")
    if not force and cached is not None and now_ts < _ASSIGNMENTS_TODO_CACHE.get("expires_at", 0):
        return cached

    today = plant_today()
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
            e_raw = r["end_utc"]
            e_local = e_raw.astimezone(site_tz) if e_raw is not None else None
            out["saved"].append({
                "id": r["id"],
                "wc_name": r["wc_name"],
                "person_name": r["person_name"],
                "first_label": s_local.strftime("%I:%M %p").lstrip("0"),
                "last_label": (e_local.strftime("%I:%M %p").lstrip("0")
                               if e_local is not None else "open"),
            })
        roster = _staffing.load_roster()
        out["people"] = sorted((p.name for p in roster if p.active), key=str.lower)
        out["count"] = len(out["items"])
    except Exception:
        out["degraded"] = True
    _ASSIGNMENTS_TODO_CACHE["value"] = out
    _ASSIGNMENTS_TODO_CACHE["expires_at"] = now_ts + 30.0
    return out


@router.get("/api/assignments-todo")
def assignments_todo_json():
    """JSON snapshot for the global "Assignments to Do" nav badge + modal."""
    return JSONResponse(assignments_todo_payload())


def _bust_assignments_todo_cache() -> None:
    _ASSIGNMENTS_TODO_CACHE["value"] = None
    _ASSIGNMENTS_TODO_CACHE["expires_at"] = 0.0


_LATE_REPORT_CACHE: dict = {"value": None, "expires_at": 0.0}


def late_report_payload(force: bool = False) -> dict:
    """Snapshot for the global Late/Absence Report badge + modal.

    Always for today. Covers people who were on today's schedule only —
    people not assigned today are never flagged for a missing punch. Returns
    four sections:
      scheduled_late:   scheduled people who haven't punched in past threshold
      unscheduled_late: always empty (kept for the JSON/UI contract)
      needs_reason:     scheduled people who punched in past threshold + no
                        late_arrivals record yet — manager fills in reason
      snoozed:          silenced rows (no reason field; transient)

    `late` is an alias for `scheduled_late` for legacy clients.
    `count` is the badge number = sum of the three actionable sections.

    Cached in-process for 30 s. Polled by every page footer every 60 s.
    ``force=True`` skips the cache read and recomputes, resetting the TTL —
    used by the inbox warmer to keep the nav badge warm for human requests.
    """
    from .. import late_report
    now_ts = time.time()
    cached = _LATE_REPORT_CACHE.get("value")
    if not force and cached is not None and now_ts < _LATE_REPORT_CACHE.get("expires_at", 0):
        return cached

    today = plant_today()
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
            now_local = plant_now()
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
            # The report covers people who were on today's schedule only.
            # "Unscheduled" people (active non-reserve roster members who simply
            # weren't assigned today) are NOT flagged for a missing punch — they
            # weren't expected in, so a no-punch isn't an exception. Passing an
            # empty unscheduled set keeps both the unscheduled_late section and
            # the unscheduled half of needs_reason empty. (Product decision
            # 2026-06-27: not on the schedule → not flagged.)

            sections = late_report.late_people_for_day_v2(
                day=today,
                scheduled_emp_ids=scheduled_ids,
                unscheduled_emp_ids=[],
                attendance=by_id,
                now_local=now_local,
                shift_start_local=shift_start_local,
                absent_ids=absent_ids,
                snoozed_ids=snoozed_ids,
                already_recorded_late_ids=already_recorded_late_ids,
            )

            id_to_name = attendance.person_id_to_name(attendance_pkg.get("name_to_id") or {})
            scheduled_wc_by_name = {}
            for wc_name, names in (sched.assignments or {}).items():
                for person_name in names or []:
                    scheduled_wc_by_name.setdefault(person_name, wc_name)

            def _resolve(emp_id):
                # id_to_name covers all active people (Odoo). No StratusTime fallback.
                return id_to_name.get(emp_id) or f"Unknown ({emp_id})"

            for r in sections["scheduled_late"]:
                name = _resolve(r["emp_id"])
                out["scheduled_late"].append({
                    "emp_id": r["emp_id"],
                    "name": name,
                    "minutes_late": r["minutes_late"],
                    "scheduled_wc": scheduled_wc_by_name.get(name),
                    "scheduled_start_time": shift_start_local.strftime("%H:%M"),
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
        now_utc = datetime.now(UTC)
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
        out["degraded"] = True
    _LATE_REPORT_CACHE["value"] = out
    _LATE_REPORT_CACHE["expires_at"] = now_ts + 30.0
    return out


@router.get("/api/late-report")
def late_report_json():
    """JSON snapshot for the global Late/Absence Report badge + modal."""
    return JSONResponse(late_report_payload())


def _bust_late_report_cache() -> None:
    _LATE_REPORT_CACHE["value"] = None
    _LATE_REPORT_CACHE["expires_at"] = 0.0


def _bust_after_mutation() -> None:
    """Drop every cache that could now be stale after a write.

    Called from POST endpoints that mutate Postgres state (clear-partial,
    declare-absent, snooze, attribute, etc.). Drops the late-report
    response cache, the assignments-todo cache, and the today bucket of
    the response cache."""
    _bust_late_report_cache()
    _bust_assignments_todo_cache()
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

    def _work():
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

    return await asyncio.to_thread(_work)


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
    def _work():
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
            rotation_mode=existing.rotation_mode,
            assignment_sources={
                wc_name: dict(sources or {})
                for wc_name, sources in existing.assignment_sources.items()
            },
        ))
        _bust_after_mutation()
        return JSONResponse({"ok": True})

    return await asyncio.to_thread(_work)


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

    def _work():
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

    return await asyncio.to_thread(_work)
