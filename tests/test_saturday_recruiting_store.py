"""Postgres-backed lifecycle contracts for Saturday recruiting."""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, time, timedelta
from threading import Barrier

import pytest

from zira_dashboard import db, saturday_recruiting_store as store
from zira_dashboard.shift_config import SITE_TZ


SATURDAY = date(2026, 7, 25)
NEXT_SATURDAY = date(2026, 8, 1)
NOW = datetime(2026, 7, 20, 12, 0, tzinfo=SITE_TZ)
DEADLINE = datetime(2026, 7, 24, 7, 0, tzinfo=SITE_TZ)
NEXT_DEADLINE = datetime(2026, 7, 31, 7, 0, tzinfo=SITE_TZ)

pytestmark = pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="needs Postgres")

WC_IDS = (910101, 910102, 910103)
SKILL_IDS = (910101, 910102)
PERSON_IDS = (910101, 910102, 910103, 910104)
PERSON_ID = PERSON_IDS[0]
RECRUITING_DAYS = (SATURDAY, NEXT_SATURDAY)


@pytest.fixture(autouse=True)
def _clean_recruiting_data():
    db.bootstrap_schema()
    db.execute("DELETE FROM saturday_recruitments WHERE day = ANY(%s)", (list(RECRUITING_DAYS),))
    db.execute("DELETE FROM schedule_assignments WHERE day = ANY(%s)", (list(RECRUITING_DAYS),))
    db.execute("DELETE FROM schedules WHERE day = ANY(%s)", (list(RECRUITING_DAYS),))
    db.execute("DELETE FROM work_center_required_skills WHERE wc_id = ANY(%s)", (list(WC_IDS),))
    db.execute("DELETE FROM person_skills WHERE person_id = ANY(%s)", (list(PERSON_IDS),))
    db.execute("DELETE FROM time_off_requests WHERE person_odoo_id = ANY(%s)", (list(PERSON_IDS),))
    db.execute("DELETE FROM skills WHERE id = ANY(%s)", (list(SKILL_IDS),))
    db.execute("DELETE FROM work_centers WHERE id = ANY(%s)", (list(WC_IDS),))
    db.execute("DELETE FROM people WHERE id = ANY(%s)", (list(PERSON_IDS),))
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
    db.execute(
        "INSERT INTO people (id, odoo_id, name, wage_type) VALUES "
        "(910101, 910101, 'Saturday Test Volunteer', 'hourly'), "
        "(910102, 910102, 'Saturday Test Repair', 'hourly'), "
        "(910103, 910103, 'Saturday Test Salaried', 'monthly'), "
        "(910104, 910104, 'Saturday Test Unqualified Person', 'hourly')"
    )
    yield
    db.execute("DELETE FROM saturday_recruitments WHERE day = ANY(%s)", (list(RECRUITING_DAYS),))
    db.execute("DELETE FROM schedule_assignments WHERE day = ANY(%s)", (list(RECRUITING_DAYS),))
    db.execute("DELETE FROM schedules WHERE day = ANY(%s)", (list(RECRUITING_DAYS),))
    db.execute("DELETE FROM work_center_required_skills WHERE wc_id = ANY(%s)", (list(WC_IDS),))
    db.execute("DELETE FROM person_skills WHERE person_id = ANY(%s)", (list(PERSON_IDS),))
    db.execute("DELETE FROM time_off_requests WHERE person_odoo_id = ANY(%s)", (list(PERSON_IDS),))
    db.execute("DELETE FROM skills WHERE id = ANY(%s)", (list(SKILL_IDS),))
    db.execute("DELETE FROM work_centers WHERE id = ANY(%s)", (list(WC_IDS),))
    db.execute("DELETE FROM people WHERE id = ANY(%s)", (list(PERSON_IDS),))


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


def _qualify(person_id, *skill_ids):
    db.execute_many(
        "INSERT INTO person_skills (person_id, skill_id, level) VALUES (%s, %s, 2)",
        [(person_id, skill_id) for skill_id in skill_ids],
    )


def _response(person_id):
    return db.query(
        "SELECT * FROM saturday_work_responses WHERE day = %s AND person_id = %s",
        (SATURDAY, person_id),
    )[0]


