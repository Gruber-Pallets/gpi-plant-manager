"""Manager endpoints for the optional Saturday recruiting lifecycle."""

from __future__ import annotations

import os
from datetime import date, datetime, time, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from zira_dashboard import (
    company_holidays,
    db,
    employee_notifications,
    odoo_client,
    optional_workday,
    saturday_recruiting_store as store,
    staffing,
)
from zira_dashboard.app import app
from zira_dashboard.routes import saturday_recruiting as routes, timeclock
from zira_dashboard.shift_config import SITE_TZ


client = TestClient(app)
SATURDAY = date(2026, 7, 25)
HOLIDAY = date(2026, 11, 27)
THANKSGIVING = date(2026, 11, 26)
NOW = datetime(2026, 7, 20, 12, tzinfo=SITE_TZ)
REPAIR_ID = 17


def _bundle(
    status: str = "recruiting",
    *,
    day: date = SATURDAY,
    day_kind: str = "saturday",
    event_name: str | None = None,
    holiday_odoo_id: int | None = None,
) -> store.RecruitmentBundle:
    return store.RecruitmentBundle(
        store.Recruitment(
            day=day,
            status=status,
            shift_start=time(6),
            shift_end=time(12),
            response_deadline=datetime(2026, 7, 24, 7, tzinfo=SITE_TZ),
            day_kind=day_kind,
            event_name=event_name,
            holiday_odoo_id=holiday_odoo_id,
        ),
        (store.sr.Opening(17, "Repair", 3, ("Repair",)),),
        (),
    )


def _holiday(odoo_id: int, name: str, day: date) -> company_holidays.CompanyHoliday:
    return company_holidays.CompanyHoliday(
        odoo_id=odoo_id,
        name=name,
        date_from=day,
        date_to=day,
        odoo_date_from=f"{day.isoformat()} 06:00:00",
        odoo_date_to=f"{day.isoformat()} 23:59:59",
    )


def test_activate_passes_snapshotted_values_and_actor(monkeypatch):
    captured = {}
    monkeypatch.setattr(routes.store, "activate", lambda **kw: captured.update(kw) or _bundle())
    monkeypatch.setattr(routes.sr, "response_deadline", lambda *_args: NOW)
    monkeypatch.setattr(
        routes.schedule_store,
        "current",
        lambda: SimpleNamespace(work_weekdays=frozenset({0, 1, 2, 3, 4})),
    )
    monkeypatch.setattr(routes, "plant_now", lambda: NOW - timedelta(days=1))
    monkeypatch.setattr(routes.staffing_routes, "_bust_after_mutation", lambda: None)

    response = client.post(
        "/api/staffing/saturday-recruiting/activate",
        json={
            "day": "2026-07-25",
            "shift_start": "06:00",
            "shift_end": "12:00",
            "requested_counts": {"17": 3, "22": 2},
        },
    )

    assert response.status_code == 200
    assert captured["day"] == SATURDAY
    assert captured["actor"] is None
    assert captured["requested_counts"] == {17: 3, 22: 2}
    assert captured["day_kind"] == "saturday"
    assert captured["event_name"] == "Saturday"
    assert captured["holiday_odoo_id"] is None


def test_non_saturday_activation_is_422():
    response = client.post(
        "/api/staffing/saturday-recruiting/activate",
        json={
            "day": "2026-07-24",
            "shift_start": "06:00",
            "shift_end": "12:00",
            "requested_counts": {"17": 1},
        },
    )
    assert response.status_code == 422
    assert response.json()["detail"] == "This date is not an optional workday."


