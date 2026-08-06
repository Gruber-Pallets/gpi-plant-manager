"""Persistence helpers for recycled work-center rotations and training blocks."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from . import db, staffing


ROTATION_GROUPS = ("Dismantler", "Repair", "Trim Saw")
PREFERENCES = ("primary", "regular", "occasional", "never")
_BLOCK_STATUSES = ("active", "paused", "completing", "completed", "ended")
_BLOCK_DAY_STATUSES = ("attended", "absent", "conflict")


class InvalidRotationPreference(ValueError):
    """Raised when a rotation group or preference is outside the supported set."""


class InvalidTrainingBlock(ValueError):
    """Raised when a training block would violate the day-one safety rules."""


@dataclass(frozen=True)
class RotationPreference:
    person_id: int
    rotation_group: str
    preference: str


@dataclass(frozen=True)
class TrainingBlock:
    id: int
    trainee_name: str
    trainer_name: str
    skill: str
    start_day: date
    planned_attended_days: int
    status: str
    # Local people.id / skills.id. Defaulted so older constructions stay valid;
    # populated from the joins below so reconciliation can promote by local id.
    trainee_id: int = 0
    skill_id: int = 0
    work_center: str | None = None
    skill_ids: tuple[int, ...] = ()


@dataclass(frozen=True)
class TrainingBlockDay:
    """One recorded outcome of a training block on a given day."""

    day: date
    status: str


def preference_for(preferences: dict[tuple[int, str], str], person_id: int, group: str) -> str:
    """Return a person's saved preference, defaulting missing entries to regular."""
    return preferences.get((person_id, group), "regular")


def _validate_preference(group: str, preference: str) -> None:
    if group not in {target.key for target in staffing.scheduling_preference_targets()}:
        raise InvalidRotationPreference(f"Unknown rotation group: {group!r}")
    if preference not in PREFERENCES:
        raise InvalidRotationPreference(f"Unknown rotation preference: {preference!r}")


def load_preferences() -> dict[tuple[int, str], str]:
    """Load saved preferences keyed by ``(person_id, rotation_group)``."""
    rows = db.query(
        "SELECT person_id, rotation_group, preference "
        "FROM person_rotation_preferences"
    )
    return {(int(row["person_id"]), row["rotation_group"]): row["preference"] for row in rows}


def load_preferences_by_name() -> dict[str, dict[str, str]]:
    """Load saved preferences keyed by person NAME for the rotation engine.

    Returns ``{person_name: {rotation_group: preference}}``. The engine takes
    name-keyed preferences (rosters carry names, not local ``people.id``), so
    this joins ``people`` to translate the id-keyed table. Missing people/groups
    simply don't appear; the engine treats an absent entry as ``regular``.
    """
    rows = db.query(
        "SELECT p.name AS name, r.rotation_group AS rotation_group, r.preference AS preference "
        "FROM person_rotation_preferences r "
        "JOIN people p ON p.id = r.person_id"
    )
    out: dict[str, dict[str, str]] = {}
    for row in rows:
        name = row["name"]
        if not name:
            continue
        out.setdefault(name, {})[row["rotation_group"]] = row["preference"]
    return out


def save_preference(person_id: int, group: str, preference: str) -> RotationPreference:
    """Upsert one person's preference for a recycled rotation group."""
    _validate_preference(group, preference)
    db.execute(
        "INSERT INTO person_rotation_preferences (person_id, rotation_group, preference) "
        "VALUES (%s, %s, %s) "
        "ON CONFLICT (person_id, rotation_group) DO UPDATE SET preference = EXCLUDED.preference",
        (person_id, group, preference),
    )
    return RotationPreference(person_id=person_id, rotation_group=group, preference=preference)


def validate_block(*, level: int, trainer_level: int, workdays: int) -> None:
    """Validate the non-negotiable starting conditions for a training block."""
    if level != 0:
        raise InvalidTrainingBlock("Trainee must be level 0 for the target skill.")
    if trainer_level != 3:
        raise InvalidTrainingBlock("Day-one trainer must be level 3 for the target skill.")
    if workdays < 1:
        raise InvalidTrainingBlock("Training block must contain at least one attended workday.")


def _skill_ids_for(required_skills: tuple[str, ...]) -> tuple[int, ...]:
    """Resolve each configured protocol skill while preserving its order."""
    required_skills = tuple(
        staffing.skill_name_for_scheduling_group(skill) for skill in required_skills
    )
    rows = db.query(
        "SELECT id, name FROM skills WHERE name = ANY(%s)",
        (list(required_skills),),
    )
    ids_by_name = {row["name"]: int(row["id"]) for row in rows}
    missing = [skill for skill in required_skills if skill not in ids_by_name]
    if missing:
        raise InvalidTrainingBlock(
            f"Could not resolve configured training skill: {missing[0]}."
        )
    return tuple(ids_by_name[skill] for skill in required_skills)


