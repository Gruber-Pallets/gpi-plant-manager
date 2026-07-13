#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from datetime import date

from zira_dashboard import rotation_store, rotation_suggestions, scheduler_time_off, staffing
from zira_dashboard.routes import staffing as staffing_route


def _read_enabled_auto_work_centers(day: date) -> set[str]:
    """Resolve Auto centers without running the normal first-use settings write."""
    saved = staffing_route.app_settings.get_setting(staffing_route.AUTO_SCHEDULE_WC_SETTING)
    if isinstance(saved, list):
        return set(staffing_route._ordered_work_center_names(saved))
    return set(staffing_route._recently_used_work_centers(day))


def replay(day: date) -> dict[str, object]:
    roster = staffing.load_roster()
    schedule = staffing.load_schedule(day)
    time_off = scheduler_time_off.time_off_entries_for_day(day)
    available = staffing_route._roster_minus_full_day_off(roster, time_off)
    enabled = _read_enabled_auto_work_centers(day)
    groups, skills = staffing_route._auto_group_maps(enabled)
    preferences = rotation_store.load_preferences_by_name()
    history = rotation_suggestions._load_recycled_history(
        day,
        group_locations=staffing_route._auto_history_group_locations(),
    )
    blocks = staffing_route._block_effects_for_day(
        day,
        time_off,
        assignments=schedule.assignments,
        assignment_sources=schedule.assignment_sources,
    )
    locks = staffing_route._protected_locks(
        schedule.assignment_sources,
        schedule.assignments,
        allowed_centers=enabled,
        strict_default_reads=True,
    )
    result = rotation_suggestions.suggest_recycled_assignments(
        day=day,
        mode=schedule.rotation_mode,
        roster=available,
        preferences=preferences,
        base_assignments=schedule.assignments,
        group_locations=groups,
        group_required_skills=skills,
        history=history,
        locked_assignments=locks,
        block_effects=blocks,
        center_minimums={
            loc.name: staffing_route._effective_minimum(loc)
            for loc in staffing.LOCATIONS
            if loc.name in enabled
        },
        center_capacities=staffing_route._configured_center_capacities(enabled),
        runnable_centers=enabled,
    )
    return {
        "day": day.isoformat(),
        "saved_assignments": schedule.assignments,
        "suggested_assignments": result.assignments,
        "staffed_centers": result.staffed_centers,
        "unresolved_centers": result.unresolved_centers,
        "issues": [issue.to_dict() for issue in result.issues],
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Read-only comparison of saved schedules with the global solver."
    )
    parser.add_argument("day", nargs="+", type=date.fromisoformat)
    args = parser.parse_args()
    print(json.dumps([replay(day) for day in args.day], indent=2, default=list))


if __name__ == "__main__":
    main()
