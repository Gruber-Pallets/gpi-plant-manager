from collections import defaultdict
from collections.abc import Collection, Mapping, Sequence

from . import schedule_solver, staffing


def _issue(code, message, *, person=None, centers=()):
    return schedule_solver.PlacementIssue(
        code=code, message=message, person=person, centers=tuple(centers)
    )


def _trim_saw_pair_is_safe(levels: Sequence[int]) -> bool:
    if len(levels) != 2:
        return False
    low, high = sorted(levels)
    return high >= 3 if low <= 1 else low >= 2


def validate_current_assignments(
    *,
    roster: Sequence[staffing.Person],
    assignments: Mapping[str, Sequence[str]],
    enabled_centers: Collection[str],
    locations: Sequence[staffing.Location],
    minimums: Mapping[str, int],
    capacities: Mapping[str, int | None],
    required_skills: Mapping[str, Sequence[str]],
    full_day_off_names: Collection[str],
    trim_saw_centers: Collection[str],
    training_trainees_by_center: Mapping[str, Collection[str]],
    exact_defaults: Mapping[str, Sequence[str]],
    group_defaults: Mapping[str, Sequence[str]],
    user_group_centers: Mapping[str, Sequence[str]],
) -> tuple[schedule_solver.PlacementIssue, ...]:
    locations_by_name = {location.name: location for location in locations}
    enabled = tuple(
        sorted(set(enabled_centers) & set(locations_by_name), key=str.lower)
    )
    by_name = {person.name: person for person in roster}
    off = set(full_day_off_names)
    visible = {
        center: tuple(str(name) for name in names or ())
        for center, names in assignments.items()
        if center in locations_by_name
    }
    assigned_centers = defaultdict(set)
    for center, names in visible.items():
        for name in names:
            assigned_centers[name].add(center)

    issues = []
    for name, centers in sorted(assigned_centers.items(), key=lambda item: item[0].lower()):
        if len(centers) > 1:
            ordered = tuple(sorted(centers, key=str.lower))
            issues.append(
                _issue(
                    "person_assigned_multiple_centers",
                    f"{name} is assigned to multiple work centers ({', '.join(ordered)}).",
                    person=name,
                    centers=ordered,
                )
            )

    for center in enabled:
        names = visible.get(center, ())
        required = tuple(required_skills.get(center, (locations_by_name[center].skill,)))
        capacity = capacities.get(center)
        minimum = max(0, int(minimums.get(center, 0)))
        if capacity is not None and minimum > capacity:
            issues.append(
                _issue(
                    "invalid_center_configuration",
                    f"{center} has a minimum of {minimum} but a maximum of {capacity}.",
                    centers=(center,),
                )
            )
        if capacity is not None and len(names) > capacity:
            issues.append(
                _issue(
                    "center_capacity_exceeded",
                    f"{center} exceeds its maximum capacity of {capacity}.",
                    centers=(center,),
                )
            )

        trainees = set(training_trainees_by_center.get(center, ()))
        green_present = any(
            (person := by_name.get(name)) is not None
            and person.active
            and not person.reserve
            and name not in off
            and all(person.level(skill) >= 3 for skill in required)
            for name in names
        )
        safe_names = set()
        for name in names:
            person = by_name.get(name)
            if person is None or not person.active or person.reserve or name in off:
                issues.append(
                    _issue(
                        "assignment_unavailable",
                        f"{name} is unavailable for {center}.",
                        person=name,
                        centers=(center,),
                    )
                )
                continue
            if name in trainees and green_present:
                safe_names.add(name)
                continue
            if not all(person.level(skill) >= 1 for skill in required):
                issues.append(
                    _issue(
                        "assignment_unqualified",
                        f"{name} is not qualified for {center}.",
                        person=name,
                        centers=(center,),
                    )
                )
                continue
            safe_names.add(name)

        missing_trainees = trainees - set(names)
        if missing_trainees or (trainees & set(names) and not green_present):
            details = sorted(missing_trainees or trainees & set(names), key=str.lower)
            issues.append(
                _issue(
                    "training_partner_missing",
                    f"{center} needs its training crew with a level 3 partner ({', '.join(details)}).",
                    centers=(center,),
                )
            )
        if center in trim_saw_centers:
            levels = [
                by_name[name].level(required[0]) if name in by_name else 0 for name in names
            ]
            if not _trim_saw_pair_is_safe(levels):
                issues.append(
                    _issue(
                        "no_safe_complete_crew",
                        f"{center} cannot form a safe complete crew.",
                        centers=(center,),
                    )
                )
        if len(safe_names) < minimum:
            issues.append(
                _issue(
                    "center_minimum_unmet",
                    f"{center} is below its safe minimum: {len(safe_names)} qualified and present, "
                    f"minimum {minimum}.",
                    centers=(center,),
                )
            )

    assigned_names = set(assigned_centers)
    for person in sorted(roster, key=lambda item: item.name.lower()):
        if (
            person.active
            and not person.reserve
            and person.name not in off
            and person.name not in assigned_names
        ):
            issues.append(
                _issue(
                    "person_unplaced",
                    f"{person.name} is not assigned to a work center.",
                    person=person.name,
                )
            )

    defaults_by_person = defaultdict(list)
    for center, names in exact_defaults.items():
        for name in names:
            defaults_by_person[str(name)].append(("exact", center))
    for group, names in group_defaults.items():
        for name in names:
            defaults_by_person[str(name)].append(("group", group))
    for name, targets in sorted(defaults_by_person.items(), key=lambda item: item[0].lower()):
        if name not in by_name or not by_name[name].active or by_name[name].reserve or name in off:
            continue
        unique = tuple(sorted(set(targets), key=lambda item: (item[0], item[1].lower())))
        if len(unique) > 1:
            issues.append(
                _issue(
                    "default_target_conflict",
                    f"{name} has conflicting default targets.",
                    person=name,
                    centers=tuple(target for _kind, target in unique),
                )
            )
            continue
        kind, target = unique[0]
        actual = assigned_centers.get(name, set())
        if kind == "exact":
            if target not in enabled or target not in locations_by_name:
                issues.append(
                    _issue(
                        "exact_default_center_disabled",
                        f"{name}'s default work center {target} is not enabled.",
                        person=name,
                        centers=(target,),
                    )
                )
                continue
            target_required = tuple(
                required_skills.get(target, (locations_by_name[target].skill,))
            )
            if not all(by_name[name].level(skill) >= 1 for skill in target_required):
                issues.append(
                    _issue(
                        "exact_default_unqualified",
                        f"{name} is not qualified for default work center {target}.",
                        person=name,
                        centers=(target,),
                    )
                )
                continue
            if actual != {target}:
                issues.append(
                    _issue(
                        "exact_default_violation",
                        f"{name} is not at default center {target}.",
                        person=name,
                        centers=(target,),
                    )
                )
        if kind == "group":
            group_centers = set(user_group_centers.get(target, ())) & set(enabled)
            if not group_centers:
                issues.append(
                    _issue(
                        "group_default_no_enabled_member",
                        f"{name}'s default group {target} has no enabled work center.",
                        person=name,
                    )
                )
                continue
            qualified_centers = {
                center
                for center in group_centers
                if all(
                    by_name[name].level(skill) >= 1
                    for skill in required_skills.get(
                        center, (locations_by_name[center].skill,)
                    )
                )
            }
            if not qualified_centers:
                issues.append(
                    _issue(
                        "group_default_no_qualified_member",
                        f"{name} is not qualified for any enabled work center in default group {target}.",
                        person=name,
                        centers=tuple(sorted(group_centers, key=str.lower)),
                    )
                )
                continue
            if not actual & qualified_centers:
                issues.append(
                    _issue(
                        "group_default_violation",
                        f"{name} is outside default group {target}.",
                        person=name,
                        centers=tuple(sorted(qualified_centers, key=str.lower)),
                    )
                )

    return tuple(
        sorted(
            issues,
            key=lambda issue: (
                issue.code,
                tuple(name.lower() for name in issue.centers),
                (issue.person or "").lower(),
            ),
        )
    )
