"""Smart default suggestions for scheduler rotations.

The module holds two engines:

- The original Trim-Saw-only smart default (``smart_defaults_for_day`` and
  ``suggest_trim_saw_pair``), kept intact for its existing callers.
- The generic Recycled rotation engine (``suggest_recycled_assignments``)
  covering the Dismantler, Repair, and Trim Saw groups. It is pure: history,
  preferences, locks, and training-block effects are all passed in, and the
  same inputs always produce the same suggestion.
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

# ---------- Generic Recycled rotation engine ----------

RECYCLED_GROUPS = ("Dismantler", "Repair", "Trim Saw")
PREFERENCE_POINTS = {"primary": 30, "regular": 15, "occasional": 3, "never": -10_000}
MODE_SKILL_POINTS = {
    "optimized": {3: 100, 2: 55, 1: 5, 0: -10_000},
    "normal": {3: 55, 2: 40, 1: 25, 0: -10_000},
    "training": {3: 45, 2: 70, 1: 80, 0: -10_000},
}

GENERATED_SOURCE = "generated"
MANUAL_SOURCE = "manual"


@dataclass(frozen=True)
class RecycledHistory:
    """Bounded per-person history for group and center rotation fairness.

    ``center_counts`` and ``last_center_by_person_group`` drive the even
    Repair 1 -> 2 -> 3 style center cycle; ``group_counts`` and
    ``most_recent_group_names`` drive "time since the group" in normal mode.
    """

    center_counts: dict[tuple[str, str], int] = field(default_factory=dict)
    last_center_by_person_group: dict[tuple[str, str], str] = field(default_factory=dict)
    group_counts: dict[tuple[str, str], int] = field(default_factory=dict)
    most_recent_group_names: dict[str, set[str]] = field(default_factory=dict)


@dataclass(frozen=True)
class RecycledSuggestion:
    assignments: dict[str, list[str]]
    sources: dict[str, dict[str, str]]
    reasons: dict[str, dict[str, str]]
    warnings: Sequence[str]
    # The resolved group -> centers map the suggestion was built with. Not part
    # of the planned public fields; it exists so the helpers below report group
    # membership exactly as the engine scheduled it.
    group_locations: dict[str, tuple[str, ...]] = field(default_factory=dict)

    @property
    def assigned_people(self) -> set[str]:
        return {name for names in self.assignments.values() for name in names}

    def people_for_group(self, group: str) -> list[str]:
        out: list[str] = []
        if self.group_locations:
            centers = set(self.group_locations.get(group, ()))
            for center, names in self.assignments.items():
                if center in centers:
                    out.extend(names)
            return out
        # Suggestions built by hand (without the map) fall back to the
        # authoritative staffing.LOCATIONS lookup.
        for center, names in self.assignments.items():
            if _group_for_center(center) == group:
                out.extend(names)
        return out


def choose_center(name: str, group: str, centers: Sequence[str], history: RecycledHistory) -> str:
    return min(centers, key=lambda center: (
        history.center_counts.get((name, center), 0),
        center == history.last_center_by_person_group.get((name, group)),
        center.lower(),
    ))


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


# ---------- Generic Recycled rotation engine internals ----------

_LOCATIONS_BY_NAME = {loc.name: loc for loc in staffing.LOCATIONS}


def _assignments_from_row(row: dict) -> dict:
    """Prefer a row's posted snapshot assignments, else its live assignments.

    Mirrors ``_history_from_schedule_rows`` (the Trim Saw loader convention):
    posted schedule data wins over the working draft when it exists.
    """
    snapshot = row.get("published_snapshot")
    if isinstance(snapshot, dict) and isinstance(snapshot.get("assignments"), dict):
        return snapshot["assignments"]
    assignments = row.get("assignments")
    return assignments if isinstance(assignments, dict) else {}


def _recycled_history_from_rows(
    rows: Sequence[dict], group_locations: dict[str, Sequence[str]]
) -> RecycledHistory:
    """Aggregate bounded Recycled history across every managed center.

    ``rows`` are recent, non-testing schedules ordered newest first (idx 0 is
    the most recent). Sibling to ``_history_from_schedule_rows`` but tallies all
    Recycled centers instead of just Trim Saw, producing the per-center and
    per-group counts the fair-rotation logic reads.
    """
    center_to_group: dict[str, str] = {}
    for group, centers in group_locations.items():
        for center in centers:
            center_to_group[center] = group

    center_counts: dict[tuple[str, str], int] = {}
    group_counts: dict[tuple[str, str], int] = {}
    last_center_by_person_group: dict[tuple[str, str], str] = {}
    most_recent_group_names: dict[str, set[str]] = {}

    for idx, row in enumerate(rows):
        assignments = _assignments_from_row(row)
        for center, names in assignments.items():
            group = center_to_group.get(center)
            if group is None:
                continue
            for raw in names or []:
                name = str(raw or "").strip()
                if not name:
                    continue
                center_counts[(name, center)] = center_counts.get((name, center), 0) + 1
                group_counts[(name, group)] = group_counts.get((name, group), 0) + 1
                # Rows are newest-first, so the first center we see for a
                # (name, group) pair is the most recent one they worked.
                last_center_by_person_group.setdefault((name, group), center)
                if idx == 0:
                    most_recent_group_names.setdefault(group, set()).add(name)

    return RecycledHistory(
        center_counts=center_counts,
        last_center_by_person_group=last_center_by_person_group,
        group_counts=group_counts,
        most_recent_group_names=most_recent_group_names,
    )


def _load_recycled_history(day: date) -> RecycledHistory:
    """Load bounded Recycled center/group history for ``day``.

    Impure sibling of ``_load_trim_saw_history``: same bounded, testing-day-
    excluding window and snapshot-preferring convention, but aggregated across
    all Recycled centers (from ``staffing.LOCATIONS``) instead of Trim Saw only.
    """
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
    return _recycled_history_from_rows(rows, _default_group_locations())


def _group_for_center(center: str) -> str | None:
    loc = _LOCATIONS_BY_NAME.get(center)
    if loc is not None:
        required = staffing.required_skills_for(loc)
        if len(required) == 1 and required[0] in RECYCLED_GROUPS:
            return required[0]
        return None
    for group in RECYCLED_GROUPS:
        if center.startswith(group):
            return group
    return None


def _center_capacity(center: str) -> int:
    loc = _LOCATIONS_BY_NAME.get(center)
    if loc is None:
        return 1
    return int(loc.max_ops) if loc.max_ops is not None else 1_000_000


def _center_min_ops(center: str) -> int:
    loc = _LOCATIONS_BY_NAME.get(center)
    return int(loc.min_ops) if loc is not None else 1


def _default_group_locations() -> dict[str, tuple[str, ...]]:
    grouped: dict[str, list[str]] = {}
    for loc in staffing.LOCATIONS:
        if loc.department != "Recycled":
            continue
        required = staffing.required_skills_for(loc)
        if len(required) == 1 and required[0] in RECYCLED_GROUPS:
            grouped.setdefault(required[0], []).append(loc.name)
    return {group: tuple(centers) for group, centers in grouped.items()}


def _preference_for(
    preferences: dict[str, dict[str, str]] | None, name: str, group: str
) -> str:
    pref = ((preferences or {}).get(name) or {}).get(group, "regular")
    return pref if pref in PREFERENCE_POINTS else "regular"


def _recycled_level(person: staffing.Person | None, group: str) -> int:
    if person is None:
        return 0
    return max(0, min(3, int(person.level(group))))


def _rotation_fairness(name: str, group: str, history: RecycledHistory) -> int:
    fairness = -int(history.group_counts.get((name, group), 0)) * APPEARANCE_PENALTY
    if name in history.most_recent_group_names.get(group, set()):
        fairness -= MOST_RECENT_PENALTY
    return fairness


def _candidate_rank_key(
    mode: str,
    person: staffing.Person,
    group: str,
    preferences: dict[str, dict[str, str]] | None,
    history: RecycledHistory,
    green_supply: dict[str, int] | None = None,
) -> tuple:
    """Deterministic candidate ordering; lower sorts first.

    ``optimized`` ranks strictly by skill, steers each level-3 toward the
    group with the fewest available greens (so a multi-group green covers the
    group nobody else can), and only then breaks ties with preference and
    rotation history. ``normal`` weighs skill, preference, and time since the
    group comparably. ``training`` fills ordinary slots with the normal
    ranking (same green coverage) and adds development placements separately.
    """
    name = person.name
    level = _recycled_level(person, group)
    pref = _preference_for(preferences, name, group)
    tiebreak = PREFERENCE_POINTS[pref] + _rotation_fairness(name, group, history)
    ranking_mode = "normal" if mode == "training" else mode
    skill_points = MODE_SKILL_POINTS[ranking_mode][level]
    if ranking_mode == "optimized":
        scarcity = green_supply.get(group, 0) if green_supply and level == 3 else 0
        return (-skill_points, scarcity, -tiebreak, name.lower(), group.lower())
    return (-(skill_points + tiebreak), -level, name.lower(), group.lower())


def _development_rank_key(
    person: staffing.Person,
    group: str,
    preferences: dict[str, dict[str, str]] | None,
    history: RecycledHistory,
) -> tuple:
    name = person.name
    level = _recycled_level(person, group)
    pref = _preference_for(preferences, name, group)
    tiebreak = PREFERENCE_POINTS[pref] + _rotation_fairness(name, group, history)
    return (-(MODE_SKILL_POINTS["training"][level] + tiebreak), name.lower(), group.lower())


def _generated_reason(level: int, pref: str, group: str, center_count: int) -> str:
    if level == 3:
        return "green coverage"
    if pref == "primary":
        return f"primary {group} operator"
    if center_count > 1:
        return f"least-recent {group} center"
    return f"{group} rotation"


def suggest_recycled_assignments(
    day: date,
    mode: str,
    roster: Sequence[staffing.Person],
    preferences: dict[str, dict[str, str]] | None = None,
    base_assignments: dict[str, list[str]] | None = None,
    group_locations: dict[str, Sequence[str]] | None = None,
    history: RecycledHistory | None = None,
    locked_assignments: dict[str, Sequence[str]] | None = None,
    block_effects: Sequence = (),
    training_cap: int = 2,
) -> RecycledSuggestion:
    """Suggest safe Recycled assignments for the Dismantler/Repair/Trim Saw groups.

    Pure and deterministic: no clock or database reads (``day`` is part of the
    stable interface for callers and future reasons). Non-Recycled assignments
    and manual locks pass through unchanged; validated training-block effects
    are reserved first; level 0 is only placed through a block effect; and
    generated placements never seat a person at a second location. (Manual
    inputs are trusted as given: a name that appears both in a lock and in a
    pass-through base center is preserved in both places, not policed here.)
    """
    if mode not in MODE_SKILL_POINTS:
        raise ValueError(f"Unknown recycled rotation mode: {mode!r}")

    resolved_history = history if history is not None else RecycledHistory()
    if group_locations is None:
        groups: dict[str, tuple[str, ...]] = _default_group_locations()
    else:
        groups = {str(group): tuple(centers) for group, centers in group_locations.items()}
    managed_centers = {center for centers in groups.values() for center in centers}

    assignments: dict[str, list[str]] = {}
    for center, names in (base_assignments or {}).items():
        if center in managed_centers:
            continue  # rebuilt below; manual entries come back via locked_assignments
        assignments[center] = [str(n) for n in (names or []) if str(n or "").strip()]

    sources: dict[str, dict[str, str]] = {}
    reasons: dict[str, dict[str, str]] = {}
    warnings: list[str] = []
    by_name = {p.name: p for p in roster}
    assigned: set[str] = {name for names in assignments.values() for name in names}

    def _place(center: str, name: str, source: str, reason: str | None = None) -> None:
        assignments.setdefault(center, []).append(name)
        sources.setdefault(center, {})[name] = source
        if reason:
            reasons.setdefault(center, {})[name] = reason
        assigned.add(name)

    # 1. Manual locks survive rebuilds untouched.
    for center, names in (locked_assignments or {}).items():
        for name in names or []:
            name = str(name)
            if not name.strip() or name in assignments.get(center, []):
                continue
            _place(center, name, MANUAL_SOURCE)

    # 2. Reserve validated training-block effects. Locked block people take a
    # normal operator slot; temporary extras (the day-one trainer) pair into
    # the same center and may exceed ordinary staffing. Block people are exempt
    # from the level-0 exclusion and the daily training cap.
    for effect in block_effects or ():
        warnings.extend(str(w) for w in (getattr(effect, "warnings", None) or ()))
        block_center_by_group: dict[str, str] = {}
        warned_groups: set[str] = set()

        def _warn_missing_group(group: str) -> None:
            if group not in warned_groups:
                warned_groups.add(group)
                warnings.append(f"Training block for {group} has no schedulable work centers.")

        for group, names in (getattr(effect, "locked_people", None) or {}).items():
            centers = groups.get(group)
            if not centers:
                _warn_missing_group(group)
                continue
            for name in names or []:
                name = str(name)
                if not name.strip() or name in assigned:
                    continue
                open_centers = [
                    c for c in centers if len(assignments.get(c, [])) < _center_capacity(c)
                ]
                center = choose_center(name, group, open_centers or list(centers), resolved_history)
                _place(center, name, GENERATED_SOURCE, "training block")
                block_center_by_group[group] = center
        for group, names in (getattr(effect, "temporary_extra_people", None) or {}).items():
            centers = groups.get(group)
            if not centers:
                _warn_missing_group(group)
                continue
            for name in names or []:
                name = str(name)
                if not name.strip() or name in assigned:
                    continue
                center = block_center_by_group.get(group) or choose_center(
                    name, group, list(centers), resolved_history
                )
                _place(center, name, GENERATED_SOURCE, "training pair")

    # 3. Rank every eligible (person, group) combination for the day's mode.
    def _eligible(person: staffing.Person, group: str) -> bool:
        if not person.active or person.reserve:
            return False
        if _recycled_level(person, group) < 1:
            return False
        return _preference_for(preferences, person.name, group) != "never"

    candidate_pairs = [
        (person, group)
        for person in roster
        for group in groups
        if _eligible(person, group)
    ]

    # Optimized mode steers each green toward the group with the fewest other
    # available greens so multi-group greens maximize level-3 coverage.
    green_supply: dict[str, int] = {}
    if mode == "optimized":
        greens_by_group: dict[str, set[str]] = {}
        for person, group in candidate_pairs:
            if person.name not in assigned and _recycled_level(person, group) == 3:
                greens_by_group.setdefault(group, set()).add(person.name)
        green_supply = {group: len(names) for group, names in greens_by_group.items()}

    candidate_pairs.sort(
        key=lambda pair: _candidate_rank_key(
            mode, pair[0], pair[1], preferences, resolved_history, green_supply
        )
    )

    def _level_of(name: str, group: str) -> int:
        return _recycled_level(by_name.get(name), group)

    # 4. Greedy fill: best candidate first, fairest center for that candidate.
    for person, group in candidate_pairs:
        if person.name in assigned:
            continue
        centers = groups[group]
        open_centers = [c for c in centers if len(assignments.get(c, [])) < _center_capacity(c)]
        if not open_centers:
            continue
        level = _recycled_level(person, group)
        pref = _preference_for(preferences, person.name, group)
        reason = _generated_reason(level, pref, group, len(centers))
        if group != TRIM_SAW_SKILL:
            center = choose_center(person.name, group, open_centers, resolved_history)
            _place(center, person.name, GENERATED_SOURCE, reason)
            continue
        # Trim Saw keeps its pairing guarantee: never generate an unsafe pair.
        # Try the candidate's open centers in fairness order so one unsafe
        # center does not discard them while another center could seat them.
        remaining = list(open_centers)
        while remaining:
            center = choose_center(person.name, group, remaining, resolved_history)
            remaining.remove(center)
            occupants = assignments.get(center, [])
            if occupants:
                if all(_valid_trim_saw_pair(level, _level_of(name, group)) for name in occupants):
                    _place(center, person.name, GENERATED_SOURCE, reason)
                    break
                continue
            if _center_capacity(center) < 2:
                continue
            partner = None
            for other, other_group in candidate_pairs:
                if other_group != group or other.name == person.name or other.name in assigned:
                    continue
                if _valid_trim_saw_pair(level, _recycled_level(other, group)):
                    partner = other
                    break
            if partner is None:
                continue  # no safe pairing from this anchor; warned about below
            _place(center, person.name, GENERATED_SOURCE, reason)
            partner_level = _recycled_level(partner, group)
            partner_pref = _preference_for(preferences, partner.name, group)
            _place(
                center,
                partner.name,
                GENERATED_SOURCE,
                _generated_reason(partner_level, partner_pref, group, len(centers)),
            )
            break

    # 5. Training mode adds capped development placements: level-1/2 people
    # paired into a center that already has a level-3 operator.
    if mode == "training":
        development = [
            (person, group)
            for person, group in candidate_pairs
            if person.name not in assigned and _recycled_level(person, group) in (1, 2)
        ]
        development.sort(
            key=lambda pair: _development_rank_key(pair[0], pair[1], preferences, resolved_history)
        )
        placed_developments = 0
        for person, group in development:
            if placed_developments >= max(0, int(training_cap)):
                break
            if person.name in assigned:
                continue
            green_centers = [
                c
                for c in groups[group]
                if any(_level_of(name, group) == 3 for name in assignments.get(c, []))
            ]
            if group == TRIM_SAW_SKILL:
                # Trim Saw is a hard-capacity paired center: development
                # placements may not overfill it or create an unsafe pair,
                # unlike single-operator centers where the pairing is the point.
                level = _recycled_level(person, group)
                green_centers = [
                    c
                    for c in green_centers
                    if len(assignments.get(c, [])) < _center_capacity(c)
                    and all(
                        _valid_trim_saw_pair(level, _level_of(name, group))
                        for name in assignments.get(c, [])
                    )
                ]
            if not green_centers:
                continue
            center = choose_center(person.name, group, green_centers, resolved_history)
            _place(center, person.name, GENERATED_SOURCE, "training pair")
            placed_developments += 1

    # 6. Unresolvable coverage becomes a warning, never an unsafe assignment.
    for group, centers in groups.items():
        for center in centers:
            staffed = len(assignments.get(center, []))
            min_ops = _center_min_ops(center)
            if staffed >= min_ops:
                continue
            if staffed:
                warnings.append(f"{center} is staffed below its minimum of {min_ops} operators.")
            elif group == TRIM_SAW_SKILL and any(
                pair_group == group and person.name not in assigned
                for person, pair_group in candidate_pairs
            ):
                warnings.append(f"No safe operator pairing available for {center}.")

    return RecycledSuggestion(
        assignments=assignments,
        sources=sources,
        reasons=reasons,
        warnings=tuple(warnings),
        group_locations=groups,
    )