def _block_from_row(row: dict) -> TrainingBlock:
    start_day = row["start_day"]
    if not isinstance(start_day, date):
        start_day = date.fromisoformat(str(start_day))
    return TrainingBlock(
        id=int(row["id"]),
        trainee_name=row["trainee_name"],
        trainer_name=row["trainer_name"],
        skill=row["skill"],
        start_day=start_day,
        planned_attended_days=int(row["planned_attended_days"]),
        status=row["status"],
        trainee_id=int(row.get("trainee_id") or 0),
        skill_id=int(row.get("skill_id") or 0),
        work_center=row.get("work_center"),
        skill_ids=tuple(row.get("skill_ids") or (int(row.get("skill_id") or 0),)),
    )


def create_block(
    *,
    trainee_id: int,
    trainer_id: int,
    work_center: str,
    start_day: date,
    planned_attended_days: int,
) -> TrainingBlock:
    """Create an active block for one exact configured work center."""
    location = staffing.location_by_name(work_center)
    if location is None:
        raise InvalidTrainingBlock(f"Unknown work center: {work_center!r}.")
    skill_ids = _skill_ids_for(staffing.required_skills_for(location))
    for skill_id in skill_ids:
        levels = db.query(
            "SELECT "
            "  COALESCE((SELECT level FROM person_skills WHERE person_id = %s AND skill_id = %s), 0) "
            "    AS trainee_level, "
            "  COALESCE((SELECT level FROM person_skills WHERE person_id = %s AND skill_id = %s), 0) "
            "    AS trainer_level",
            (trainee_id, skill_id, trainer_id, skill_id),
        )
        if not levels:
            raise InvalidTrainingBlock("Could not determine training skill levels.")
        validate_block(
            level=int(levels[0]["trainee_level"]),
            trainer_level=int(levels[0]["trainer_level"]),
            workdays=planned_attended_days,
        )
    rows = db.query(
        "WITH inserted AS ("
        "  INSERT INTO rotation_training_blocks "
        "    (trainee_id, trainer_id, skill_id, work_center, skill_ids, start_day, planned_attended_days, status) "
        "  VALUES (%s, %s, %s, %s, %s, %s, %s, 'active') "
        "  RETURNING id, trainee_id, trainer_id, skill_id, work_center, skill_ids, start_day, planned_attended_days, status"
        ") "
        "SELECT i.id, trainee.name AS trainee_name, trainer.name AS trainer_name, skill.name AS skill, "
        "  i.start_day, i.planned_attended_days, i.status, i.trainee_id, i.skill_id, i.work_center, i.skill_ids "
        "FROM inserted i "
        "JOIN people trainee ON trainee.id = i.trainee_id "
        "JOIN people trainer ON trainer.id = i.trainer_id "
        "JOIN skills skill ON skill.id = i.skill_id",
        (trainee_id, trainer_id, skill_ids[0], work_center, list(skill_ids), start_day, planned_attended_days),
    )
    if not rows:
        raise InvalidTrainingBlock("Could not create training block.")
    return _block_from_row(rows[0])


def active_blocks_for_day(day: date) -> list[TrainingBlock]:
    """Return blocks that are active on or after their configured start day."""
    rows = db.query(
        "SELECT b.id, trainee.name AS trainee_name, trainer.name AS trainer_name, skill.name AS skill, "
        "  b.start_day, b.planned_attended_days, b.status, b.trainee_id, b.skill_id, b.work_center, b.skill_ids "
        "FROM rotation_training_blocks b "
        "JOIN people trainee ON trainee.id = b.trainee_id "
        "JOIN people trainer ON trainer.id = b.trainer_id "
        "JOIN skills skill ON skill.id = b.skill_id "
        "WHERE b.status = 'active' AND b.start_day <= %s "
        "ORDER BY b.start_day, b.id",
        (day,),
    )
    return [_block_from_row(row) for row in rows]


def _block_rows_query(*, where_sql: str, params: tuple = ()) -> list[dict]:
    """Load joined training-block rows for a caller-supplied filter."""
    rows = db.query(
        "SELECT b.id, trainee.name AS trainee_name, trainer.name AS trainer_name, skill.name AS skill, "
        "  b.start_day, b.planned_attended_days, b.status, b.trainee_id, b.skill_id, b.work_center, b.skill_ids "
        "FROM rotation_training_blocks b "
        "JOIN people trainee ON trainee.id = b.trainee_id "
        "JOIN people trainer ON trainer.id = b.trainer_id "
        "JOIN skills skill ON skill.id = b.skill_id "
        f"WHERE {where_sql} "
        "ORDER BY b.start_day, b.id",
        params,
    )
    return rows