def test_holiday_activation_passes_local_mirror_metadata_without_odoo_writes(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        company_holidays,
        "for_day",
        lambda day: _holiday(481, "Black Friday", HOLIDAY) if day == HOLIDAY else None,
    )
    monkeypatch.setattr(
        routes.store,
        "activate",
        lambda **kw: (
            captured.update(kw)
            or _bundle(
                day=HOLIDAY,
                day_kind="holiday",
                event_name="Black Friday",
                holiday_odoo_id=481,
            )
        ),
    )
    monkeypatch.setattr(routes.sr, "response_deadline", lambda *_args: NOW)
    monkeypatch.setattr(
        routes.schedule_store,
        "current",
        lambda: SimpleNamespace(work_weekdays=frozenset({0, 1, 2, 3, 4})),
    )
    monkeypatch.setattr(routes, "plant_now", lambda: NOW - timedelta(days=1))
    monkeypatch.setattr(routes.staffing_routes, "_bust_after_mutation", lambda: None)
    odoo_calls = []
    monkeypatch.setattr(
        odoo_client,
        "execute",
        lambda *args, **kwargs: odoo_calls.append((args, kwargs)),
    )

    response = client.post(
        "/api/staffing/saturday-recruiting/activate",
        json={
            "day": HOLIDAY.isoformat(),
            "shift_start": "06:00",
            "shift_end": "12:00",
            "requested_counts": {"17": 3},
        },
    )

    assert response.status_code == 200
    assert captured["day_kind"] == "holiday"
    assert captured["event_name"] == "Black Friday"
    assert captured["holiday_odoo_id"] == 481
    assert odoo_calls == []


def test_activate_from_schedule_uses_enabled_center_minimums(monkeypatch):
    """Scheduler activation uses the effective configured minimum, not min_ops."""
    seen = {}
    location = staffing.Location(
        "Repair 1",
        "Repair",
        "Bay",
        "Recycled",
        None,
        min_ops=2,
        max_ops=4,
    )
    monkeypatch.setattr(
        routes.staffing,
        "load_schedule",
        lambda _day: staffing.Schedule(
            day=SATURDAY,
            auto_enabled_work_centers=["Repair 1"],
        ),
    )
    monkeypatch.setattr(routes.staffing_routes, "_effective_minimum", lambda _loc: 3)
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
    assert seen["requested_counts"] == {REPAIR_ID: 3}
    assert seen["shift_start"] == time(6)
    assert seen["shift_end"] == time(12)


def test_holiday_activation_from_schedule_uses_effective_demand_hours_and_deadline(
    monkeypatch,
):
    seen = {}
    location = staffing.Location(
        "Repair 1",
        "Repair",
        "Bay",
        "Recycled",
        None,
        min_ops=2,
        max_ops=5,
    )
    mirrored = {
        HOLIDAY: _holiday(481, "Black Friday", HOLIDAY),
        THANKSGIVING: _holiday(480, "Thanksgiving", THANKSGIVING),
    }
    monkeypatch.setattr(
        company_holidays,
        "for_day",
        lambda day: mirrored.get(day),
    )
    monkeypatch.setattr(
        routes.staffing,
        "load_schedule",
        lambda _day: staffing.Schedule(
            day=HOLIDAY,
            auto_enabled_work_centers=["Repair 1"],
        ),
    )
    monkeypatch.setattr(routes.staffing_routes, "_effective_minimum", lambda _loc: 4)
    monkeypatch.setattr(routes.staffing, "LOCATIONS", (location,))
    monkeypatch.setattr(
        routes.store,
        "available_positions",
        lambda: (store.AvailablePosition(REPAIR_ID, "Repair 1", ("Repair",)),),
    )
    monkeypatch.setattr(
        routes.store,
        "activate",
        lambda **kw: (
            seen.update(kw)
            or _bundle(
                day=HOLIDAY,
                day_kind="holiday",
                event_name="Black Friday",
                holiday_odoo_id=481,
            )
        ),
    )
    monkeypatch.setattr(
        routes.schedule_store,
        "current",
        lambda: SimpleNamespace(work_weekdays=frozenset({0, 1, 2, 3, 4})),
    )
    monkeypatch.setattr(
        routes.shift_config,
        "configured_shift_start_for",
        lambda day: time(5, 30) if day == HOLIDAY else time(7),
    )
    monkeypatch.setattr(
        routes.shift_config,
        "configured_shift_end_for",
        lambda _day: time(13),
    )
    monkeypatch.setattr(routes, "plant_now", lambda: NOW - timedelta(days=1))
    monkeypatch.setattr(routes.staffing_routes, "_bust_after_mutation", lambda: None)

    response = client.post(
        "/api/staffing/saturday-recruiting/activate-from-schedule",
        json={"day": HOLIDAY.isoformat()},
    )

    assert response.status_code == 200
    assert seen["requested_counts"] == {REPAIR_ID: 4}
    assert seen["shift_start"] == time(5, 30)
    assert seen["shift_end"] == time(13)
    assert seen["response_deadline"] == datetime(
        2026,
        11,
        25,
        7,
        tzinfo=SITE_TZ,
    )
    assert seen["day_kind"] == "holiday"
    assert seen["event_name"] == "Black Friday"
    assert seen["holiday_odoo_id"] == 481


