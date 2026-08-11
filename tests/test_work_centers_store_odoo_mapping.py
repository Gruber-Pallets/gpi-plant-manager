import os

import pytest

from zira_dashboard import db, staffing, work_centers_store

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"), reason="needs Postgres"
)

LOC = next(loc for loc in staffing.LOCATIONS if loc.name == "Repair 1")
REPAIR_2 = next(loc for loc in staffing.LOCATIONS if loc.name == "Repair 2")


@pytest.fixture(autouse=True)
def mapping_cleanup():
    db.execute(
        "UPDATE work_centers SET odoo_work_center_id = NULL, "
        "odoo_work_center_name = NULL WHERE name IN (%s, %s)",
        (LOC.name, REPAIR_2.name),
    )
    work_centers_store._invalidate_caches()
    yield
    db.execute(
        "UPDATE work_centers SET odoo_work_center_id = NULL, "
        "odoo_work_center_name = NULL WHERE name IN (%s, %s)",
        (LOC.name, REPAIR_2.name),
    )
    work_centers_store._invalidate_caches()


def test_mapping_round_trips_in_both_directions():
    work_centers_store.set_odoo_work_center(
        LOC, odoo_id=987_654, odoo_name="Repair #1"
    )
    assert work_centers_store.odoo_work_center_id_for("Repair 1") == 987_654
    assert work_centers_store.app_work_center_name_for_odoo_id(987_654) == "Repair 1"


def test_mapping_can_be_cleared():
    work_centers_store.set_odoo_work_center(
        LOC, odoo_id=987_654, odoo_name="Repair #1"
    )
    work_centers_store.set_odoo_work_center(LOC, odoo_id=None, odoo_name=None)
    assert work_centers_store.odoo_work_center_id_for("Repair 1") is None
    assert work_centers_store.app_work_center_name_for_odoo_id(987_654) is None


def test_batch_replace_allows_two_work_centers_to_swap_targets():
    work_centers_store.set_odoo_work_center(
        LOC, odoo_id=987_654, odoo_name="Repair #1"
    )
    work_centers_store.set_odoo_work_center(
        REPAIR_2, odoo_id=987_655, odoo_name="Repair #2"
    )
    work_centers_store.replace_odoo_work_center_mappings({
        "Repair 1": {"odoo_id": 987_655, "odoo_name": "Repair #2"},
        "Repair 2": {"odoo_id": 987_654, "odoo_name": "Repair #1"},
    })
    assert work_centers_store.odoo_work_center_id_for("Repair 1") == 987_655
    assert work_centers_store.odoo_work_center_id_for("Repair 2") == 987_654
