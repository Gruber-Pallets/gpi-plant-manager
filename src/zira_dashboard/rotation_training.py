"""Level-0 training-block day effects and automatic level-1 promotion.

Two responsibilities:

- Pure per-day effects (:func:`planned_block_days`, :func:`effect_for_day`,
  :class:`BlockEffect`). These take every input explicitly apart from the
  shared operational-day decision. They never touch the clock.
- :func:`reconcile_blocks`, which promotes a trainee to level 1 once the block's
  requested attended days are all recorded. It reaches the database *only*
  through :mod:`rotation_store` and the shared writer in :mod:`skill_levels`, so
  it is fully monkeypatchable and has no hidden side effects.
- :func:`complete_block_now`, which promotes early from an active or paused
  block using the early-completion claim helpers, then marks the block completed.

``BlockEffect`` is shaped to match what ``rotation_suggestions`` already
consumes: ``locked_people`` occupies normal operator slots (exempt from the
level-0 exclusion and the training cap), ``temporary_extra_people`` is the
day-one supervised pair that may exceed ordinary center staffing, and
``warnings`` is passed through to the suggestion.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta

from . import db, rotation_store, scheduler_time_off, shift_config, skill_levels, staffing

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class BlockEffect:
    """A single active block's contribution to one day's Recycled schedule."""

    locked_people: dict[str, list[str]] = field(default_factory=dict)
    temporary_extra_people: dict[str, list[str]] = field(default_factory=dict)
    locked_work_centers: dict[str, list[str]] = field(default_factory=dict)
    temporary_extra_work_centers: dict[str, list[str]] = field(default_factory=dict)
    warnings: Sequence[str] = ()


_EMPTY_EFFECT = BlockEffect()

# Defensive cap so a misconfigured (empty) working week can never spin
# ``planned_block_days`` into an infinite loop. It comfortably covers weekends
# plus a full year of absences for any realistic block length.
_MAX_SCAN_DAYS = 366


def planned_block_days(
    block: rotation_store.TrainingBlock,
    absence_by_day: Mapping[date, set[str]],
    *,
    scheduling_day: date | None = None,
) -> list[date]:
    """Return the block's attended working days.

    Walks forward from ``block.start_day`` collecting working days on which the
    trainee is not on a full-day absence, until ``planned_attended_days`` days
    are gathered. Absent days do not count, so the block naturally extends.
    While building a Saturday schedule, include that candidate day even before
    publication. Other unpublished Saturdays still do not consume training days.
    """
    out: list[date] = []
    outcomes = getattr(block, "day_statuses", {})
    cursor = block.start_day
    limit = block.start_day + timedelta(days=block.planned_attended_days + _MAX_SCAN_DAYS)
    while len(out) < block.planned_attended_days and cursor <= limit:
        if cursor in outcomes:
            if outcomes[cursor] == "attended":
                out.append(cursor)
        elif (
            (cursor == scheduling_day and cursor.weekday() == 5)
            or shift_config.is_workday(cursor)
        ) and block.trainee_name not in absence_by_day.get(cursor, set()):
            out.append(cursor)
        cursor += timedelta(days=1)
    return out


def effect_for_day(
    block: rotation_store.TrainingBlock,
    day: date,
    absence_by_day: Mapping[date, set[str]] | None = None,
    manual_assignees: set[str] | None = None,
) -> BlockEffect:
    """Return this block's effect for ``day``.

    The day-one attended day pairs the trainee with the level-3 trainer; later
    attended days reserve only the trainee. Saturday drafts are eligible before
    publication. A non-attended day (Sunday, before
    the block, past the window, or a trainee absence) yields an empty effect.
    A manual conflicting assignment for the trainee or trainer produces a
    warning and does not displace the manual choice.
    """
    if getattr(block, "status", "active") != "active":
        return _EMPTY_EFFECT
    absence_by_day = absence_by_day or {}
    manual = set(manual_assignees or ())

    planned = planned_block_days(block, absence_by_day, scheduling_day=day)
    if day not in planned:
        return _EMPTY_EFFECT

    # Effects use persisted scheduling-group keys for legacy records, which can
    # differ from the source matrix/Odoo skill name (Dismantler is stored in
    # Odoo as Dismantle). New protocol records target their saved work center.
    group = staffing.scheduling_group_for_skill(block.skill)
    work_center = getattr(block, "work_center", None)
    warnings: list[str] = []

    if block.trainee_name in manual:
        warnings.append(
            f"{block.trainee_name} has a manual assignment on {day.isoformat()}; "
            f"the {group} training block was not applied."
        )
        return BlockEffect(warnings=tuple(warnings))

    locked: dict[str, list[str]] = {}
    extra: dict[str, list[str]] = {}
    locked_work_centers: dict[str, list[str]] = {}
    extra_work_centers: dict[str, list[str]] = {}
    if work_center:
        locked_work_centers[work_center] = [block.trainee_name]
    else:
        locked[group] = [block.trainee_name]

    if day == planned[0]:
        if block.trainer_name in manual:
            warnings.append(
                f"Trainer {block.trainer_name} has a manual assignment on "
                f"{day.isoformat()}; day-one pairing was not applied."
            )
        else:
            if work_center:
                extra_work_centers[work_center] = [block.trainer_name]
            else:
                extra[group] = [block.trainer_name]

    return BlockEffect(
        locked_people=locked,
        temporary_extra_people=extra,
        locked_work_centers=locked_work_centers,
        temporary_extra_work_centers=extra_work_centers,
        warnings=tuple(warnings),
    )


def _is_attended(day_record) -> bool:
    status = getattr(day_record, "status", None)
    if status is None and isinstance(day_record, Mapping):
        status = day_record.get("status")
    return status == "attended"


def _reservation_was_applied(block, day: date, first_day: date) -> bool:
    """Whether the persisted schedule retained this protocol reservation.

    Exact-center protocol attendance is earned only by the generated
    reservation. This makes a manual conflict, a full center, or a disabled
    center (which leaves no generated reservation) a non-attended conflict
    rather than silently consuming a training day. Legacy group blocks have no
    exact target to inspect, so retain their established attendance behavior.
    """
    work_center = getattr(block, "work_center", None)
    if not work_center:
        return True
    try:
        schedule = staffing.load_schedule(day)
        assigned = set(getattr(schedule, "assignments", {}).get(work_center, ()))
        sources = getattr(schedule, "assignment_sources", {}).get(work_center, {})
    except Exception:  # noqa: BLE001 - unknown reservation must not count
        log.exception("Could not read training reservation for %s", day)
        return False

    required = {block.trainee_name}
    if day == first_day:
        required.add(block.trainer_name)
    return all(name in assigned and sources.get(name) == "generated" for name in required)


def _record_elapsed_day_outcomes(block, as_of: date) -> None:
    """Persist the scheduler-owned outcome for each elapsed protocol workday.

    ``as_of`` itself is still in progress, so only earlier workdays are
    recorded. Existing outcomes are immutable: they may have been resolved by
    an operator and must not be overwritten by a later scheduler tick.
    """
    start_day = getattr(block, "start_day", None)
    trainee_name = getattr(block, "trainee_name", None)
    if not isinstance(start_day, date) or not trainee_name:
        return

    existing = {record.day: record for record in rotation_store.resolved_days(block.id)}
    attended = 0
    cursor = start_day
    first_day: date | None = None
    while cursor < as_of and attended < block.planned_attended_days:
        if shift_config.is_workday(cursor):
            record = existing.get(cursor)
            if record is not None:
                if _is_attended(record):
                    attended += 1
                    if first_day is None:
                        first_day = cursor
            else:
                try:
                    absent_names = scheduler_time_off.full_day_off_names(cursor)
                except Exception:  # noqa: BLE001 - unknown attendance must not become attended
                    log.exception("Could not resolve training absence for %s", cursor)
                    return
                if trainee_name in absent_names:
                    status = "absent"
                else:
                    # Match ``planned_block_days``: the day-one pair moves to
                    # the first non-absent workday, while a conflicting
                    # attempted reservation remains that day's conflict.
                    status = (
                        "attended"
                        if _reservation_was_applied(block, cursor, first_day or cursor)
                        else "conflict"
                    )
                    if status == "attended" and first_day is None:
                        first_day = cursor
                rotation_store.record_attended_day(block.id, cursor, status)
                attended += int(status == "attended")
        cursor += timedelta(days=1)


def _promotion_skill_rows(block) -> list[dict]:
    skill_ids = tuple(getattr(block, "skill_ids", ()) or (block.skill_id,))
    rows = db.query(
        "SELECT s.id, s.skill_type, COALESCE(ps.level, 0) AS level "
        "FROM skills s LEFT JOIN person_skills ps "
        "ON ps.skill_id = s.id AND ps.person_id = %s WHERE s.id = ANY(%s)",
        (block.trainee_id, list(skill_ids)),
    )
    if {int(row["id"]) for row in rows} != set(skill_ids):
        raise rotation_store.InvalidTrainingBlock("A training skill is missing. Review this training plan.")
    return rows


def _promote_protocol_skills(block) -> None:
    # A manager or another training plan may have already raised a skill. Never
    # lower it, and never use a training plan to award a certification.
    for row in _promotion_skill_rows(block):
        if row["skill_type"] != "Certifications" and int(row["level"]) == 0:
            skill_levels.promote_untrained_person_skill(block.trainee_id, int(row["id"]))


def reconcile_blocks(as_of: date) -> list[int]:
    """Promote trainees whose blocks have reached their requested attended days.

    ``as_of`` is the current plant day; it frames the reconciliation and is part
    of the stable interface (callers pass ``plant_today()``). Reconciliation is
    also the scheduler's owner of elapsed-day recording: past workdays become
    attended or absent from the scheduler attendance source before progress is
    counted.

    For each still-active block with at least ``planned_attended_days`` attended
    days, this calls the shared skill writer for level 1, marks the block
    completed, and returns its id. Once completed, ``rotation_store.active_blocks``
    no longer returns it, so a block is never promoted twice.
    """
    promoted: list[int] = []
    for block in rotation_store.completing_blocks():
        with rotation_store.completion_guard(block.id) as acquired:
            if not acquired:
                continue
            current = rotation_store.get_block(block.id)
            if current is None or current.status != "completing":
                continue
            try:
                _promote_protocol_skills(current)
                rotation_store.mark_completed(current.id)
            except Exception:
                log.exception("Training block %s completion will retry", block.id)
                continue
            promoted.append(block.id)

    for candidate in rotation_store.active_blocks():
        with rotation_store.completion_guard(candidate.id) as acquired:
            if not acquired:
                continue
            block = rotation_store.get_block(candidate.id)
            if block is None or getattr(block, "status", "active") != "active":
                continue
            _record_elapsed_day_outcomes(block, as_of)
            attended = sum(1 for d in rotation_store.resolved_days(block.id) if _is_attended(d))
            if attended < block.planned_attended_days:
                continue
            if not rotation_store.claim_completion(block.id):
                continue
            try:
                _promote_protocol_skills(block)
            except Exception:  # noqa: BLE001 - one block's failure must not abort the pass
                # Leave the block active (do NOT mark completed) so the next
                # reconciliation retries the promotion, and keep going.
                log.exception(
                    "Training block %s promotion failed; leaving active to retry",
                    getattr(block, "id", "?"),
                )
                rotation_store.release_completion_claim(block.id)
                continue
            try:
                rotation_store.mark_completed(block.id)
            except Exception:  # noqa: BLE001 - promotion happened; retry finalization only
                log.exception("Training block %s finalization failed; leaving completing to retry", block.id)
                continue
            promoted.append(block.id)
    return promoted


def complete_block_now(block_id: int) -> None:
    """Promote a trainee early and mark the training block completed.

    Claims the block from ``active`` or ``paused``, writes level 1 for every
    target skill through the shared skill writer, then finalizes the block.
    If promotion fails, restores the prior status so the manager can retry.
    If finalization fails after a successful promotion, leave ``completing``
    for a later reconcile/retry (same durable-claim contract as reconcile).
    """
    with rotation_store.completion_guard(block_id) as acquired:
        if not acquired:
            raise rotation_store.InvalidTrainingBlock("Training is being updated. Please try again.")
        block = rotation_store.get_block(block_id)
        if block is None:
            raise rotation_store.InvalidTrainingBlock("Unknown training block.")
        prior = ("completing" if block.status == "completing"
                 else rotation_store.claim_early_completion(block_id))
        if prior is None:
            raise rotation_store.InvalidTrainingBlock("Training cannot be completed. Refresh the page.")
        try:
            _promote_protocol_skills(block)
        except Exception:
            rotation_store.release_early_completion_claim(block_id, prior)
            raise
        rotation_store.mark_completed(block_id)
