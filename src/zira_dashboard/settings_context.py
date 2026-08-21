"""Pure builders for the context rendered by the settings page."""

from __future__ import annotations


def schedule_context(schedule, weekday_names: list[str]) -> dict:
    return {
        "shift_start": f"{schedule.shift_start.hour:02d}:{schedule.shift_start.minute:02d}",
        "shift_end": f"{schedule.shift_end.hour:02d}:{schedule.shift_end.minute:02d}",
        "work_weekdays": sorted(schedule.work_weekdays),
        "weekday_names": weekday_names,
        "breaks": [
            {
                "start": f"{item.start.hour:02d}:{item.start.minute:02d}",
                "end": f"{item.end.hour:02d}:{item.end.minute:02d}",
                "name": item.name,
            }
            for item in schedule.breaks
        ],
    }


def work_center_rows(locations, people, effective_for) -> list[dict]:
    """Rows for the work-center table, including each row's default-people pool.

    The pool is the set of people the picker renders a checkbox for, and the
    Work Centers form rewrites the stored defaults from exactly those
    checkboxes (see ``work_centers_store.replace_default_targets``). So every
    already-saved default MUST appear in the pool — an inactive or off-roster
    default with no checkbox would be deleted by the next save of an unrelated
    field, while the picker summary still showed the name. Such entries are
    flagged ``preserved`` so the template can mark them as no longer normally
    selectable.
    """
    rows = []
    for location in locations:
        effective = effective_for(location)
        max_ops = effective["max_ops"]
        required_skills = effective["required_skills"]
        selected = set(effective["default_people"])
        pool = []
        listed = set()
        for person in people:
            active = bool(getattr(person, "active", True))
            if not active and person.name not in selected:
                continue
            if required_skills:
                level = min((person.level(skill) for skill in required_skills), default=0)
            else:
                level = 2
            pool.append(
                {
                    "name": person.name,
                    "level": level,
                    "reserve": person.reserve,
                    "preserved": not active,
                }
            )
            listed.add(person.name)
        for name in effective["default_people"]:
            # Saved defaults who aren't on the roster at all: hidden by the
            # Roster Filter, or renamed/removed in Odoo.
            if name not in listed:
                pool.append(
                    {"name": name, "level": 0, "reserve": False, "preserved": True}
                )
                listed.add(name)
        pool.sort(key=lambda row: (row["reserve"], -row["level"], row["name"].lower()))
        rows.append(
            {
                "key": location.meter_id or f"name:{location.name}",
                "name": location.name,
                "bay": location.bay,
                "required_skills": required_skills,
                "min_ops": effective["min_ops"],
                "max_ops": max_ops if max_ops is not None else "",
                "goal": effective["goal_per_day"],
                "note": effective["note"],
                "groups": effective["groups"],
                "department": effective["department"],
                "default_people": effective["default_people"],
                "default_pool": pool,
                "odoo_work_center_id": effective.get("odoo_work_center_id"),
                "odoo_work_center_name": effective.get("odoo_work_center_name"),
                "odoo_mapping_missing": effective.get("odoo_work_center_id") is None,
            }
        )
    return rows


def group_summary(
    kind, *, all_names, members, auto_goal, override_goal, effective_goal
) -> list[dict]:
    rows = []
    for name in all_names(kind):
        group_members = members(kind, name)
        auto = auto_goal(kind, name)
        override = override_goal(kind, name)
        rows.append(
            {
                "name": name,
                "count": len(group_members),
                "auto": auto,
                "override": "" if override is None else override,
                "effective": effective_goal(kind, name),
            }
        )
    return rows