def test_activate_from_schedule_rejects_an_ordinary_date(monkeypatch):
    ordinary_day = date(2026, 11, 25)
    monkeypatch.setattr(optional_workday, "for_day", lambda _day: None)

    response = client.post(
        "/api/staffing/saturday-recruiting/activate-from-schedule",
        json={"day": ordinary_day.isoformat()},
    )

    assert response.status_code == 422
    assert response.json()["detail"] == "This date is not an optional workday."


def test_activate_from_schedule_rejects_no_enabled_centers(monkeypatch):
    monkeypatch.setattr(
        routes.staffing,
        "load_schedule",
        lambda _day: staffing.Schedule(day=SATURDAY, auto_enabled_work_centers=[]),
    )

    response = client.post(
        "/api/staffing/saturday-recruiting/activate-from-schedule",
        json={"day": "2026-07-25"},
    )

    assert response.status_code == 422
    assert response.json()["detail"] == "Turn on at least one work center before recruiting."


def test_openings_can_add_a_new_requested_work_center_while_recruiting(monkeypatch):
    captured = {}
    monkeypatch.setattr(routes.store, "get", lambda day: _bundle(day=day))
    monkeypatch.setattr(
        routes.store, "update_openings", lambda **kw: captured.update(kw) or _bundle()
    )
    monkeypatch.setattr(routes, "plant_now", lambda: NOW)
    monkeypatch.setattr(routes.staffing_routes, "_bust_after_mutation", lambda: None)

    response = client.post(
        "/api/staffing/saturday-recruiting/openings",
        json={
            "day": "2026-07-25",
            "shift_start": "06:00",
            "shift_end": "12:00",
            "requested_counts": {"17": 4, "22": 1},
        },
    )

    assert response.status_code == 200
    assert captured["requested_counts"] == {17: 4, 22: 1}


def test_holiday_openings_use_persisted_metadata_after_mirror_removal(monkeypatch):
    captured = {}
    bundle = _bundle(
        day=HOLIDAY,
        day_kind="holiday",
        event_name="Black Friday",
        holiday_odoo_id=481,
    )
    monkeypatch.setattr(optional_workday, "for_day", lambda _day: None)
    monkeypatch.setattr(routes.store, "get", lambda day: bundle if day == HOLIDAY else None)
    monkeypatch.setattr(
        routes.store,
        "update_openings",
        lambda **kw: captured.update(kw) or bundle,
    )
    monkeypatch.setattr(routes, "plant_now", lambda: NOW)
    monkeypatch.setattr(routes.staffing_routes, "_bust_after_mutation", lambda: None)

    response = client.post(
        "/api/staffing/saturday-recruiting/openings",
        json={
            "day": HOLIDAY.isoformat(),
            "shift_start": "06:00",
            "shift_end": "12:00",
            "requested_counts": {"17": 4},
        },
    )

    assert response.status_code == 200
    assert captured["day"] == HOLIDAY


def test_filled_count_reduction_returns_409(monkeypatch):
    monkeypatch.setattr(routes.store, "get", lambda day: _bundle(day=day))
    monkeypatch.setattr(
        routes.store,
        "update_openings",
        lambda **_kw: (_ for _ in ()).throw(store.LifecycleConflict("coverage")),
    )
    monkeypatch.setattr(routes, "plant_now", lambda: NOW)

    response = client.post(
        "/api/staffing/saturday-recruiting/openings",
        json={
            "day": "2026-07-25",
            "shift_start": "06:00",
            "shift_end": "12:00",
            "requested_counts": {"17": 1},
        },
    )

    assert response.status_code == 409
    assert response.json()["detail"] == "coverage"


