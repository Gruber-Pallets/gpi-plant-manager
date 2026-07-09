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
