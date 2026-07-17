"""Main staffing scheduler page: GET /staffing and POST /staffing."""

from __future__ import annotations

import asyncio
import copy
import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor
from collections.abc import Collection, Sequence
from datetime import date, datetime, timedelta, UTC
from urllib.parse import urlencode

from fastapi import APIRouter, BackgroundTasks, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from .. import _http_cache, app_settings, attendance, auto_schedule_capacity, db, late_report, rotation_store, rotation_suggestions, rotation_training, saturday_recruiting as sr, saturday_recruiting_store, schedule_solver, schedule_store, scheduler_time_off, shift_config, staffing, staffing_view, time_format, time_off_sync, work_centers_store
from .._http_cache import invalidate_today_cache
from ..deps import templates
from ..plant_day import today as plant_today, now as plant_now
from ..staffing_attendance import _late_emp_ids, _safe_attendance, _safe_time_off_entries, _time_off_entries_cached
from ..time_off_wizard import shape_to_hour_bounds

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
DEFAULT_AUTO_WORK_CENTERS_SETTING = "rotation_auto_enabled_work_centers"
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


def _saturday_response_summary(bundle) -> dict[str, list[str]]:
    """Group live Saturday responses for the compact Scheduler status line."""
    summary = {"yes": [], "no": [], "deciding": []}
    if bundle is None or bundle.recruitment.status == "cancelled":
        return summary
    for item in bundle.commitments:
        if item.status == "cancelled":
            continue
        key = (
            "yes" if item.status == "committed"
            else "no" if item.status == "declined"
            else "deciding"
        )
        summary[key].append(item.person_name)
    for names in summary.values():
        names.sort(key=str.lower)
    return summary


def _effective_minimum(loc) -> int:
    """Read the authoritative configured minimum for a work center."""
    return work_centers_store.min_ops(loc)


def _current_minimum_coverage_issues(
    *, roster, assignments, time_off_entries, enabled_centers,
) -> tuple[schedule_solver.PlacementIssue, ...]:
    """Report minimum shortages in the saved schedule currently on screen."""
    enabled = set(_ordered_work_center_names(enabled_centers))
    absent = rotation_suggestions._full_day_time_off_names(time_off_entries or [])
    by_name = {person.name: person for person in roster}
    issues = []
    for loc in staffing.LOCATIONS:
        if loc.name not in enabled:
            continue
        try:
            minimum = max(0, _effective_minimum(loc))
        except Exception:
            minimum = max(0, int(loc.min_ops))
        try:
            required = tuple(work_centers_store.required_skills(loc))
        except Exception:
            required = staffing.required_skills_for(loc)
        safe_names = {
            name
            for name in (assignments or {}).get(loc.name, ())
            if (
                (person := by_name.get(name)) is not None
                and person.active
                and not person.reserve
                and name not in absent
                and all(person.level(skill) >= 1 for skill in required)
            )
        }
        if len(safe_names) < minimum:
            issues.append(schedule_solver.PlacementIssue(
                code="center_minimum_unmet",
                centers=(loc.name,),
                message=(
                    f"{loc.name} is below its minimum staffing level: "
                    f"{len(safe_names)} qualified and present, minimum {minimum}."
                ),
            ))
    return tuple(issues)


def _minimum_crew_balance_for_day(
    *, roster, schedule, time_off_entries, enabled_centers,
    available_names: Collection[str] | None = None,
):
    """Compare people waiting with enabled work centers' open minimum slots."""
    enabled = _ordered_work_center_names(enabled_centers)
    enabled_set = set(enabled)
    absent = rotation_suggestions._full_day_time_off_names(time_off_entries or [])
    by_name = {person.name: person for person in roster}
    assigned = {
        name for center, names in (schedule.assignments or {}).items()
        if center != staffing.TIME_OFF_KEY for name in (names or [])
    }
    available = set(available_names) if available_names is not None else None
    waiting = sum(
        person.active and not person.reserve and person.name not in absent
        and person.name not in assigned
        and (available is None or person.name in available)
        for person in roster
    )
    slots = {}
    for loc in staffing.LOCATIONS:
        try:
            minimum = max(0, _effective_minimum(loc))
        except Exception:
            # The balance is advisory; a Settings read failure must not make
            # the scheduler page unavailable.
            minimum = max(0, int(loc.min_ops))
        if loc.name not in enabled_set:
            slots[loc.name] = minimum
            continue
        qualified = sum(
            person is not None and person.active and not person.reserve
            and person.name not in absent
            and all(person.level(skill) >= 1 for skill in staffing.required_skills_for(loc))
            for name in (schedule.assignments or {}).get(loc.name, ())
            for person in (by_name.get(name),)
        )
        slots[loc.name] = max(0, minimum - qualified)
    return auto_schedule_capacity.analyze_minimum_crew_balance(
        unassigned_people=waiting,
        enabled_centers=enabled,
        disabled_centers=[loc.name for loc in staffing.LOCATIONS if loc.name not in enabled_set],
        open_minimum_slots_by_center=slots,
        center_order=_location_order(),
    )


def _minimum_crew_balance_payload(balance) -> dict[str, object]:
    return {
        "unassigned_people": balance.unassigned_people,
        "open_minimum_slots": balance.open_minimum_slots,
        "direction": balance.direction,
        "center_count": balance.center_count,
        "slot_delta": balance.slot_delta,
        "recommended_centers": list(balance.recommended_centers),
    }


def _publish_shortages(
    assignments: dict[str, list[str]], enabled_work_centers,
) -> list[str]:
    """Return each enabled work center whose submitted crew is below minimum."""
    enabled = set(enabled_work_centers)
    shortages = []
    for loc in staffing.LOCATIONS:
        if loc.name not in enabled:
            continue
        minimum = _effective_minimum(loc)
        count = len(assignments.get(loc.name, []))
        if count < minimum:
            shortages.append(
                f"{loc.name} requires {minimum} operators — currently {count}."
            )
    return shortages


def _configured_center_capacities(
    centers, *, strict: bool = False,
) -> dict[str, int | None]:
    """Read configured maxima for engine input, retaining its static fallback.

    A transient Settings read must not make normal scheduling unavailable.  The
    advisory makes its own strict reads and simply disappears on such a
    failure; the engine can safely retain each Location's built-in maximum.
    """
    wanted = set(centers)
    capacities: dict[str, int | None] = {}
    for loc in staffing.LOCATIONS:
        if loc.name not in wanted:
            continue
        try:
            capacities[loc.name] = work_centers_store.max_ops(loc)
        except Exception:
            if strict:
                raise
            log.exception("Could not load configured maximum for %s; using static fallback", loc.name)
            capacities[loc.name] = loc.max_ops
    return capacities


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


