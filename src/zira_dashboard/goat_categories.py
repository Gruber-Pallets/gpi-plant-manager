from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from . import staffing, work_centers_store


@dataclass(frozen=True)
class GoatCategory:
    key: str
    label: str
    leaderboard_label: str
    group_name: str | None = None
    skill: str | None = None
    minimum_data_days: int = 1
    group_aliases: tuple[str, ...] = ()


_CATEGORIES = (
    GoatCategory("repairs", "Repairs", "Repair GOAT", group_name="Repairs"),
    GoatCategory("dismantlers", "Dismantlers", "Dismantler GOAT", group_name="Dismantlers"),
    GoatCategory("juniors", "Juniors", "Junior GOAT", skill="Junior"),
    GoatCategory("woodpecker", "Woodpecker", "Woodpecker GOAT", skill="Woodpecker"),
    GoatCategory(
        "hand_build",
        "Hand Build",
        "Hand Build GOAT",
        skill="Hand Build",
        minimum_data_days=30,
        group_aliases=("Hand Builds",),
    ),
)


def all_categories() -> tuple[GoatCategory, ...]:
    return _CATEGORIES


def has_category_key(key: str | None) -> bool:
    return any(category.key == key for category in _CATEGORIES)


def category_for_key(key: str) -> GoatCategory:
    return next(category for category in _CATEGORIES if category.key == key)


def category_for_group_name(group_name: str) -> GoatCategory | None:
    for category in _CATEGORIES:
        names = {category.label, *category.group_aliases}
        if category.group_name:
            names.add(category.group_name)
        if group_name in names:
            return category
    return None


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


def positive_data_days(category: GoatCategory, records: list[dict]) -> set[date]:
    names = work_center_names(category)
    return {
        record["day"]
        for record in records
        if record.get("wc") in names and float(record.get("units") or 0) > 0
    }


def is_goat_ready(category: GoatCategory, records: list[dict]) -> bool:
    return len(positive_data_days(category, records)) >= category.minimum_data_days