def test_available_positions_includes_qualified_rows_and_excludes_unqualified_rows():
    positions = set(store.available_positions())
    assert store.AvailablePosition(
        910101, "Saturday Test Repair", ("Saturday Test Repair skill",)
    ) in positions
    assert store.AvailablePosition(
        910102, "Saturday Test Dismantle", ("Saturday Test Dismantle skill",)
    ) in positions
    assert all(position.wc_id != 910103 for position in positions)


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


def test_concurrent_identical_activation_is_idempotent():
    barrier = Barrier(2)

    def activate_together():
        barrier.wait()
        return _activate()

    with ThreadPoolExecutor(max_workers=2) as executor:
        first, second = [future.result() for future in (
            executor.submit(activate_together),
            executor.submit(activate_together),
        )]

    assert first == second
    assert db.query(
        "SELECT count(*) AS count FROM saturday_recruitments WHERE day = %s", (SATURDAY,)
    )[0]["count"] == 1


def test_reactivation_with_different_payload_is_rejected():
    _activate()
    with pytest.raises(store.LifecycleConflict):
        _activate(requested_counts={910101: 4, 910102: 2})


def test_update_rejects_positive_openings_that_cannot_match_current_commitments():
    _activate(requested_counts={910101: 2})
    db.execute(
        "INSERT INTO saturday_work_responses "
        "(day, person_id, status, availability_start, availability_end, eligible_wc_ids) "
        "VALUES (%s, 910101, 'committed', '06:00', '12:00', '[910101]'::jsonb)",
        (SATURDAY,),
    )
    with pytest.raises(store.LifecycleConflict):
        store.update_openings(SATURDAY, {910102: 1}, time(6, 0), time(12, 0), None, NOW)


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


def test_closed_recruitment_allows_shift_change_before_first_commitment():
    _activate(requested_counts={910101: 3})
    assert store.close_due(DEADLINE) == 1
    updated = store.update_openings(
        SATURDAY, {910101: 3}, time(6, 30), time(12, 0), None, NOW
    )
    assert updated.recruitment.shift_start == time(6, 30)


def test_commit_rematches_multi_skilled_volunteer_to_preserve_requested_coverage():
    _qualify(910101, 910101, 910102)
    _qualify(910102, 910101)
    _activate(requested_counts={910101: 1, 910102: 1})

    first = store.commit(SATURDAY, 910101, time(6, 0), time(12, 0), NOW)
    second = store.commit(SATURDAY, 910102, time(7, 0), time(11, 30), NOW)

    assert first.status == second.status == "committed"
    coverage = store.sr.match_commitments(
        second.bundle.openings,
        [
            store.sr.Commitment(c.person_id, c.eligible_wc_ids)
            for c in second.bundle.commitments
            if c.status == "committed"
        ],
    )
    assert coverage is not None
    assert coverage.wc_by_person == {910101: 910102, 910102: 910101}


def test_decline_suppresses_future_offer_for_same_saturday():
    _qualify(PERSON_ID, 910101)
    _activate(requested_counts={910101: 1})

    declined = store.decline(SATURDAY, PERSON_ID, NOW)

    assert declined.status == "declined"
    assert store.offer_for_person(PERSON_ID, NOW) is None


@pytest.mark.parametrize("earlier_response", ["declined", "cancelled"])
def test_earlier_saturday_response_does_not_suppress_later_offer(earlier_response):
    _qualify(PERSON_ID, 910101)
    _activate(requested_counts={910101: 1})
    _activate(
        day=NEXT_SATURDAY,
        response_deadline=NEXT_DEADLINE,
        requested_counts={910101: 1},
    )
    if earlier_response == "declined":
        store.decline(SATURDAY, PERSON_ID, NOW)
    else:
        store.commit(SATURDAY, PERSON_ID, time(6, 0), time(12, 0), NOW)
        store.cancel_by_employee(SATURDAY, PERSON_ID, NOW + timedelta(hours=1))

    offer = store.offer_for_person(PERSON_ID, NOW + timedelta(hours=2))

    assert offer is not None
    assert offer.day == NEXT_SATURDAY


