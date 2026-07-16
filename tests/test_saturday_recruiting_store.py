"""Postgres-backed lifecycle contracts for Saturday recruiting."""

from __future__ import annotations

import os
from datetime import date, datetime, time, timedelta

import pytest

from zira_dashboard import db, saturday_recruiting_store as store
from zira_dashboard.shift_config import SITE_TZ


SATURDAY = date(2026, 7, 25)
NOW = datetime(2026, 7, 20, 12, 0, tzinfo=SITE_TZ)
DEADLINE = datetime(2026, 7, 24, 7, 0, tzinfo=SITE_TZ)

pytestmark = pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="needs Postgres")

WC_IDS = (910101, 910102, 910103)
SKILL_IDS = (910101, 910102)
PERSON_ID = 910101


@pytest.fixture(autouse=True)
def _clean_recruiting_data():
    db.bootstrap_schema()
    db.execute("DELETE FROM saturday_recruitments WHERE day = %s", (SATURDAY,))
    db.execute("DELETE FROM schedule_assignments WHERE day = %s", (SATURDAY,))
    db.execute("DELETE FROM schedules WHERE day = %s", (SATURDAY,))
    db.execute("DELETE FROM work_center_required_skills WHERE wc_id = ANY(%s)", (list(WC_IDS),))
    db.execute("DELETE FROM person_skills WHERE person_id = %s", (PERSON_ID,))
    db.execute("DELETE FROM skills WHERE id = ANY(%s)", (list(SKILL_IDS),))
    db.execute("DELETE FROM work_centers WHERE id = ANY(%s)", (list(WC_IDS),))
    db.execute("DELETE FROM people WHERE id = %s", (PERSON_ID,))
    db.execute(
        "INSERT INTO work_centers (id, name, category) VALUES "
        "(910101, 'Saturday Test Repair', 'Repair'), "
        "(910102, 'Saturday Test Dismantle', 'Dismantler'), "
        "(910103, 'Saturday Test Unqualified', 'Other')"
    )
    db.execute(
        "INSERT INTO skills (id, name, skill_type) VALUES "
        "(910101, 'Saturday Test Repair skill', 'Certification'), "
        "(910102, 'Saturday Test Dismantle skill', 'Certification')"
    )
    db.execute(
        "INSERT INTO work_center_required_skills (wc_id, skill_id) VALUES "
        "(910101, 910101), (910102, 910102)"
    )
    db.execute("INSERT INTO people (id, name) VALUES (910101, 'Saturday Test Volunteer')")
    yield
    db.execute("DELETE FROM saturday_recruitments WHERE day = %s", (SATURDAY,))
    db.execute("DELETE FROM schedule_assignments WHERE day = %s", (SATURDAY,))
    db.execute("DELETE FROM schedules WHERE day = %s", (SATURDAY,))
    db.execute("DELETE FROM work_center_required_skills WHERE wc_id = ANY(%s)", (list(WC_IDS),))
    db.execute("DELETE FROM person_skills WHERE person_id = %s", (PERSON_ID,))
    db.execute("DELETE FROM skills WHERE id = ANY(%s)", (list(SKILL_IDS),))
    db.execute("DELETE FROM work_centers WHERE id = ANY(%s)", (list(WC_IDS),))
    db.execute("DELETE FROM people WHERE id = %s", (PERSON_ID,))


def _activate(**changes):
    values = {
        "day": SATURDAY,
        "shift_start": time(6, 0),
        "shift_end": time(12, 0),
        "response_deadline": DEADLINE,
        "requested_counts": {910101: 3, 910102: 2},
        "actor": "manager@gruberpallets.com",
        "now": NOW,
    }
    values.update(changes)
    return store.activate(**values)


def test_available_positions_returns_only_work_centers_with_required_skills():
    assert store.available_positions() == (
        store.AvailablePosition(910101, "Saturday Test Repair", ("Saturday Test Repair skill",)),
        store.AvailablePosition(910102, "Saturday Test Dismantle", ("Saturday Test Dismantle skill",)),
    )