def get_block(block_id: int) -> TrainingBlock | None:
    """Return one training block by id, or ``None`` when it does not exist."""
    rows = _block_rows_query(where_sql="b.id = %s", params=(block_id,))
    return _block_from_row(rows[0]) if rows else None


def attended_day_count(block_id: int) -> int:
    """Return how many attended workdays have been recorded for a block."""
    return sum(1 for day in resolved_days(block_id) if day.status == "attended")


def manageable_blocks() -> list[TrainingBlock]:
    """Return every active or paused block for sidebar management."""
    rows = _block_rows_query(where_sql="b.status IN ('active', 'paused')")
    return [_block_from_row(row) for row in rows]


def update_block(
    block_id: int,
    *,
    trainer_id: int,
    work_center: str,
    start_day: date,
    planned_attended_days: int,
) -> TrainingBlock:
    """Update editable fields on an active or paused training block."""
    block = get_block(block_id)
    if block is None or block.status not in ("active", "paused"):
        raise InvalidTrainingBlock("Training is not editable.")
    attended = attended_day_count(block_id)
    if planned_attended_days < attended:
        raise InvalidTrainingBlock(
            f"Planned days cannot be below attended days ({attended})."
        )
    location = staffing.location_by_name(work_center)
    if location is None:
        raise InvalidTrainingBlock(f"Unknown work center: {work_center!r}.")
    skill_ids = _skill_ids_for(staffing.required_skills_for(location))
    for skill_id in skill_ids:
        levels = db.query(
            "SELECT "
            "  COALESCE((SELECT level FROM person_skills WHERE person_id = %s AND skill_id = %s), 0) "
            "    AS trainee_level, "
            "  COALESCE((SELECT level FROM person_skills WHERE person_id = %s AND skill_id = %s), 0) "
            "    AS trainer_level",
            (block.trainee_id, skill_id, trainer_id, skill_id),
        )
        if not levels:
            raise InvalidTrainingBlock("Could not determine training skill levels.")
        validate_block(
            level=int(levels[0]["trainee_level"]),
            trainer_level=int(levels[0]["trainer_level"]),
            workdays=planned_attended_days,
        )
    rows = db.query(
        "WITH updated AS ("
        "  UPDATE rotation_training_blocks "
        "  SET trainer_id = %s, work_center = %s, skill_id = %s, skill_ids = %s, "
        "      start_day = %s, planned_attended_days = %s "
        "  WHERE id = %s AND status IN ('active', 'paused') "
        "  RETURNING id, trainee_id, trainer_id, skill_id, work_center, skill_ids, start_day, planned_attended_days, status"
        ") "
        "SELECT u.id, trainee.name AS trainee_name, trainer.name AS trainer_name, skill.name AS skill, "
        "  u.start_day, u.planned_attended_days, u.status, u.trainee_id, u.skill_id, u.work_center, u.skill_ids "
        "FROM updated u "
        "JOIN people trainee ON trainee.id = u.trainee_id "
        "JOIN people trainer ON trainer.id = u.trainer_id "
        "JOIN skills skill ON skill.id = u.skill_id",
        (
            trainer_id,
            work_center,
            skill_ids[0],
            list(skill_ids),
            start_day,
            planned_attended_days,
            block_id,
        ),
    )
    if not rows:
        raise InvalidTrainingBlock("Training is not editable.")
    return _block_from_row(rows[0])


def claim_early_completion(block_id: int) -> str | None:
    """Atomically claim a block for early completion from active or paused."""
    rows = db.query(
        "WITH prior AS ("
        "  SELECT id, status AS prior_status "
        "  FROM rotation_training_blocks "
        "  WHERE id = %s AND status IN ('active', 'paused')"
        "), "
        "updated AS ("
        "  UPDATE rotation_training_blocks b "
        "  SET status = 'completing' "
        "  FROM prior p "
        "  WHERE b.id = p.id AND b.status IN ('active', 'paused') "
        "  RETURNING b.id, p.prior_status AS status"
        ") "
        "SELECT status FROM updated",
        (block_id,),
    )
    return rows[0]["status"] if rows else None


def release_early_completion_claim(block_id: int, prior_status: str) -> None:
    """Restore a failed early-completion claim to its prior editable status."""
    if prior_status not in ("active", "paused"):
        return
    db.execute(
        "UPDATE rotation_training_blocks SET status = %s "
        "WHERE id = %s AND status = 'completing'",
        (prior_status, block_id),
    )


