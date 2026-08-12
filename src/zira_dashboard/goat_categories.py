from __future__ import annotations

from dataclasses import dataclass

from . import staffing, work_centers_store


@dataclass(frozen=True)
class GoatCategory:
    key: str
    label: str
    leaderboard_label: str
    group_name: str | None = None
    skill: str | None = None


_CATEGORIES = (
    GoatCategory("repairs", "Repairs", "Repair GOAT", group_name="Repairs"),
    GoatCategory("dismantlers", "Dismantlers", "Dismantler GOAT", group_name="Dismantlers"),
    GoatCategory("juniors", "Juniors", "Junior GOAT", skill="Junior"),
    GoatCategory("woodpecker", "Woodpecker", "Woodpecker GOAT", skill="Woodpecker"),
    GoatCategory("hand_build", "Hand Build", "Hand Build GOAT", skill="Hand Build"),
)


def all_categories() -> tuple[GoatCategory, ...]:
    return _CATEGORIES


def has_category_key(key: str | None) -> bool:
    return any(category.key == key for category in _CATEGORIES)


def category_for_key(key: str) -> GoatCategory:
    return next(category for category in _CATEGORIES if category.key == key)


def recycling_categories() -> tuple[GoatCategory, ...]:
    return tuple(category for category in _CATEGORIES if category.group_name is not None)


def new_categories() -> tuple[GoatCategory, ...]:
    return tuple(category for category in _CATEGORIES if category.skill is not None)


def members(category: GoatCategory):
    if category.group_name is not None:
        return tuple(work_centers_store.members("group", category.group_name))
    return tuple(location for location in staffing.LOCATIONS if location.skill == category.skill)


def work_center_names(category: GoatCategory) -> set[str]:
    return {location.name for location in members(category)}


def has_metered_source(category: GoatCategory) -> bool:
    return any(location.meter_id for location in members(category))
