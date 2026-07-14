from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Literal, Sequence

Preference = Literal["primary", "regular", "occasional", "never"]


@dataclass(frozen=True)
class CandidateRejection:
    person: str
    code: str
    detail: str

    def to_dict(self) -> dict[str, str]:
        return {"person": self.person, "code": self.code, "detail": self.detail}


@dataclass(frozen=True)
class CandidateEdge:
    person: str
    center: str
    level: int
    preference: Preference
    rank_cost: int = 0

    @property
    def override_cost(self) -> int:
        return int(self.preference == "never")


@dataclass(frozen=True)
class CrewOption:
    center: str
    members: tuple[CandidateEdge, ...]

    @property
    def people(self) -> tuple[str, ...]:
        return tuple(member.person for member in self.members)


@dataclass(frozen=True)
class CenterRequirement:
    center: str
    group: str
    remaining_slots: int
    protected_people: tuple[str, ...] = ()
    candidates: tuple[CandidateEdge, ...] = ()
    crew_options: tuple[CrewOption, ...] = ()
    level_zero_people: tuple[str, ...] = ()
    rejections: tuple[CandidateRejection, ...] = ()
    unresolved_code: str = "insufficient_qualified_headcount"
    unresolved_message: str = ""


@dataclass(frozen=True)
class AssignmentDecision:
    center: str
    person: str
    level: int
    preference: Preference
    reason_code: str
    reason: str
    rank_cost: int


@dataclass(frozen=True)
class CoverageIssue:
    center: str
    group: str
    code: str
    message: str
    rejections: tuple[CandidateRejection, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "center": self.center,
            "group": self.group,
            "code": self.code,
            "message": self.message,
            "rejections": [item.to_dict() for item in self.rejections],
        }


@dataclass(frozen=True)
class CoverageResult:
    decisions: tuple[AssignmentDecision, ...]
    staffed_centers: tuple[str, ...]
    unresolved_centers: tuple[str, ...]
    issues: tuple[CoverageIssue, ...]

    @property
    def assigned_people(self) -> frozenset[str]:
        return frozenset(item.person for item in self.decisions)


@dataclass(frozen=True)
class CompleteCenter:
    center: str
    group: str
    minimum: int
    capacity: int
    protected_people: tuple[str, ...] = ()
    crew_options: tuple[CrewOption, ...] = ()


@dataclass(frozen=True)
class PlacementIssue:
    code: str
    message: str
    person: str | None = None
    centers: tuple[str, ...] = ()
    rejections: tuple[CandidateRejection, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "code": self.code,
            "message": self.message,
            "person": self.person,
            "centers": list(self.centers),
            "rejections": [item.to_dict() for item in self.rejections],
        }


@dataclass(frozen=True)
class CompleteScheduleResult:
    complete: bool
    decisions: tuple[AssignmentDecision, ...]
    placed_people: tuple[str, ...]
    unplaced_people: tuple[str, ...]
    staffed_centers: tuple[str, ...]
    issues: tuple[PlacementIssue, ...]
    total_cost: int = 0


@dataclass
class _Arc:
    to: int
    reverse: int
    capacity: int
    cost: int


def _add_arc(graph: list[list[_Arc]], start: int, end: int, capacity: int, cost: int) -> int:
    forward_index = len(graph[start])
    graph[start].append(_Arc(end, len(graph[end]), capacity, cost))
    graph[end].append(_Arc(start, forward_index, 0, -cost))
    return forward_index


def _decision(edge: CandidateEdge) -> AssignmentDecision:
    override = edge.preference == "never"
    return AssignmentDecision(
        center=edge.center,
        person=edge.person,
        level=edge.level,
        preference=edge.preference,
        reason_code="preference_override" if override else "minimum_coverage",
        reason=(
            "Assigned despite Never to meet minimum coverage."
            if override
            else "Assigned to meet minimum coverage."
        ),
        rank_cost=edge.rank_cost,
    )