def _blocks_with_status(status: str) -> list[TrainingBlock]:
    """Return every block in ``status``, regardless of start day."""
    rows = db.query(
        "SELECT b.id, trainee.name AS trainee_name, trainer.name AS trainer_name, skill.name AS skill, "
        "  b.start_day, b.planned_attended_days, b.status, b.trainee_id, b.skill_id, b.work_center, b.skill_ids "
        "FROM rotation_training_blocks b "
        "JOIN people trainee ON trainee.id = b.trainee_id "
        "JOIN people trainer ON trainer.id = b.trainer_id "
        "JOIN skills skill ON skill.id = b.skill_id "
        "WHERE b.status = %s "
        "ORDER BY b.start_day, b.id",
        (status,),
    )
    return [_block_from_row(row) for row in rows]


def active_blocks() -> list[TrainingBlock]:
    """Return every active block, regardless of start day, deterministically."""
    return _blocks_with_status("active")


def completing_blocks() -> list[TrainingBlock]:
    """Return claims whose promotion succeeded but finalization must retry."""
    return _blocks_with_status("completing")


def resolved_days(block_id: int) -> list[TrainingBlockDay]:
    """Return the recorded day outcomes for a block, ordered by day."""
    rows = db.query(
        "SELECT day, status FROM rotation_training_block_days "
        "WHERE block_id = %s ORDER BY day",
        (block_id,),
    )
    out: list[TrainingBlockDay] = []
    for row in rows:
        day = row["day"]
        if not isinstance(day, date):
            day = date.fromisoformat(str(day))
        out.append(TrainingBlockDay(day=day, status=row["status"]))
    return out


def mark_completed(block_id: int) -> None:
    """Finalize a claimed block; keep direct active completion backward-compatible."""
    db.execute(
        "UPDATE rotation_training_blocks "
        "SET status = 'completed', completed_at = now() "
        "WHERE id = %s AND status IN ('active', 'completing')",
        (block_id,),
    )


def claim_completion(block_id: int) -> bool:
    """Atomically reserve an eligible block's external promotion exactly once.

    The claim commits before the Odoo-backed skill writer runs, so a second
    worker sees ``completing`` rather than ``active`` and cannot duplicate the
    external side effect.
    """
    rows = db.query(
        "UPDATE rotation_training_blocks SET status = 'completing' "
        "WHERE id = %s AND status = 'active' RETURNING id",
        (block_id,),
    )
    return bool(rows)


def release_completion_claim(block_id: int) -> None:
    """Return a failed promotion claim to active so a later tick can retry."""
    db.execute(
        "UPDATE rotation_training_blocks SET status = 'active' "
        "WHERE id = %s AND status = 'completing'",
        (block_id,),
    )


def pause_block(block_id: int) -> None:
    """Pause an active block; a no-op unless it is currently active.

    A paused block is excluded from ``active_blocks``/``active_blocks_for_day``,
    so it stops driving scheduling and reconciliation without being completed.
    """
    db.execute(
        "UPDATE rotation_training_blocks SET status = 'paused' "
        "WHERE id = %s AND status = 'active'",
        (block_id,),
    )


def resume_block(block_id: int) -> None:
    """Resume a paused block back to active; a no-op unless it is paused."""
    db.execute(
        "UPDATE rotation_training_blocks SET status = 'active' "
        "WHERE id = %s AND status = 'paused'",
        (block_id,),
    )


def end_block(block_id: int) -> None:
    """End a block without completing it; a no-op once it is neither active
    nor paused. Ending never promotes the target skill (unlike completion)."""
    db.execute(
        "UPDATE rotation_training_blocks SET status = 'ended' "
        "WHERE id = %s AND status IN ('active', 'paused')",
        (block_id,),
    )


def record_attended_day(block_id: int, day: date, status: str = "attended") -> None:
    """Record one day's outcome for a block. Pure recorder — never completes.

    This only upserts the day row; it deliberately does NOT flip the block to
    ``completed``. Completion and the level-1 promotion are owned solely by
    ``rotation_training.reconcile_blocks`` (which promotes the skill *and* marks
    the block completed in one place). Auto-completing here would let a block
    reach ``completed`` without ever promoting, since ``active_blocks`` — the
    only input reconcile sees — filters to ``status = 'active'``.
    """
    if status not in _BLOCK_DAY_STATUSES:
        raise InvalidTrainingBlock(f"Unknown training-day status: {status!r}")
    db.execute(
        "INSERT INTO rotation_training_block_days (block_id, day, status) "
        "VALUES (%s, %s, %s) "
        "ON CONFLICT (block_id, day) DO UPDATE SET status = EXCLUDED.status",
        (block_id, day, status),
    )