def with_group_default_context(
    rows,
    active_people,
    *,
    members_for,
    required_skills_for,
    defaults_for,
    conflicts,
) -> list[dict]:
    """Add eligible default-person choices to user-managed group rows.

    Already-selected people are always in the pool, even when they've stopped
    qualifying (gone reserve, dropped to level 0, gone inactive, left the
    roster) — the group form rewrites the stored defaults from the rendered
    checkboxes, so a selected person without one is silently deleted by the
    next save. Those entries carry ``preserved`` and no eligible centers.
    """
    result: list[dict] = []
    for row in rows:
        members = members_for("group", row["name"])
        selected = list(defaults_for(row["name"]))
        eligible_people: list[tuple[object, list[str]]] = []
        for person in active_people:
            if person.reserve:
                continue
            eligible_centers = [
                member.name
                for member in members
                if all(
                    person.level(skill) >= 1
                    for skill in required_skills_for(member)
                )
            ]
            if eligible_centers:
                eligible_people.append((person, eligible_centers))
        eligible_people.sort(key=lambda item: item[0].name.lower())
        pool = [
            {
                "name": person.name,
                "eligible_centers": tuple(eligible_centers),
            }
            for person, eligible_centers in eligible_people
        ]
        listed = {entry["name"] for entry in pool}
        for name in selected:
            if name not in listed:
                pool.append(
                    {"name": name, "eligible_centers": (), "preserved": True}
                )
                listed.add(name)
        result.append(
            {
                **row,
                "default_people": selected,
                "default_pool": pool,
                "default_conflicts": {
                    name: conflicts[name]
                    for name in selected
                    if name in conflicts
                },
            }
        )
    return result


def work_schedule_context(overrides, hours_display) -> list[dict]:
    return [
        {
            "resource_calendar_id": override.resource_calendar_id,
            "name": override.name or f"Schedule {override.resource_calendar_id}",
            "hours_display": hours_display(override.work_hours),
            "in_before_min": override.rounding.in_before_min,
            "in_after_min": override.rounding.in_after_min,
            "out_before_min": override.rounding.out_before_min,
            "out_after_min": override.rounding.out_after_min,
        }
        for override in overrides
    ]


def rounding_system_context(systems) -> list[dict]:
    return [
        {
            "id": system.id,
            "name": system.name,
            "in_before_min": system.rounding.in_before_min,
            "in_after_min": system.rounding.in_after_min,
            "out_before_min": system.rounding.out_before_min,
            "out_after_min": system.rounding.out_after_min,
        }
        for system in systems
    ]


def department_rounding_context(departments, department_map) -> list[dict]:
    return [
        {"department": department, "system_id": department_map.get(department)}
        for department in departments
    ]


def saturday_schedule_context(schedule) -> dict:
    return {
        "shift_start": f"{schedule.shift_start.hour:02d}:{schedule.shift_start.minute:02d}",
        "shift_end": f"{schedule.shift_end.hour:02d}:{schedule.shift_end.minute:02d}",
        "breaks": [
            {
                "start": f"{item.start.hour:02d}:{item.start.minute:02d}",
                "end": f"{item.end.hour:02d}:{item.end.minute:02d}",
                "name": item.name,
            }
            for item in schedule.breaks
        ],
    }


def auto_lunch_context(settings) -> dict:
    return {
        "mode": (
            "off"
            if not settings.enabled
            else ("observe" if settings.observe_only else "live")
        ),
        "flex_after_hours": settings.flex_after_hours,
        "flex_minutes": settings.flex_minutes,
    }


def _auto_lunch_mode(enabled, observe_only) -> str:
    if not enabled:
        return "Off"
    return "Observe only" if observe_only else "Live"


def _auto_lunch_value_label(row: dict, prefix: str) -> str:
    mode = _auto_lunch_mode(row[f"{prefix}_enabled"], row[f"{prefix}_observe_only"])
    hours = float(row[f"{prefix}_flex_after_hours"])
    hours_label = f"{hours:g} hours"
    return f"{mode} · {hours_label} · {int(row[f'{prefix}_flex_minutes'])} minutes"


def auto_lunch_history_context(events: list[dict]) -> list[dict]:
    from . import shift_config
    shaped = []
    for row in events:
        source = row.get("source")
        baseline = source == "baseline"
        if baseline:
            actor_label = "Monitoring started"
        elif source == "external":
            actor_label = "Outside app / detected automatically"
        else:
            actor_label = row.get("actor_name") or row.get("actor_upn") or "Unknown manager"
        changed_at = row["changed_at"].astimezone(shift_config.SITE_TZ)
        shaped.append({
            "time_label": changed_at.strftime("%-m/%-d/%Y %-I:%M %p"),
            "before_label": None if baseline else _auto_lunch_value_label(row, "before"),
            "after_label": _auto_lunch_value_label(row, "after"),
            "actor_label": actor_label,
            "is_baseline": baseline,
        })
    return shaped
