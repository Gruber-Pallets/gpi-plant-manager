from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass


@dataclass(frozen=True)
class AutoCapacity:
    required_people: int
    available_people: int
    shortage: int
    centers_to_disable: int
    runnable_centers: tuple[str, ...]
    blocked_centers: tuple[str, ...]


@dataclass(frozen=True)
class AutoExpansion:
    unassigned_people: int
    centers_to_enable: int | None
    usable_centers: tuple[str, ...]


def analyze_auto_expansion(
    *,
    unassigned_people: int,
    disabled_centers: Sequence[str],
    open_slots_by_center: Mapping[str, int],
    center_order: Mapping[str, int],
) -> AutoExpansion:
    remaining = max(0, int(unassigned_people))
    usable_names = tuple(
        sorted(
            (
                center
                for center in dict.fromkeys(disabled_centers)
                if int(open_slots_by_center.get(center, 0)) > 0
            ),
            key=lambda center: (
                -int(open_slots_by_center.get(center, 0)),
                center_order.get(center, 1_000_000),
                center.lower(),
            ),
        )
    )
    if remaining == 0:
        return AutoExpansion(0, 0, usable_names)

    covered = 0
    for count, center in enumerate(usable_names, start=1):
        covered += max(0, int(open_slots_by_center.get(center, 0)))
        if covered >= remaining:
            return AutoExpansion(remaining, count, usable_names)
    return AutoExpansion(remaining, None, usable_names)


def analyze_auto_capacity(
    *,
    enabled_centers: Sequence[str],
    minimum_by_center: Mapping[str, int],
    manual_count_by_center: Mapping[str, int],
    available_people: int,
    center_order: Mapping[str, int],
) -> AutoCapacity:
    ordered = tuple(
        sorted(
            dict.fromkeys(enabled_centers),
            key=lambda center: (center_order.get(center, 1_000_000), center.lower()),
        )
    )
    remaining = {
        center: max(
            0,
            int(minimum_by_center.get(center, 1))
            - int(manual_count_by_center.get(center, 0)),
        )
        for center in ordered
    }
    required = sum(remaining.values())
    available = max(0, int(available_people))
    runnable, used = [], 0
    for center in ordered:
        if used + remaining[center] <= available:
            runnable.append(center)
            used += remaining[center]
    runnable_centers = tuple(runnable)
    runnable_set = set(runnable_centers)
    blocked = tuple(center for center in ordered if center not in runnable_set)
    shortage = max(0, required - available)
    released, disable_count = 0, 0
    for center in sorted(
        ordered,
        key=lambda center: (
            -remaining[center],
            center_order.get(center, 1_000_000),
            center.lower(),
        ),
    ):
        if released >= shortage:
            break
        released += remaining[center]
        disable_count += 1
    return AutoCapacity(
        required_people=required,
        available_people=available,
        shortage=shortage,
        centers_to_disable=disable_count if shortage else 0,
        runnable_centers=runnable_centers,
        blocked_centers=blocked,
    )