def _default_auto_work_centers(d: date) -> list[str]:
    saved = app_settings.get_setting(DEFAULT_AUTO_WORK_CENTERS_SETTING)
    if isinstance(saved, list):
        return _ordered_work_center_names(saved)
    defaults = _recently_used_work_centers(d)
    app_settings.set_setting(DEFAULT_AUTO_WORK_CENTERS_SETTING, defaults)
    return defaults


def _save_default_auto_work_centers(names, *, cur=None) -> list[str]:
    enabled = _ordered_work_center_names(names)
    app_settings.set_setting(DEFAULT_AUTO_WORK_CENTERS_SETTING, enabled, cur=cur)
    return enabled


def _enabled_auto_work_centers(d: date) -> set[str]:
    return set(staffing.load_schedule(d).auto_enabled_work_centers)


def _saturday_recruit_requested_counts(enabled: Sequence[str]) -> dict[int, int]:
    enabled_names = set(enabled)
    positions_by_name = {
        position.wc_name: position.wc_id
        for position in saturday_recruiting_store.available_positions()
    }
    return {
        positions_by_name[location.name]: _effective_minimum(location)
        for location in staffing.LOCATIONS
        if location.name in enabled_names
        and location.name in positions_by_name
        and _effective_minimum(location) > 0
    }


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


def _protected_locks(
    assignment_sources,
    assignments=None,
    *,
    allowed_centers=None,
    strict_default_reads: bool = False,
    include_saved_defaults: bool = True,
):
    """Manual locks plus saved default people, optionally scoped by WC name."""
    allowed = set(allowed_centers) if allowed_centers is not None else None
    locks = _manual_locks_from_sources(assignment_sources, assignments)
    if include_saved_defaults:
        for loc in staffing.LOCATIONS:
            if allowed is not None and loc.name not in allowed:
                continue
            try:
                defaults = work_centers_store.default_people(loc)
            except Exception:
                if strict_default_reads:
                    raise
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


def _default_inputs(strict: bool = False):
    """Load exact defaults, group defaults, and user-group membership once."""
    try:
        exact = {
            loc.name: tuple(work_centers_store.default_people(loc))
            for loc in staffing.LOCATIONS
        }
        groups = {
            name: tuple(people)
            for name, people in work_centers_store.group_defaults_map().items()
        }
        members = {
            name: tuple(
                loc.name for loc in work_centers_store.members("group", name)
            )
            for name in groups
        }
        return exact, groups, members
    except Exception:
        if strict:
            raise
        log.exception("Could not load automatic-scheduling defaults")
        return {}, {}, {}


def _defaults_only_assignments(
    *, roster, full_day_off_names, exact_defaults, group_defaults,
    user_group_centers, enabled_centers, center_capacities, history,
) -> tuple[dict[str, list[str]], dict[str, dict[str, str]]]:
    available = {
        person.name for person in roster
        if person.active and not person.reserve and person.name not in full_day_off_names
    }
    assignments: dict[str, list[str]] = {}
    sources: dict[str, dict[str, str]] = {}
    assigned: set[str] = set()

    def place(center: str, name: str) -> None:
        if not center or name not in available or name in assigned:
            return
        assignments.setdefault(center, []).append(name)
        sources.setdefault(center, {})[name] = "default"
        assigned.add(name)

    for center, names in exact_defaults.items():
        for raw_name in names:
            place(str(center).strip(), str(raw_name).strip())

    enabled = set(enabled_centers)
    for group, names in group_defaults.items():
        group_centers = tuple(
            center for center in user_group_centers.get(group, ()) if center in enabled
        )
        for raw_name in names:
            name = str(raw_name).strip()
            available_centers = tuple(
                center for center in group_centers
                if center_capacities.get(center) is None
                or len(assignments.get(center, ())) < center_capacities[center]
            )
            if not available_centers or name not in available or name in assigned:
                continue
            least_load = min(len(assignments.get(center, ())) for center in available_centers)
            tied_centers = tuple(
                center for center in available_centers
                if len(assignments.get(center, ())) == least_load
            )
            place(rotation_suggestions.choose_center(name, str(group), tied_centers, history), name)
    return assignments, sources


def _auto_solver_base_assignments(base_assignments, enabled_centers):
    """Keep only assignments outside the Auto solver's owned centers."""
    enabled = set(enabled_centers)
    return {
        center: list(names or [])
        for center, names in (base_assignments or {}).items()
        if center not in enabled
    }


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


def _block_effects_for_day(d: date, time_off_entries, *, assignments=None, assignment_sources=None):
    """Load active training effects while respecting existing manual picks."""
    manual_assignees = {
        name
        for names in _manual_locks_from_sources(assignment_sources, assignments).values()
        for name in names
    }
    effects = []
    for block in rotation_store.active_blocks_for_day(d):
        absence_by_day = _absence_by_day_for_block(block, d)
        effects.append(
            rotation_training.effect_for_day(
                block,
                d,
                absence_by_day=absence_by_day,
                manual_assignees=manual_assignees,
            )
        )
    return effects


def _gather_recycled_inputs(
    d: date,
    time_off_entries,
    *,
    assignments=None,
    assignment_sources=None,
    user_group_centers=None,
):
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
        user_group_centers=user_group_centers,
    )
    rotation_training.reconcile_blocks(plant_today())
    active_blocks = rotation_store.active_blocks_for_day(d)
    manual_assignees = {
        name
        for names in _manual_locks_from_sources(assignment_sources, assignments).values()
        for name in names
    }
    block_effects = []
    for block in active_blocks:
        absence_by_day = _absence_by_day_for_block(block, d)
        block_effects.append(
            rotation_training.effect_for_day(
                block,
                d,
                absence_by_day=absence_by_day,
                manual_assignees=manual_assignees,
            )
        )
    return preferences, history, block_effects, active_blocks


