"""JSON APIs for scheduling preferences, Recycled training blocks, and rebuilds.

Routes:
  POST /api/rotations/preferences      — save one person's group preference
  POST /api/rotations/training-blocks  — start a level-0 training block
  POST /api/rotations/auto-work-centers — save enabled auto-schedule centers
  POST /api/rotations/rebuild          — regenerate enabled auto-schedule centers

Each endpoint parses JSON, runs the blocking DB work in a worker thread
(``asyncio.to_thread``, matching ``routes/skills.py``), returns ``200`` with the
saved/rebuilt model on success, or ``422`` ``{"ok": false, "error": ...}`` on a
validation failure. Writes invalidate the staffing response cache so the next
GET reflects them.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from datetime import date

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from .. import (
    _http_cache,
    db,
    rotation_store,
    rotation_suggestions,
    schedule_solver,
    scheduler_time_off,
    staffing,
)
from . import staffing as staffing_route

router = APIRouter()

_VALID_MODES = ("optimized", "normal", "training")


def _error(message: str, status_code: int = 422) -> JSONResponse:
    return JSONResponse({"ok": False, "error": message}, status_code=status_code)


def _coverage_payload(suggestion) -> dict[str, object]:
    return {
        "staffed_centers": list(suggestion.staffed_centers),
        "unresolved_centers": list(suggestion.unresolved_centers),
        "issues": [issue.to_dict() for issue in suggestion.issues],
    }


def _placement_payload(suggestion, issues=()) -> dict[str, object]:
    return {
        "available_people": list(suggestion.available_people),
        "placed_people": list(suggestion.placed_people),
        "unplaced_people": list(suggestion.unused_people),
        "defaults": dict(suggestion.default_assignments),
        "issues": [issue.to_dict() for issue in issues],
    }


def _validate_complete_rebuild(
    *,
    available_people: Sequence[str],
    protected_assignments: Mapping[str, Sequence[str]],
    enabled_centers: Sequence[str],
    center_minimums: Mapping[str, int],
    center_capacities: Mapping[str, int | None],
    required_skills: Mapping[str, Sequence[str]],
    roster: Sequence[staffing.Person],
    exact_defaults: Mapping[str, Sequence[str]],
    group_defaults: Mapping[str, Sequence[str]],
    user_group_centers: Mapping[str, Sequence[str]],
    proposed_assignments: Mapping[str, Sequence[str]],
    proposed_sources: Mapping[str, Mapping[str, str]],
) -> tuple[schedule_solver.PlacementIssue, ...]:
    """Independently verify a complete proposal immediately before saving."""
    enabled = frozenset(enabled_centers)
    available = tuple(dict.fromkeys(str(name) for name in available_people))
    available_set = frozenset(available)
    protected = frozenset(
        str(name)
        for names in protected_assignments.values()
        for name in names
    )
    by_name = {person.name: person for person in roster}
    locations: dict[str, list[str]] = {name: [] for name in available}
    for center, names in proposed_assignments.items():
        for raw_name in names or ():
            name = str(raw_name)
            if name in available_set:
                locations[name].append(center)

    issues: list[schedule_solver.PlacementIssue] = []
    for name in available:
        centers = tuple(sorted(locations[name], key=str.lower))
        if not centers:
            issues.append(schedule_solver.PlacementIssue(
                code="person_missing_from_complete_schedule",
                person=name,
                message=f"{name} is not assigned. Previous schedule kept.",
            ))
        elif len(centers) > 1:
            issues.append(schedule_solver.PlacementIssue(
                code="person_assigned_multiple_centers",
                person=name,
                centers=centers,
                message=(
                    f"{name} is assigned to multiple work centers. "
                    "Previous schedule kept."
                ),
            ))

    def _qualified(name: str, center: str) -> bool:
        person = by_name.get(name)
        skills = tuple(required_skills.get(center, ()))
        return person is not None and all(person.level(skill) >= 1 for skill in skills)

    for center in enabled:
        names = tuple(str(name) for name in proposed_assignments.get(center, ()))
        capacity = center_capacities.get(center)
        if capacity is not None and len(names) > int(capacity):
            issues.append(schedule_solver.PlacementIssue(
                code="center_capacity_exceeded",
                centers=(center,),
                message=f"{center} exceeds its maximum capacity. Previous schedule kept.",
            ))
        qualified_names = {name for name in names if _qualified(name, center)}
        # A level-zero training-block person is safe only with a fully trained
        # co-worker at the same center.
        has_green = any(
            (person := by_name.get(name)) is not None
            and all(person.level(skill) >= 3 for skill in required_skills.get(center, ()))
            for name in names
        )
        safe_names = qualified_names | ({*names} if has_green else set())
        if len(safe_names) < int(center_minimums.get(center, 0)):
            issues.append(schedule_solver.PlacementIssue(
                code="center_minimum_unmet",
                centers=(center,),
                message=f"{center} is below its safe minimum. Previous schedule kept.",
            ))
        for name in names:
            if (
                proposed_sources.get(center, {}).get(name) == "generated"
                and name not in safe_names
            ):
                issues.append(schedule_solver.PlacementIssue(
                    code="generated_assignment_unqualified",
                    person=name,
                    centers=(center,),
                    message=(
                        f"{name} is not qualified for generated assignment at {center}. "
                        "Previous schedule kept."
                    ),
                ))

    for center, sources in proposed_sources.items():
        if center in enabled:
            continue
        generated = tuple(
            name
            for name, source in sources.items()
            if source == "generated" and name in available_set
        )
        if generated:
            issues.append(schedule_solver.PlacementIssue(
                code="generated_assignment_center_disabled",
                centers=(center,),
                message=(
                    f"Generated assignments target disabled work center {center}. "
                    "Previous schedule kept."
                ),
            ))

    default_targets: dict[str, list[tuple[str, str]]] = {}
    for center, names in exact_defaults.items():
        for name in names:
            default_targets.setdefault(str(name), []).append(("exact", center))
    for group, names in group_defaults.items():
        for name in names:
            default_targets.setdefault(str(name), []).append(("group", group))
    for name, targets in default_targets.items():
        if name not in available_set or name in protected:
            continue
        unique = tuple(sorted(set(targets), key=lambda item: (item[0], item[1].lower())))
        if len(unique) != 1:
            issues.append(schedule_solver.PlacementIssue(
                code="default_target_conflict",
                person=name,
                centers=tuple(target for _kind, target in unique),
                message=f"{name} has conflicting default targets. Previous schedule kept.",
            ))
            continue
        kind, target = unique[0]
        actual = locations.get(name, [])
        if kind == "exact" and actual != [target]:
            issues.append(schedule_solver.PlacementIssue(
                code="exact_default_violation",
                person=name,
                centers=(target,),
                message=f"{name} is not at default center {target}. Previous schedule kept.",
            ))
        if kind == "group":
            allowed = frozenset(user_group_centers.get(target, ())) & enabled
            if len(actual) != 1 or actual[0] not in allowed:
                issues.append(schedule_solver.PlacementIssue(
                    code="group_default_violation",
                    person=name,
                    centers=tuple(sorted(allowed, key=str.lower)),
                    message=f"{name} is outside default group {target}. Previous schedule kept.",
                ))
    return tuple(issues)


async def _json_body(request: Request):
    try:
        body = await request.json()
    except Exception:
        return None
    return body if isinstance(body, dict) else None


def _person_id_by_name(name: str) -> int | None:
    # Assumes names are unique among active, non-excluded people — the same
    # name-keyed assumption the whole rotation engine (and roster) rely on. If
    # two such rows ever share a name this binds to whichever the DB returns first.
    rows = db.query("SELECT id FROM people WHERE name = %s AND NOT excluded", (name,))
    return int(rows[0]["id"]) if rows else None


def _block_to_dict(block) -> dict:
    return {
        "id": block.id,
        "trainee": block.trainee_name,
        "trainer": block.trainer_name,
        "work_center": block.work_center,
        "skill_ids": list(block.skill_ids),
        "group": block.skill,
        "skill": block.skill,
        "start_day": block.start_day.isoformat(),
        "planned_attended_days": block.planned_attended_days,
        "status": block.status,
    }


@router.post("/api/rotations/preferences")
async def save_rotation_preference(request: Request):
    body = await _json_body(request)
    if body is None:
        return _error("Invalid JSON body.", 400)
    person = str(body.get("person") or "").strip()
    group = str(body.get("group") or "").strip()
    preference = str(body.get("preference") or "").strip()
    if not person or not group or not preference:
        return _error("person, group, and preference are required.")

    def _work():
        person_id = _person_id_by_name(person)
        if person_id is None:
            return _error(f"Unknown person: {person}")
        roster_person = next(
            (roster_person for roster_person in staffing.load_roster() if roster_person.name == person),
            None,
        )
        target_keys = {
            target.key for target in staffing.scheduling_preference_targets()
        }
        eligible_target_keys = {
            target.key
            for target in staffing.eligible_scheduling_preference_targets(roster_person)
        } if roster_person is not None else set()
        if group in target_keys and group not in eligible_target_keys:
            return _error(f"{person} is not qualified for {group}.")
        try:
            saved = rotation_store.save_preference(person_id, group, preference)
        except rotation_store.InvalidRotationPreference as exc:
            return _error(str(exc))
        _http_cache.invalidate_today_cache()
        _http_cache.invalidate_stable_cache()
        return JSONResponse({
            "ok": True,
            "person": person,
            "group": saved.rotation_group,
            "preference": saved.preference,
        })

    return await asyncio.to_thread(_work)


@router.post("/api/rotations/training-blocks")
async def create_training_block(request: Request):
    body = await _json_body(request)
    if body is None:
        return _error("Invalid JSON body.", 400)
    trainee = str(body.get("trainee") or "").strip()
    trainer = str(body.get("trainer") or "").strip()
    work_center = str(body.get("work_center") or "").strip()
    start_day_raw = str(body.get("start_day") or "").strip()
    workdays = body.get("workdays")

    if not trainee or not trainer or not work_center:
        return _error("trainee, trainer, and work center are required.")
    try:
        start_day = date.fromisoformat(start_day_raw)
    except ValueError:
        return _error("Invalid start day.")
    if not isinstance(workdays, int) or isinstance(workdays, bool) or workdays < 1:
        return _error("workdays must be a positive integer.")

    def _work():
        trainee_id = _person_id_by_name(trainee)
        if trainee_id is None:
            return _error(f"Unknown person: {trainee}")
        trainer_id = _person_id_by_name(trainer)
        if trainer_id is None:
            return _error(f"Unknown person: {trainer}")
        try:
            block = rotation_store.create_block(
                trainee_id=trainee_id,
                trainer_id=trainer_id,
                work_center=work_center,
                start_day=start_day,
                planned_attended_days=workdays,
            )
        except rotation_store.InvalidTrainingBlock as exc:
            return _error(str(exc))
        _http_cache.invalidate_today_cache()
        _http_cache.invalidate_stable_cache()
        return JSONResponse({"ok": True, "block": _block_to_dict(block)})

    return await asyncio.to_thread(_work)


async def _lifecycle(block_id: int, store_fn_name: str, status: str) -> JSONResponse:
    """Shared body for the pause/resume/end block endpoints.

    Resolves the store helper by name at call time (so tests can monkeypatch
    ``rotation_store.<fn>``), invalidates the caches the schedule reads from, and
    returns the resulting status. A non-positive id is a client error (422).
    """
    if not isinstance(block_id, int) or isinstance(block_id, bool) or block_id <= 0:
        return _error("block_id must be a positive integer.")

    def _work():
        getattr(rotation_store, store_fn_name)(block_id)
        _http_cache.invalidate_today_cache()
        _http_cache.invalidate_stable_cache()
        return JSONResponse({"ok": True, "id": block_id, "status": status})

    return await asyncio.to_thread(_work)


@router.post("/api/rotations/training-blocks/{block_id}/pause")
async def pause_training_block(block_id: int):
    return await _lifecycle(block_id, "pause_block", "paused")


@router.post("/api/rotations/training-blocks/{block_id}/resume")
async def resume_training_block(block_id: int):
    return await _lifecycle(block_id, "resume_block", "active")


@router.post("/api/rotations/training-blocks/{block_id}/end")
async def end_training_block(block_id: int):
    return await _lifecycle(block_id, "end_block", "ended")


@router.post("/api/rotations/auto-work-centers")
async def save_auto_work_centers(request: Request):
    body = await _json_body(request)
    if body is None:
        return _error("Invalid JSON body.", 400)
    day_raw = str(body.get("day") or "").strip()
    names = body.get("work_centers")
    turn_off = body.get("turn_off")
    try:
        d = date.fromisoformat(day_raw)
    except ValueError:
        return _error("Invalid day.")
    if not isinstance(names, list) or not isinstance(turn_off, list):
        return _error("work_centers and turn_off must be lists.")
    def _work():
        proposed = staffing_route._ordered_work_center_names(names)
        turn_off_names = set(staffing_route._ordered_work_center_names(turn_off))
        enabled = [name for name in proposed if name not in turn_off_names]
        roster = staffing.load_roster()
        sched = staffing.load_schedule(d)
        try:
            time_off = scheduler_time_off.time_off_entries_for_day(d)
        except Exception:
            return _error("Could not verify daily staffing coverage.", 503)
        enabled = staffing_route._save_enabled_auto_work_centers(enabled)
        minimum_crew_balance = staffing_route._minimum_crew_balance_payload(
            staffing_route._minimum_crew_balance_for_day(
                roster=roster,
                schedule=sched,
                time_off_entries=time_off,
                enabled_centers=enabled,
            )
        )
        _http_cache.invalidate_today_cache()
        _http_cache.invalidate_stable_cache()
        return JSONResponse({
            "ok": True,
            "enabled_work_centers": enabled,
            "minimum_crew_balance": minimum_crew_balance,
            "warnings": [],
            "coverage": {
                "staffed_centers": [],
                "unresolved_centers": [],
                "issues": [],
            },
            "placement": {
                "available_people": [],
                "placed_people": [],
                "unplaced_people": [],
                "defaults": {},
                "issues": [],
            },
        })

    return await asyncio.to_thread(_work)


def _build_assignment_sources(existing_sources, suggestion) -> dict[str, dict[str, str]]:
    """Manual entries kept as ``manual``, engine placements as ``generated``.

    Non-Recycled centers keep whatever source they already had; the engine only
    reports sources for the Recycled centers it (re)built.
    """
    managed = {c for centers in suggestion.group_locations.values() for c in centers}
    new_sources: dict[str, dict[str, str]] = {}
    for wc, sources in (existing_sources or {}).items():
        if wc not in managed:
            new_sources[wc] = dict(sources)
    for wc, sources in suggestion.sources.items():
        new_sources[wc] = dict(sources)
    return new_sources


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
        group_centers = tuple(center for center in user_group_centers.get(group, ()) if center in enabled)
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
            tied_centers = tuple(center for center in available_centers if len(assignments.get(center, ())) == least_load)
            place(rotation_suggestions.choose_center(name, str(group), tied_centers, history), name)
    return assignments, sources


@router.post("/api/rotations/rebuild")
async def rebuild_rotation(request: Request):
    body = await _json_body(request)
    if body is None:
        return _error("Invalid JSON body.", 400)
    day_raw = str(body.get("day") or "").strip()
    mode = str(body.get("mode") or "").strip()
    reset_to_defaults = body.get("reset_to_defaults", False)
    if not isinstance(reset_to_defaults, bool):
        return _error("reset_to_defaults must be a boolean.")
    try:
        d = date.fromisoformat(day_raw)
    except ValueError:
        return _error("Invalid day.")
    if mode not in _VALID_MODES:
        return _error(f"Unknown mode: {mode}")
    def _work():
        roster = staffing.load_roster()
        sched = staffing.load_schedule(d)
        base_assignments = {k: list(v) for k, v in sched.assignments.items()}
        try:
            time_off = scheduler_time_off.time_off_entries_for_day(d)
            exact_defaults, group_defaults, user_group_centers = (
                staffing_route._default_inputs(strict=True)
            )
            enabled_centers = staffing_route._ordered_work_center_names(
                staffing_route._enabled_auto_work_centers(d)
            )
            center_capacities = staffing_route._configured_center_capacities(
                enabled_centers,
                strict=True,
            )
            if reset_to_defaults:
                absent = rotation_suggestions._full_day_time_off_names(time_off)
                history = rotation_suggestions._load_recycled_history(
                    d,
                    group_locations=staffing_route._auto_history_group_locations(),
                    user_group_centers=user_group_centers,
                )
                assignments, sources = _defaults_only_assignments(
                    roster=roster,
                    full_day_off_names=absent,
                    exact_defaults=exact_defaults,
                    group_defaults=group_defaults,
                    user_group_centers=user_group_centers,
                    enabled_centers=enabled_centers,
                    center_capacities=center_capacities,
                    history=history,
                )
                staffing.save_schedule(staffing.Schedule(
                    day=d,
                    published=sched.published,
                    assignments=assignments,
                    notes=sched.notes,
                    wc_notes=dict(sched.wc_notes),
                    testing_day=sched.testing_day,
                    published_snapshot=sched.published_snapshot,
                    custom_hours=sched.custom_hours,
                    rotation_mode=sched.rotation_mode,
                    assignment_sources=sources,
                ))
                _http_cache.invalidate_today_cache()
                return JSONResponse({
                    "ok": True,
                    "applied": True,
                    "assignments": assignments,
                    "sources": sources,
                    "reasons": {},
                    "warnings": [],
                    "unplaced": [],
                    "coverage": {
                        "staffed_centers": [],
                        "unresolved_centers": [],
                        "issues": [],
                    },
                    "enabled_work_centers": enabled_centers,
                    "placement": {
                        "available_people": [],
                        "placed_people": [],
                        "unplaced_people": [],
                        "defaults": {},
                        "issues": [],
                    },
                })
            manual_locks = staffing_route._protected_locks(
                sched.assignment_sources,
                sched.assignments,
                allowed_centers=enabled_centers,
                strict_default_reads=True,
                include_saved_defaults=False,
            )
            if reset_to_defaults:
                manual_locks = {}
            center_minimums = {
                loc.name: staffing_route._effective_minimum(loc)
                for loc in staffing.LOCATIONS if loc.name in enabled_centers
            }
            group_locations, group_required_skills = staffing_route._auto_group_maps(
                enabled_centers
            )
            required_skills = {
                center: group_required_skills[group]
                for group, centers in group_locations.items()
                for center in centers
            }
        except Exception:
            return _error("Could not rebuild the schedule.", 503)
        if not enabled_centers:
            return _error("Select at least one Auto work center before rebuilding.")
        suggestion = staffing_route._recycled_suggestion_for_day(
            d,
            roster,
            mode,
            base_assignments=base_assignments,
            locked_assignments=manual_locks,
            time_off_entries=time_off,
            enabled_work_centers=enabled_centers,
            assignment_sources=sched.assignment_sources,
            center_minimums=center_minimums,
            center_capacities=center_capacities,
            exact_defaults=exact_defaults,
            group_defaults=group_defaults,
            user_group_centers=user_group_centers,
            minimum_only=True,
        )
        if suggestion is None:
            return _error("Could not rebuild the schedule.", 503)

        new_assignments = staffing_route._merge_recycled_assignments(base_assignments, suggestion)
        new_sources = _build_assignment_sources(sched.assignment_sources, suggestion)

        validation_issues = _validate_complete_rebuild(
            available_people=suggestion.available_people,
            protected_assignments=manual_locks,
            enabled_centers=enabled_centers,
            center_minimums=center_minimums,
            center_capacities=center_capacities,
            required_skills=required_skills,
            roster=staffing_route._roster_minus_full_day_off(roster, time_off),
            exact_defaults=exact_defaults,
            group_defaults=group_defaults,
            user_group_centers=user_group_centers,
            proposed_assignments=new_assignments,
            proposed_sources=new_sources,
        )
        hard_codes = {
            "person_assigned_multiple_centers",
            "center_capacity_exceeded",
            "generated_assignment_unqualified",
            "generated_assignment_center_disabled",
        }
        hard_issues = tuple(
            issue for issue in validation_issues if issue.code in hard_codes
        )
        if hard_issues:
            return JSONResponse(
                {
                    "ok": False,
                    "error": "Auto produced an unsafe assignment. Previous schedule kept.",
                    "schedule_kept": True,
                    "placement": _placement_payload(suggestion, hard_issues),
                },
                status_code=422,
            )

        reporting_issues = tuple(suggestion.placement_issues) + tuple(
            issue for issue in validation_issues if issue.code not in hard_codes
        )
        warning_messages = list(suggestion.warnings)
        for issue in reporting_issues:
            message = issue.message.replace(" Previous schedule kept.", "")
            if message not in warning_messages:
                warning_messages.append(message)

        # Persist the rebuild, preserving everything not owned by rotation
        # (published state, snapshot, testing day, notes, custom hours).
        staffing.save_schedule(staffing.Schedule(
            day=d,
            published=sched.published,
            assignments=new_assignments,
            notes=sched.notes,
            wc_notes=dict(sched.wc_notes),
            testing_day=sched.testing_day,
            published_snapshot=sched.published_snapshot,
            custom_hours=sched.custom_hours,
            rotation_mode=mode,
            assignment_sources=new_sources,
        ))
        _http_cache.invalidate_today_cache()
        return JSONResponse({
            "ok": True,
            "applied": True,
            "assignments": new_assignments,
            "sources": new_sources,
            "reasons": {wc: dict(r) for wc, r in suggestion.reasons.items()},
            "warnings": warning_messages,
            "unplaced": list(suggestion.unused_people),
            "coverage": _coverage_payload(suggestion),
            "enabled_work_centers": enabled_centers,
            "placement": _placement_payload(suggestion, reporting_issues),
        })

    return await asyncio.to_thread(_work)