def test_manager_commitment_cancel_requires_reason():
    response = client.post(
        "/api/staffing/saturday-recruiting/commitments/99/cancel",
        json={
            "day": "2026-07-25",
            "reason": "  ",
        },
    )
    assert response.status_code == 422


def test_manager_commitment_cancel_uses_persisted_holiday_lifecycle(monkeypatch):
    bundle = _bundle(
        day=HOLIDAY,
        day_kind="holiday",
        event_name="Black Friday",
        holiday_odoo_id=481,
    )
    loaded = []
    cancelled = []
    monkeypatch.setattr(
        routes.store,
        "get",
        lambda day: loaded.append(day) or bundle,
    )
    monkeypatch.setattr(
        routes.store,
        "cancel_by_manager",
        lambda *args: cancelled.append(args) or store.DecisionResult("cancelled", bundle),
    )
    monkeypatch.setattr(routes, "plant_now", lambda: NOW)
    monkeypatch.setattr(routes.staffing_routes, "_bust_after_mutation", lambda: None)

    response = client.post(
        "/api/staffing/saturday-recruiting/commitments/99/cancel",
        json={"day": HOLIDAY.isoformat(), "reason": "Coverage changed"},
    )

    assert response.status_code == 200
    assert loaded == [HOLIDAY]
    assert cancelled[0][0:2] == (HOLIDAY, 99)


def test_full_cancel_notifies_committed_people_and_reports_failures(monkeypatch):
    targets = (
        store.StoredCommitment(1, 101, "Ana", "committed", time(6), time(12), frozenset()),
        store.StoredCommitment(2, 102, "Ben", "committed", time(6), time(12), frozenset()),
    )
    notified = []
    monkeypatch.setattr(routes.store, "get", lambda _day: _bundle())
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
    assert "Saturday cancellation notice" in response.json()["warning"]


def test_full_holiday_cancel_uses_persisted_notification_metadata_and_warning(
    monkeypatch,
):
    bundle = _bundle(
        day=HOLIDAY,
        day_kind="holiday",
        event_name="Black Friday",
        holiday_odoo_id=481,
    )
    targets = (
        store.StoredCommitment(
            1,
            101,
            "Ana",
            "committed",
            time(6),
            time(12),
            frozenset(),
        ),
        store.StoredCommitment(
            2,
            None,
            "Ben",
            "committed",
            time(6),
            time(12),
            frozenset(),
        ),
    )
    notified = []
    monkeypatch.setattr(optional_workday, "for_day", lambda _day: None)
    monkeypatch.setattr(routes.store, "get", lambda day: bundle if day == HOLIDAY else None)
    monkeypatch.setattr(routes.store, "cancel_recruitment", lambda *_args: targets)
    monkeypatch.setattr(routes, "plant_now", lambda: NOW)
    monkeypatch.setattr(routes.staffing, "invalidate_schedule_cache", lambda _day: None)
    monkeypatch.setattr(routes.staffing_routes, "_bust_after_mutation", lambda: None)
    monkeypatch.setattr(
        employee_notifications,
        "create_saturday_cancelled",
        lambda odoo_id, day, **kwargs: notified.append((odoo_id, day, kwargs)),
    )

    response = client.post(
        "/api/staffing/saturday-recruiting/cancel",
        json={"day": HOLIDAY.isoformat()},
    )

    assert response.status_code == 200
    assert notified == [
        (
            101,
            HOLIDAY,
            {"day_kind": "holiday", "event_name": "Black Friday"},
        )
    ]
    assert "Ben" in response.json()["warning"]
    assert "Black Friday cancellation notice" in response.json()["warning"]