def _match_single_requirements(
    requirements: Sequence[CenterRequirement],
    unavailable_people: frozenset[str] = frozenset(),
) -> tuple[AssignmentDecision, ...]:
    centers = tuple(sorted(requirements, key=lambda item: item.center.lower()))
    edges = tuple(sorted(
        (
            edge
            for requirement in centers
            for edge in requirement.candidates
            if edge.person not in unavailable_people
        ),
        key=lambda edge: (edge.person.lower(), edge.center.lower(), edge.rank_cost),
    ))
    people = tuple(sorted({edge.person for edge in edges}, key=str.lower))
    source = 0
    person_node = {name: index + 1 for index, name in enumerate(people)}
    center_offset = 1 + len(people)
    center_node = {item.center: center_offset + index for index, item in enumerate(centers)}
    sink = center_offset + len(centers)
    graph: list[list[_Arc]] = [[] for _ in range(sink + 1)]

    for person in people:
        _add_arc(graph, source, person_node[person], 1, 0)
    for requirement in centers:
        _add_arc(graph, center_node[requirement.center], sink, 1, 0)

    ordinary_bound = sum(max(0, edge.rank_cost) for edge in edges) + len(edges) + 1
    unmatched_digit = len(people)
    tie_radix = unmatched_digit + 1
    tie_range = tie_radix ** len(centers) - 1
    tie_scale = tie_range + 1
    person_digit = {person: index for index, person in enumerate(people)}
    center_place = {
        requirement.center: tie_radix ** (len(centers) - index - 1)
        for index, requirement in enumerate(centers)
    }
    chosen_arcs: list[tuple[int, int, CandidateEdge]] = []
    for edge in edges:
        primary_cost = edge.override_cost * ordinary_bound + max(0, edge.rank_cost)
        tie_cost = (
            person_digit[edge.person] - unmatched_digit
        ) * center_place[edge.center]
        cost = primary_cost * tie_scale + tie_cost
        start = person_node[edge.person]
        arc_index = _add_arc(graph, start, center_node[edge.center], 1, cost)
        chosen_arcs.append((start, arc_index, edge))

    node_count = len(graph)
    while True:
        distance: list[int | None] = [None] * node_count
        previous: list[tuple[int, int] | None] = [None] * node_count
        distance[source] = 0
        pending = deque((source,))
        queued = {source}
        while pending:
            node = pending.popleft()
            queued.remove(node)
            node_distance = distance[node]
            if node_distance is None:
                continue
            for arc_index, arc in enumerate(graph[node]):
                candidate = node_distance + arc.cost
                if arc.capacity and (
                    distance[arc.to] is None or candidate < distance[arc.to]
                ):
                    distance[arc.to] = candidate
                    previous[arc.to] = (node, arc_index)
                    if arc.to not in queued:
                        pending.append(arc.to)
                        queued.add(arc.to)
        if previous[sink] is None:
            break
        node = sink
        while node != source:
            prior_node, arc_index = previous[node]  # type: ignore[misc]
            arc = graph[prior_node][arc_index]
            arc.capacity -= 1
            graph[node][arc.reverse].capacity += 1
            node = prior_node

    selected = [
        _decision(edge)
        for start, arc_index, edge in chosen_arcs
        if graph[start][arc_index].capacity == 0
    ]
    return tuple(sorted(selected, key=lambda item: (item.center.lower(), item.person.lower())))


def _issue(
    requirement: CenterRequirement,
    decisions: Sequence[AssignmentDecision],
) -> CoverageIssue:
    if requirement.level_zero_people and not requirement.candidates and not requirement.crew_options:
        code = "training_required"
        message = (
            f"{requirement.center} could not be staffed. "
            f"Training is required for {requirement.group}."
        )
    else:
        code = requirement.unresolved_code
        message = requirement.unresolved_message or (
            f"{requirement.center} could not be staffed to its minimum."
        )
    assigned_center = {item.person: item.center for item in decisions}
    candidate_people = {
        edge.person
        for edge in requirement.candidates
    } | {
        edge.person
        for option in requirement.crew_options
        for edge in option.members
    }
    rejections = list(requirement.rejections)
    known = {(item.person, item.code) for item in rejections}
    for person in sorted(candidate_people, key=str.lower):
        other_center = assigned_center.get(person)
        if other_center and other_center != requirement.center:
            item = CandidateRejection(
                person=person,
                code="needed_elsewhere",
                detail=f"Used at {other_center} by the maximum-coverage solution.",
            )
        else:
            item = CandidateRejection(
                person=person,
                code="global_tie_break",
                detail="Available, but assigning them here would not improve total coverage.",
            )
        if (item.person, item.code) not in known:
            rejections.append(item)
    return CoverageIssue(
        center=requirement.center,
        group=requirement.group,
        code=code,
        message=message,
        rejections=tuple(rejections),
    )


