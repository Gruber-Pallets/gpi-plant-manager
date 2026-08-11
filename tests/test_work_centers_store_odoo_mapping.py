import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Barrier
from uuid import uuid4

import pytest

from zira_dashboard import db, staffing, work_centers_store
from zira_dashboard.routes import settings

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


@pytest.fixture
def absent_known_location(monkeypatch):
    """A disposable configured location whose database row cannot preexist."""
    suffix = uuid4().hex
    location = replace(
        LOC,
        name=f"__mapping-first-save-{suffix}",
        meter_id=f"mapping-first-save-{suffix}",
    )
    monkeypatch.setattr(staffing, "LOCATIONS", (location,))
    monkeypatch.setattr(work_centers_store, "LOCATIONS", (location,))
    try:
        yield location
    finally:
        db.execute("DELETE FROM work_centers WHERE name = %s", (location.name,))
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


def test_mapping_only_partial_update_persists_when_known_work_center_row_is_absent(
    absent_known_location,
):
    """The update shape from a Settings partial post must create its row.

    A mapping-only post has no ordinary work-center fields, so it never calls
    ``save_one`` to create the app work-center row first.
    """
    location = absent_known_location
    field = f"wc__{location.meter_id}__odoo_work_center_id"
    updates = settings._odoo_work_center_updates(
        {field: "987654"},
        [{"id": 987_654, "name": "Repair #1"}],
    )
    assert db.query("SELECT 1 FROM work_centers WHERE name = %s", (location.name,)) == []

    work_centers_store.replace_odoo_work_center_mappings(updates)

    assert db.query(
        "SELECT odoo_work_center_id, odoo_work_center_name, odoo_id "
        "FROM work_centers WHERE name = %s",
        (location.name,),
    ) == [{
        "odoo_work_center_id": 987_654,
        "odoo_work_center_name": "Repair #1",
        "odoo_id": None,
    }]


def test_batch_replace_rejects_live_mapping_claim_without_changing_either_center():
    """The storage boundary, not a caller cache, owns collision detection."""
    work_centers_store.set_odoo_work_center(
        REPAIR_2, odoo_id=987_654, odoo_name="Repair #1"
    )

    with pytest.raises(ValueError, match="Repair 1 and Repair 2 cannot use the same"):
        work_centers_store.replace_odoo_work_center_mappings({
            "Repair 1": {"odoo_id": 987_654, "odoo_name": "Repair #1"},
        })

    assert work_centers_store.odoo_work_center_id_for("Repair 1") is None
    assert work_centers_store.odoo_work_center_id_for("Repair 2") == 987_654


def test_concurrent_mapping_claims_leave_one_winner_and_no_partial_mapping():
    """Independent Settings mapping writes serialize around a shared Odoo ID."""
    barrier = Barrier(2)
    odoo_id = 1_500_000_000 + (uuid4().int % 500_000_000)

    def claim(location):
        barrier.wait()
        try:
            work_centers_store.replace_odoo_work_center_mappings({
                location.name: {"odoo_id": odoo_id, "odoo_name": "Repair #1"},
            })
        except work_centers_store.OdooWorkCenterMappingConflict:
            return "conflict"
        return "saved"

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(claim, (LOC, REPAIR_2)))

    assert sorted(results) == ["conflict", "saved"]
    assert db.query(
        "SELECT name, odoo_work_center_id, odoo_work_center_name "
        "FROM work_centers WHERE name IN (%s, %s) "
        "AND odoo_work_center_id IS NOT NULL",
        (LOC.name, REPAIR_2.name),
    ) in ([{
        "name": LOC.name,
        "odoo_work_center_id": odoo_id,
        "odoo_work_center_name": "Repair #1",
    }], [{
        "name": REPAIR_2.name,
        "odoo_work_center_id": odoo_id,
        "odoo_work_center_name": "Repair #1",
    }])
