"""JSON APIs for Recycled rotation preferences, training blocks, and rebuilds.

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
from datetime import date

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from .. import _http_cache, db, rotation_store, staffing
from . import staffing as staffing_route

router = APIRouter()

_VALID_MODES = ("optimized", "normal", "training")


def _error(message: str, status_code: int = 422) -> JSONResponse:
    return JSONResponse({"ok": False, "error": message}, status_code=status_code)


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
    group = str(body.get("group") or "").strip()
    start_day_raw = str(body.get("start_day") or "").strip()
    workdays = body.get("workdays")

    if not trainee or not trainer or not group:
        return _error("trainee, trainer, and group are required.")
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
        # The rotation group name IS the target skill name (Dismantler / Repair
        # / Trim Saw); resolve it to a local skills.id for create_block. Skill
        # names are assumed unique; a duplicate would bind to the first row.
        skill_rows = db.query("SELECT id FROM skills WHERE name = %s", (group,))
        if not skill_rows:
            return _error(f"Unknown group: {group}")
        skill_id = int(skill_rows[0]["id"])
        try:
            block = rotation_store.create_block(
                trainee_id=trainee_id,
                trainer_id=trainer_id,
                skill_id=skill_id,
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
    names = body.get("work_centers")
    if not isinstance(names, list):
        return _error("work_centers must be a list.")

    def _work():
        enabled = staffing_route._save_enabled_auto_work_centers(names)
        _http_cache.invalidate_today_cache()
        _http_cache.invalidate_stable_cache()
        return JSONResponse({"ok": True, "enabled_work_centers": enabled})

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


@router.post("/api/rotations/rebuild")
async def rebuild_rotation(request: Request):
    body = await _json_body(request)
    if body is None:
        return _error("Invalid JSON body.", 400)
    day_raw = str(body.get("day") or "").strip()
    mode = str(body.get("mode") or "").strip()
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
        enabled_centers = staffing_route._ordered_work_center_names(
            staffing_route._enabled_auto_work_centers(d)
        )
        locked = staffing_route._protected_locks(
            sched.assignment_sources,
            sched.assignments,
            allowed_centers=enabled_centers,
        )
        time_off = staffing_route._safe_time_off_entries(d)
        suggestion = staffing_route._recycled_suggestion_for_day(
            d,
            roster,
            mode,
            base_assignments=base_assignments,
            locked_assignments=locked,
            time_off_entries=time_off,
            enabled_work_centers=enabled_centers,
        )
        if suggestion is None:
            return _error("Could not rebuild the schedule.", 503)

        new_assignments = staffing_route._merge_recycled_assignments(base_assignments, suggestion)
        new_sources = _build_assignment_sources(sched.assignment_sources, suggestion)

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
            "assignments": new_assignments,
            "sources": new_sources,
            "reasons": {wc: dict(r) for wc, r in suggestion.reasons.items()},
            "warnings": list(suggestion.warnings),
            "enabled_work_centers": enabled_centers,
        })

    return await asyncio.to_thread(_work)