def _assemble_result(
    requirements: Sequence[CenterRequirement],
    decisions: Sequence[AssignmentDecision],
) -> CoverageResult:
    assigned_centers = {item.center for item in decisions}
    staffed = {
        requirement.center
        for requirement in requirements
        if requirement.remaining_slots == 0 or requirement.center in assigned_centers
    }
    unresolved = tuple(sorted(
        (item for item in requirements if item.center not in staffed),
        key=lambda item: item.center.lower(),
    ))
    return CoverageResult(
        decisions=tuple(sorted(
            decisions,
            key=lambda item: (item.center.lower(), item.person.lower()),
        )),
        staffed_centers=tuple(sorted(staffed, key=str.lower)),
        unresolved_centers=tuple(item.center for item in unresolved),
        issues=tuple(_issue(item, decisions) for item in unresolved),
    )


def _result_is_better(candidate: CoverageResult, current: CoverageResult | None) -> bool:
    if current is None:
        return True
    if len(candidate.staffed_centers) != len(current.staffed_centers):
        return len(candidate.staffed_centers) > len(current.staffed_centers)
    candidate_overrides = sum(item.preference == "never" for item in candidate.decisions)
    current_overrides = sum(item.preference == "never" for item in current.decisions)
    if candidate_overrides != current_overrides:
        return candidate_overrides < current_overrides
    candidate_cost = sum(item.rank_cost for item in candidate.decisions)
    current_cost = sum(item.rank_cost for item in current.decisions)
    if candidate_cost != current_cost:
        return candidate_cost < current_cost
    candidate_key = tuple((item.center.lower(), item.person.lower()) for item in candidate.decisions)
    current_key = tuple((item.center.lower(), item.person.lower()) for item in current.decisions)
    return candidate_key < current_key


def _crew_decisions(option: CrewOption) -> tuple[AssignmentDecision, ...]:
    return tuple(_decision(member) for member in option.members)


def solve_minimum_coverage(
    requirements: Sequence[CenterRequirement],
) -> CoverageResult:
    normalized = tuple(sorted(requirements, key=lambda item: item.center.lower()))
    singles = tuple(item for item in normalized if item.remaining_slots == 1)
    coupled = tuple(sorted(
        (item for item in normalized if item.remaining_slots > 1),
        key=lambda item: (len(item.crew_options), item.center.lower()),
    ))
    if any(
        len(option.members) != requirement.remaining_slots
        or option.center != requirement.center
        or len(set(option.people)) != len(option.people)
        or any(member.center != requirement.center for member in option.members)
        for requirement in coupled
        for option in requirement.crew_options
    ):
        raise ValueError("crew options must be complete, unique, and center-scoped")
    all_edges = tuple(
        edge
        for requirement in normalized
        for edge in requirement.candidates
    ) + tuple(
        member
        for requirement in coupled
        for option in requirement.crew_options
        for member in option.members
    )
    if any(edge.level not in {1, 2, 3} for edge in all_edges):
        raise ValueError("candidate and crew member levels must be 1, 2, or 3")

    best: CoverageResult | None = None
    single_candidate_people = frozenset(
        edge.person for requirement in singles for edge in requirement.candidates
    )
    single_match_cache: dict[frozenset[str], tuple[AssignmentDecision, ...]] = {}
    seen_prefix: dict[
        tuple[int, frozenset[str]],
        tuple[int, int, int, tuple[tuple[str, str], ...]],
    ] = {}

    def visit(
        index: int,
        used_people: frozenset[str],
        decisions: tuple[AssignmentDecision, ...],
    ) -> None:
        nonlocal best
        staffed_coupled = len({item.center for item in decisions})
        prefix_score = (
            -staffed_coupled,
            sum(item.preference == "never" for item in decisions),
            sum(item.rank_cost for item in decisions),
            tuple(sorted(
                (item.center.lower(), item.person.lower())
                for item in decisions
            )),
        )
        state = (index, used_people)
        previous_prefix = seen_prefix.get(state)
        if previous_prefix is not None and previous_prefix <= prefix_score:
            return
        seen_prefix[state] = prefix_score
        optimistic = (
            sum(item.remaining_slots == 0 for item in normalized)
            + staffed_coupled
            + (len(coupled) - index)
            + len(singles)
        )
        if best is not None and optimistic < len(best.staffed_centers):
            return
        if index == len(coupled):
            unavailable_singles = used_people & single_candidate_people
            matched = single_match_cache.get(unavailable_singles)
            if matched is None:
                matched = _match_single_requirements(singles, unavailable_singles)
                single_match_cache[unavailable_singles] = matched
            candidate = _assemble_result(normalized, decisions + matched)
            if _result_is_better(candidate, best):
                best = candidate
            return

        requirement = coupled[index]
        ordered_options = sorted(
            requirement.crew_options,
            key=lambda option: (
                sum(member.preference == "never" for member in option.members),
                sum(member.rank_cost for member in option.members),
                tuple(name.lower() for name in option.people),
            ),
        )
        for option in ordered_options:
            option_people = frozenset(option.people)
            if option_people & used_people:
                continue
            visit(
                index + 1,
                used_people | option_people,
                decisions + _crew_decisions(option),
            )
        visit(index + 1, used_people, decisions)

    reserved_people = frozenset(
        person
        for requirement in normalized
        for person in requirement.protected_people
    )
    visit(0, reserved_people, ())
    return best if best is not None else _assemble_result(normalized, ())


