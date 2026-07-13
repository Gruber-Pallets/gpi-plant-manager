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
from collections.abc import Collection, Iterable, Mapping, Sequence

from . import schedule_solver, staffing

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
    reason_codes: dict[str, dict[str, str]] = field(default_factory=dict)
    staffed_centers: tuple[str, ...] = ()
    unresolved_centers: tuple[str, ...] = ()
    issues: tuple[schedule_solver.CoverageIssue, ...] = ()
    unused_people: tuple[str, ...] = ()

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


def _load_recycled_history(
    day: date,
    group_locations: dict[str, Sequence[str]] | None = None,
) -> RecycledHistory:
    """Load bounded Recycled center/group history for ``day``.

    Impure sibling of ``_load_trim_saw_history``: same bounded, testing-day-
    excluding window and snapshot-preferring convention. Direct callers retain
    the legacy Recycled-only grouping; the staffing route supplies all Auto
    scheduling-preference targets so their center fairness is also recorded.
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
    return _recycled_history_from_rows(
        rows,
        _default_group_locations() if group_locations is None else group_locations,
    )


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


def _group_level(
    person: staffing.Person | None,
    group: str,
    group_required_skills: dict[str, tuple[str, ...]],
) -> int:
    if person is None:
        return 0
    skills = group_required_skills.get(group, (group,))
    return min(
        (max(0, min(3, int(person.level(skill)))) for skill in skills),
        default=0,
    )


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
    group_required_skills: dict[str, tuple[str, ...]],
    green_supply: dict[str, int] | None = None,
) -> tuple:
    """Deterministic candidate ordering; lower sorts first.

    ``optimized`` ranks strictly by skill, steers each level-3 toward the
    group with the fewest available greens (so a multi-group green covers the
    group nobody else can), and only then breaks ties with preference and
    rotation history. ``normal`` weighs skill, preference, and time since the
    group comparably. ``training`` fills ordinary slots with the normal
    ranking and adds development placements separately.
    """
    name = person.name
    level = _group_level(person, group, group_required_skills)
    pref = _preference_for(preferences, name, group)
    tiebreak = PREFERENCE_POINTS[pref] + _rotation_fairness(name, group, history)
    ranking_mode = "normal" if mode == "training" else mode
    skill_points = MODE_SKILL_POINTS[ranking_mode][level]
    if ranking_mode == "optimized":
        scarcity = green_supply.get(group, 0) if green_supply and level == 3 else 0
        return (-skill_points, scarcity, -tiebreak, name.lower(), group.lower())
    return (
        -(skill_points + tiebreak),
        -PREFERENCE_POINTS[pref],
        -level,
        name.lower(),
        group.lower(),
    )


def _development_rank_key(
    person: staffing.Person,
    group: str,
    preferences: dict[str, dict[str, str]] | None,
    history: RecycledHistory,
    group_required_skills: dict[str, tuple[str, ...]],
) -> tuple:
    name = person.name
    level = _group_level(person, group, group_required_skills)
    pref = _preference_for(preferences, name, group)
    tiebreak = PREFERENCE_POINTS[pref] + _rotation_fairness(name, group, history)
    return (-(MODE_SKILL_POINTS["training"][level] + tiebreak), name.lower(), group.lower())


def _generated_reason(level: int, pref: str, group: str, center_count: int) -> str | None:
    if level == 3:
        return None
    if pref == "primary":
        return f"primary {group} operator"
    if center_count > 1:
        return f"least-recent {group} center"
    return f"{group} rotation"


def _minimum_eligible(
    person: staffing.Person,
    group: str,
    preferences: dict[str, dict[str, str]],
    group_required_skills: Mapping[str, tuple[str, ...]],
) -> bool:
    return (
        person.active
        and not person.reserve
        and _group_level(person, group, group_required_skills) >= 1
    )


def _minimum_rank_cost(
    person: staffing.Person,
    group: str,
    center: str,
    mode: str,
    preferences: dict[str, dict[str, str]],
    history: RecycledHistory,
    group_required_skills: Mapping[str, tuple[str, ...]],
    center_order: Mapping[str, int],
) -> int:
    level = _group_level(person, group, group_required_skills)
    mode_key = _candidate_rank_key(
        mode,
        person,
        group,
        preferences,
        history,
        group_required_skills,
    )
    stable_center = center_order.get(center, 1_000_000)
    stable_name = sum(ord(char) for char in person.name.lower())
    mode_cost = 10_000 + int(mode_key[0])
    return (
        (3 - level) * 1_000_000_000_000
        + mode_cost * 1_000_000
        + stable_center * 10_000
        + stable_name
    )


def _coverage_crew_is_safe(
    *,
    group: str,
    existing: Sequence[str],
    new_people: Sequence[str],
    by_name: Mapping[str, staffing.Person],
    required_skills: Mapping[str, tuple[str, ...]],
    trainees: Collection[str],
) -> bool:
    final_people = tuple(existing) + tuple(new_people)
    if trainees and not any(
        name not in trainees
        and _group_level(by_name.get(name), group, dict(required_skills)) == 3
        for name in final_people
    ):
        return False
    if group != TRIM_SAW_SKILL:
        return True
    levels = [
        _group_level(by_name.get(name), group, dict(required_skills))
        for name in final_people
    ]
    return len(levels) == 2 and _valid_trim_saw_pair(levels[0], levels[1])


def _coverage_rejections(
    *,
    group: str,
    roster: Sequence[staffing.Person],
    assigned: Collection[str],
    required_skills: Mapping[str, tuple[str, ...]],
) -> tuple[schedule_solver.CandidateRejection, ...]:
    rejected = []
    required = required_skills.get(group, (group,))
    for person in sorted(roster, key=lambda item: item.name.lower()):
        level = _group_level(person, group, dict(required_skills))
        if not person.active:
            code, detail = "inactive", "Person is inactive."
        elif person.reserve:
            code, detail = "reserve", "Person is in Reserves."
        elif person.name in assigned:
            code, detail = "already_assigned", "Person is already committed elsewhere."
        elif level == 0 and any(skill not in person.skills for skill in required):
            code, detail = "missing_skill", "Person is missing a required skill."
        elif level == 0:
            code, detail = (
                "level_zero",
                "Skill level is 0; an active training block is required.",
            )
        else:
            continue
        rejected.append(schedule_solver.CandidateRejection(
            person=person.name,
            code=code,
            detail=detail,
        ))
    return tuple(rejected)


def _coverage_requirements(
    *,
    mode: str,
    roster: Sequence[staffing.Person],
    groups: Mapping[str, Sequence[str]],
    required_skills: Mapping[str, tuple[str, ...]],
    preferences: dict[str, dict[str, str]],
    history: RecycledHistory,
    assignments: Mapping[str, Sequence[str]],
    sources: Mapping[str, Mapping[str, str]],
    assigned: Collection[str],
    allowed_centers: Collection[str],
    minimum_for,
    capacity_for,
    block_trainees_by_center: Mapping[str, Collection[str]],
) -> tuple[schedule_solver.CenterRequirement, ...]:
    by_name = {person.name: person for person in roster}
    center_order = {
        center: index
        for index, center in enumerate(center for values in groups.values() for center in values)
    }
    requirements = []
    for group, centers in groups.items():
        for center in centers:
            if center not in allowed_centers:
                continue
            existing = tuple(assignments.get(center, ()))
            trainees = set(block_trainees_by_center.get(center, ()))
            safe_existing = tuple(
                name
                for name in existing
                if name in trainees or (
                    (person := by_name.get(name)) is not None
                    and _minimum_eligible(person, group, preferences, required_skills)
                )
            )
            protected_crew_is_safe = _coverage_crew_is_safe(
                group=group,
                existing=safe_existing,
                new_people=(),
                by_name=by_name,
                required_skills=required_skills,
                trainees=trainees,
            )
            if group == TRIM_SAW_SKILL and len(existing) >= 2 and not protected_crew_is_safe:
                safe_existing = ()
            minimum = minimum_for(center)
            needs_green_partner = bool(trainees) and not any(
                name not in trainees
                and (person := by_name.get(name)) is not None
                and _group_level(person, group, required_skills) == 3
                for name in safe_existing
            )
            remaining = max(0, minimum - len(safe_existing), int(needs_green_partner))
            open_slots = max(0, capacity_for(center) - len(existing))
            available_people = [
                person
                for person in roster
                if person.name not in assigned
                and _minimum_eligible(person, group, preferences, required_skills)
            ]
            if needs_green_partner:
                available_people = [
                    person for person in available_people
                    if _group_level(person, group, required_skills) == 3
                ]
            edges = tuple(
                schedule_solver.CandidateEdge(
                    person=person.name,
                    center=center,
                    level=_group_level(person, group, required_skills),
                    preference=_preference_for(preferences, person.name, group),
                    rank_cost=_minimum_rank_cost(
                        person,
                        group,
                        center,
                        mode,
                        preferences,
                        history,
                        required_skills,
                        center_order,
                    ),
                )
                for person in sorted(available_people, key=lambda item: item.name.lower())
            )
            single_candidates = tuple(
                edge
                for edge in edges
                if _coverage_crew_is_safe(
                    group=group,
                    existing=existing,
                    new_people=(edge.person,),
                    by_name=by_name,
                    required_skills=required_skills,
                    trainees=trainees,
                )
            ) if remaining == 1 else ()
            crew_options = tuple(
                schedule_solver.CrewOption(center=center, members=tuple(crew))
                for crew in combinations(edges, remaining)
                if remaining > 1
                and remaining <= open_slots
                and _coverage_crew_is_safe(
                    group=group,
                    existing=existing,
                    new_people=tuple(member.person for member in crew),
                    by_name=by_name,
                    required_skills=required_skills,
                    trainees=trainees,
                )
            )
            qualified_people_exist = any(
                person.active
                and not person.reserve
                and _group_level(person, group, required_skills) >= 1
                for person in roster
            )
            level_zero_people = (
                ()
                if qualified_people_exist
                else tuple(sorted(
                    person.name
                    for person in roster
                    if person.active
                    and not person.reserve
                    and _group_level(person, group, required_skills) == 0
                ))
            )
            rejections = _coverage_rejections(
                group=group,
                roster=roster,
                assigned=assigned,
                required_skills=required_skills,
            )
            requirements.append(schedule_solver.CenterRequirement(
                center=center,
                group=group,
                remaining_slots=remaining,
                protected_people=safe_existing,
                candidates=single_candidates if open_slots >= 1 else (),
                crew_options=crew_options,
                level_zero_people=level_zero_people,
                rejections=rejections,
                unresolved_code=(
                    "no_safe_pair"
                    if edges and not (single_candidates or crew_options)
                    and (group == TRIM_SAW_SKILL or needs_green_partner)
                    else "insufficient_qualified_headcount"
                ),
                unresolved_message=(
                    f"{center} could not be staffed to its minimum of {minimum} operators."
                ),
            ))
    return tuple(requirements)


def suggest_recycled_assignments(
    day: date,
    mode: str,
    roster: Sequence[staffing.Person],
    preferences: dict[str, dict[str, str]] | None = None,
    base_assignments: dict[str, list[str]] | None = None,
    group_locations: dict[str, Sequence[str]] | None = None,
    group_required_skills: dict[str, tuple[str, ...]] | None = None,
    history: RecycledHistory | None = None,
    locked_assignments: dict[str, Sequence[str]] | None = None,
    block_effects: Sequence = (),
    training_cap: int = 2,
    center_minimums: Mapping[str, int] | None = None,
    center_capacities: Mapping[str, int | None] | None = None,
    runnable_centers: Collection[str] | None = None,
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
    resolved_preferences = preferences or {}
    if group_locations is None:
        groups: dict[str, tuple[str, ...]] = _default_group_locations()
    else:
        groups = {str(group): tuple(centers) for group, centers in group_locations.items()}
    resolved_group_required_skills = {
        str(group): tuple(skills)
        for group, skills in (group_required_skills or {}).items()
    }
    managed_centers = {center for centers in groups.values() for center in centers}

    def _effective_minimum(center: str) -> int:
        if center_minimums is not None and center in center_minimums:
            return max(0, int(center_minimums[center]))
        return _center_min_ops(center)

    def _effective_capacity(center: str) -> int:
        if center_capacities is not None and center in center_capacities:
            configured = center_capacities[center]
            return 1_000_000 if configured is None else max(0, int(configured))
        return _center_capacity(center)

    allowed_centers = (
        managed_centers
        if runnable_centers is None
        else managed_centers & set(runnable_centers)
    )

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

    def _center_priority(center: str) -> tuple[int, int, str]:
        deficit = max(0, _effective_minimum(center) - len(assignments.get(center, [])))
        return (0 if deficit else 1, -deficit, center.lower())

    def _choose_prioritized_center(name: str, group: str, centers: Sequence[str]) -> str:
        ordered = sorted(centers, key=_center_priority)
        # Center name makes priority deterministic, but must not displace the
        # existing per-person rotation fairness when deficits are otherwise tied.
        best_priority = _center_priority(ordered[0])[:2]
        prioritized = [
            center for center in ordered if _center_priority(center)[:2] == best_priority
        ]
        return choose_center(name, group, prioritized, resolved_history)

    def _eligible(person: staffing.Person, group: str) -> bool:
        if not person.active or person.reserve:
            return False
        if _group_level(person, group, resolved_group_required_skills) < 1:
            return False
        return _preference_for(resolved_preferences, person.name, group) != "never"

    def _level_of(name: str, group: str) -> int:
        return _group_level(by_name.get(name), group, resolved_group_required_skills)

    # 1. Manual locks survive rebuilds untouched.
    for center, names in (locked_assignments or {}).items():
        for name in names or []:
            name = str(name)
            if not name.strip() or name in assignments.get(center, []):
                continue
            _place(center, name, MANUAL_SOURCE)

    # 2. Reserve validated training-block effects. Locked block people take a
    # normal operator slot; temporary extras (the day-one trainer) may pair
    # into the same center only while its hard capacity remains open. Block
    # people are exempt from the level-0 exclusion and the daily training cap.
    protected_block_people: set[str] = set()
    block_centers: set[tuple[str, str]] = set()
    for effect in block_effects or ():
        warnings.extend(str(w) for w in (getattr(effect, "warnings", None) or ()))
        block_center_by_group: dict[str, str] = {}
        warned_groups: set[str] = set()

        def _warn_missing_group(group: str) -> None:
            if group not in warned_groups:
                warned_groups.add(group)
                warnings.append(f"Training block for {group} has no schedulable work centers.")

        for group, names in (getattr(effect, "locked_people", None) or {}).items():
            centers = [center for center in groups.get(group, ()) if center in allowed_centers]
            if not centers:
                _warn_missing_group(group)
                continue
            for name in names or []:
                name = str(name)
                if not name.strip() or name in assigned:
                    continue
                open_centers = [
                    c for c in centers if len(assignments.get(c, [])) < _effective_capacity(c)
                ]
                if not open_centers:
                    warning = (
                        f"Training block for {group} could not reserve an open work center."
                    )
                    if warning not in warnings:
                        warnings.append(warning)
                    continue
                center = _choose_prioritized_center(name, group, open_centers)
                _place(center, name, GENERATED_SOURCE, "training block")
                protected_block_people.add(name)
                block_centers.add((group, center))
                block_center_by_group[group] = center
        for group, names in (getattr(effect, "temporary_extra_people", None) or {}).items():
            centers = [center for center in groups.get(group, ()) if center in allowed_centers]
            if not centers:
                _warn_missing_group(group)
                continue
            for name in names or []:
                name = str(name)
                if not name.strip() or name in assigned:
                    continue
                center = block_center_by_group.get(group) or _choose_prioritized_center(
                    name, group, centers
                )
                if len(assignments.get(center, [])) >= _effective_capacity(center):
                    continue
                _place(center, name, GENERATED_SOURCE, "training pair")

    block_trainees_by_center = {
        center: {
            name for name in assignments.get(center, ())
            if name in protected_block_people
        }
        for _group, center in block_centers
    }
    requirements = _coverage_requirements(
        mode=mode,
        roster=roster,
        groups=groups,
        required_skills=resolved_group_required_skills,
        preferences=resolved_preferences,
        history=resolved_history,
        assignments=assignments,
        sources=sources,
        assigned=assigned,
        allowed_centers=allowed_centers,
        minimum_for=_effective_minimum,
        capacity_for=_effective_capacity,
        block_trainees_by_center=block_trainees_by_center,
    )
    coverage = schedule_solver.solve_minimum_coverage(requirements)
    reason_codes: dict[str, dict[str, str]] = {}
    for decision in coverage.decisions:
        _place(decision.center, decision.person, GENERATED_SOURCE, decision.reason)
        reason_codes.setdefault(decision.center, {})[decision.person] = decision.reason_code

    warnings.extend(
        issue.message for issue in coverage.issues if issue.message not in warnings
    )
    for issue in coverage.issues:
        if issue.group == TRIM_SAW_SKILL and issue.code == "no_safe_pair":
            warning = f"No safe operator pairing available for {issue.center}."
            if warning not in warnings:
                warnings.append(warning)

    # Minimum coverage is now frozen. Rank the remaining optional candidates
    # without allowing a Never preference to fill spare capacity.

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
            if person.name not in assigned and _group_level(
                person, group, resolved_group_required_skills
            ) == 3:
                greens_by_group.setdefault(group, set()).add(person.name)
        green_supply = {group: len(names) for group, names in greens_by_group.items()}

    candidate_pairs.sort(
        key=lambda pair: _candidate_rank_key(
            mode,
            pair[0],
            pair[1],
            preferences,
            resolved_history,
            resolved_group_required_skills,
            green_supply,
        )
    )

    # Fill optional capacity only at centers whose complete minimum crew was
    # established by the global solver or protected inputs.
    for person, group in candidate_pairs:
        if person.name in assigned:
            continue
        centers = groups[group]
        open_centers = [
            c
            for c in centers
            if c in allowed_centers
            and c in coverage.staffed_centers
            and len(assignments.get(c, [])) < _effective_capacity(c)
        ]
        if not open_centers:
            continue
        level = _group_level(person, group, resolved_group_required_skills)
        pref = _preference_for(preferences, person.name, group)
        reason = _generated_reason(level, pref, group, len(centers))
        if group != TRIM_SAW_SKILL:
            center = _choose_prioritized_center(person.name, group, open_centers)
            _place(center, person.name, GENERATED_SOURCE, reason)
            continue
        # Trim Saw keeps its pairing guarantee: never generate an unsafe pair.
        # Try the candidate's open centers in fairness order so one unsafe
        # center does not discard them while another center could seat them.
        remaining = list(open_centers)
        while remaining:
            center = _choose_prioritized_center(person.name, group, remaining)
            remaining.remove(center)
            occupants = assignments.get(center, [])
            if occupants:
                if all(_valid_trim_saw_pair(level, _level_of(name, group)) for name in occupants):
                    _place(center, person.name, GENERATED_SOURCE, reason)
                    break
                continue
            if _effective_capacity(center) < 2:
                continue
            partner = None
            for other, other_group in candidate_pairs:
                if other_group != group or other.name == person.name or other.name in assigned:
                    continue
                if _valid_trim_saw_pair(
                    level,
                    _group_level(other, group, resolved_group_required_skills),
                ):
                    partner = other
                    break
            if partner is None:
                continue  # no safe pairing from this anchor; warned about below
            _place(center, person.name, GENERATED_SOURCE, reason)
            partner_level = _group_level(partner, group, resolved_group_required_skills)
            partner_pref = _preference_for(preferences, partner.name, group)
            _place(
                center,
                partner.name,
                GENERATED_SOURCE,
                _generated_reason(partner_level, partner_pref, group, len(centers)),
            )
            break

    # Training mode adds capped development placements: level-1/2 people
    # paired into a center that already has a level-3 operator, without ever
    # exceeding that center's hard capacity.
    if mode == "training":
        development = [
            (person, group)
            for person, group in candidate_pairs
            if person.name not in assigned
            and _group_level(person, group, resolved_group_required_skills) in (1, 2)
        ]
        development.sort(
            key=lambda pair: _development_rank_key(
                pair[0],
                pair[1],
                preferences,
                resolved_history,
                resolved_group_required_skills,
            )
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
                if c in allowed_centers
                and c in coverage.staffed_centers
                and len(assignments.get(c, [])) < _effective_capacity(c)
                if any(_level_of(name, group) == 3 for name in assignments.get(c, []))
            ]
            if group == TRIM_SAW_SKILL:
                # Trim Saw also retains its pairing guarantee.
                level = _group_level(person, group, resolved_group_required_skills)
                green_centers = [
                    c
                    for c in green_centers
                    if all(
                        _valid_trim_saw_pair(level, _level_of(name, group))
                        for name in assignments.get(c, [])
                    )
                ]
            if not green_centers:
                continue
            center = _choose_prioritized_center(person.name, group, green_centers)
            _place(center, person.name, GENERATED_SOURCE, "training pair")
            placed_developments += 1

    generated_people = {
        name
        for center_sources in sources.values()
        for name, source in center_sources.items()
        if source == GENERATED_SOURCE
    }
    eligible_people = {
        person.name
        for person in roster
        if person.active and not person.reserve
    }
    for center in allowed_centers:
        assignments.setdefault(center, [])
    return RecycledSuggestion(
        assignments=assignments,
        sources=sources,
        reasons=reasons,
        warnings=tuple(warnings),
        group_locations={group: tuple(centers) for group, centers in groups.items()},
        reason_codes=reason_codes,
        staffed_centers=coverage.staffed_centers,
        unresolved_centers=coverage.unresolved_centers,
        issues=coverage.issues,
        unused_people=tuple(sorted(eligible_people - generated_people - assigned, key=str.lower)),
    )