def _recycled_suggestion_for_day(
    d: date, roster, mode: str, base_assignments, locked_assignments, time_off_entries,
    enabled_work_centers=None, assignment_sources=None,
    center_minimums=None, center_capacities=None,
    exact_defaults=None, group_defaults=None, user_group_centers=None,
    minimum_only: bool = False,
):
    """Compute the pure Recycled suggestion for ``d``, or ``None`` on any failure.

    Loads preferences/history/block effects, prunes absent people, and runs
    ``suggest_recycled_assignments``. Every read is behind one try/except so the
    scheduler always has a stored-defaults fallback (see ``_smart_defaults_for_day``).
    """
    try:
        if exact_defaults is None or group_defaults is None or user_group_centers is None:
            exact_defaults, group_defaults, user_group_centers = _default_inputs()
        preferences, history, block_effects, _blocks = _gather_recycled_inputs(
            d,
            time_off_entries,
            assignments=base_assignments,
            assignment_sources=assignment_sources,
            user_group_centers=user_group_centers,
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
        resolved_minimums = center_minimums if center_minimums is not None else {
            loc.name: _effective_minimum(loc)
            for loc in staffing.LOCATIONS if loc.name in enabled
        }
        resolved_capacities = (
            center_capacities
            if center_capacities is not None
            else _configured_center_capacities(enabled)
        )
        scoped_locks = {
            wc: list(names or [])
            for wc, names in (locked_assignments or {}).items()
            if wc in enabled
        }
        solver_base_assignments = _auto_solver_base_assignments(base_assignments, enabled)
        suggestion = rotation_suggestions.suggest_recycled_assignments(
            day=d,
            mode=mode,
            roster=available,
            preferences=preferences,
            base_assignments=solver_base_assignments,
            group_locations=group_locations,
            group_required_skills=group_required_skills,
            history=history,
            locked_assignments=scoped_locks,
            block_effects=block_effects,
            training_cap=_RECYCLED_TRAINING_CAP,
            center_minimums=resolved_minimums,
            center_capacities=resolved_capacities,
            runnable_centers=enabled,
            exact_defaults=exact_defaults,
            group_defaults=group_defaults,
            user_group_centers=user_group_centers,
            minimum_only=minimum_only,
        )
        return suggestion
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
            "work_center": block.work_center,
            "group": staffing.scheduling_group_for_skill(block.skill),
            "skill": block.skill,
            "start_day": block.start_day.isoformat(),
            "planned_attended_days": block.planned_attended_days,
            "remaining_attended_days": max(0, block.planned_attended_days - attended),
            "status": block.status,
        })
    return out


def _page_placement_issues_for_day(
    d: date,
    work_weekdays: frozenset[int],
    issues: tuple[schedule_solver.PlacementIssue, ...],
) -> tuple[schedule_solver.PlacementIssue, ...]:
    """Return page-visible placement issues for the selected Staffing day."""
    if d.weekday() != 5 or 5 in work_weekdays:
        return tuple(issues)
    return tuple(
        issue for issue in issues
        if issue.code != "exact_default_center_disabled"
    )


def _recycled_context_for_day(
    d: date, roster, mode: str, base_assignments, locked_assignments, time_off_entries,
    enabled_work_centers=None, assignment_sources=None, current_assignments=None,
    *, work_weekdays: frozenset[int],
):
    """Recycled template context: mode, per-assignment reasons, warnings, blocks.

    Passive page context defers minimum coverage warnings until an explicit
    scheduling or publish action. Recommendation-data failures retain safe
    empty preview defaults so the staffing page never 500s.
    """
    ctx = {
        "recycled_rotation_mode": mode or "normal",
        "rotation_reasons": {},
        "rotation_reason_codes": {},
        "rotation_warnings": [],
        "rotation_issues": [],
        "active_training_blocks": [],
    }
    try:
        enabled = set(
            _ordered_work_center_names(
                enabled_work_centers
                if enabled_work_centers is not None
                else _enabled_auto_work_centers(d)
            )
        )
    except Exception:
        log.exception(
            "Current staffing coverage failed for %s; degrading to empty defaults", d,
        )
        return ctx

    try:
        exact_defaults, group_defaults, user_group_centers = _default_inputs()
        preferences, history, block_effects, active_blocks = _gather_recycled_inputs(
            d,
            time_off_entries,
            assignments=base_assignments,
            assignment_sources=assignment_sources,
            user_group_centers=user_group_centers,
        )
        available = _roster_minus_full_day_off(roster, time_off_entries)
        group_locations, group_required_skills = _auto_group_maps(enabled)
        center_minimums = {
            loc.name: _effective_minimum(loc)
            for loc in staffing.LOCATIONS if loc.name in enabled
        }
        center_capacities = _configured_center_capacities(enabled)
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
            center_minimums=center_minimums,
            center_capacities=center_capacities,
            runnable_centers=enabled,
            exact_defaults=exact_defaults,
            group_defaults=group_defaults,
            user_group_centers=user_group_centers,
        )
        ctx["rotation_reasons"] = {wc: dict(r) for wc, r in suggestion.reasons.items()}
        ctx["rotation_reason_codes"] = {
            wc: dict(values) for wc, values in suggestion.reason_codes.items()
        }
        action_only_codes = {"person_unplaced", "center_minimum_unmet"}
        action_only_messages = {
            issue.message
            for issue in suggestion.placement_issues
            if issue.code in action_only_codes
        }
        page_placement_issues = tuple(
            issue
            for issue in suggestion.placement_issues
            if issue.code not in action_only_codes
        )
        page_placement_issues = _page_placement_issues_for_day(
            d, work_weekdays, page_placement_issues,
        )
        ctx["rotation_warnings"] = [
            warning
            for warning in suggestion.warnings
            if warning not in action_only_messages
        ]
        ctx["rotation_issues"].extend(
            issue.to_dict()
            for issue in (
                *suggestion.issues,
                *page_placement_issues,
            )
        )
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
    assignment_sources=None,
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
        locks = _protected_locks(assignment_sources, defaults, allowed_centers=enabled)
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
        assignment_sources=assignment_sources,
    )
    if suggestion is None:
        return {k: list(v) for k, v in (defaults or {}).items()}
    try:
        return _merge_recycled_assignments(defaults, suggestion)
    except Exception:
        return {k: list(v) for k, v in (defaults or {}).items()}


def _seed_new_future_draft(
    day: date,
    today: date,
    sched: staffing.Schedule,
    roster: Sequence[staffing.Person],
    time_off_entries,
) -> staffing.Schedule:
    if day <= today or time_off_entries is None:
        return sched
    try:
        if staffing.schedule_revision(day) is not None:
            return sched
        exact_defaults, group_defaults, user_group_centers = _default_inputs(strict=True)
        enabled_centers = _default_auto_work_centers(day)
        center_capacities = _configured_center_capacities(enabled_centers, strict=True)
        history = rotation_suggestions._load_recycled_history(
            day,
            group_locations=_auto_history_group_locations(),
            user_group_centers=user_group_centers,
        )
        assignments, sources = _defaults_only_assignments(
            roster=roster,
            full_day_off_names=rotation_suggestions._full_day_time_off_names(time_off_entries),
            exact_defaults=exact_defaults,
            group_defaults=group_defaults,
            user_group_centers=user_group_centers,
            enabled_centers=enabled_centers,
            center_capacities=center_capacities,
            history=history,
        )
    except Exception:
        log.exception("Could not seed default staffing draft for %s", day)
        return sched
    seeded = staffing.Schedule(
        day=day,
        published=False,
        assignments=assignments,
        notes=sched.notes,
        wc_notes=dict(sched.wc_notes),
        testing_day=sched.testing_day,
        custom_hours=sched.custom_hours,
        rotation_mode=sched.rotation_mode,
        assignment_sources=sources,
        auto_enabled_work_centers=enabled_centers,
    )
    if staffing.create_schedule_if_absent(seeded):
        _http_cache.invalidate_today_cache()
        return seeded
    return staffing.load_schedule(day)