@dataclass(frozen=True)
class _CompleteAttempt:
    decisions: tuple[AssignmentDecision, ...]
    placed_people: tuple[str, ...]
    staffed_centers: tuple[str, ...]
    unsatisfied_slots: int
    all_people_placed: bool

    @property
    def complete(self) -> bool:
        return self.all_people_placed and self.unsatisfied_slots == 0


def _complete_flow_attempt(
    *,
    people: Sequence[str],
    centers: Sequence[CompleteCenter],
    candidates: Sequence[CandidateEdge],
) -> _CompleteAttempt:
    """Maximum-cardinality, minimum-cost flow for non-coupled centers."""
    ordered_people = tuple(sorted(people, key=str.lower))
    ordered_centers = tuple(sorted(centers, key=lambda item: item.center.lower()))
    people_set = frozenset(ordered_people)
    center_names = frozenset(item.center for item in ordered_centers)

    # A caller can produce the same safe edge through more than one target
    # path. Keep its cheapest canonical representation.
    best_edge: dict[tuple[str, str], CandidateEdge] = {}
    for edge in sorted(
        candidates,
        key=lambda item: (
            item.person.lower(),
            item.center.lower(),
            item.override_cost,
            max(0, item.rank_cost),
        ),
    ):
        if edge.person not in people_set or edge.center not in center_names:
            continue
        key = (edge.person, edge.center)
        current = best_edge.get(key)
        if current is None or (
            edge.override_cost,
            max(0, edge.rank_cost),
        ) < (
            current.override_cost,
            max(0, current.rank_cost),
        ):
            best_edge[key] = edge
    edges = tuple(best_edge.values())

    source = 0
    person_node = {name: index + 1 for index, name in enumerate(ordered_people)}
    center_offset = 1 + len(ordered_people)
    center_node = {
        item.center: center_offset + index
        for index, item in enumerate(ordered_centers)
    }
    sink = center_offset + len(ordered_centers)
    graph: list[list[_Arc]] = [[] for _ in range(sink + 1)]

    for person in ordered_people:
        _add_arc(graph, source, person_node[person], 1, 0)

    ordinary_bound = (
        1
        + sum(max(0, edge.rank_cost) + edge.override_cost for edge in edges)
        + len(edges)
    )
    tie_width = max(1, len(ordered_people) * max(1, len(ordered_centers)) + 1)
    tie_scale = tie_width * (len(ordered_people) + 1)
    person_rank = {name: index for index, name in enumerate(ordered_people)}
    center_rank = {
        item.center: index for index, item in enumerate(ordered_centers)
    }
    chosen_arcs: list[tuple[int, int, CandidateEdge]] = []
    encoded_costs: list[int] = []
    for edge in edges:
        primary_cost = (
            edge.override_cost * ordinary_bound + max(0, edge.rank_cost)
        )
        tie_cost = (
            center_rank[edge.center] * max(1, len(ordered_people))
            + person_rank[edge.person]
        )
        cost = primary_cost * tie_scale + tie_cost
        start = person_node[edge.person]
        arc_index = _add_arc(graph, start, center_node[edge.center], 1, cost)
        chosen_arcs.append((start, arc_index, edge))
        encoded_costs.append(cost)

    required_reward = (
        1 + sum(abs(cost) for cost in encoded_costs)
    ) * (len(ordered_people) + 1)
    required_arcs: dict[str, tuple[int, int]] = {}
    for center in ordered_centers:
        protected_count = len(center.protected_people)
        remaining_minimum = max(0, center.minimum - protected_count)
        remaining_capacity = center.capacity - protected_count
        node = center_node[center.center]
        arc_index = _add_arc(
            graph,
            node,
            sink,
            remaining_minimum,
            -required_reward,
        )
        required_arcs[center.center] = (arc_index, remaining_minimum)
        _add_arc(
            graph,
            node,
            sink,
            max(0, remaining_capacity - remaining_minimum),
            0,
        )

    node_count = len(graph)
    while True:
        distance: list[int | None] = [None] * node_count
        previous: list[tuple[int, int] | None] = [None] * node_count
        distance[source] = 0
        pending = deque((source,))
        queued = {source}
        while pending:
            node = pending.popleft()
            queued.remove(node)
            node_distance = distance[node]
            if node_distance is None:
                continue
            for arc_index, arc in enumerate(graph[node]):
                candidate = node_distance + arc.cost
                if arc.capacity and (
                    distance[arc.to] is None or candidate < distance[arc.to]
                ):
                    distance[arc.to] = candidate
                    previous[arc.to] = (node, arc_index)
                    if arc.to not in queued:
                        pending.append(arc.to)
                        queued.add(arc.to)
        if previous[sink] is None:
            break
        node = sink
        while node != source:
            prior_node, arc_index = previous[node]  # type: ignore[misc]
            arc = graph[prior_node][arc_index]
            arc.capacity -= 1
            graph[node][arc.reverse].capacity += 1
            node = prior_node

    selected = tuple(sorted(
        (
            _decision(edge)
            for start, arc_index, edge in chosen_arcs
            if graph[start][arc_index].capacity == 0
        ),
        key=lambda item: (item.center.lower(), item.person.lower()),
    ))
    placed = tuple(sorted((item.person for item in selected), key=str.lower))
    assigned_by_center: dict[str, int] = {}
    for decision in selected:
        assigned_by_center[decision.center] = assigned_by_center.get(decision.center, 0) + 1
    staffed = tuple(sorted(
        (
            center.center
            for center in ordered_centers
            if len(center.protected_people) + assigned_by_center.get(center.center, 0)
            >= center.minimum
        ),
        key=str.lower,
    ))
    unsatisfied = sum(
        graph[center_node[center.center]][required_arcs[center.center][0]].capacity
        for center in ordered_centers
    )
    return _CompleteAttempt(
        decisions=selected,
        placed_people=placed,
        staffed_centers=staffed,
        unsatisfied_slots=unsatisfied,
        all_people_placed=len(placed) == len(ordered_people),
    )