def test_later_keeps_offer_and_reserves_no_capacity():
    _qualify(PERSON_ID, 910101)
    _activate(requested_counts={910101: 1})

    later = store.record_later(SATURDAY, PERSON_ID, NOW)

    assert later.status == "later"
    assert [item for item in later.bundle.commitments if item.status == "committed"] == []
    assert store.offer_for_person(PERSON_ID, NOW) is not None


def test_full_day_time_off_has_no_offer():
    _qualify(PERSON_ID, 910101)
    _activate(requested_counts={910101: 1})
    db.execute(
        "INSERT INTO time_off_requests "
        "(person_odoo_id, shape, holiday_status_id, date_from, date_to, state) "
        "VALUES (%s, 'full_day', 1, %s, %s, 'validate')",
        (PERSON_ID, SATURDAY, SATURDAY),
    )

    assert store.offer_for_person(PERSON_ID, NOW) is None


def test_salaried_person_has_no_offer():
    _qualify(910103, 910101)
    _activate(requested_counts={910101: 1})

    assert store.offer_for_person(910103, NOW) is None


def test_employee_cancel_before_cutoff_reopens_capacity():
    _qualify(PERSON_ID, 910101)
    _activate(requested_counts={910101: 1})
    assert store.commit(SATURDAY, PERSON_ID, time(6, 0), time(12, 0), NOW).status == "committed"
    before = store.home_banner(NOW)

    cancelled = store.cancel_by_employee(SATURDAY, PERSON_ID, NOW + timedelta(hours=1))
    after = store.home_banner(NOW + timedelta(hours=1))

    assert cancelled.status == "cancelled"
    assert before is None
    assert after is not None
    assert after.phase == "available"
    assert after.remaining_count == 1
    assert store.offer_for_person(PERSON_ID, NOW + timedelta(hours=1)) == store.Offer(
        SATURDAY, time(6, 0), time(12, 0), DEADLINE, frozenset({910101})
    )


def test_home_banner_becomes_tomorrow_plan_at_the_response_deadline():
    _activate(requested_counts={910101: 1})

    assert store.home_banner(DEADLINE) == store.HomeBanner(
        SATURDAY, DEADLINE, 0, "tomorrow", time(6), time(12)
    )


def test_home_banner_becomes_today_plan_until_the_snapshotted_shift_ends():
    _activate(requested_counts={910101: 1})

    assert store.home_banner(
        datetime(2026, 7, 25, 11, 59, tzinfo=SITE_TZ)
    ) == store.HomeBanner(SATURDAY, DEADLINE, 0, "today", time(6), time(12))
    assert store.home_banner(datetime(2026, 7, 25, 12, 0, tzinfo=SITE_TZ)) is None


def test_home_banner_never_shows_a_cancelled_saturday():
    _activate(requested_counts={910101: 1})
    store.cancel_recruitment(SATURDAY, "scheduler-manager", DEADLINE)

    assert store.home_banner(datetime(2026, 7, 24, 8, tzinfo=SITE_TZ)) is None


def test_cancelled_employee_can_recommit_partial_availability_before_deadline():
    _qualify(PERSON_ID, 910101)
    _activate(requested_counts={910101: 1})
    store.commit(SATURDAY, PERSON_ID, time(7, 0), time(11, 30), NOW)
    store.cancel_by_employee(SATURDAY, PERSON_ID, NOW + timedelta(hours=1))

    recommitted = store.commit(
        SATURDAY, PERSON_ID, time(6, 30), time(11, 0), NOW + timedelta(hours=2)
    )

    assert recommitted.status == "committed"
    commitment = next(item for item in recommitted.bundle.commitments if item.person_id == PERSON_ID)
    assert (commitment.availability_start, commitment.availability_end) == (
        time(6, 30),
        time(11, 0),
    )
    assert (
        _response(PERSON_ID)["availability_start"],
        _response(PERSON_ID)["availability_end"],
    ) == (
        time(6, 30),
        time(11, 0),
    )


def test_employee_cancel_at_or_after_cutoff_is_rejected():
    _qualify(PERSON_ID, 910101)
    _activate(requested_counts={910101: 1})
    store.commit(SATURDAY, PERSON_ID, time(6, 0), time(12, 0), NOW)

    with pytest.raises(store.RecruitingClosed):
        store.cancel_by_employee(SATURDAY, PERSON_ID, DEADLINE)


