"""Level-0 training-block day effects and automatic level-1 promotion.

Two responsibilities:

- Pure per-day effects (:func:`planned_block_days`, :func:`effect_for_day`,
  :class:`BlockEffect`). These take every input explicitly; the only external
  read is ``schedule_store.current().work_weekdays`` for the working calendar,
  exactly as the plan specifies. They never touch the database or the clock.
- :func:`reconcile_blocks`, which promotes a trainee to level 1 once the block's
  requested attended days are all recorded. It reaches the database *only*
  through :mod:`rotation_store` and the shared writer in :mod:`skill_levels`, so
  it is fully monkeypatchable and has no hidden side effects.

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

from . import rotation_store, schedule_store, skill_levels

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class BlockEffect:
    """A single active block's contribution to one day's Recycled schedule."""

    locked_people: dict[str, list[str]] = field(default_factory=dict)
    temporary_extra_people: dict[str, list[str]] = field(default_factory=dict)
    warnings: Sequence[str] = ()


_EMPTY_EFFECT = BlockEffect()

# Defensive cap so a misconfigured (empty) working week can never spin
# ``planned_block_days`` into an infinite loop. It comfortably covers weekends
# plus a full year of absences for any realistic block length.
_MAX_SCAN_DAYS = 366


def planned_block_days(
    block: rotation_store.TrainingBlock,
    absence_by_day: Mapping[date, set[str]],
) -> list[date]:
    """Return the block's attended working days.

    Walks forward from ``block.start_day`` collecting working days on which the
    trainee is not on a full-day absence, until ``planned_attended_days`` days
    are gathered. Absent days do not count, so the block naturally extends.
    """
    out: list[date] = []
    cursor = block.start_day
    work_weekdays = schedule_store.current().work_weekdays
    limit = block.start_day + timedelta(days=block.planned_attended_days + _MAX_SCAN_DAYS)
    while len(out) < block.planned_attended_days and cursor <= limit:
        if (
            cursor.weekday() in work_weekdays
            and block.trainee_name not in absence_by_day.get(cursor, set())
        ):
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
    attended days reserve only the trainee. A non-attended day (weekend, before
    the block, past the window, or a trainee absence) yields an empty effect.
    A manual conflicting assignment for the trainee or trainer produces a
    warning and does not displace the manual choice.
    """
    absence_by_day = absence_by_day or {}
    manual = set(manual_assignees or ())

    planned = planned_block_days(block, absence_by_day)
    if day not in planned:
        return _EMPTY_EFFECT

    # The effect is keyed by ``block.skill`` because a Recycled skill name is
    # also its rotation-group name (Dismantler / Repair / Trim Saw). If a future
    # group ever stops matching its skill name, this mapping must be revisited.
    group = block.skill
    warnings: list[str] = []

    if block.trainee_name in manual:
        warnings.append(
            f"{block.trainee_name} has a manual assignment on {day.isoformat()}; "
            f"the {group} training block was not applied."
        )
        return BlockEffect(warnings=tuple(warnings))

    locked: dict[str, list[str]] = {group: [block.trainee_name]}
    extra: dict[str, list[str]] = {}

    if day == planned[0]:
        if block.trainer_name in manual:
            warnings.append(
                f"Trainer {block.trainer_name} has a manual assignment on "
                f"{day.isoformat()}; day-one pairing was not applied."
            )
        else:
            extra[group] = [block.trainer_name]

    return BlockEffect(
        locked_people=locked,
        temporary_extra_people=extra,
        warnings=tuple(warnings),
    )


def _is_attended(day_record) -> bool:
    status = getattr(day_record, "status", None)
    if status is None and isinstance(day_record, Mapping):
        status = day_record.get("status")
    return status == "attended"


def reconcile_blocks(as_of: date) -> list[int]:
    """Promote trainees whose blocks have reached their requested attended days.

    ``as_of`` is the current plant day; it frames the reconciliation and is part
    of the stable interface (callers pass ``plant_today()``). Promotion itself
    is decided by the count of ``attended`` days already recorded for the block,
    so recording of individual days stays the caller's responsibility.

    For each still-active block with at least ``planned_attended_days`` attended
    days, this calls the shared skill writer for level 1, marks the block
    completed, and returns its id. Once completed, ``rotation_store.active_blocks``
    no longer returns it, so a block is never promoted twice.
    """
    promoted: list[int] = []
    for block in rotation_store.active_blocks():
        if getattr(block, "status", "active") != "active":
            continue
        attended = sum(1 for d in rotation_store.resolved_days(block.id) if _is_attended(d))
        if attended < block.planned_attended_days:
            continue
        try:
            skill_levels.set_person_skill_level(block.trainee_id, block.skill_id, 1)
        except Exception:  # noqa: BLE001 - one block's failure must not abort the pass
            # Leave the block active (do NOT mark completed) so the next
            # reconciliation retries the promotion, and keep going.
            log.exception(
                "Training block %s promotion failed; leaving active to retry",
                getattr(block, "id", "?"),
            )
            continue
        rotation_store.mark_completed(block.id)
        promoted.append(block.id)
    return promoted
