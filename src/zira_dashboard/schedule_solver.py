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