def test_commitment_status_keeps_partial_hours_after_cutoff():
    _qualify(PERSON_ID, 910101)
    _activate(requested_counts={910101: 1})
    store.commit(SATURDAY, PERSON_ID, time(7, 0), time(11, 30), NOW)

    status = store.commitment_for_person(PERSON_ID, DEADLINE)

    assert status == store.CommitmentStatus(
        SATURDAY, time(7, 0), time(11, 30), DEADLINE, False
    )


def test_manager_cancel_after_cutoff_records_actor_and_reason():
    _qualify(PERSON_ID, 910101)
    _activate(requested_counts={910101: 1})
    store.commit(SATURDAY, PERSON_ID, time(6, 0), time(12, 0), NOW)

    cancelled = store.cancel_by_manager(
        SATURDAY, PERSON_ID, "manager@gruberpallets.com", "Machine maintenance", DEADLINE
    )
    response = _response(PERSON_ID)

    assert cancelled.status == "cancelled"
    assert response["cancelled_by"] == "manager@gruberpallets.com"
    assert response["cancellation_reason"] == "Machine maintenance"
    assert response["cancelled_at"] == DEADLINE


def test_repeated_identical_commit_is_idempotent():
    _qualify(PERSON_ID, 910101)
    _activate(requested_counts={910101: 1})
    first = store.commit(SATURDAY, PERSON_ID, time(6, 0), time(12, 0), NOW)
    committed_at = _response(PERSON_ID)["committed_at"]

    second = store.commit(SATURDAY, PERSON_ID, time(6, 0), time(12, 0), NOW + timedelta(hours=1))

    assert first.status == second.status == "committed"
    assert db.query(
        "SELECT count(*) AS count FROM saturday_work_responses WHERE day = %s AND person_id = %s",
        (SATURDAY, PERSON_ID),
    )[0]["count"] == 1
    assert _response(PERSON_ID)["committed_at"] == committed_at


def test_repeated_employee_cancel_is_idempotent():
    _qualify(PERSON_ID, 910101)
    _activate(requested_counts={910101: 1})
    store.commit(SATURDAY, PERSON_ID, time(6, 0), time(12, 0), NOW)
    first = store.cancel_by_employee(SATURDAY, PERSON_ID, NOW + timedelta(hours=1))
    cancelled_at = _response(PERSON_ID)["cancelled_at"]

    second = store.cancel_by_employee(SATURDAY, PERSON_ID, NOW + timedelta(hours=2))

    assert first.status == second.status == "cancelled"
    assert _response(PERSON_ID)["cancelled_at"] == cancelled_at


def test_stale_decline_cannot_replace_commitment():
    _qualify(PERSON_ID, 910101)
    _activate(requested_counts={910101: 1})
    store.commit(SATURDAY, PERSON_ID, time(6, 0), time(12, 0), NOW)

    with pytest.raises(store.LifecycleConflict):
        store.decline(SATURDAY, PERSON_ID, NOW + timedelta(minutes=1))

    assert _response(PERSON_ID)["status"] == "committed"


def test_stale_later_cannot_replace_commitment():
    _qualify(PERSON_ID, 910101)
    _activate(requested_counts={910101: 1})
    store.commit(SATURDAY, PERSON_ID, time(6, 0), time(12, 0), NOW)

    with pytest.raises(store.LifecycleConflict):
        store.record_later(SATURDAY, PERSON_ID, NOW + timedelta(minutes=1))

    assert _response(PERSON_ID)["status"] == "committed"


def test_concurrent_final_slot_allows_exactly_one_commitment():
    _qualify(910101, 910101)
    _qualify(910102, 910101)
    _activate(requested_counts={910101: 1})
    barrier = Barrier(2)

    def commit_together(person_id):
        barrier.wait()
        try:
            return store.commit(SATURDAY, person_id, time(6, 0), time(12, 0), NOW)
        except store.NoCompatibleOpening:
            return None

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(commit_together, (910101, 910102)))

    assert sum(result is not None for result in results) == 1
    assert db.query(
        "SELECT count(*) AS count FROM saturday_work_responses "
        "WHERE day = %s AND status = 'committed'",
        (SATURDAY,),
    )[0]["count"] == 1
