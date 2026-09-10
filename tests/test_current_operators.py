"""Pure read-only current-operator join and canonical presence extraction."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from zira_dashboard import current_operators, staffing
from zira_dashboard.attendance_timeline import LocationSpan

NOW = datetime(2026, 8, 31, 16, 0, tzinfo=UTC)
DAY = NOW.date()


def _span(
    *,
    employee_id: int,
    person_name: str,
    status: str = "valid",
    wc_name: str | None = "Dismantler 1",
    start_utc: datetime | None = None,
    end_utc: datetime | None = None,
    attendance_id: int = 91,
) -> LocationSpan:
    start = start_utc or NOW - timedelta(hours=2)
    end = end_utc or NOW + timedelta(minutes=1)
    return LocationSpan(
        employee_odoo_id=employee_id,
        employee_name=person_name,
        start_utc=start,
        end_utc=end,
        status=status,
        app_work_center_name=wc_name,
        odoo_work_center_id=77 if wc_name else None,
        odoo_work_center_name=wc_name,
        attendance_ids=(attendance_id,),
        department_repair=None,
    )


def _location_snapshot(
    spans: tuple[LocationSpan, ...] = (),
    *,
    current_attendance_ids: frozenset[int] = frozenset(),
    mirror_owned: bool = True,
    available: bool = True,
    stale: bool = False,
    verified_cap_utc: datetime | None = None,
):
    return SimpleNamespace(
        policy=SimpleNamespace(
            mirror_owned=mirror_owned,
            available=available,
            stale=stale,
        ),
        spans=spans,
        verified_cap_utc=verified_cap_utc or NOW,
        current_attendance_ids=current_attendance_ids,
    )


def _source(
    *presences: current_operators.OperatorPresence,
    departures=(),
    available=True,
    mirror_owned=True,
    complete=True,
):
    return current_operators.OperatorSourceSnapshot(
        presences=presences,
        departures=departures,
        available=available,
        mirror_owned=mirror_owned,
        complete=complete,
    )


def test_person_planned_here_but_present_elsewhere_is_gray_and_full_strength():
    source = _source(
        current_operators.OperatorPresence("Christian C.", "Dismantler 3", NOW, 8),
    )

    display = current_operators.build_display_by_work_center(
        {"Dismantler 1": ["Christian C."]},
        planned_employee_ids={"Christian C.": 8},
        absent_names=set(),
        source=source,
        is_today=True,
    )

    assert display["Dismantler 1"] == (
        current_operators.OperatorDisplayRow("Christian C.", 8, True, False),
    )
    assert display["Dismantler 3"] == (
        current_operators.OperatorDisplayRow("Christian C.", 8, False, True),
    )


def test_planned_only_shows_gray_without_physical_presence():
    source = _source()

    display = current_operators.build_display_by_work_center(
        {"Repair 1": ["Jose O."]},
        planned_employee_ids={"Jose O.": 11},
        absent_names=set(),
        source=source,
        is_today=True,
    )

    assert display["Repair 1"] == (
        current_operators.OperatorDisplayRow("Jose O.", 11, True, False),
    )


def test_planned_and_present_deduplicates_to_one_full_strength_row():
    source = _source(
        current_operators.OperatorPresence("Jose O.", "Repair 1", NOW, 11),
    )

    display = current_operators.build_display_by_work_center(
        {"Repair 1": ["Jose O."]},
        planned_employee_ids={"Jose O.": 11},
        absent_names=set(),
        source=source,
        is_today=True,
    )

    assert display["Repair 1"] == (
        current_operators.OperatorDisplayRow("Jose O.", 11, True, True),
    )


def test_unplanned_physical_presence_shows_full_strength_only():
    source = _source(
        current_operators.OperatorPresence("Christian C.", "Dismantler 3", NOW, 8),
    )

    display = current_operators.build_display_by_work_center(
        {},
        planned_employee_ids={},
        absent_names=set(),
        source=source,
        is_today=True,
    )

    assert display["Dismantler 3"] == (
        current_operators.OperatorDisplayRow("Christian C.", 8, False, True),
    )


def test_two_present_people_at_one_center():
    source = _source(
        current_operators.OperatorPresence("Alex", "Dismantler 2", NOW, 101),
        current_operators.OperatorPresence("Blake", "Dismantler 2", NOW, 202),
    )

    display = current_operators.build_display_by_work_center(
        {},
        planned_employee_ids={},
        absent_names=set(),
        source=source,
        is_today=True,
    )

    assert display["Dismantler 2"] == (
        current_operators.OperatorDisplayRow("Alex", 101, False, True),
        current_operators.OperatorDisplayRow("Blake", 202, False, True),
    )


def test_same_display_name_with_two_odoo_ids_stays_distinct():
    source = _source(
        current_operators.OperatorPresence("Alex", "Dismantler 2", NOW, 101),
        current_operators.OperatorPresence("Alex", "Dismantler 2", NOW, 202),
    )

    display = current_operators.build_display_by_work_center(
        {"Dismantler 2": ["Alex"]},
        planned_employee_ids={"Alex": 101},
        absent_names=set(),
        source=source,
        is_today=True,
    )

    assert display["Dismantler 2"] == (
        current_operators.OperatorDisplayRow("Alex", 101, True, True),
        current_operators.OperatorDisplayRow("Alex", 202, False, True),
    )


def test_full_day_absence_filters_planned_but_not_physical_presence():
    source = _source(
        current_operators.OperatorPresence("Christian C.", "Dismantler 3", NOW, 8),
    )

    display = current_operators.build_display_by_work_center(
        {
            "Dismantler 1": ["Christian C."],
            "Dismantler 3": ["Other Person"],
        },
        planned_employee_ids={"Christian C.": 8, "Other Person": 9},
        absent_names={"Christian C."},
        source=source,
        is_today=True,
    )

    assert display["Dismantler 1"] == ()
    assert display["Dismantler 3"] == (
        current_operators.OperatorDisplayRow("Other Person", 9, True, False),
        current_operators.OperatorDisplayRow("Christian C.", 8, False, True),
    )


def test_non_today_returns_no_live_display_rows():
    source = _source(
        current_operators.OperatorPresence("Christian C.", "Dismantler 3", NOW, 8),
    )

    display = current_operators.build_display_by_work_center(
        {"Dismantler 1": ["Christian C."]},
        planned_employee_ids={"Christian C.": 8},
        absent_names=set(),
        source=source,
        is_today=False,
    )

    assert display == {}


def test_planned_first_ordering_is_deterministic():
    source = _source(
        current_operators.OperatorPresence("Charlie", "Repair 1", NOW, 33),
        current_operators.OperatorPresence("Bravo", "Repair 1", NOW, 22),
    )

    display = current_operators.build_display_by_work_center(
        {"Repair 1": ["Alpha", "Bravo"]},
        planned_employee_ids={"Alpha": 11, "Bravo": 22},
        absent_names=set(),
        source=source,
        is_today=True,
    )

    assert display["Repair 1"] == (
        current_operators.OperatorDisplayRow("Alpha", 11, True, False),
        current_operators.OperatorDisplayRow("Bravo", 22, True, True),
        current_operators.OperatorDisplayRow("Charlie", 33, False, True),
    )


def test_time_off_key_is_excluded_from_display():
    source = _source(
        current_operators.OperatorPresence("Taylor", "Repair 1", NOW, 44),
    )

    display = current_operators.build_display_by_work_center(
        {staffing.TIME_OFF_KEY: ["Taylor"], "Repair 1": ["Alpha"]},
        planned_employee_ids={"Taylor": 44, "Alpha": 55},
        absent_names=set(),
        source=source,
        is_today=True,
    )

    assert staffing.TIME_OFF_KEY not in display
    assert display["Repair 1"] == (
        current_operators.OperatorDisplayRow("Alpha", 55, True, False),
        current_operators.OperatorDisplayRow("Taylor", 44, False, True),
    )


def test_stale_source_shows_planned_only():
    source = current_operators.OperatorSourceSnapshot(
        presences=(
            current_operators.OperatorPresence("Christian C.", "Dismantler 3", NOW, 8),
        ),
        departures=(),
        available=False,
        mirror_owned=True,
        complete=False,
    )

    display = current_operators.build_display_by_work_center(
        {"Dismantler 1": ["Christian C."]},
        planned_employee_ids={"Christian C.": 8},
        absent_names=set(),
        source=source,
        is_today=True,
    )

    assert display == {
        "Dismantler 1": (
            current_operators.OperatorDisplayRow("Christian C.", 8, True, False),
        ),
    }


@pytest.mark.parametrize(
    "available,mirror_owned",
    [
        (False, True),
        (True, False),
    ],
)
def test_unavailable_or_mirror_off_source_shows_planned_only(
    available, mirror_owned
):
    source = current_operators.OperatorSourceSnapshot(
        presences=(
            current_operators.OperatorPresence("Christian C.", "Dismantler 3", NOW, 8),
        ),
        departures=(),
        available=available,
        mirror_owned=mirror_owned,
        complete=False,
    )

    display = current_operators.build_display_by_work_center(
        {"Dismantler 1": ["Christian C."]},
        planned_employee_ids={"Christian C.": 8},
        absent_names=set(),
        source=source,
        is_today=True,
    )

    assert display == {
        "Dismantler 1": (
            current_operators.OperatorDisplayRow("Christian C.", 8, True, False),
        ),
    }


def test_source_from_location_snapshot_extracts_current_presence():
    span = _span(
        employee_id=101,
        person_name="Juan",
        wc_name="Dismantler 2",
        end_utc=NOW,
        attendance_id=91,
    )
    snapshot = _location_snapshot(
        (span,),
        current_attendance_ids=frozenset({91}),
    )

    source = current_operators.source_from_location_snapshot(snapshot)

    assert source.available is True
    assert source.mirror_owned is True
    assert source.complete is True
    assert source.presences == (
        current_operators.OperatorPresence("Juan", "Dismantler 2", span.start_utc, 101),
    )


def test_source_from_location_snapshot_records_closed_departures():
    transfer_at = NOW - timedelta(minutes=20)
    old_span = _span(
        employee_id=101,
        person_name="Juan",
        wc_name="Dismantler 2",
        end_utc=transfer_at,
        attendance_id=91,
    )
    new_span = _span(
        employee_id=101,
        person_name="Juan",
        wc_name="Repair 3",
        start_utc=transfer_at,
        end_utc=NOW,
        attendance_id=92,
    )
    snapshot = _location_snapshot(
        (old_span, new_span),
        current_attendance_ids=frozenset({92}),
    )

    source = current_operators.source_from_location_snapshot(snapshot)

    assert [(p.wc_name, p.arrival_utc) for p in source.presences] == [
        ("Repair 3", transfer_at)
    ]
    assert [(d.wc_name, d.departure_utc) for d in source.departures] == [
        ("Dismantler 2", transfer_at)
    ]


@pytest.mark.parametrize(
    "invalid_status",
    [
        "pending_first_location",
        "missing_required_location",
        "unmapped_location",
        "conflicting_location",
        "stale_open_location",
    ],
)
def test_source_from_location_snapshot_rejects_invalid_current_spans(invalid_status):
    span = _span(
        employee_id=101,
        person_name="Juan",
        status=invalid_status,
        wc_name=None if "location" in invalid_status else "Dismantler 2",
        end_utc=NOW,
        attendance_id=91,
    )
    snapshot = _location_snapshot(
        (span,),
        current_attendance_ids=frozenset({91}),
    )

    source = current_operators.source_from_location_snapshot(snapshot)

    assert source.presences == ()
    assert source.complete is False


def test_source_from_location_snapshot_skips_exempt_no_location():
    valid = _span(
        employee_id=101,
        person_name="Juan",
        wc_name="Dismantler 2",
        end_utc=NOW,
        attendance_id=91,
    )
    exempt = _span(
        employee_id=202,
        person_name="Taylor",
        status="exempt_no_location",
        wc_name=None,
        end_utc=NOW,
        attendance_id=92,
    )
    snapshot = _location_snapshot(
        (valid, exempt),
        current_attendance_ids=frozenset({91, 92}),
    )

    source = current_operators.source_from_location_snapshot(snapshot)

    assert source.complete is True
    assert [operator.employee_odoo_id for operator in source.presences] == [101]


@pytest.mark.parametrize(
    ("available", "stale"),
    [(False, False), (True, True)],
)
def test_source_from_location_snapshot_never_uses_unavailable_or_stale(available, stale):
    span = _span(
        employee_id=101,
        person_name="Juan",
        wc_name="Dismantler 2",
        end_utc=NOW,
        attendance_id=91,
    )
    snapshot = _location_snapshot(
        (span,),
        current_attendance_ids=frozenset({91}),
        available=available,
        stale=stale,
    )

    source = current_operators.source_from_location_snapshot(snapshot)

    assert source.presences == ()
    assert source.available is False
    assert source.mirror_owned is True


def test_source_from_location_snapshot_mirror_off_is_unavailable():
    span = _span(
        employee_id=101,
        person_name="Juan",
        wc_name="Dismantler 2",
        end_utc=NOW,
        attendance_id=91,
    )
    snapshot = _location_snapshot(
        (span,),
        current_attendance_ids=frozenset({91}),
        mirror_owned=False,
    )

    source = current_operators.source_from_location_snapshot(snapshot)

    assert source.presences == ()
    assert source.available is False
    assert source.mirror_owned is False


def test_source_from_location_snapshot_keeps_distinct_employee_ids_with_same_name():
    one = _span(
        employee_id=101,
        person_name="Alex",
        wc_name="Dismantler 2",
        end_utc=NOW,
        attendance_id=91,
    )
    two = _span(
        employee_id=202,
        person_name="Alex",
        wc_name="Dismantler 2",
        start_utc=NOW - timedelta(hours=1),
        end_utc=NOW,
        attendance_id=92,
    )
    snapshot = _location_snapshot(
        (one, two),
        current_attendance_ids=frozenset({91, 92}),
    )

    source = current_operators.source_from_location_snapshot(snapshot)

    assert [operator.employee_odoo_id for operator in source.presences] == [101, 202]