def test_activate_reads_bundle_and_closes_when_deadline_is_due():
    bundle = _activate()
    assert bundle.recruitment.status == "recruiting"
    assert {opening.wc_id: opening.requested_count for opening in bundle.openings} == {
        910101: 3,
        910102: 2,
    }
    assert store.get(SATURDAY) == bundle
    assert store.close_due(DEADLINE) == 1
    assert store.get(SATURDAY).recruitment.status == "closed"


def test_activate_rejects_non_saturday():
    with pytest.raises(store.SaturdayRecruitingError):
        _activate(day=SATURDAY - timedelta(days=1))


def test_activate_rejects_elapsed_deadline():
    with pytest.raises(store.LifecycleConflict):
        _activate(response_deadline=NOW)


def test_activate_rejects_empty_requested_counts():
    with pytest.raises(store.LifecycleConflict):
        _activate(requested_counts={})


def test_activate_rejects_work_center_without_required_skills():
    with pytest.raises(store.LifecycleConflict):
        _activate(requested_counts={910103: 1})


def test_activate_rejects_existing_draft_assignments():
    db.execute("INSERT INTO schedules (day) VALUES (%s)", (SATURDAY,))
    db.execute(
        "INSERT INTO schedule_assignments (day, wc_id, person_id) VALUES (%s, 910101, 910101)",
        (SATURDAY,),
    )
    with pytest.raises(
        store.LifecycleConflict,
        match="Clear existing Saturday assignments before activating recruiting.",
    ):
        _activate()


def test_activate_rejects_already_published_schedule():
    db.execute("INSERT INTO schedules (day, published) VALUES (%s, TRUE)", (SATURDAY,))
    with pytest.raises(store.LifecycleConflict):
        _activate()


def test_repeated_identical_activation_is_idempotent():
    first = _activate()
    activated_at = db.query(
        "SELECT activated_at FROM saturday_recruitments WHERE day = %s", (SATURDAY,)
    )[0]["activated_at"]
    second = _activate(now=NOW + timedelta(hours=1))
    assert second == first
    assert db.query(
        "SELECT activated_at FROM saturday_recruitments WHERE day = %s", (SATURDAY,)
    )[0]["activated_at"] == activated_at


def test_reactivation_with_different_payload_is_rejected():
    _activate()
    with pytest.raises(store.LifecycleConflict):
        _activate(requested_counts={910101: 4, 910102: 2})


def test_update_rejects_count_below_current_coverage():
    _activate(requested_counts={910101: 2})
    db.execute(
        "INSERT INTO saturday_work_responses "
        "(day, person_id, status, availability_start, availability_end, eligible_wc_ids) "
        "VALUES (%s, 910101, 'committed', '06:00', '12:00', '[910101]'::jsonb)",
        (SATURDAY,),
    )
    with pytest.raises(store.LifecycleConflict):
        store.update_openings(SATURDAY, {910101: 0}, time(6, 0), time(12, 0), None, NOW)


def test_update_rejects_shift_hour_change_after_first_commitment():
    _activate(requested_counts={910101: 2})
    db.execute(
        "INSERT INTO saturday_work_responses "
        "(day, person_id, status, availability_start, availability_end, eligible_wc_ids) "
        "VALUES (%s, 910101, 'committed', '06:00', '12:00', '[910101]'::jsonb)",
        (SATURDAY,),
    )
    with pytest.raises(store.LifecycleConflict):
        store.update_openings(SATURDAY, {910101: 2}, time(6, 30), time(12, 0), None, NOW)


def test_closed_recruitment_can_only_reduce_unfilled_count():
    _activate(requested_counts={910101: 3})
    assert store.close_due(DEADLINE) == 1
    reduced = store.update_openings(SATURDAY, {910101: 2}, time(6, 0), time(12, 0), None, NOW)
    assert reduced.openings[0].requested_count == 2
    with pytest.raises(store.LifecycleConflict):
        store.update_openings(SATURDAY, {910101: 3}, time(6, 0), time(12, 0), None, NOW)
    with pytest.raises(store.LifecycleConflict):
        store.update_openings(SATURDAY, {910101: 2, 910102: 1}, time(6, 0), time(12, 0), None, NOW)