def _validate_complete_problem(
    people: Sequence[str],
    centers: Sequence[CompleteCenter],
    candidates: Sequence[CandidateEdge],
) -> None:
    if len(set(people)) != len(people):
        raise ValueError("requested people must be unique")
    center_names = [center.center for center in centers]
    if len(set(center_names)) != len(center_names):
        raise ValueError("complete center names must be unique")
    requested = frozenset(people)
    known_centers = frozenset(center_names)
    protected_seen: set[str] = set()
    for center in centers:
        if center.minimum < 0 or center.capacity < 0:
            raise ValueError("center minimums and capacities must be nonnegative")
        if center.minimum > center.capacity:
            raise ValueError("center minimum cannot exceed capacity")
        if len(set(center.protected_people)) != len(center.protected_people):
            raise ValueError("protected people must be unique within a center")
        if protected_seen.intersection(center.protected_people):
            raise ValueError("a protected person cannot occupy multiple centers")
        protected_seen.update(center.protected_people)
        if len(center.protected_people) > center.capacity:
            raise ValueError("protected people cannot exceed center capacity")
        remaining_minimum = max(0, center.minimum - len(center.protected_people))
        remaining_capacity = center.capacity - len(center.protected_people)
        for option in center.crew_options:
            if option.center != center.center:
                raise ValueError("crew options must be center-scoped")
            if len(set(option.people)) != len(option.people):
                raise ValueError("crew option people must be unique")
            if not remaining_minimum <= len(option.members) <= remaining_capacity:
                raise ValueError("crew option size must satisfy remaining center bounds")
            if set(option.people).intersection(center.protected_people):
                raise ValueError("crew options cannot include protected people")
            for member in option.members:
                if member.center != center.center:
                    raise ValueError("crew members must match their option center")
                if member.person not in requested:
                    raise ValueError("crew option people must be requested")
                if member.level not in {1, 2, 3}:
                    raise ValueError("candidate and crew member levels must be 1, 2, or 3")
    for edge in candidates:
        if edge.person not in requested:
            raise ValueError("candidate people must be requested")
        if edge.center not in known_centers:
            raise ValueError("candidate centers must exist")
        if edge.level not in {1, 2, 3}:
            raise ValueError("candidate and crew member levels must be 1, 2, or 3")