def test_full_holiday_cancel_dispatches_through_keyword_compatible_notification(
    monkeypatch,
):
    bundle = _bundle(
        day=HOLIDAY,
        day_kind="holiday",
        event_name="Black Friday",
        holiday_odoo_id=481,
    )
    targets = (
        store.StoredCommitment(
            1,
            101,
            "Ana",
            "committed",
            time(6),
            time(12),
            frozenset(),
        ),
    )
    executes = []
    monkeypatch.setattr(routes.store, "get", lambda _day: bundle)
    monkeypatch.setattr(routes.store, "cancel_recruitment", lambda *_args: targets)
    monkeypatch.setattr(routes, "plant_now", lambda: NOW)
    monkeypatch.setattr(routes.staffing, "invalidate_schedule_cache", lambda _day: None)
    monkeypatch.setattr(routes.staffing_routes, "_bust_after_mutation", lambda: None)
    monkeypatch.setattr(
        employee_notifications.db,
        "execute",
        lambda sql, params=None: executes.append((sql, params)),
    )

    response = client.post(
        "/api/staffing/saturday-recruiting/cancel",
        json={"day": HOLIDAY.isoformat()},
    )

    assert response.status_code == 200
    assert "warning" not in response.json()
    assert len(executes) == 1
    assert executes[0][1] == (
        101,
        "saturday_work_cancelled",
        HOLIDAY,
        "Holiday work cancelled",
        "Black Friday work was cancelled. Do not report to work.",
    )


def test_browser_fallback_error_uses_optional_workday_copy():
    javascript = (
        Path(routes.__file__).parents[1] / "static" / "saturday-recruiting.js"
    ).read_text()

    assert "Could not start optional workday recruiting." in javascript
    assert "Could not start Saturday recruiting." not in javascript


pytestmark_db = pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="needs Postgres")


@pytestmark_db
def test_full_cancel_unpublishes_and_clears_assignments_atomically(monkeypatch, request):
    """A cancellation drops only the live publication/assignments, in one transaction."""
    work_center_id = 910117
    person_id = 910117

    def clear_test_data():
        db.execute("DELETE FROM saturday_recruitments WHERE day = %s", (SATURDAY,))
        db.execute("DELETE FROM schedule_assignments WHERE day = %s", (SATURDAY,))
        db.execute("DELETE FROM schedules WHERE day = %s", (SATURDAY,))
        db.execute("DELETE FROM work_centers WHERE id = %s", (work_center_id,))
        db.execute("DELETE FROM people WHERE id = %s", (person_id,))

    db.bootstrap_schema()
    clear_test_data()
    request.addfinalizer(clear_test_data)
    db.execute(
        "INSERT INTO schedules (day, published, notes) VALUES (%s, TRUE, 'keep')",
        (SATURDAY,),
    )
    db.execute(
        "INSERT INTO work_centers (id, name, category) VALUES (%s, 'Cancel Test', 'Repair') ON CONFLICT (id) DO NOTHING",
        (work_center_id,),
    )
    db.execute(
        "INSERT INTO people (id, name) VALUES (%s, 'Cancel Person') ON CONFLICT (id) DO NOTHING",
        (person_id,),
    )
    db.execute(
        "INSERT INTO schedule_assignments (day, wc_id, person_id) VALUES (%s, %s, %s)",
        (SATURDAY, work_center_id, person_id),
    )
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
    assert (
        db.query("SELECT status FROM saturday_recruitments WHERE day = %s", (SATURDAY,))[0][
            "status"
        ]
        == "cancelled"
    )
    assert (
        db.query("SELECT published FROM schedules WHERE day = %s", (SATURDAY,))[0]["published"]
        is False
    )
    assert db.query("SELECT * FROM schedule_assignments WHERE day = %s", (SATURDAY,)) == []


@pytestmark_db
def test_schedule_activation_makes_timeclock_banner_live(monkeypatch, request):
    """The Scheduler's live recruiting round is the Timeclock banner source."""
    work_center_id = 910118
    skill_id = 910118
    work_center_name = "Live Banner Test"
    location = staffing.Location(
        work_center_name,
        "Repair",
        "Bay",
        "Recycled",
        None,
        min_ops=2,
        max_ops=4,
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
    # min_ops must mirror the Location: _effective_minimum reads the DB row
    # (work_centers.min_ops, NOT NULL DEFAULT 1), not the code-side Location.
    db.execute(
        "INSERT INTO work_centers (id, name, category, min_ops, max_ops) "
        "VALUES (%s, %s, 'Repair', 2, 4)",
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
    # The route reads the day's enabled Auto centers from the saved schedule
    # (auto_enabled_work_centers) — the old _enabled_auto_work_centers helper
    # is gone.
    monkeypatch.setattr(
        routes.staffing,
        "load_schedule",
        lambda _day: SimpleNamespace(auto_enabled_work_centers={work_center_name}),
    )
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
