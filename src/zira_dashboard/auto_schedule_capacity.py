from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass


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
