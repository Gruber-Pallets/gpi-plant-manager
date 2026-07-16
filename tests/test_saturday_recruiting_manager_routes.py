"""Manager endpoints for the optional Saturday recruiting lifecycle."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
import os
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from zira_dashboard import db, saturday_recruiting_store as store, staffing
from zira_dashboard import employee_notifications
from zira_dashboard.app import app
from zira_dashboard.routes import saturday_recruiting as routes, timeclock
from zira_dashboard.shift_config import SITE_TZ


client = TestClient(app)
SATURDAY = date(2026, 7, 25)
NOW = datetime(2026, 7, 20, 12, tzinfo=SITE_TZ)
REPAIR_ID = 17


def _bundle(status: str = "recruiting") -> store.RecruitmentBundle:
    return store.RecruitmentBundle(
        store.Recruitment(SATURDAY, status, time(6), time(12), datetime(2026, 7, 24, 7, tzinfo=SITE_TZ)),
        (store.sr.Opening(17, "Repair", 3, ("Repair",)),),
        (),
    )


def test_activate_passes_snapshotted_values_and_actor(monkeypatch):
    captured = {}
    monkeypatch.setattr(routes.store, "activate", lambda **kw: captured.update(kw) or _bundle())
    monkeypatch.setattr(routes.sr, "response_deadline", lambda *_args: NOW)
    monkeypatch.setattr(routes.schedule_store, "current", lambda: SimpleNamespace(work_weekdays=frozenset({0, 1, 2, 3, 4})))
    monkeypatch.setattr(routes, "plant_now", lambda: NOW - timedelta(days=1))
    monkeypatch.setattr(routes.staffing_routes, "_bust_after_mutation", lambda: None)

    response = client.post("/api/staffing/saturday-recruiting/activate", json={
        "day": "2026-07-25", "shift_start": "06:00", "shift_end": "12:00",
        "requested_counts": {"17": 3, "22": 2},
    })

    assert response.status_code == 200
    assert captured["day"] == SATURDAY
    assert captured["actor"] is None
    assert captured["requested_counts"] == {17: 3, 22: 2}


def test_non_saturday_activation_is_422():
    response = client.post("/api/staffing/saturday-recruiting/activate", json={
        "day": "2026-07-24", "shift_start": "06:00", "shift_end": "12:00",
        "requested_counts": {"17": 1},
    })
    assert response.status_code == 422


def test_activate_from_schedule_uses_enabled_center_minimums(monkeypatch):
    """Scheduler activation derives openings from enabled centers, not the browser."""
    seen = {}
    location = staffing.Location(
        "Repair 1", "Repair", "Bay", "Recycled", None, min_ops=2, max_ops=4,
    )
    monkeypatch.setattr(routes.staffing_routes, "_enabled_auto_work_centers", lambda _: {"Repair 1"})
    monkeypatch.setattr(routes.staffing, "LOCATIONS", (location,))
    monkeypatch.setattr(
        routes.store,
        "available_positions",
        lambda: (store.AvailablePosition(REPAIR_ID, "Repair 1", ("Repair",)),),
    )
    monkeypatch.setattr(routes.store, "activate", lambda **kw: seen.update(kw) or _bundle())
    monkeypatch.setattr(routes.sr, "response_deadline", lambda *_args: NOW)
    monkeypatch.setattr(
        routes.schedule_store,
        "current",
        lambda: SimpleNamespace(work_weekdays=frozenset({0, 1, 2, 3, 4})),
    )
    monkeypatch.setattr(routes.shift_config, "configured_shift_start_for", lambda _day: time(6))
    monkeypatch.setattr(routes.shift_config, "configured_shift_end_for", lambda _day: time(12))
    monkeypatch.setattr(routes, "plant_now", lambda: NOW - timedelta(days=1))
    monkeypatch.setattr(routes.staffing_routes, "_bust_after_mutation", lambda: None)

    response = client.post(
        "/api/staffing/saturday-recruiting/activate-from-schedule",
        json={"day": "2026-07-25"},
    )

    assert response.status_code == 200
    assert seen["requested_counts"] == {REPAIR_ID: 2}
    assert seen["shift_start"] == time(6)
    assert seen["shift_end"] == time(12)


def test_activate_from_schedule_rejects_no_enabled_centers(monkeypatch):
    monkeypatch.setattr(routes.staffing_routes, "_enabled_auto_work_centers", lambda _: set())

    response = client.post(
        "/api/staffing/saturday-recruiting/activate-from-schedule",
        json={"day": "2026-07-25"},
    )

    assert response.status_code == 422
    assert response.json()["detail"] == "Turn on at least one work center before recruiting."


def test_openings_can_add_a_new_requested_work_center_while_recruiting(monkeypatch):
    captured = {}
    monkeypatch.setattr(routes.store, "update_openings", lambda **kw: captured.update(kw) or _bundle())
    monkeypatch.setattr(routes, "plant_now", lambda: NOW)
    monkeypatch.setattr(routes.staffing_routes, "_bust_after_mutation", lambda: None)

    response = client.post("/api/staffing/saturday-recruiting/openings", json={
        "day": "2026-07-25", "shift_start": "06:00", "shift_end": "12:00",
        "requested_counts": {"17": 4, "22": 1},
    })

    assert response.status_code == 200
    assert captured["requested_counts"] == {17: 4, 22: 1}


def test_filled_count_reduction_returns_409(monkeypatch):
    monkeypatch.setattr(
        routes.store,
        "update_openings",
        lambda **_kw: (_ for _ in ()).throw(store.LifecycleConflict("coverage")),
    )
    monkeypatch.setattr(routes, "plant_now", lambda: NOW)

    response = client.post("/api/staffing/saturday-recruiting/openings", json={
        "day": "2026-07-25", "shift_start": "06:00", "shift_end": "12:00",
        "requested_counts": {"17": 1},
    })

    assert response.status_code == 409
    assert response.json()["detail"] == "coverage"


def test_manager_commitment_cancel_requires_reason():
    response = client.post("/api/staffing/saturday-recruiting/commitments/99/cancel", json={
        "day": "2026-07-25", "reason": "  ",
    })
    assert response.status_code == 422


def test_full_cancel_notifies_committed_people_and_reports_failures(monkeypatch):
    targets = (
        store.StoredCommitment(1, 101, "Ana", "committed", time(6), time(12), frozenset()),
        store.StoredCommitment(2, 102, "Ben", "committed", time(6), time(12), frozenset()),
    )
    notified = []
    monkeypatch.setattr(routes.store, "cancel_recruitment", lambda *_args: targets)
    monkeypatch.setattr(routes, "plant_now", lambda: NOW)
    monkeypatch.setattr(routes.staffing, "invalidate_schedule_cache", lambda _day: None)
    monkeypatch.setattr(routes.staffing_routes, "_bust_after_mutation", lambda: None)

    def notify(odoo_id, day):
        notified.append((odoo_id, day))
        if odoo_id == 102:
            raise RuntimeError("notification down")

    monkeypatch.setattr(employee_notifications, "create_saturday_cancelled", notify)

    response = client.post("/api/staffing/saturday-recruiting/cancel", json={"day": "2026-07-25"})

    assert response.status_code == 200
    assert notified == [(101, SATURDAY), (102, SATURDAY)]
    assert "Ben" in response.json()["warning"]


pytestmark_db = pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="needs Postgres")


@pytestmark_db
def test_full_cancel_unpublishes_and_clears_assignments_atomically(monkeypatch):
    """A cancellation drops only the live publication/assignments, in one transaction."""
    db.bootstrap_schema()
    db.execute("DELETE FROM saturday_recruitments WHERE day = %s", (SATURDAY,))
    db.execute("DELETE FROM schedule_assignments WHERE day = %s", (SATURDAY,))
    db.execute("DELETE FROM schedules WHERE day = %s", (SATURDAY,))
    db.execute(
        "INSERT INTO schedules (day, published, notes, wc_notes) VALUES (%s, TRUE, 'keep', '{\"Repair\": \"note\"}'::jsonb)",
        (SATURDAY,),
    )
    db.execute("INSERT INTO work_centers (id, name, category) VALUES (910117, 'Cancel Test', 'Repair') ON CONFLICT (id) DO NOTHING")
    db.execute("INSERT INTO people (id, name) VALUES (910117, 'Cancel Person') ON CONFLICT (id) DO NOTHING")
    db.execute("INSERT INTO schedule_assignments (day, wc_id, person_id) VALUES (%s, 910117, 910117)", (SATURDAY,))
    db.execute(
        "INSERT INTO saturday_recruitments (day, status, shift_start, shift_end, response_deadline) "
        "VALUES (%s, 'published', '06:00', '12:00', %s)",
        (SATURDAY, NOW),
    )
    monkeypatch.setattr(routes, "plant_now", lambda: NOW)
    monkeypatch.setattr(routes.staffing_routes, "_bust_after_mutation", lambda: None)
    monkeypatch.setattr(routes.staffing, "invalidate_schedule_cache", lambda _day: None)

    response = client.post("/api/staffing/saturday-recruiting/cancel", json={"day": "2026-07-25"})

    assert response.status_code == 200
    assert db.query("SELECT status FROM saturday_recruitments WHERE day = %s", (SATURDAY,))[0]["status"] == "cancelled"
    assert db.query("SELECT published FROM schedules WHERE day = %s", (SATURDAY,))[0]["published"] is False
    assert db.query("SELECT * FROM schedule_assignments WHERE day = %s", (SATURDAY,)) == []


@pytestmark_db
def test_schedule_activation_makes_timeclock_banner_live(monkeypatch, request):
    """The Scheduler's live recruiting round is the Timeclock banner source."""
    work_center_id = 910118
    skill_id = 910118
    work_center_name = "Live Banner Test"
    location = staffing.Location(
        work_center_name, "Repair", "Bay", "Recycled", None, min_ops=2, max_ops=4,
    )

    def clear_test_data():
        db.execute("DELETE FROM saturday_recruitments WHERE day = %s", (SATURDAY,))
        db.execute("DELETE FROM schedule_assignments WHERE day = %s", (SATURDAY,))
        db.execute("DELETE FROM schedules WHERE day = %s", (SATURDAY,))
        db.execute("DELETE FROM work_center_required_skills WHERE wc_id = %s", (work_center_id,))
        db.execute("DELETE FROM work_centers WHERE id = %s", (work_center_id,))
        db.execute("DELETE FROM skills WHERE id = %s", (skill_id,))

    db.bootstrap_schema()
    clear_test_data()
    request.addfinalizer(clear_test_data)
    db.execute(
        "INSERT INTO work_centers (id, name, category) VALUES (%s, %s, 'Repair')",
        (work_center_id, work_center_name),
    )
    db.execute(
        "INSERT INTO skills (id, name, skill_type) VALUES (%s, 'Live banner skill', 'Certification')",
        (skill_id,),
    )
    db.execute(
        "INSERT INTO work_center_required_skills (wc_id, skill_id) VALUES (%s, %s)",
        (work_center_id, skill_id),
    )
    monkeypatch.setattr(routes.staffing_routes, "_enabled_auto_work_centers", lambda _: {work_center_name})
    monkeypatch.setattr(routes.staffing, "LOCATIONS", (location,))
    monkeypatch.setattr(
        routes.schedule_store,
        "current",
        lambda: SimpleNamespace(work_weekdays=frozenset({0, 1, 2, 3, 4})),
    )
    monkeypatch.setattr(routes.shift_config, "configured_shift_start_for", lambda _day: time(6))
    monkeypatch.setattr(routes.shift_config, "configured_shift_end_for", lambda _day: time(12))
    monkeypatch.setattr(routes, "plant_now", lambda: NOW)
    monkeypatch.setattr(timeclock, "plant_now", lambda: NOW)
    monkeypatch.setattr(routes.staffing_routes, "_bust_after_mutation", lambda: None)

    response = client.post(
        "/api/staffing/saturday-recruiting/activate-from-schedule",
        json={"day": SATURDAY.isoformat()},
    )

    assert response.status_code == 200
    banner = timeclock._saturday_banner_context()
    assert banner is not None
    assert banner["day"] == SATURDAY.isoformat()
    assert banner["remaining_count"] == 2
