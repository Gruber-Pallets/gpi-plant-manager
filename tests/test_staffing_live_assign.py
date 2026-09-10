"""Live Odoo transfers must land on the destination work-center assignment."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace

from zira_dashboard.staffing_view import StaffingPersonLocation


NOW = datetime(2026, 9, 9, 20, 30, tzinfo=UTC)
DAY = date(2026, 9, 9)


def _location(
    name: str,
    *,
    employee_id: int,
    planned: str | None,
    live: str | None,
    status: str = "valid",
    raw: str | None = None,
    profile: str | None = None,
    disambiguator: str | None = None,
) -> StaffingPersonLocation:
    return StaffingPersonLocation(
        employee_odoo_id=employee_id,
        person_name=name,
        planned_work_center=planned,
        live_work_center=live,
        raw_odoo_work_center=raw or live,
        status=status,
        since_utc=NOW - timedelta(hours=1),
        source_fresh_at=NOW,
        profile_person_name=profile or name,
        identity_disambiguator=disambiguator,
    )


def test_plan_moves_inbound_transfer_onto_empty_destination():
    from zira_dashboard.staffing_live_assign import plan_live_odoo_assignments

    planned = {
        "Hand Build #2": ["Humberto S.", "Christian C."],
        "Repair 1": [],
    }
    sources = {
        "Hand Build #2": {"Humberto S.": "default", "Christian C.": "generated"},
    }
    result = plan_live_odoo_assignments(
        planned,
        sources,
        (
            _location(
                "Christian C.",
                employee_id=8,
                planned="Hand Build #2",
                live="Repair 1",
            ),
        ),
    )

    assert result.changed is True
    assert result.assignments["Repair 1"] == ["Christian C."]
    assert result.assignments["Hand Build #2"] == ["Humberto S."]
    assert result.sources["Repair 1"]["Christian C."] == "generated"
    assert "Christian C." not in result.sources.get("Hand Build #2", {})


def test_plan_adds_unscheduled_person_to_live_work_center():
    from zira_dashboard.staffing_live_assign import plan_live_odoo_assignments

    result = plan_live_odoo_assignments(
        {"Repair 1": []},
        {},
        (
            _location(
                "Christian C.",
                employee_id=8,
                planned=None,
                live="Repair 1",
            ),
        ),
    )

    assert result.assignments["Repair 1"] == ["Christian C."]
    assert result.sources["Repair 1"]["Christian C."] == "generated"


def test_plan_is_noop_when_person_already_seated_at_live_center():
    from zira_dashboard.staffing_live_assign import plan_live_odoo_assignments

    planned = {"Repair 1": ["Christian C."]}
    sources = {"Repair 1": {"Christian C.": "manual"}}
    result = plan_live_odoo_assignments(
        planned,
        sources,
        (
            _location(
                "Christian C.",
                employee_id=8,
                planned="Repair 1",
                live="Repair 1",
            ),
        ),
    )

    assert result.changed is False
    assert result.assignments == planned
    assert result.sources == sources


def test_plan_skips_unmapped_and_unknown_identities():
    from zira_dashboard.staffing_live_assign import plan_live_odoo_assignments

    result = plan_live_odoo_assignments(
        {"Repair 1": []},
        {},
        (
            _location(
                "Mystery",
                employee_id=9,
                planned=None,
                live=None,
                status="unmapped_location",
                raw="Odoo Mystery",
            ),
            _location(
                "Unknown",
                employee_id=10,
                planned=None,
                live="Repair 1",
                profile=None,
                disambiguator="Odoo employee #10",
            ),
        ),
    )

    assert result.changed is False
    assert result.assignments["Repair 1"] == []


def test_inbound_live_by_work_center_omits_people_already_planned_there():
    from zira_dashboard.staffing_view import inbound_live_by_work_center

    locations = (
        _location(
            "Christian C.",
            employee_id=8,
            planned="Hand Build #2",
            live="Repair 1",
        ),
        _location(
            "Jose O.",
            employee_id=25,
            planned="Repair 2",
            live="Repair 2",
        ),
    )

    inbound = inbound_live_by_work_center(locations)

    assert [item.person_name for item in inbound["Repair 1"]] == ["Christian C."]
    assert "Repair 2" not in inbound


def test_apply_saves_changed_schedule_and_busts_today_cache(monkeypatch):
    from zira_dashboard import staffing, staffing_live_assign

    saved = []
    busts = []
    schedule = staffing.Schedule(
        day=DAY,
        published=True,
        assignments={
            "Hand Build #2": ["Humberto S.", "Christian C."],
            "Repair 1": [],
        },
        assignment_sources={
            "Hand Build #2": {"Humberto S.": "default", "Christian C.": "generated"},
        },
    )
    monkeypatch.setattr(staffing, "load_schedule", lambda day: schedule)
    monkeypatch.setattr(
        staffing,
        "save_schedule",
        lambda sched, **_kwargs: saved.append(sched),
    )
    monkeypatch.setattr(
        staffing_live_assign,
        "_after_schedule_write",
        lambda: busts.append(True),
    )

    changed = staffing_live_assign.apply_live_odoo_assignments_for_day(
        DAY,
        (
            _location(
                "Christian C.",
                employee_id=8,
                planned="Hand Build #2",
                live="Repair 1",
            ),
        ),
    )

    assert changed is True
    assert saved[0].assignments["Repair 1"] == ["Christian C."]
    assert saved[0].assignments["Hand Build #2"] == ["Humberto S."]
    assert saved[0].published is True
    assert busts == [True]


def test_unattributed_skips_work_center_already_occupied_in_odoo(monkeypatch):
    from zira_dashboard import staffing, wc_attributions
    from zira_dashboard import leaderboard as _lb

    monkeypatch.setattr(
        staffing,
        "load_schedule",
        lambda d: SimpleNamespace(assignments={}),
    )
    monkeypatch.setattr(wc_attributions, "for_day", lambda d: [])
    monkeypatch.setattr(wc_attributions, "people_by_wc", lambda d, rows=None: {})
    monkeypatch.setattr(wc_attributions, "testing_windows_for_day", lambda d, rows=None: {})
    monkeypatch.setattr(
        wc_attributions,
        "live_occupied_work_centers",
        lambda d: {"Repair 1"},
    )
    repair = next(loc for loc in staffing.LOCATIONS if loc.name == "Repair 1")
    result = SimpleNamespace(
        station=SimpleNamespace(meter_id=repair.meter_id, name="Repair 1"),
        units=225,
        active_intervals=((NOW - timedelta(hours=3), NOW),),
    )
    monkeypatch.setattr(
        _lb,
        "cached_leaderboard",
        lambda client, stations, day, now_utc=None: [result],
    )

    out = wc_attributions.unattributed_for_day(DAY, object())

    assert all(item["wc_name"] != "Repair 1" for item in out)


def test_assigned_operators_include_live_odoo_people_for_today(monkeypatch):
    from zira_dashboard import attendance, plant_day, staffing, wc_dashboard_data

    monkeypatch.setattr(plant_day, "today", lambda: DAY)
    monkeypatch.setattr(
        staffing,
        "load_schedule",
        lambda d: staffing.Schedule(day=d, published=True, assignments={}),
    )
    monkeypatch.setattr(attendance, "full_day_absent_names", lambda d: set())
    monkeypatch.setattr(
        wc_dashboard_data,
        "live_people_at_work_center",
        lambda wc_name, day: ["Christian C."] if wc_name == "Repair 1" else [],
    )

    assert wc_dashboard_data.assigned_operators_for_wc("Repair 1", DAY) == [
        "Christian C."
    ]


def test_apply_after_sync_only_writes_when_today_changed(monkeypatch):
    from zira_dashboard import plant_day, staffing_live_assign

    called = []
    monkeypatch.setattr(plant_day, "today", lambda: DAY)
    monkeypatch.setattr(
        staffing_live_assign,
        "apply_live_odoo_assignments_for_day",
        lambda day: called.append(day) or True,
    )

    assert staffing_live_assign.apply_after_attendance_sync(
        frozenset({date(2026, 9, 8)})
    ) is False
    assert called == []
    assert staffing_live_assign.apply_after_attendance_sync(frozenset({DAY})) is True
    assert called == [DAY]
