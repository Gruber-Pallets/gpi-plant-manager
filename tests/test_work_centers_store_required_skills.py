"""_effective_uncached's required-skill logic:

(a) WC row absent in DB → fall back to LOCATIONS.skill (bootstrap).
(b) WC row present, no req-skill rows → return [] (user cleared).
(c) WC row present, with req-skill rows → return the DB list.

DB-backed; skipped when DATABASE_URL is unset.
"""

import os
import pytest

from zira_dashboard import db, staffing, work_centers_store


pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="needs Postgres",
)


# Pick a Location we won't conflict with other tests. "Repair 1" has
# loc.skill == "Repair". We clean its work_centers row before and after
# each test so we control the (row exists / req-skill rows) state.
TEST_LOC = next(loc for loc in staffing.LOCATIONS if loc.name == "Repair 1")
assert TEST_LOC.skill == "Repair"


@pytest.fixture(autouse=True)
def _clean_wc_row():
    db.execute(
        "DELETE FROM work_center_required_skills WHERE wc_id = "
        "(SELECT id FROM work_centers WHERE name = %s)",
        (TEST_LOC.name,),
    )
    db.execute("DELETE FROM work_centers WHERE name = %s", (TEST_LOC.name,))
    work_centers_store._invalidate_caches()
    yield
    db.execute(
        "DELETE FROM work_center_required_skills WHERE wc_id = "
        "(SELECT id FROM work_centers WHERE name = %s)",
        (TEST_LOC.name,),
    )
    db.execute("DELETE FROM work_centers WHERE name = %s", (TEST_LOC.name,))
    work_centers_store._invalidate_caches()


def test_no_row_falls_back_to_locations_skill():
    """Bootstrap state: no work_centers row at all → use loc.skill."""
    eff = work_centers_store._effective_uncached(TEST_LOC)
    assert eff["required_skills"] == ["Repair"]


def test_row_present_with_no_required_skill_rows_returns_empty():
    """User has saved Settings for this WC (row exists) and cleared
    the required-skill checkboxes (no req rows). Treat as explicit
    empty — do NOT fall back to loc.skill."""
    # save_one ensures the row exists; passing an empty list clears
    # any req rows.
    work_centers_store.save_one(TEST_LOC, {"required_skills": []})
    work_centers_store._invalidate_caches()
    eff = work_centers_store._effective_uncached(TEST_LOC)
    assert eff["required_skills"] == []


def test_row_present_with_required_skill_rows_returns_db_list():
    """The normal case — DB has required-skill rows, return them."""
    work_centers_store.save_one(TEST_LOC, {"required_skills": ["Repair"]})
    work_centers_store._invalidate_caches()
    eff = work_centers_store._effective_uncached(TEST_LOC)
    assert eff["required_skills"] == ["Repair"]
