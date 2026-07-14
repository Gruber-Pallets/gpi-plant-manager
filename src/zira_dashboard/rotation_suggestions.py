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
    complete: bool = False
    available_people: tuple[str, ...] = ()
    placed_people: tuple[str, ...] = ()
    placement_issues: tuple[schedule_solver.PlacementIssue, ...] = ()
    default_assignments: dict[str, str] = field(default_factory=dict)

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
    rows: Sequence[dict],
    group_locations: dict[str, Sequence[str]],
    user_group_centers: Mapping[str, Sequence[str]] | None = None,
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
    user_groups_by_center: dict[str, list[str]] = {}
    for group, centers in (user_group_centers or {}).items():
        for center in centers:
            user_groups_by_center.setdefault(center, []).append(str(group))

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
                for user_group in user_groups_by_center.get(center, ()):
                    last_center_by_person_group.setdefault(
                        (name, f"User Group:{user_group}"), center
                    )
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
    user_group_centers: Mapping[str, Sequence[str]] | None = None,
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
        user_group_centers,
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


def _optional_reason(
    mode: str,
    level: int,
    pref: str,
    group: str,
    center_count: int,
    *,
    training_development: bool = False,
) -> tuple[str, str]:
    if training_development:
        if pref == "primary":
            text = f"primary {group} operator"
        elif center_count > 1:
            text = f"least-recent {group} center"
        else:
            text = f"{group} rotation"
        return "training_development", text
    if mode == "optimized":
        return "strongest_coverage", f"strongest {group} coverage"
    if pref == "primary":
        return "primary_preference", f"primary {group} operator"
    if center_count > 1:
        return "rotation_fairness", f"least-recent {group} center"
    return "rotation_fairness", f"{group} rotation"


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
    mode_cost = 10_000 + int(mode_key[0])
    center_fairness_cost = (
        int(history.center_counts.get((person.name, center), 0)) * 2
        + int(center == history.last_center_by_person_group.get((person.name, group)))
    )
    return (
        (3 - level) * 1_000_000_000_000
        + mode_cost * 1_000_000
        + center_fairness_cost
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


def _protected_assignment_issues(
    *,
    roster: Sequence[staffing.Person],
    groups: Mapping[str, Sequence[str]],
    required_skills: Mapping[str, tuple[str, ...]],
    assignments: Mapping[str, Sequence[str]],
    sources: Mapping[str, Mapping[str, str]],
    allowed_centers: Collection[str],
    block_trainees_by_center: Mapping[str, Collection[str]],
) -> tuple[schedule_solver.CoverageIssue, ...]:
    by_name = {person.name: person for person in roster}
    issues = []
    for group, centers in groups.items():
        required = required_skills.get(group, (group,))
        for center in centers:
            if center not in allowed_centers:
                continue
            trainees = set(block_trainees_by_center.get(center, ()))
            rejections = []
            for name in assignments.get(center, ()):
                if name in trainees or name not in sources.get(center, {}):
                    continue
                person = by_name.get(name)
                if person is None:
                    reason = "is unavailable in the active roster"
                elif not person.active:
                    reason = "is inactive"
                elif person.reserve:
                    reason = "is in Reserves"
                elif any(skill not in person.skills for skill in required):
                    reason = "is missing a required skill"
                elif _group_level(person, group, dict(required_skills)) < 1:
                    reason = "has skill level 0"
                else:
                    continue
                rejections.append(schedule_solver.CandidateRejection(
                    person=name,
                    code="protected_assignment_unqualified",
                    detail=(
                        f"Protected assignment {reason} and does not safely count "
                        "toward minimum coverage."
                    ),
                ))
            if rejections:
                issues.append(schedule_solver.CoverageIssue(
                    center=center,
                    group=group,
                    code="protected_assignment_unqualified",
                    message=(
                        f"{center} has a protected assignment that does not safely "
                        "count toward minimum coverage."
                    ),
                    rejections=tuple(rejections),
                ))
    return tuple(issues)


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
    conflicting_protected_people: Collection[str],
) -> tuple[schedule_solver.CenterRequirement, ...]:
    by_name = {person.name: person for person in roster}
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
                if name not in conflicting_protected_people
                and (name in trainees or (
                    (person := by_name.get(name)) is not None
                    and _minimum_eligible(person, group, preferences, required_skills)
                ))
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
            capacity = capacity_for(center)
            if minimum > capacity:
                requirements.append(schedule_solver.CenterRequirement(
                    center=center,
                    group=group,
                    remaining_slots=max(1, minimum - len(safe_existing)),
                    protected_people=safe_existing,
                    unresolved_code="invalid_center_configuration",
                    unresolved_message=(
                        f"{center} has an invalid configuration: its minimum of "
                        f"{minimum} exceeds its maximum of {capacity}."
                    ),
                ))
                continue
            needs_green_partner = bool(trainees) and not any(
                name not in trainees
                and (person := by_name.get(name)) is not None
                and _group_level(person, group, required_skills) == 3
                for name in safe_existing
            )
            remaining = max(0, minimum - len(safe_existing), int(needs_green_partner))
            open_slots = max(0, capacity - len(existing))
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
    exact_defaults: Mapping[str, Sequence[str]] | None = None,
    group_defaults: Mapping[str, Sequence[str]] | None = None,
    user_group_centers: Mapping[str, Sequence[str]] | None = None,
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

    by_name = {p.name: p for p in roster}
    available_names = tuple(sorted(
        (person.name for person in roster if person.active and not person.reserve),
        key=str.lower,
    ))
    available_set = frozenset(available_names)
    center_group: dict[str, str] = {}
    for group, centers in sorted(groups.items(), key=lambda item: item[0].lower()):
        for center in centers:
            if center in allowed_centers:
                center_group.setdefault(center, group)

    assignments: dict[str, list[str]] = {}
    assigned: set[str] = set()
    warnings: list[str] = []
    for center, names in (base_assignments or {}).items():
        if center not in allowed_centers:
            copied = [str(name) for name in (names or []) if str(name or "").strip()]
            assignments[center] = copied
            assigned.update(copied)
            continue
        group = center_group.get(center)
        for raw_name in names or ():
            name = str(raw_name or "").strip()
            person = by_name.get(name)
            if not name or name in assigned:
                continue
            if (
                group is None
                or name not in available_set
                or _group_level(person, group, resolved_group_required_skills) < 1
                or len(assignments.get(center, ())) >= _effective_capacity(center)
            ):
                warnings.append(
                    f"{name} was removed from {center} because it is not a safe Auto assignment."
                )
                continue
            assignments.setdefault(center, []).append(name)
            assigned.add(name)

    sources: dict[str, dict[str, str]] = {}
    reasons: dict[str, dict[str, str]] = {}
    reason_codes: dict[str, dict[str, str]] = {}

    def _place(
        center: str,
        name: str,
        source: str,
        reason: str | None = None,
        reason_code: str | None = None,
    ) -> None:
        assignments.setdefault(center, []).append(name)
        sources.setdefault(center, {})[name] = source
        if source == GENERATED_SOURCE:
            if not reason or not reason_code:
                raise ValueError("generated assignments require a reason code and display text")
            reasons.setdefault(center, {})[name] = reason
            reason_codes.setdefault(center, {})[name] = reason_code
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

    # 1. Valid manual locks survive rebuilds. Invalid locks are cleared like
    # any other enabled Auto assignment so rebuild never leaves an unsafe slot.
    for center, names in (locked_assignments or {}).items():
        for name in names or []:
            name = str(name).strip()
            if not name:
                continue
            if name in assignments.get(center, []):
                sources.setdefault(center, {})[name] = MANUAL_SOURCE
                continue
            group = center_group.get(center)
            person = by_name.get(name)
            if (
                center not in allowed_centers
                or group is None
                or name in assigned
                or name not in available_set
                or _group_level(person, group, resolved_group_required_skills) < 1
                or len(assignments.get(center, ())) >= _effective_capacity(center)
            ):
                warnings.append(
                    f"{name} was removed from {center} because it is not a safe Auto assignment."
                )
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
                _place(center, name, GENERATED_SOURCE, "training block", "training_block")
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
                _place(center, name, GENERATED_SOURCE, "training pair", "training_block")

    block_trainees_by_center = {
        center: {
            name for name in assignments.get(center, ())
            if name in protected_block_people
        }
        for _group, center in block_centers
    }
    protected_centers_by_name: dict[str, list[str]] = {}
    for center, names in assignments.items():
        for name in names:
            protected_centers_by_name.setdefault(name, []).append(center)
    conflicting_protected = {
        name: tuple(sorted(set(centers), key=str.lower))
        for name, centers in protected_centers_by_name.items()
        if len(set(centers)) > 1
    }

    # Rebuild fills every remaining safe Auto opening. It does not require
    # every available person or center minimum to be satisfied first.
    solver_people = tuple(name for name in available_names if name not in assigned)

    protected_issues = _protected_assignment_issues(
        roster=roster,
        groups=groups,
        required_skills=resolved_group_required_skills,
        assignments=assignments,
        sources=sources,
        allowed_centers=allowed_centers,
        block_trainees_by_center=block_trainees_by_center,
    )
    placement_issues: list[schedule_solver.PlacementIssue] = []
    for name, centers in sorted(conflicting_protected.items(), key=lambda item: item[0].lower()):
        placement_issues.append(schedule_solver.PlacementIssue(
            code="protected_assignment_conflict",
            person=name,
            centers=centers,
            message=(
                f"{name} is protected at multiple work centers "
                f"({', '.join(centers)}). Previous schedule kept."
            ),
        ))

    default_targets: dict[str, list[tuple[str, str]]] = {}
    for center, names in (exact_defaults or {}).items():
        for raw_name in names or ():
            name = str(raw_name or "").strip()
            if name:
                default_targets.setdefault(name, []).append(("exact", str(center)))
    for group_name, names in (group_defaults or {}).items():
        for raw_name in names or ():
            name = str(raw_name or "").strip()
            if name:
                default_targets.setdefault(name, []).append(("group", str(group_name)))

    exact_target_by_person: dict[str, str] = {}
    group_target_by_person: dict[str, str] = {}
    solver_people_set = frozenset(solver_people)
    for name, targets in sorted(default_targets.items(), key=lambda item: item[0].lower()):
        if name not in solver_people_set:
            continue  # absent, reserve, inactive, or manually protected today
        unique_targets = tuple(sorted(set(targets), key=lambda item: (item[0], item[1].lower())))
        if len(unique_targets) > 1:
            labels = tuple(f"{kind}:{target}" for kind, target in unique_targets)
            placement_issues.append(schedule_solver.PlacementIssue(
                code="default_target_conflict",
                person=name,
                centers=tuple(target for _kind, target in unique_targets),
                message=(
                    f"{name} has multiple default targets ({', '.join(labels)}). "
                    "The Auto scheduler will use its best safe placement."
                ),
            ))
            continue
        kind, target = unique_targets[0]
        if kind == "exact":
            if target not in allowed_centers or target not in center_group:
                placement_issues.append(schedule_solver.PlacementIssue(
                    code="exact_default_center_disabled",
                    person=name,
                    centers=(target,),
                message=(
                    f"{name}'s default work center {target} is not enabled."
                ),
            ))
            continue
            group = center_group[target]
            if _group_level(by_name.get(name), group, resolved_group_required_skills) < 1:
                placement_issues.append(schedule_solver.PlacementIssue(
                    code="exact_default_unqualified",
                    person=name,
                    centers=(target,),
                message=(
                    f"{name} is not qualified for default work center {target}."
                ),
            ))
            continue
        exact_target_by_person[name] = target
        continue

        members = tuple(
            center
            for center in (user_group_centers or {}).get(target, ())
            if center in allowed_centers and center in center_group
        )
        if not members:
            placement_issues.append(schedule_solver.PlacementIssue(
                code="group_default_no_enabled_member",
                person=name,
                centers=(),
                message=(
                    f"{name}'s default group {target} has no enabled member work center."
                ),
            ))
            continue
        qualified = tuple(
            center
            for center in members
            if _group_level(
                by_name.get(name),
                center_group[center],
                resolved_group_required_skills,
            ) >= 1
        )
        if not qualified:
            placement_issues.append(schedule_solver.PlacementIssue(
                code="group_default_no_qualified_member",
                person=name,
                centers=tuple(sorted(members, key=str.lower)),
                message=(
                    f"{name} is not qualified for any enabled work center in "
                    f"default group {target}."
                ),
            ))
            continue
        group_target_by_person[name] = target

    for center in sorted(allowed_centers, key=str.lower):
        minimum = _effective_minimum(center)
        capacity = _effective_capacity(center)
        if minimum > capacity:
            placement_issues.append(schedule_solver.PlacementIssue(
                code="invalid_center_configuration",
                centers=(center,),
                message=(
                    f"{center} has a minimum of {minimum} but a maximum of "
                    f"{capacity}."
                ),
            ))

    def _finish_failure(
        failures: Sequence[schedule_solver.PlacementIssue],
        *,
        solver_result: schedule_solver.CompleteScheduleResult | None = None,
    ) -> RecycledSuggestion:
        combined = tuple(failures)
        failure_warnings = list(warnings)
        for issue in (*protected_issues, *combined):
            if issue.message not in failure_warnings:
                failure_warnings.append(issue.message)
        placed = tuple(sorted(
            (name for name in available_names if name in assigned),
            key=str.lower,
        ))
        unused = tuple(name for name in available_names if name not in assigned)
        unresolved = tuple(sorted(
            {
                center
                for issue in combined
                for center in issue.centers
                if center in allowed_centers
            },
            key=str.lower,
        ))
        return RecycledSuggestion(
            assignments=assignments,
            sources=sources,
            reasons=reasons,
            warnings=tuple(failure_warnings),
            group_locations={group: tuple(centers) for group, centers in groups.items()},
            reason_codes=reason_codes,
            staffed_centers=(solver_result.staffed_centers if solver_result else ()),
            unresolved_centers=unresolved,
            issues=protected_issues,
            unused_people=unused,
            complete=False,
            available_people=available_names,
            placed_people=placed,
            placement_issues=combined,
        )

    candidate_edges: list[schedule_solver.CandidateEdge] = []
    edges_by_center: dict[str, list[schedule_solver.CandidateEdge]] = {
        center: [] for center in allowed_centers
    }
    for name in solver_people:
        person = by_name[name]
        for center in sorted(allowed_centers, key=str.lower):
            group = center_group.get(center)
            if group is None:
                continue
            level = _group_level(person, group, resolved_group_required_skills)
            if level < 1:
                continue
            preference = _preference_for(resolved_preferences, name, group)
            if name in group_target_by_person:
                user_group = group_target_by_person[name]
                rank_cost = (
                    int(resolved_history.center_counts.get((name, center), 0))
                    * 10_000_000_000_000_000
                    + int(
                        center
                        == resolved_history.last_center_by_person_group.get(
                            (name, f"User Group:{user_group}")
                        )
                    )
                    * 1_000_000_000_000_000
                    + _minimum_rank_cost(
                        person,
                        group,
                        center,
                        mode,
                        resolved_preferences,
                        resolved_history,
                        resolved_group_required_skills,
                    )
                )
            else:
                rank_cost = _minimum_rank_cost(
                    person,
                    group,
                    center,
                    mode,
                    resolved_preferences,
                    resolved_history,
                    resolved_group_required_skills,
                )
            edge = schedule_solver.CandidateEdge(
                person=name,
                center=center,
                level=level,
                preference=preference,
                rank_cost=rank_cost,
            )
            candidate_edges.append(edge)
            edges_by_center[center].append(edge)

    complete_centers: list[schedule_solver.CompleteCenter] = []
    direct_candidates: list[schedule_solver.CandidateEdge] = []
    coupled_failure: list[schedule_solver.PlacementIssue] = []
    remaining_minimum_by_center: dict[str, int] = {}
    for center in sorted(allowed_centers, key=str.lower):
        group = center_group[center]
        existing = tuple(assignments.get(center, ()))
        trainees = set(block_trainees_by_center.get(center, ()))
        safe_existing = tuple(
            name
            for name in existing
            if name not in conflicting_protected
            and (
                name in trainees
                or (
                    (person := by_name.get(name)) is not None
                    and _minimum_eligible(
                        person,
                        group,
                        resolved_preferences,
                        resolved_group_required_skills,
                    )
                )
            )
        )
        remaining_minimum = max(0, _effective_minimum(center) - len(safe_existing))
        remaining_capacity = max(0, _effective_capacity(center) - len(existing))
        remaining_minimum_by_center[center] = remaining_minimum
        needs_green_partner = bool(trainees) and not any(
            name not in trainees
            and (person := by_name.get(name)) is not None
            and _group_level(person, group, resolved_group_required_skills) == 3
            for name in existing
        )
        coupled = group == TRIM_SAW_SKILL or needs_green_partner
        crew_options: tuple[schedule_solver.CrewOption, ...] = ()
        if coupled and remaining_capacity:
            options = []
            minimum_generated = max(remaining_minimum, int(needs_green_partner))
            maximum_generated = min(
                remaining_capacity,
                len(edges_by_center.get(center, ())),
            )
            for size in range(max(1, minimum_generated), maximum_generated + 1):
                for crew in combinations(edges_by_center.get(center, ()), size):
                    if _coverage_crew_is_safe(
                        group=group,
                        existing=existing,
                        new_people=tuple(member.person for member in crew),
                        by_name=by_name,
                        required_skills=resolved_group_required_skills,
                        trainees=trainees,
                    ):
                        options.append(schedule_solver.CrewOption(center, tuple(crew)))
            crew_options = tuple(options)
            if remaining_minimum and not crew_options:
                coupled_failure.append(schedule_solver.PlacementIssue(
                    code="no_safe_complete_crew",
                    centers=(center,),
                    message=(
                        f"{center} cannot form a safe complete crew. "
                        "Previous schedule kept."
                    ),
                ))
        if coupled and not crew_options:
            # No safe optional crew exists. Preserve the center at zero
            # generated capacity so ordinary flow can never create an unsafe
            # partial pair.
            remaining_capacity = 0
        elif not coupled:
            direct_candidates.extend(edges_by_center.get(center, ()))
        complete_centers.append(schedule_solver.CompleteCenter(
            center=center,
            group=group,
            minimum=remaining_minimum,
            capacity=remaining_capacity,
            crew_options=crew_options,
        ))

    placement_issues.extend(coupled_failure)

    # Choose safe coupled crews first, then greedily place each remaining
    # person at their best qualified ordinary center. Minimum coverage is not
    # a prerequisite for either decision; capacity and qualification are.
    preference_order = {"primary": 0, "regular": 1, "occasional": 2, "never": 3}
    selected: list[schedule_solver.AssignmentDecision] = []
    selected_people: set[str] = set()
    remaining_capacity = {center.center: center.capacity for center in complete_centers}
    for center in sorted(
        (item for item in complete_centers if item.crew_options),
        key=lambda item: item.center.lower(),
    ):
        options = [
            option for option in center.crew_options
            if not set(option.people).intersection(selected_people)
            and len(option.members) <= remaining_capacity[center.center]
        ]
        if not options:
            continue
        option = min(options, key=lambda item: (
            -sum(member.level for member in item.members),
            sum(preference_order[member.preference] for member in item.members),
            sum(member.rank_cost for member in item.members),
            tuple(member.person.lower() for member in item.members),
        ))
        for edge in option.members:
            selected.append(schedule_solver.AssignmentDecision(
                center=edge.center,
                person=edge.person,
                level=edge.level,
                preference=edge.preference,
                reason_code="partial_fill",
                reason="Assigned to a safe Auto work center.",
                rank_cost=edge.rank_cost,
            ))
            selected_people.add(edge.person)
            remaining_capacity[center.center] -= 1

    for name in sorted(solver_people, key=str.lower):
        if name in selected_people:
            continue
        choices = [
            edge for edge in direct_candidates
            if edge.person == name and remaining_capacity.get(edge.center, 0) > 0
        ]
        if not choices:
            continue
        choice = min(choices, key=lambda edge: (
            -edge.level,
            preference_order[edge.preference],
            edge.rank_cost,
            edge.center.lower(),
        ))
        selected.append(schedule_solver.AssignmentDecision(
            center=choice.center,
            person=choice.person,
            level=choice.level,
            preference=choice.preference,
            reason_code="partial_fill",
            reason="Assigned to a safe Auto work center.",
            rank_cost=choice.rank_cost,
        ))
        selected_people.add(choice.person)
        remaining_capacity[choice.center] -= 1

    complete_result = schedule_solver.CompleteScheduleResult(
        complete=len(selected_people) == len(solver_people),
        decisions=tuple(sorted(
            selected, key=lambda item: (item.center.lower(), item.person.lower())
        )),
        placed_people=tuple(sorted(selected_people, key=str.lower)),
        unplaced_people=tuple(sorted(
            set(solver_people) - selected_people, key=str.lower
        )),
        staffed_centers=tuple(sorted(
            (
                center.center for center in complete_centers
                if len(assignments.get(center.center, ()))
                + sum(item.center == center.center for item in selected) >= center.minimum
            ),
            key=str.lower,
        )),
        issues=(),
    )

    default_assignments: dict[str, str] = {}
    decisions_by_center: dict[str, list[schedule_solver.AssignmentDecision]] = {}
    for decision in complete_result.decisions:
        decisions_by_center.setdefault(decision.center, []).append(decision)
    for center, center_decisions in decisions_by_center.items():
        remaining_minimum = remaining_minimum_by_center.get(center, 0)
        ordered_decisions = sorted(
            center_decisions,
            key=lambda item: (
                item.person not in exact_target_by_person
                and item.person not in group_target_by_person,
                item.rank_cost,
                item.person.lower(),
            ),
        )
        for decision in ordered_decisions:
            group = center_group[center]
            if decision.preference == "never":
                reason_code = "preference_override"
                reason = "Assigned despite Never so every available person is scheduled."
            elif (
                exact_target_by_person.get(decision.person) == center
            ):
                reason_code = "exact_default"
                reason = f"default work center: {center}"
                default_assignments[decision.person] = center
            elif (
                decision.person in group_target_by_person
                and center in (user_group_centers or {}).get(
                    group_target_by_person[decision.person], ()
                )
            ):
                reason_code = "group_default"
                reason = (
                    f"default group {group_target_by_person[decision.person]}; "
                    "least-used qualified center"
                )
                default_assignments[decision.person] = center
            elif remaining_minimum > 0:
                reason_code = "minimum_coverage"
                reason = "Assigned to meet minimum coverage."
            else:
                level = _group_level(
                    by_name.get(decision.person),
                    group,
                    resolved_group_required_skills,
                )
                reason_code, reason = _optional_reason(
                    mode,
                    level,
                    decision.preference,
                    group,
                    len(groups.get(group, ())),
                    training_development=(mode == "training" and level in (1, 2)),
                )
            _place(
                center,
                decision.person,
                GENERATED_SOURCE,
                reason,
                reason_code,
            )
            if remaining_minimum > 0:
                remaining_minimum -= 1

    for centers in groups.values():
        for center in centers:
            if center in allowed_centers:
                assignments.setdefault(center, [])
    placed_people = tuple(sorted(
        (name for name in available_names if name in assigned),
        key=str.lower,
    ))
    unused_people = tuple(name for name in available_names if name not in assigned)
    for name in unused_people:
        placement_issues.append(schedule_solver.PlacementIssue(
            code="person_unplaced",
            person=name,
            message=f"{name} could not be placed in an enabled Auto work center.",
        ))
    for center in sorted(allowed_centers, key=str.lower):
        if len(assignments.get(center, ())) < _effective_minimum(center):
            placement_issues.append(schedule_solver.PlacementIssue(
                code="center_minimum_unmet",
                centers=(center,),
                message=f"{center} is below its minimum Auto staffing level.",
            ))
    for issue in protected_issues:
        if issue.message not in warnings:
            warnings.append(issue.message)
    return RecycledSuggestion(
        assignments=assignments,
        sources=sources,
        reasons=reasons,
        warnings=tuple(warnings),
        group_locations={group: tuple(centers) for group, centers in groups.items()},
        reason_codes=reason_codes,
        staffed_centers=complete_result.staffed_centers,
        unresolved_centers=tuple(sorted({
            center
            for issue in placement_issues
            for center in issue.centers
            if center in allowed_centers
        }, key=str.lower)),
        issues=protected_issues,
        unused_people=unused_people,
        complete=not unused_people and not any(
            issue.code == "center_minimum_unmet" for issue in placement_issues
        ),
        available_people=available_names,
        placed_people=placed_people,
        placement_issues=tuple(placement_issues),
        default_assignments=default_assignments,
    )
