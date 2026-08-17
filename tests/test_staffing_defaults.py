from pathlib import Path

import pytest

from zira_dashboard import staffing, work_centers_store


LOADING_JOCKEYING_SKILLS = (
    "Loading",
    "CPUs/VDOs",
    "Trailer Jockeying",
)


def test_loading_jockeying_defaults_to_loading_cpus_and_trailer_jockeying():
    loc = next(loc for loc in staffing.LOCATIONS if loc.name == "Loading/Jockeying")

    assert staffing.required_skills_for(loc) == LOADING_JOCKEYING_SKILLS


@pytest.mark.parametrize(
    "stored_required",
    [
        ["Heat Treat"],
        ["Forklift: Load/Jockey"],
        ["Heat Treat", "Loading"],
    ],
)
def test_loading_jockeying_effective_skills_ignore_stale_saved_required_skills(
    stored_required,
):
    loc = next(loc for loc in staffing.LOCATIONS if loc.name == "Loading/Jockeying")
    rec = {"min_ops": loc.min_ops, "max_ops": loc.max_ops}

    effective = work_centers_store._shape_effective(loc, rec, stored_required, [])

    assert effective["required_skills"] == list(LOADING_JOCKEYING_SKILLS)


CDL_AUTOMATICS = "CDL (Automatics) Certified"
CDL_MANUALS = "CDL (Manuals) Certified"


def test_truck_driver_always_requires_cdl_automatics():
    loc = next(loc for loc in staffing.LOCATIONS if loc.name == "Truck Driver")

    assert staffing.required_skills_for(loc) == (CDL_AUTOMATICS,)


@pytest.mark.parametrize(
    "stored_required",
    [
        [],
        ["Heat Treat"],
        ["Repair", "Trim Saw"],
    ],
)
def test_truck_driver_effective_skills_ignore_saved_required_skills(stored_required):
    loc = next(loc for loc in staffing.LOCATIONS if loc.name == "Truck Driver")
    rec = {"min_ops": 4, "max_ops": 5}

    effective = work_centers_store._shape_effective(loc, rec, stored_required, [])

    assert effective["required_skills"] == [CDL_AUTOMATICS]


def test_cdl_level_counts_either_automatics_or_manuals_cert():
    auto_only = staffing.Person("Caleb", skills={CDL_AUTOMATICS: 3})
    manuals_only = staffing.Person("Pat", skills={CDL_MANUALS: 3})
    neither = staffing.Person("Adrian", skills={"Repair": 3})

    assert auto_only.level(CDL_AUTOMATICS) == 3
    assert auto_only.level(CDL_MANUALS) == 3
    assert manuals_only.level(CDL_AUTOMATICS) == 3
    assert manuals_only.level(CDL_MANUALS) == 3
    assert neither.level(CDL_AUTOMATICS) == 0
    assert neither.level(CDL_MANUALS) == 0


def test_settings_shows_truck_driver_cdl_rule_instead_of_skill_picker():
    html = Path("src/zira_dashboard/templates/settings.html").read_text()

    assert 'r.name == "Truck Driver"' in html
    assert "CDL certified" in html
    assert "required_skills_locked" in html or "req-skills-locked" in html


def test_effective_minimum_preserves_saved_zero():
    loc = next(loc for loc in staffing.LOCATIONS if loc.name == "Repair 1")

    effective = work_centers_store._shape_effective(
        loc,
        {"min_ops": 0, "max_ops": loc.max_ops},
        ["Repair"],
        [],
    )

    assert effective["min_ops"] == 0