@router.get("/staffing", response_class=HTMLResponse)
def staffing_page(
    request: Request,
    day: str | None = Query(default=None),
    publish_blocked: int = Query(default=0),
    publish_error: list[str] = Query(default=[]),
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
    publish_errors = tuple(
        str(error) for error in publish_error
    ) if isinstance(publish_error, (list, tuple)) else ()
    response_cache_key = (
        "staffing", d.isoformat(), view_mode_normalized, int(publish_blocked or 0),
        publish_errors,
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
        f_time_off_entries = pool.submit(_time_off_entries_cached, d)
        # Independent of schedule/roster — fire immediately.
        f_assignments_todo = pool.submit(_safe_assignments_todo)
        f_assignments_done = pool.submit(_safe_assignments_done)
        person_certs = f_certs.result()
        roster = f_roster.result()
        sched = f_sched.result()
        try:
            time_off_entries = f_time_off_entries.result()
        except Exception:
            log.exception("Could not load time off for future-draft initialization on %s", d)
            time_off_entries = None
    sched = _seed_new_future_draft(d, today, sched, roster, time_off_entries)
    # A failed authoritative read must not create a draft, but the display
    # remains best-effort and can render its time-off panel as empty.
    time_off_entries = time_off_entries or []
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
        sched.saturday_availability_overrides = dict(
            snap.get("saturday_availability_overrides") or {}
        )
        sched.auto_enabled_work_centers = staffing._normalize_auto_enabled_work_centers(
            snap.get("auto_enabled_work_centers")
        )
        sched.custom_hours = copy.deepcopy(snap.get("custom_hours"))
        sched.published_delivery = staffing._delivery_mapping(snap.get("published_delivery"))
    try:
        enabled_auto_work_centers = list(sched.auto_enabled_work_centers)
    except Exception:
        log.exception("Could not load auto-schedule work-center settings for %s", d)
        enabled_auto_work_centers = []
    # Automatic scheduling is an explicit manager action and is available for
    # every displayed calendar day.
    auto_scheduler_available = True

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
        enabled_work_centers=enabled_auto_work_centers,
        publish_errors=publish_errors,
    )
    unscheduled_count = len(bay_model.get("unassigned") or ())
    auto_on_count = len(enabled_auto_work_centers)
    rotation_auto_summary = {
        "unscheduled_count": unscheduled_count,
        "auto_on_count": auto_on_count,
        "delta": auto_on_count - unscheduled_count,
    }
    minimum_crew_balance = _minimum_crew_balance_payload(
        _minimum_crew_balance_for_day(
            roster=roster,
            schedule=sched,
            time_off_entries=time_off_entries,
            enabled_centers=enabled_auto_work_centers,
        )
    )
    raw_defaults_by_loc = bay_model.get("defaults_by_loc") or {}
    if sched.assignments and auto_scheduler_available:
        # A saved day keeps its stored mode so the empty-slot fill hints agree
        # with the reason badges/warnings (which also use sched.rotation_mode).
        smart_defaults_by_loc = _smart_defaults_for_day(
            d,
            roster,
            {k: list(v) for k, v in raw_defaults_by_loc.items()},
            time_off_entries,
            mode=sched.rotation_mode or "normal",
            enabled_work_centers=enabled_auto_work_centers,
            assignment_sources=sched.assignment_sources,
        )
    else:
        smart_defaults_by_loc = {}

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

    saturday_bundle = None
    saturday_positions = []
    saturday_coverage = None
    saturday_commitments = []
    saturday_staffing_commitments = None
    saturday_deadline = None
    saturday_recruit_enabled_count = 0
    saturday_response_summary = {"yes": [], "no": [], "deciding": []}
    if d.weekday() == 5:
        try:
            saturday_bundle = saturday_recruiting_store.get(d)
            saturday_positions = saturday_recruiting_store.available_positions()
            enabled_saturday_centers = set(enabled_auto_work_centers)
            saturday_recruit_enabled_count = sum(
                location.name in enabled_saturday_centers
                for location in staffing.LOCATIONS
            )
            saturday_deadline = (
                saturday_bundle.recruitment.response_deadline
                if saturday_bundle else sr.response_deadline(
                    d,
                    schedule_store.current().work_weekdays,
                    shift_config.configured_shift_start_for,
                )
            )
            saturday_payload = (
                saturday_recruiting_store.serialize_bundle(saturday_bundle)
                if saturday_bundle else None
            )
            saturday_coverage = saturday_payload["coverage"] if saturday_payload else None
            saturday_commitments = saturday_payload["commitments"] if saturday_payload else []
            saturday_response_summary = _saturday_response_summary(saturday_bundle)
            if saturday_bundle is None or saturday_bundle.recruitment.status == "cancelled":
                saturday_staffing_commitments = {}
            else:
                saturday_staffing_commitments = {
                    item.person_name: {
                        "start": item.availability_start,
                        "end": item.availability_end,
                    }
                    for item in saturday_bundle.commitments
                    if item.status == "committed"
                    and item.availability_start is not None
                    and item.availability_end is not None
                }
        except Exception:
            log.exception("Saturday recruiting context failed for %s", d)

    saturday_context = {
        "day_is_saturday": d.weekday() == 5,
        "saturday_recruiting": saturday_bundle.recruitment if saturday_bundle else None,
        "saturday_recruit_enabled_count": saturday_recruit_enabled_count,
        "saturday_response_summary": saturday_response_summary,
        "saturday_positions": saturday_positions,
        "saturday_coverage": saturday_coverage,
        "saturday_commitments": saturday_commitments,
        "saturday_shift_start": (
            saturday_bundle.recruitment.shift_start.strftime("%H:%M")
            if saturday_bundle else eff_start.strftime("%H:%M")
        ),
        "saturday_shift_end": (
            saturday_bundle.recruitment.shift_end.strftime("%H:%M")
            if saturday_bundle else eff_end.strftime("%H:%M")
        ),
        "saturday_deadline_label": (
            sr.format_deadline(saturday_deadline) if saturday_deadline else ""
        ),
        "saturday_publish_locked": bool(
            saturday_bundle
            and saturday_bundle.recruitment.status != "cancelled"
            and plant_now() < saturday_bundle.recruitment.response_deadline
        ),
        "saturday_publish_lock_message": (
            "Saturday recruiting stays open until "
            f"{sr.format_deadline(saturday_bundle.recruitment.response_deadline)}."
            if saturday_bundle
            and saturday_bundle.recruitment.status != "cancelled"
            and plant_now() < saturday_bundle.recruitment.response_deadline
            else ""
        ),
    }

    # Rebuild the pure roster view once Saturday recruiting is known.  A
    # recruiting Saturday starts closed: only commitments enter Unassigned or
    # a work-center picker; everyone else remains visibly Off.
    if d.weekday() == 5:
        bay_model = staffing_view.build_staffing_bays(
            roster=roster,
            sched=sched,
            time_off_entries=time_off_entries,
            publish_blocked=publish_blocked,
            enabled_work_centers=enabled_auto_work_centers,
            saturday_commitments=saturday_staffing_commitments or {},
            saturday_shift=(
                (saturday_bundle.recruitment.shift_start, saturday_bundle.recruitment.shift_end)
                if saturday_bundle and saturday_bundle.recruitment.status != "cancelled"
                else None
            ),
            saturday_availability_overrides=sched.saturday_availability_overrides,
            publish_errors=publish_errors,
        )
        minimum_crew_balance = _minimum_crew_balance_payload(
            _minimum_crew_balance_for_day(
                roster=roster,
                schedule=sched,
                time_off_entries=time_off_entries,
                enabled_centers=enabled_auto_work_centers,
                available_names=bay_model.get("unassigned") or (),
            )
        )

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
    # warnings, and active training blocks. Current staffing issues are computed
    # independently so they survive recommendation-preview failures; preview-only
    # context degrades to safe empty defaults so the page never 500s.
    work_weekdays = (
        schedule_store.current().work_weekdays or frozenset({0, 1, 2, 3, 4})
    )
    recycled_ctx = _recycled_context_for_day(
        d,
        roster,
        sched.rotation_mode or "normal",
        base_assignments=_auto_solver_base_assignments(
            sched.assignments, enabled_auto_work_centers,
        ),
        locked_assignments=_protected_locks(
            sched.assignment_sources,
            sched.assignments,
            allowed_centers=enabled_auto_work_centers,
            include_saved_defaults=False,
        ),
        time_off_entries=time_off_entries,
        enabled_work_centers=enabled_auto_work_centers,
        assignment_sources=sched.assignment_sources,
        current_assignments=sched.assignments,
        work_weekdays=work_weekdays,
    )

    posted_delivery = (
        dict(sched.published_delivery or {}) if (sched.published or viewing_posted) else {}
    )
    posted_version = posted_delivery.get("version")
    try:
        display_schedule_revision = staffing.schedule_revision(d)
    except Exception:
        log.exception("Could not load schedule revision for %s", d)
        display_schedule_revision = None

    with _Phase(phases, "render"):
        response = templates.TemplateResponse(
            request,
            "staffing.html",
            {
                "active": "plant",
                **saturday_context,
                **recycled_ctx,
                "rotation_mode_help": _rotation_mode_help(
                    recycled_ctx["recycled_rotation_mode"]
                ),
                "sched": sched,
                "auto_scheduler_available": auto_scheduler_available,
                "auto_schedule_enabled_wc_names": enabled_auto_work_centers,
                "auto_schedule_available_wc_names": [loc.name for loc in staffing.LOCATIONS],
                "rotation_auto_summary": rotation_auto_summary,
                "minimum_crew_balance": minimum_crew_balance,
                "recycled_wc_names": _recycled_wc_names(),
                "training_protocol_people": sorted(
                    (person.name for person in roster if person.active), key=str.lower
                ),
                "training_protocol_work_centers": [loc.name for loc in staffing.LOCATIONS],
                "day": d.isoformat(),
                "day_short": d.strftime("%m/%d/%y"),
                "day_pretty": f"{d.strftime('%A, %B')} {d.day}, {d.year}",
                "tomorrow": _next_working_day(today).isoformat(),
                "today": today.isoformat(),
                "published": sched.published,
                "posted_delivery": posted_delivery,
                "posted_version": posted_version,
                "schedule_revision": display_schedule_revision,
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
    action = (form.get("action") or "save").strip().lower()
    if action not in {"save", "publish"}:
        return JSONResponse({"ok": False, "error": "Unknown staffing action."}, status_code=400)
    if (form.get("viewing_posted") or "").strip() == "1":
        return JSONResponse(
            {"ok": False, "error": "The posted schedule view is read-only."},
            status_code=400,
        )
    assignments: dict[str, list[str]] = {}
    default_updates: list[tuple[object, list[str]]] = []
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
            default_updates.append((loc, clean_defaults))
    # Time-off is sourced from the Odoo mirror. The scheduler UI
    # no longer collects time-off entries via form fields, so we ignore any
    # `loc____time_off` values that a stale tab might still be posting.

    notes = (form.get("notes") or "").strip()[:2000]
    wc_notes: dict[str, str] = {}
    for loc in staffing.LOCATIONS:
        v = (form.get(f"wc_note__{loc.name}") or "").strip()[:500]
        if v:
            wc_notes[loc.name] = v
    testing_day = (form.get("testing_day") or "").strip() in ("1", "on", "true")

    saturday_bundle = None
    saturday_lookup_failed = False
    if d.weekday() == 5:
        try:
            saturday_bundle = saturday_recruiting_store.get(d)
        except Exception:
            saturday_lookup_failed = True
            log.exception("Saturday recruiting lookup failed for %s", d)

    if saturday_lookup_failed:
        return JSONResponse(
            {
                "ok": False,
                "error": "Saturday recruiting state could not be verified. No schedule changes were saved.",
            },
            status_code=409,
        )

    active_saturday_recruiting = bool(
        saturday_bundle
        and saturday_bundle.recruitment.status in {"recruiting", "closed", "published"}
    )
    committed_names = set()
    saturday_available_names = set()
    if active_saturday_recruiting:
        recruiting_commitments = {
            item.person_name
            : {"start": item.availability_start, "end": item.availability_end}
            for item in saturday_bundle.commitments
            if item.status == "committed"
        }
        existing_for_saturday_availability = staffing.load_schedule(d)
        effective_commitments = staffing.effective_saturday_commitments(
            recruiting_commitments,
            existing_for_saturday_availability.saturday_availability_overrides,
            saturday_bundle.recruitment.shift_start,
            saturday_bundle.recruitment.shift_end,
        )
        saturday_available_names = set(effective_commitments)
        committed_names = saturday_available_names

    def _noncommitted_names(candidate_assignments):
        return sorted({
            name
            for names in candidate_assignments.values()
            for name in names
            if name not in committed_names
        })

    if active_saturday_recruiting and action in {"save", "publish"}:
        noncommitted_names = _noncommitted_names(assignments)
        if noncommitted_names:
            return JSONResponse(
                {
                    "ok": False,
                    "error": "Only committed Saturday volunteers can be scheduled.",
                    "validation_errors": [
                        f"{name} is not committed to Saturday." for name in noncommitted_names
                    ],
                },
                status_code=409,
            )

    existing = staffing.load_schedule(d)

    for loc, clean_defaults in default_updates:
        work_centers_store.save_one(loc, {"default_people": clean_defaults})

    publish_block = []
    if action == "publish" and not active_saturday_recruiting:
        publish_block = _publish_shortages(assignments, existing.auto_enabled_work_centers)
    if action == "publish" and active_saturday_recruiting:
        try:
            assert saturday_bundle is not None
            roster = staffing.load_roster()
            people_by_name = {person.name: person for person in roster}
            full_day_off_names = {
                entry["name"]
                for entry in _safe_time_off_entries(d)
                if entry.get("hours") is None
            }
            saturday_block = sr.validate_publish(
                saturday_bundle,
                assignments,
                people_by_name,
                full_day_off_names,
                available_names=saturday_available_names,
                require_coverage=False,
            )
            publish_block = saturday_block
        except Exception:
            log.exception("Saturday publish validation failed for %s", d)
            publish_block = ["Saturday recruiting could not be verified. Please try again."]

    if action == "save" or (action == "publish" and publish_block):
        existing = staffing.draft_from_posted(existing)

    if action == "publish" and not publish_block:
        published = True
        published_snapshot = None
        published_delivery = staffing.new_published_delivery()
    else:
        published = existing.published
        published_snapshot = existing.published_snapshot
        published_delivery = existing.published_delivery

    # An ordinary save owns the current grid, so sources only survive for
    # people still submitted in the same work center. This prevents a cleared
    # manual pick from becoming a stale rebuild lock.
    assignment_sources = {}
    for wc_name, sources in existing.assignment_sources.items():
        assigned_names = set(assignments.get(wc_name, []))
        remaining_sources = {
            name: source
            for name, source in (sources or {}).items()
            if name in assigned_names
        }
        if remaining_sources:
            assignment_sources[wc_name] = remaining_sources

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
        # publish / save so the user's overrides aren't dropped.
        custom_hours=existing.custom_hours,
        published_delivery=published_delivery,
        rotation_mode=existing.rotation_mode,
        assignment_sources=assignment_sources,
        saturday_availability_overrides=existing.saturday_availability_overrides,
        auto_enabled_work_centers=existing.auto_enabled_work_centers,
    ))
    if action == "publish" and published and saturday_bundle is not None:
        try:
            saturday_recruiting_store.mark_published(d, plant_now())
        except Exception:
            # The schedule itself is already published. Keep that source of
            # truth visible and let the next manager action retry the marker.
            log.exception("Could not mark Saturday recruiting as published for %s", d)
    # Bust the today response cache so the next GET sees fresh data.
    _http_cache.invalidate_today_cache()

    wants_json = auto or (request.headers.get("accept") or "").startswith("application/json")
    if publish_block and wants_json:
        return JSONResponse(
            {
                "ok": False,
                "error": "Publish blocked — staff every work center to its minimum.",
                "publish_block_reasons": publish_block,
            },
            status_code=409,
        )
    if wants_json:
        delivery = (
            published_delivery if published
            else (published_snapshot or {}).get("published_delivery") or {}
        )
        return JSONResponse({
            "ok": True,
            "revision": staffing.schedule_revision(d),
            "published": published,
            "has_snapshot": bool(published_snapshot) and not published,
            "posted_version": delivery.get("version"),
            "testing_day": testing_day,
        })

    # If publish was blocked, bounce back to the same day with a flag so the UI can show the alert.
    if publish_block:
        query = urlencode(
            [("day", d.isoformat()), ("publish_blocked", "1")]
            + [("publish_error", reason) for reason in publish_block],
        )
        return RedirectResponse(f"/staffing?{query}", status_code=303)

    # Successful publish leaves the next working day untouched. New schedules
    # start blank and automatic scheduling is always an explicit action.
    if action == "publish" and published:
        return RedirectResponse(f"/staffing?day={d.isoformat()}", status_code=303)

    return RedirectResponse(f"/staffing?day={d.isoformat()}", status_code=303)


@router.get("/staffing/live")
def staffing_live(day: str = Query(...)):
    try:
        target_day = date.fromisoformat(day)
    except ValueError:
        return JSONResponse({"ok": False, "error": "bad day"}, status_code=400)
    sched = staffing.load_schedule(target_day)
    delivery = (
        sched.published_delivery if sched.published
        else (sched.published_snapshot or {}).get("published_delivery") or {}
    )
    response = JSONResponse({
        "ok": True,
        "revision": staffing.schedule_revision(target_day),
        "published": sched.published,
        "has_snapshot": bool(sched.published_snapshot) and not sched.published,
        "posted_version": delivery.get("version"),
    })
    response.headers["Cache-Control"] = "no-store"
    return response


@router.post("/staffing/mark-printed")
def staffing_mark_printed(day: str = Query(...), version: str = Query(...)):
    try:
        target_day = date.fromisoformat(day)
    except ValueError:
        return JSONResponse({"ok": False, "error": "bad day"}, status_code=400)
    if not staffing.delivery_for_version(target_day, version):
        return JSONResponse(
            {"ok": False, "error": "This posted schedule has changed."},
            status_code=409,
        )
    delivery = staffing.record_delivery(
        target_day, version, {"printed_at": plant_now().isoformat()},
    )
    if not delivery:
        return JSONResponse(
            {"ok": False, "error": "This posted schedule has changed."},
            status_code=409,
        )
    _http_cache.invalidate_today_cache()
    return JSONResponse({"ok": True, "delivery": delivery})


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

        sched = staffing.draft_from_posted(staffing.load_schedule(d))

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
    five sections:
      scheduled_late:   scheduled people who haven't punched in past threshold
      unscheduled_late: always empty (kept for the JSON/UI contract)
      needs_reason:     scheduled people who punched in past threshold + no
                        late_arrivals record yet — manager fills in reason
      snoozed:          silenced rows (no reason field; transient)
      running_late:     expected arrivals (informational; no action required)

    `late` is an alias for `scheduled_late` for legacy clients.
    `count` is the badge number = sum of the three actionable sections.

    Cached in-process for 30 s. Polled by every page footer every 60 s.
    ``force=True`` skips the cache read and recomputes, resetting the TTL —
    used by the inbox warmer to keep the nav badge warm for human requests.
    """
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
        "running_late": [],
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
            for row in late_report.expected_arrivals_for_day(today):
                emp_id = str(row["emp_id"])
                if (by_id.get(emp_id) or {}).get("status") != "no_punch":
                    late_report.clear_expected_arrival(today, emp_id)
            expected_arrivals = [
                row for row in late_report.active_expected_arrivals(today)
                if (by_id.get(str(row["emp_id"])) or {}).get("status") == "no_punch"
            ]
            expected_ids = {str(row["emp_id"]) for row in expected_arrivals}
            snoozed_ids = {
                str(s["emp_id"]) for s in late_report.active_snoozes(today)
            } | expected_ids
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

            now_utc = datetime.now(UTC)
            for row in expected_arrivals:
                emp_id = str(row["emp_id"])
                expected_local = row["expected_at_utc"].astimezone(shift_config.SITE_TZ)
                fmt = "%#I:%M %p" if os.name == "nt" else "%-I:%M %p"
                out["running_late"].append({
                    "emp_id": emp_id,
                    "name": row["name"],
                    "until_iso": row["expected_at_utc"].isoformat(),
                    "expected_label": expected_local.strftime(fmt),
                    "mins_remaining": max(
                        0, int((row["expected_at_utc"] - now_utc).total_seconds() // 60)
                    ),
                })

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


def _queue_time_off_push(request_id: int) -> None:
    """Push one staged scheduler edit to Odoo (kept patchable for routes)."""
    time_off_sync.push_one(request_id)


def _editable_time_off_for_day(request_id: int, day: date) -> dict | None:
    """Return a visible, Odoo-backed leave a supervisor may change."""
    rows = db.query(
        "SELECT id, shape, holiday_status_id, date_from, date_to, hour_from, hour_to, "
        "odoo_leave_id FROM time_off_requests WHERE id = %s "
        "AND odoo_leave_id IS NOT NULL AND NOT local_record "
        "AND state = ANY(%s) AND date_from <= %s AND date_to >= %s",
        (request_id, list(scheduler_time_off._VISIBLE_STATES), day, day),
    )
    return rows[0] if rows else None


def _stage_supervisor_time_off_edit(
    *,
    request_id: int,
    date_from: date,
    date_to: date,
    hour_from: float | None,
    hour_to: float | None,
    shape: str,
    holiday_status_id: int,
    day: date,
) -> bool:
    """Stage an edit only while the row remains scheduler-editable."""
    rows = db.query(
        "UPDATE time_off_requests SET shape = %s, date_from = %s, date_to = %s, "
        "hour_from = %s, hour_to = %s, state = 'draft_edit', "
        "synced_to_odoo = FALSE, sync_error = NULL, updated_at = now() "
        "WHERE id = %s AND holiday_status_id = %s "
        "AND odoo_leave_id IS NOT NULL AND NOT local_record "
        "AND state = ANY(%s) AND date_from <= %s AND date_to >= %s "
        "RETURNING id",
        (
            shape, date_from, date_to, hour_from, hour_to, request_id,
            holiday_status_id, list(scheduler_time_off._VISIBLE_STATES), day, day,
        ),
    )
    return bool(rows)


def _stage_supervisor_time_off_cancel(request_id: int, day: date) -> bool:
    """Stage cancellation only while the row remains scheduler-editable."""
    rows = db.query(
        "UPDATE time_off_requests SET state = 'draft_cancel', "
        "synced_to_odoo = FALSE, sync_error = NULL, updated_at = now() "
        "WHERE id = %s AND odoo_leave_id IS NOT NULL AND NOT local_record "
        "AND state = ANY(%s) AND date_from <= %s AND date_to >= %s "
        "RETURNING id",
        (request_id, list(scheduler_time_off._VISIBLE_STATES), day, day),
    )
    return bool(rows)


def _scheduler_shift_bounds(day: date) -> tuple[float, float]:
    start = shift_config.configured_shift_start_for(day)
    end = shift_config.configured_shift_end_for(day)
    return (
        start.hour + start.minute / 60,
        end.hour + end.minute / 60,
    )


def _parse_scheduler_editor_body(body: dict) -> tuple[date, date, date, str, str] | JSONResponse:
    try:
        day = date.fromisoformat(body["day"])
        date_from = date.fromisoformat(body["date_from"])
        date_to = date.fromisoformat(body["date_to"])
    except (KeyError, TypeError, ValueError):
        return JSONResponse({"ok": False, "error": "invalid date"}, status_code=422)
    if date_to < date_from:
        return JSONResponse({"ok": False, "error": "date_to precedes date_from"}, status_code=422)
    return day, date_from, date_to, body.get("time_from") or "", body.get("time_to") or ""


def _edit_scheduler_time_off(
    request_id: int,
    body: dict,
    background_tasks: BackgroundTasks | None = None,
) -> JSONResponse:
    parsed = _parse_scheduler_editor_body(body)
    if isinstance(parsed, JSONResponse):
        return parsed
    day, date_from, date_to, time_from, time_to = parsed
    row = _editable_time_off_for_day(request_id, day)
    if row is None:
        return JSONResponse({"ok": False, "error": "time off request not found"}, status_code=404)

    # Whole-day leaves have no hour window; only partials need the configured
    # schedule bounds and therefore the schedule-store read.
    shift_from, shift_to = (
        _scheduler_shift_bounds(date_from)
        if row["shape"] != "full_day" else (0.0, 0.0)
    )
    hour_from, hour_to, error = shape_to_hour_bounds(
        row["shape"], time_from, time_to, shift_from, shift_to,
    )
    if error:
        return JSONResponse({"ok": False, "error": error}, status_code=422)

    staged = _stage_supervisor_time_off_edit(
        request_id=request_id,
        date_from=date_from,
        date_to=date_to,
        hour_from=hour_from,
        hour_to=hour_to,
        shape=row["shape"],
        holiday_status_id=row["holiday_status_id"],
        day=day,
    )
    if not staged:
        return JSONResponse({"ok": False, "error": "time off request not found"}, status_code=404)
    invalidate_today_cache()
    if background_tasks is None:
        _queue_time_off_push(request_id)
    else:
        background_tasks.add_task(_queue_time_off_push, request_id)
    return JSONResponse({"ok": True})


def _cancel_scheduler_time_off(
    request_id: int,
    body: dict,
    background_tasks: BackgroundTasks | None = None,
) -> JSONResponse:
    try:
        day = date.fromisoformat(body["day"])
    except (KeyError, TypeError, ValueError):
        return JSONResponse({"ok": False, "error": "invalid date"}, status_code=422)
    if _editable_time_off_for_day(request_id, day) is None:
        return JSONResponse({"ok": False, "error": "time off request not found"}, status_code=404)
    if not _stage_supervisor_time_off_cancel(request_id, day):
        return JSONResponse({"ok": False, "error": "time off request not found"}, status_code=404)
    invalidate_today_cache()
    if background_tasks is None:
        _queue_time_off_push(request_id)
    else:
        background_tasks.add_task(_queue_time_off_push, request_id)
    return JSONResponse({"ok": True})


@router.post("/api/staffing/time-off/{request_id}/edit")
async def edit_scheduler_time_off(
    request_id: int,
    request: Request,
    background_tasks: BackgroundTasks,
):
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001 -- respond consistently to malformed JSON
        body = {}
    return _edit_scheduler_time_off(request_id, body, background_tasks)


@router.post("/api/staffing/time-off/{request_id}/cancel")
async def cancel_scheduler_time_off(
    request_id: int,
    request: Request,
    background_tasks: BackgroundTasks,
):
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001 -- respond consistently to malformed JSON
        body = {}
    return _cancel_scheduler_time_off(request_id, body, background_tasks)


def _set_saturday_availability_work(day: date, name: str, destination: str) -> dict:
    """Persist one manager correction to a live Saturday recruiting roster."""
    if day.weekday() != 5:
        raise HTTPException(status_code=409, detail="Saturday availability can only be changed on Saturday.")
    if destination not in {"unassigned", "off"}:
        raise HTTPException(
            status_code=409, detail="Saturday availability destination must be Unassigned or Off."
        )
    bundle = saturday_recruiting_store.get(day)
    if bundle is None or bundle.recruitment.status not in {"recruiting", "closed", "published"}:
        raise HTTPException(status_code=409, detail="Saturday recruiting is not active for this date.")

    people = {person.name: person for person in staffing.load_roster()}
    person = people.get(name)
    if person is None or not person.active or person.reserve:
        raise HTTPException(status_code=409, detail=f"{name} is not an active non-reserve employee.")
    full_day_off_names = {
        entry["name"]
        for entry in _safe_time_off_entries(day)
        if entry.get("hours") is None
    }
    if name in full_day_off_names:
        raise HTTPException(status_code=409, detail=f"{name} has approved full-day time off.")

    schedule = staffing.draft_from_posted(staffing.load_schedule(day))
    overrides = dict(schedule.saturday_availability_overrides or {})
    overrides[name] = destination
    schedule.saturday_availability_overrides = overrides
    staffing.save_schedule(schedule)
    _bust_after_mutation()

    recruiting_commitments = {
        item.person_name: {"start": item.availability_start, "end": item.availability_end}
        for item in bundle.commitments
        if item.status == "committed"
        and item.availability_start is not None
        and item.availability_end is not None
    }
    available_names = set(staffing.effective_saturday_commitments(
        recruiting_commitments,
        overrides,
        bundle.recruitment.shift_start,
        bundle.recruitment.shift_end,
    ))
    assigned_names = {
        person_name
        for names in (schedule.assignments or {}).values()
        for person_name in names
    }
    eligible_people = [person for person in people.values() if person.active and not person.reserve]
    unassigned_count = sum(
        person.name in available_names
        and person.name not in assigned_names
        and person.name not in full_day_off_names
        for person in eligible_people
    )
    off_count = sum(
        person.name not in available_names
        and person.name not in assigned_names
        and person.name not in full_day_off_names
        for person in eligible_people
    )
    return {
        "ok": True,
        "destination": destination,
        "unassigned_count": unassigned_count,
        "off_count": off_count,
    }


@router.post("/api/staffing/saturday-availability")
async def set_saturday_availability(request: Request):
    """Move one active person between Saturday Unassigned and Off."""
    try:
        body = await request.json()
        day = date.fromisoformat(str(body["day"]))
        name = str(body["name"]).strip()
        destination = str(body["destination"])
    except (AttributeError, KeyError, TypeError, ValueError):
        return JSONResponse(
            {"ok": False, "error": "A Saturday date, person, and destination are required."},
            status_code=422,
        )
    if not name:
        return JSONResponse(
            {"ok": False, "error": "A person is required."}, status_code=422,
        )
    try:
        result = await asyncio.to_thread(_set_saturday_availability_work, day, name, destination)
    except HTTPException as exc:
        return JSONResponse({"ok": False, "error": str(exc.detail)}, status_code=exc.status_code)
    except Exception:
        log.exception("Could not update Saturday availability for %s on %s", name, day)
        return JSONResponse(
            {"ok": False, "error": "Could not update Saturday availability."}, status_code=500,
        )
    return JSONResponse(result)


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
        sched = staffing.draft_from_posted(staffing.load_schedule(day))
        try:
            if name:
                late_report.clear_partial_by_name(day, name)
            elif request_id:
                late_report.clear_time_off_request(day, int(request_id))
            else:
                late_report.clear_non_work_shift(day, str(emp_id))
        except Exception as e:
            return JSONResponse({"ok": False, "error": str(e)}, status_code=500)
        staffing.save_schedule(sched)
        _bust_after_mutation()
        return JSONResponse({"ok": True})

    return await asyncio.to_thread(_work)


@router.post("/api/staffing/clear-testing-day")
async def staffing_clear_testing_day(request: Request):
    """Flip a schedule's testing_day flag back to False without touching
    anything else (assignments, notes, published state, custom_hours).
    Powers the × on the Testing Day pill at the top of the staffing page.

    The regular save path requires Edit mode on a published schedule. This
    endpoint is the explicit clear path — idempotent, JSON-only, no Edit mode
    needed.

    Body: {day: ISO date}
    """
    from datetime import date as _date
    body = await request.json()
    try:
        d = _date.fromisoformat(body["day"])
    except (KeyError, TypeError, ValueError) as e:
        return JSONResponse({"ok": False, "error": f"bad day: {e}"}, status_code=400)
    def _work():
        existing = staffing.draft_from_posted(staffing.load_schedule(d))
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
            published_delivery=existing.published_delivery,
            rotation_mode=existing.rotation_mode,
            assignment_sources={
                wc_name: dict(sources or {})
                for wc_name, sources in existing.assignment_sources.items()
            },
            auto_enabled_work_centers=existing.auto_enabled_work_centers,
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
        sched = staffing.draft_from_posted(staffing.load_schedule(day))
        try:
            if name:
                late_report.restore_partial_by_name(day, name)
            elif request_id:
                late_report.restore_time_off_request(day, int(request_id))
            else:
                late_report.restore_non_work_shift(day, str(emp_id))
        except Exception as e:
            return JSONResponse({"ok": False, "error": str(e)}, status_code=500)
        staffing.save_schedule(sched)
        _bust_after_mutation()
        return JSONResponse({"ok": True})

    return await asyncio.to_thread(_work)