def _complete_attempt_key(attempt: _CompleteAttempt) -> tuple[object, ...]:
    return (
        sum(item.preference == "never" for item in attempt.decisions),
        sum(item.rank_cost for item in attempt.decisions),
        tuple(
            (item.center.lower(), item.person.lower())
            for item in attempt.decisions
        ),
    )


def solve_complete_schedule(
    *,
    people: Sequence[str],
    centers: Sequence[CompleteCenter],
    candidates: Sequence[CandidateEdge],
) -> CompleteScheduleResult:
    """Place every requested person or return a zero-decision failure."""
    _validate_complete_problem(people, centers, candidates)
    ordered_people = tuple(sorted(people, key=str.lower))
    ordered_centers = tuple(sorted(centers, key=lambda item: item.center.lower()))
    coupled = tuple(sorted(
        (center for center in ordered_centers if center.crew_options),
        key=lambda item: (len(item.crew_options), item.center.lower()),
    ))
    ordinary = tuple(center for center in ordered_centers if not center.crew_options)
    coupled_names = frozenset(center.center for center in coupled)
    ordinary_candidates = tuple(
        edge for edge in candidates if edge.center not in coupled_names
    )
    best_complete: _CompleteAttempt | None = None
    best_diagnostic: _CompleteAttempt | None = None

    def consider(
        crew_decisions: tuple[AssignmentDecision, ...],
        chosen_by_center: dict[str, int],
    ) -> None:
        nonlocal best_complete, best_diagnostic
        crew_people = frozenset(item.person for item in crew_decisions)
        remaining_people = tuple(
            person for person in ordered_people if person not in crew_people
        )
        flow = _complete_flow_attempt(
            people=remaining_people,
            centers=ordinary,
            candidates=ordinary_candidates,
        )
        decisions = tuple(sorted(
            crew_decisions + flow.decisions,
            key=lambda item: (item.center.lower(), item.person.lower()),
        ))
        placed = tuple(sorted((item.person for item in decisions), key=str.lower))
        assigned_by_center: dict[str, int] = dict(chosen_by_center)
        for item in flow.decisions:
            assigned_by_center[item.center] = assigned_by_center.get(item.center, 0) + 1
        staffed = tuple(sorted(
            (
                center.center
                for center in ordered_centers
                if len(center.protected_people)
                + assigned_by_center.get(center.center, 0)
                >= center.minimum
            ),
            key=str.lower,
        ))
        unsatisfied = sum(
            max(
                0,
                center.minimum
                - len(center.protected_people)
                - assigned_by_center.get(center.center, 0),
            )
            for center in ordered_centers
        )
        attempt = _CompleteAttempt(
            decisions=decisions,
            placed_people=placed,
            staffed_centers=staffed,
            unsatisfied_slots=unsatisfied,
            all_people_placed=len(placed) == len(ordered_people),
        )
        if attempt.complete and (
            best_complete is None
            or _complete_attempt_key(attempt) < _complete_attempt_key(best_complete)
        ):
            best_complete = attempt
        diagnostic_key = (
            -len(attempt.placed_people),
            attempt.unsatisfied_slots,
            _complete_attempt_key(attempt),
        )
        if best_diagnostic is None or diagnostic_key < (
            -len(best_diagnostic.placed_people),
            best_diagnostic.unsatisfied_slots,
            _complete_attempt_key(best_diagnostic),
        ):
            best_diagnostic = attempt

    def visit(
        index: int,
        used_people: frozenset[str],
        decisions: tuple[AssignmentDecision, ...],
        chosen_by_center: dict[str, int],
    ) -> None:
        if index == len(coupled):
            consider(decisions, chosen_by_center)
            return
        center = coupled[index]
        options = sorted(
            center.crew_options,
            key=lambda option: (
                sum(member.preference == "never" for member in option.members),
                sum(member.rank_cost for member in option.members),
                tuple(name.lower() for name in option.people),
            ),
        )
        for option in options:
            option_people = frozenset(option.people)
            if option_people.intersection(used_people):
                continue
            next_counts = dict(chosen_by_center)
            next_counts[center.center] = len(option.members)
            visit(
                index + 1,
                used_people | option_people,
                decisions + _crew_decisions(option),
                next_counts,
            )
        # The empty branch is a valid complete choice only when protected
        # occupants already cover the minimum, but it remains useful for a
        # deterministic failure diagnostic otherwise.
        visit(index + 1, used_people, decisions, dict(chosen_by_center))

    visit(0, frozenset(), (), {})
    if best_complete is not None:
        return CompleteScheduleResult(
            complete=True,
            decisions=best_complete.decisions,
            placed_people=best_complete.placed_people,
            unplaced_people=(),
            staffed_centers=best_complete.staffed_centers,
            issues=(),
            total_cost=sum(item.rank_cost for item in best_complete.decisions),
        )

    diagnostic = best_diagnostic or _CompleteAttempt((), (), (), 0, False)
    placed_set = frozenset(diagnostic.placed_people)
    unplaced = tuple(
        person for person in ordered_people if person not in placed_set
    )
    candidate_centers: dict[str, set[str]] = {person: set() for person in ordered_people}
    for edge in candidates:
        candidate_centers[edge.person].add(edge.center)
    for center in coupled:
        for option in center.crew_options:
            for member in option.members:
                candidate_centers[member.person].add(center.center)
    issues: list[PlacementIssue] = []
    for person in unplaced:
        centers_for_person = tuple(sorted(candidate_centers[person], key=str.lower))
        if not centers_for_person:
            issues.append(PlacementIssue(
                code="person_no_enabled_qualified_center",
                person=person,
                centers=(),
                message=(
                    f"{person} has no enabled work center they are qualified to run. "
                    "Previous schedule kept."
                ),
            ))
        else:
            issues.append(PlacementIssue(
                code="person_all_qualified_centers_full",
                person=person,
                centers=centers_for_person,
                message=(
                    f"Every enabled work center {person} is qualified for is full. "
                    "Previous schedule kept."
                ),
            ))
    staffed_set = frozenset(diagnostic.staffed_centers)
    for center in ordered_centers:
        if center.center not in staffed_set:
            issues.append(PlacementIssue(
                code=(
                    "no_safe_complete_crew"
                    if center.crew_options
                    else "center_minimum_unmet"
                ),
                centers=(center.center,),
                message=(
                    f"{center.center} cannot be staffed safely to its minimum. "
                    "Previous schedule kept."
                ),
            ))
    return CompleteScheduleResult(
        complete=False,
        decisions=(),
        placed_people=diagnostic.placed_people,
        unplaced_people=unplaced,
        staffed_centers=diagnostic.staffed_centers,
        issues=tuple(issues),
        total_cost=0,
    )
