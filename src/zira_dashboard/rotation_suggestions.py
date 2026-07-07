"""Smart default suggestions for scheduler rotations.

Version 1 is intentionally scoped to Trim Saw. Keep the constants isolated so
other rotating work centers can reuse the shape later without changing the
staffing route again.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from itertools import combinations
from collections.abc import Iterable, Sequence

from . import staffing

TRIM_SAW_WC = "Trim Saw 1"
TRIM_SAW_SKILL = "Trim Saw"
LOOKBACK_SCHEDULE_COUNT = 20
TRIM_SAW_MAX_OPERATORS = 2

SKILL_BASE_WEIGHTS = {
    3: 100,
    2: 70,
    1: 25,
    0: 0,
}
APPEARANCE_PENALTY = 12
MOST_RECENT_PENALTY = 8


@dataclass(frozen=True)
class TrimSawHistory:
    appearance_counts: dict[str, int] = field(default_factory=dict)
    most_recent_names: set[str] = field(default_factory=set)


def _names_from_assignments(assignments) -> list[str]:
    if not isinstance(assignments, dict):
        return []
    return [
        str(name)
        for name in (assignments.get(TRIM_SAW_WC) or [])
        if str(name or "").strip()
    ]


def _history_from_schedule_rows(rows: Sequence[dict]) -> TrimSawHistory:
    counts: dict[str, int] = {}
    most_recent_names: set[str] = set()
    for idx, row in enumerate(rows):
        snapshot = row.get("published_snapshot")
        if isinstance(snapshot, dict) and isinstance(snapshot.get("assignments"), dict):
            names = _names_from_assignments(snapshot.get("assignments"))
        else:
            names = _names_from_assignments(row.get("assignments"))
        if idx == 0:
            most_recent_names = set(names)
        for name in names:
            counts[name] = counts.get(name, 0) + 1
    return TrimSawHistory(appearance_counts=counts, most_recent_names=most_recent_names)


def _trim_saw_level(person: staffing.Person | None) -> int:
    if person is None:
        return 0
    return max(0, min(3, int(person.level(TRIM_SAW_SKILL))))


def _valid_trim_saw_pair(level_a: int, level_b: int) -> bool:
    low = min(int(level_a), int(level_b))
    high = max(int(level_a), int(level_b))
    if low <= 1:
        return high >= 3
    return low >= 2


def _candidate_score(
    person: staffing.Person,
    history: TrimSawHistory,
    *,
    pinned: bool = False,
) -> int:
    level = _trim_saw_level(person)
    base = SKILL_BASE_WEIGHTS.get(level, 0)
    if pinned and level == 0:
        base = 1
    appearances = int(history.appearance_counts.get(person.name, 0))
    score = base - (appearances * APPEARANCE_PENALTY)
    if person.name in history.most_recent_names:
        score -= MOST_RECENT_PENALTY
    return score


def _person_sort_key(
    person: staffing.Person,
    history: TrimSawHistory,
    *,
    pinned: bool = False,
) -> tuple[int, int, int, str]:
    return (
        -_candidate_score(person, history, pinned=pinned),
        -_trim_saw_level(person),
        int(history.appearance_counts.get(person.name, 0)),
        person.name.lower(),
    )


def _best_person(
    people: Sequence[staffing.Person],
    history: TrimSawHistory,
    pinned_names: set[str],
) -> staffing.Person | None:
    if not people:
        return None
    return min(
        people,
        key=lambda p: _person_sort_key(p, history, pinned=p.name in pinned_names),
    )


def _best_compatible_partner(
    anchor: staffing.Person,
    candidates: Sequence[staffing.Person],
    history: TrimSawHistory,
    pinned_names: set[str],
) -> staffing.Person | None:
    anchor_level = _trim_saw_level(anchor)
    compatible = [
        p
        for p in candidates
        if p.name != anchor.name and _valid_trim_saw_pair(anchor_level, _trim_saw_level(p))
    ]
    return _best_person(compatible, history, pinned_names)


def _best_pair(
    candidates: Sequence[staffing.Person],
    history: TrimSawHistory,
    pinned_names: set[str],
) -> list[staffing.Person]:
    valid_pairs: list[tuple[staffing.Person, staffing.Person]] = []
    for left, right in combinations(candidates, 2):
        if _valid_trim_saw_pair(_trim_saw_level(left), _trim_saw_level(right)):
            valid_pairs.append((left, right))
    if valid_pairs:
        best_left, best_right = min(
            valid_pairs,
            key=lambda pair: (
                -(
                    _candidate_score(pair[0], history, pinned=pair[0].name in pinned_names)
                    + _candidate_score(pair[1], history, pinned=pair[1].name in pinned_names)
                ),
                -max(_trim_saw_level(pair[0]), _trim_saw_level(pair[1])),
                -min(_trim_saw_level(pair[0]), _trim_saw_level(pair[1])),
                sum(history.appearance_counts.get(p.name, 0) for p in pair),
                tuple(sorted(p.name.lower() for p in pair)),
            ),
        )
        return sorted(
            [best_left, best_right],
            key=lambda p: _person_sort_key(p, history, pinned=p.name in pinned_names),
        )
    strongest = _best_person(candidates, history, pinned_names)
    return [strongest] if strongest is not None else []


def _full_day_time_off_names(time_off_entries: Sequence[dict]) -> set[str]:
    return {
        str(entry.get("name") or "")
        for entry in (time_off_entries or [])
        if entry.get("hours") is None and str(entry.get("name") or "").strip()
    }


def smart_defaults_for_day(
    day: date,
    roster: Sequence[staffing.Person],
    base_assignments: dict[str, list[str]],
    time_off_entries: Sequence[dict],
) -> dict[str, list[str]]:
    smart = {wc: list(names or []) for wc, names in (base_assignments or {}).items()}
    pinned = list(smart.get(TRIM_SAW_WC, []))
    unavailable = _full_day_time_off_names(time_off_entries)
    for wc_name, names in smart.items():
        if wc_name == TRIM_SAW_WC:
            continue
        unavailable.update(names or [])
    pair = suggest_trim_saw_pair(day, roster, pinned, unavailable)
    if pair:
        smart[TRIM_SAW_WC] = pair[:TRIM_SAW_MAX_OPERATORS]
    else:
        smart.pop(TRIM_SAW_WC, None)
    return smart


def suggest_trim_saw_pair(
    day: date,
    roster: Sequence[staffing.Person],
    pinned_names: Sequence[str],
    unavailable_names: Iterable[str],
    history: TrimSawHistory | None = None,
) -> list[str]:
    if history is not None:
        resolved_history = history
    else:
        try:
            resolved_history = _load_trim_saw_history(day)
        except Exception:
            resolved_history = TrimSawHistory()
    unavailable = set(unavailable_names or [])
    by_name = {p.name: p for p in roster if p.active and not p.reserve and p.name not in unavailable}
    pinned_set = set(pinned_names or [])
    pinned_people = [by_name[name] for name in pinned_names if name in by_name]
    candidates = list(by_name.values())

    if len(pinned_people) >= TRIM_SAW_MAX_OPERATORS:
        first_two = pinned_people[:TRIM_SAW_MAX_OPERATORS]
        if _valid_trim_saw_pair(_trim_saw_level(first_two[0]), _trim_saw_level(first_two[1])):
            return [p.name for p in first_two]
        anchor = _best_person(first_two, resolved_history, pinned_set)
        if anchor is None:
            return []
        partner = _best_compatible_partner(
            anchor,
            [p for p in candidates if p.name != anchor.name],
            resolved_history,
            pinned_set,
        )
        return [anchor.name] + ([partner.name] if partner else [])

    if len(pinned_people) == 1:
        anchor = pinned_people[0]
        partner = _best_compatible_partner(
            anchor,
            [p for p in candidates if p.name != anchor.name],
            resolved_history,
            pinned_set,
        )
        return [anchor.name] + ([partner.name] if partner else [])

    return [p.name for p in _best_pair(candidates, resolved_history, pinned_set)]


def _load_trim_saw_history(day: date) -> TrimSawHistory:
    from . import db

    rows = db.query(
        "SELECT s.day, s.published_snapshot, "
        "       COALESCE(jsonb_object_agg(wc.name, names.people) "
        "                FILTER (WHERE wc.name IS NOT NULL), '{}'::jsonb) AS assignments "
        "FROM ("
        "  SELECT day, published_snapshot "
        "  FROM schedules "
        "  WHERE day < %s "
        "    AND COALESCE((published_snapshot->>'testing_day')::boolean, testing_day, FALSE) = FALSE "
        "  ORDER BY day DESC "
        "  LIMIT %s"
        ") s "
        "LEFT JOIN LATERAL ("
        "  SELECT sa.day, sa.wc_id, jsonb_agg(pe.name ORDER BY sa.sort_order) AS people "
        "  FROM schedule_assignments sa "
        "  JOIN people pe ON pe.id = sa.person_id "
        "  WHERE sa.day = s.day "
        "  GROUP BY sa.day, sa.wc_id"
        ") names ON TRUE "
        "LEFT JOIN work_centers wc ON wc.id = names.wc_id "
        "GROUP BY s.day, s.published_snapshot "
        "ORDER BY s.day DESC",
        (day, LOOKBACK_SCHEDULE_COUNT),
    )
    return _history_from_schedule_rows(rows)
