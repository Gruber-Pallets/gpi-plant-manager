from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta, timezone

import pytest

from zira_dashboard import attendance_mirror, attendance_timeline
from zira_dashboard.attendance_timeline import LocationSpan


BASE = datetime(2026, 8, 29, 8, 0, tzinfo=UTC)


def at(hours: int = 0, minutes: int = 0, seconds: int = 0) -> datetime:
    return BASE + timedelta(hours=hours, minutes=minutes, seconds=seconds)


def row(
    attendance_id: int = 101,
    *,
    employee_id: int = 41,
    employee_name: str = "Adrian A.",
    check_in: datetime = BASE,
    check_out: datetime | None = None,
    work_center_id: int | None = 71,
    work_center_name: str | None = "Odoo Repair One",
    department_id: int | None = 8,
    department_name: str | None = "01 Recycled",
    write_date: datetime = datetime(2026, 8, 29, 7, 59, tzinfo=UTC),
) -> dict[str, object]:
    """A complete normalized mirror row, including every source field."""
    return {
        "odoo_attendance_id": attendance_id,
        "employee_odoo_id": employee_id,
        "employee_name": employee_name,
        "check_in_utc": check_in,
        "check_out_utc": check_out,
        "odoo_work_center_id": work_center_id,
        "odoo_work_center_name": work_center_name,
        "odoo_department_id": department_id,
        "odoo_department_name": department_name,
        "odoo_write_date": write_date,
    }


def project(
    rows,
    *,
    as_of: datetime = at(3),
    verified: datetime = at(3),
    mapping: dict[int, str] | None = None,
    required_departments: set[str | None] | None = None,
    expected_departments: dict[str, int | None] | None = None,
    grace: timedelta = timedelta(minutes=5),
    stale_after: timedelta = timedelta(seconds=90),
) -> tuple[LocationSpan, ...]:
    mapping = {71: "Repair 1"} if mapping is None else mapping
    required_departments = {"01 Recycled"} if required_departments is None else required_departments
    expected_departments = {"Repair 1": 8} if expected_departments is None else expected_departments
    return attendance_timeline.project_rows(
        rows,
        as_of_utc=as_of,
        verified_through_utc=verified,
        map_work_center=mapping.get,
        requires_work_center=lambda name: name in required_departments,
        expected_department_id=expected_departments.get,
        grace=grace,
        stale_after=stale_after,
    )


def expected_span(
    start: datetime,
    end: datetime,
    status: attendance_timeline.LocationStatus,
    *,
    employee_id: int = 41,
    employee_name: str = "Adrian A.",
    app_work_center_name: str | None = None,
    odoo_work_center_id: int | None = None,
    odoo_work_center_name: str | None = None,
    attendance_ids: tuple[int, ...] = (101,),
    department_repair: tuple[int, int, datetime] | None = None,
) -> LocationSpan:
    return LocationSpan(
        employee_odoo_id=employee_id,
        employee_name=employee_name,
        start_utc=start,
        end_utc=end,
        status=status,
        app_work_center_name=app_work_center_name,
        odoo_work_center_id=odoo_work_center_id,
        odoo_work_center_name=odoo_work_center_name,
        attendance_ids=attendance_ids,
        department_repair=department_repair,
    )


def test_location_span_is_a_frozen_typed_value_and_mapped_location_is_valid():
    spans = project([row(check_out=at(1))])

    assert spans == (
        expected_span(
            at(),
            at(1),
            "valid",
            app_work_center_name="Repair 1",
            odoo_work_center_id=71,
            odoo_work_center_name="Odoo Repair One",
        ),
    )
    assert spans[0].start_utc.tzinfo is UTC
    assert spans[0].end_utc.tzinfo is UTC
    with pytest.raises(FrozenInstanceError):
        spans[0].status = "conflicting_location"  # type: ignore[misc]


def test_required_first_location_grace_splits_at_exactly_five_minutes():
    spans = project(
        [
            row(
                check_out=at(minutes=10),
                work_center_id=None,
                work_center_name=None,
            )
        ]
    )

    assert spans == (
        expected_span(at(), at(minutes=5), "pending_first_location"),
        expected_span(at(minutes=5), at(minutes=10), "missing_required_location"),
    )


def test_exempt_no_location_does_not_split_or_become_missing_after_grace():
    spans = project(
        [
            row(
                check_out=at(minutes=20),
                work_center_id=None,
                work_center_name=None,
                department_name="Maintenance",
            )
        ],
        required_departments=set(),
    )

    assert spans == (expected_span(at(), at(minutes=20), "exempt_no_location"),)


def test_later_work_center_less_coverage_is_missing_immediately_after_any_wc():
    spans = project(
        [
            row(check_out=at(1)),
            row(
                102,
                check_in=at(1),
                check_out=at(2),
                work_center_id=None,
                work_center_name=None,
            ),
        ]
    )

    assert spans == (
        expected_span(
            at(),
            at(1),
            "valid",
            app_work_center_name="Repair 1",
            odoo_work_center_id=71,
            odoo_work_center_name="Odoo Repair One",
        ),
        expected_span(
            at(1),
            at(2),
            "missing_required_location",
            attendance_ids=(102,),
        ),
    )


def test_first_wc_starting_at_grace_boundary_leaves_no_zero_length_missing_span():
    spans = project(
        [
            row(
                check_out=at(minutes=5),
                work_center_id=None,
                work_center_name=None,
            ),
            row(102, check_in=at(minutes=5), check_out=at(1)),
        ]
    )

    assert spans == (
        expected_span(at(), at(minutes=5), "pending_first_location"),
        expected_span(
            at(minutes=5),
            at(1),
            "valid",
            app_work_center_name="Repair 1",
            odoo_work_center_id=71,
            odoo_work_center_name="Odoo Repair One",
            attendance_ids=(102,),
        ),
    )
    assert all(span.end_utc > span.start_utc for span in spans)


def test_required_policy_wins_only_during_overlapping_no_location_coverage():
    spans = project(
        [
            row(
                check_out=at(minutes=20),
                work_center_id=None,
                work_center_name=None,
                department_name="Maintenance",
            ),
            row(
                102,
                check_in=at(minutes=10),
                check_out=at(minutes=15),
                work_center_id=None,
                work_center_name=None,
            ),
        ],
        required_departments={"01 Recycled"},
    )

    assert spans == (
        expected_span(at(), at(minutes=10), "exempt_no_location"),
        expected_span(
            at(minutes=10),
            at(minutes=15),
            "missing_required_location",
            attendance_ids=(101, 102),
        ),
        expected_span(
            at(minutes=15),
            at(minutes=20),
            "exempt_no_location",
        ),
    )


def test_unmapped_location_preserves_exact_raw_odoo_identity_and_unknown_name():
    spans = project(
        [
            row(
                check_out=at(1),
                work_center_id=404,
                work_center_name="Unknown Odoo / Hand Build Zeta",
            )
        ],
        mapping={},
    )

    assert spans == (
        expected_span(
            at(),
            at(1),
            "unmapped_location",
            odoo_work_center_id=404,
            odoo_work_center_name="Unknown Odoo / Hand Build Zeta",
        ),
    )


def test_same_wc_duplicate_overlap_collapses_once_with_atomic_shoulders():
    spans = project(
        [
            row(102, check_in=at(1), check_out=at(2), write_date=at(minutes=-1)),
            row(101, check_out=at(3), write_date=at(minutes=-2)),
        ]
    )

    assert spans == (
        expected_span(
            at(),
            at(1),
            "valid",
            app_work_center_name="Repair 1",
            odoo_work_center_id=71,
            odoo_work_center_name="Odoo Repair One",
        ),
        expected_span(
            at(1),
            at(2),
            "valid",
            app_work_center_name="Repair 1",
            odoo_work_center_id=71,
            odoo_work_center_name="Odoo Repair One",
            attendance_ids=(101, 102),
        ),
        expected_span(
            at(2),
            at(3),
            "valid",
            app_work_center_name="Repair 1",
            odoo_work_center_id=71,
            odoo_work_center_name="Odoo Repair One",
        ),
    )


def test_same_wc_duplicate_labels_use_newest_source_version_deterministically():
    older = row(
        101,
        check_out=at(1),
        work_center_name="Old Odoo Label",
        write_date=at(minutes=-3),
    )
    newer = row(
        102,
        check_out=at(1),
        work_center_name="New Odoo Label",
        write_date=at(minutes=-1),
    )

    forward = project([older, newer])
    reverse = project([newer, older])

    expected = expected_span(
        at(),
        at(1),
        "valid",
        app_work_center_name="Repair 1",
        odoo_work_center_id=71,
        odoo_work_center_name="New Odoo Label",
        attendance_ids=(101, 102),
    )
    assert forward == (expected,)
    assert reverse == (expected,)


def test_distinct_wc_overlap_is_conflicting_without_credit_and_keeps_shoulders():
    spans = project(
        [
            row(check_out=at(3)),
            row(
                102,
                check_in=at(1),
                check_out=at(2),
                work_center_id=72,
                work_center_name="Odoo Repair Two",
            ),
        ],
        mapping={71: "Repair 1", 72: "Repair 2"},
        expected_departments={"Repair 1": 8, "Repair 2": 8},
    )

    assert spans == (
        expected_span(
            at(),
            at(1),
            "valid",
            app_work_center_name="Repair 1",
            odoo_work_center_id=71,
            odoo_work_center_name="Odoo Repair One",
        ),
        expected_span(
            at(1),
            at(2),
            "conflicting_location",
            attendance_ids=(101, 102),
        ),
        expected_span(
            at(2),
            at(3),
            "valid",
            app_work_center_name="Repair 1",
            odoo_work_center_id=71,
            odoo_work_center_name="Odoo Repair One",
        ),
    )


def test_adjacent_rows_keep_distinct_source_identity_and_gap_is_not_filled():
    spans = project(
        [
            row(check_out=at(1)),
            row(102, check_in=at(1), check_out=at(2)),
            row(103, check_in=at(2, 10), check_out=at(3)),
        ]
    )

    assert [(span.start_utc, span.end_utc, span.attendance_ids) for span in spans] == [
        (at(), at(1), (101,)),
        (at(1), at(2), (102,)),
        (at(2, 10), at(3), (103,)),
    ]
    assert not any(span.start_utc < at(2, 10) < span.end_utc for span in spans)


def test_fresh_open_row_ends_exactly_at_as_of_and_threshold_equality_is_fresh():
    spans = project(
        [row()],
        as_of=at(2),
        verified=at(1, 58, 30),
        stale_after=timedelta(seconds=90),
    )

    assert spans == (
        expected_span(
            at(),
            at(2),
            "valid",
            app_work_center_name="Repair 1",
            odoo_work_center_id=71,
            odoo_work_center_name="Odoo Repair One",
        ),
    )


def test_stale_open_row_splits_at_verified_boundary_and_withholds_app_wc():
    spans = project(
        [row()],
        as_of=at(2),
        verified=at(1, 58),
        stale_after=timedelta(seconds=90),
    )

    assert spans == (
        expected_span(
            at(),
            at(1, 58),
            "valid",
            app_work_center_name="Repair 1",
            odoo_work_center_id=71,
            odoo_work_center_name="Odoo Repair One",
        ),
        expected_span(
            at(1, 58),
            at(2),
            "stale_open_location",
            odoo_work_center_id=71,
            odoo_work_center_name="Odoo Repair One",
        ),
    )


def test_stale_verified_boundary_before_row_start_clamps_to_whole_stale_row():
    spans = project(
        [row(check_in=at(1))],
        as_of=at(2),
        verified=at(minutes=30),
    )

    assert spans == (
        expected_span(
            at(1),
            at(2),
            "stale_open_location",
            odoo_work_center_id=71,
            odoo_work_center_name="Odoo Repair One",
        ),
    )


def test_verified_boundary_after_as_of_is_clamped_without_future_output():
    spans = project([row()], as_of=at(1), verified=at(2))

    assert spans == (
        expected_span(
            at(),
            at(1),
            "valid",
            app_work_center_name="Repair 1",
            odoo_work_center_id=71,
            odoo_work_center_name="Odoo Repair One",
        ),
    )


def test_open_row_starting_at_or_after_as_of_emits_no_fabricated_time():
    assert project([row(check_in=at(1))], as_of=at(1), verified=at(1)) == ()
    assert project([row(check_in=at(2))], as_of=at(1), verified=at(1)) == ()


def test_closed_historical_row_never_becomes_stale():
    spans = project([row(check_out=at(1))], as_of=at(3), verified=at())

    assert spans == (
        expected_span(
            at(),
            at(1),
            "valid",
            app_work_center_name="Repair 1",
            odoo_work_center_id=71,
            odoo_work_center_name="Odoo Repair One",
        ),
    )


def test_cross_midnight_source_uses_exact_utc_and_grace_boundaries():
    start = datetime(2026, 8, 30, 4, 58, tzinfo=UTC)
    end = datetime(2026, 8, 30, 5, 10, tzinfo=UTC)
    local_midnight = datetime(2026, 8, 30, 5, 0, tzinfo=UTC)
    spans = project(
        [
            row(
                check_in=start,
                check_out=end,
                work_center_id=None,
                work_center_name=None,
            )
        ],
        as_of=end,
        verified=end,
    )

    assert spans == (
        expected_span(
            start,
            local_midnight + timedelta(minutes=5),
            "pending_first_location",
        ),
        expected_span(
            local_midnight + timedelta(minutes=5),
            end,
            "missing_required_location",
        ),
    )


def test_first_location_grace_restarts_on_each_plant_calendar_day():
    day_two = BASE + timedelta(days=1)
    day_two_end = day_two + timedelta(minutes=10)

    spans = project(
        [
            row(check_out=at(1)),
            row(
                102,
                check_in=day_two,
                check_out=day_two_end,
                work_center_id=None,
                work_center_name=None,
            ),
        ],
        as_of=day_two_end,
        verified=day_two_end,
    )

    assert spans[-2:] == (
        expected_span(
            day_two,
            day_two + timedelta(minutes=5),
            "pending_first_location",
            attendance_ids=(102,),
        ),
        expected_span(
            day_two + timedelta(minutes=5),
            day_two_end,
            "missing_required_location",
            attendance_ids=(102,),
        ),
    )


def test_department_mismatch_keeps_valid_wc_and_requests_versioned_repair():
    version = at(minutes=-1)
    spans = project(
        [row(check_out=at(1), department_id=9, write_date=version)],
        expected_departments={"Repair 1": 8},
    )

    assert spans == (
        expected_span(
            at(),
            at(1),
            "valid",
            app_work_center_name="Repair 1",
            odoo_work_center_id=71,
            odoo_work_center_name="Odoo Repair One",
            department_repair=(101, 8, version),
        ),
    )


@pytest.mark.parametrize("observed", [8, None])
def test_department_repair_is_omitted_when_already_matching_or_target_unavailable(
    observed,
):
    expected = {"Repair 1": 8} if observed == 8 else {"Repair 1": None}
    spans = project(
        [row(check_out=at(1), department_id=observed)],
        expected_departments=expected,
    )

    assert spans[0].status == "valid"
    assert spans[0].department_repair is None


def test_one_mismatched_duplicate_source_has_deterministic_repair():
    version = at(minutes=-1)
    spans = project(
        [
            row(102, check_out=at(1), department_id=8),
            row(101, check_out=at(1), department_id=9, write_date=version),
        ]
    )

    assert spans == (
        expected_span(
            at(),
            at(1),
            "valid",
            app_work_center_name="Repair 1",
            odoo_work_center_id=71,
            odoo_work_center_name="Odoo Repair One",
            attendance_ids=(101, 102),
            department_repair=(101, 8, version),
        ),
    )


def test_multiple_duplicate_repairs_fail_visibly_without_losing_work():
    with pytest.raises(
        ValueError,
        match=r"multiple department repairs.*101, 102",
    ):
        project(
            [
                row(102, check_out=at(1), department_id=10),
                row(101, check_out=at(1), department_id=9),
            ]
        )


@pytest.mark.parametrize(
    ("field", "value", "error"),
    [
        ("odoo_attendance_id", True, "odoo_attendance_id"),
        ("employee_odoo_id", False, "employee_odoo_id"),
        ("odoo_work_center_id", True, "odoo_work_center_id"),
        ("odoo_department_id", False, "odoo_department_id"),
        ("odoo_attendance_id", 0, "odoo_attendance_id"),
        ("employee_odoo_id", -1, "employee_odoo_id"),
        ("employee_name", None, "employee_name"),
        ("odoo_work_center_name", 7, "odoo_work_center_name"),
        ("odoo_department_name", 8, "odoo_department_name"),
    ],
)
def test_invalid_row_field_shapes_are_rejected(field, value, error):
    bad = row(check_out=at(1))
    bad[field] = value

    with pytest.raises((TypeError, ValueError), match=error):
        project([bad])


def test_rows_must_be_a_sequence_of_complete_mappings():
    with pytest.raises(TypeError, match="rows must be a sequence"):
        project("not rows")
    with pytest.raises(TypeError, match="attendance row must be a mapping"):
        project([object()])

    incomplete = row(check_out=at(1))
    incomplete.pop("odoo_write_date")
    with pytest.raises(ValueError, match="odoo_write_date"):
        project([incomplete])


def test_duplicate_attendance_id_and_inconsistent_employee_identity_are_rejected():
    with pytest.raises(ValueError, match="duplicate odoo_attendance_id 101"):
        project([row(check_out=at(1)), row(check_out=at(2))])

    with pytest.raises(ValueError, match="inconsistent employee identity"):
        project(
            [
                row(check_out=at(1)),
                row(102, check_in=at(1), check_out=at(2), employee_name="Changed"),
            ]
        )


@pytest.mark.parametrize(
    ("check_out", "message"),
    [
        (at(minutes=-1), "after check_in_utc"),
        (at(), "after check_in_utc"),
    ],
)
def test_reversed_or_zero_source_intervals_are_rejected(check_out, message):
    with pytest.raises(ValueError, match=message):
        project([row(check_out=check_out)])


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("check_in_utc", datetime(2026, 8, 29, 8)),
        ("check_out_utc", datetime(2026, 8, 29, 9)),
        ("odoo_write_date", datetime(2026, 8, 29, 7, 59)),
        (
            "check_in_utc",
            datetime(2026, 8, 29, 9, tzinfo=timezone(timedelta(hours=1))),
        ),
    ],
)
def test_row_datetimes_must_be_aware_utc(field, value):
    bad = row(check_out=at(1))
    bad[field] = value

    with pytest.raises((TypeError, ValueError), match="aware UTC"):
        project([bad])


@pytest.mark.parametrize("argument", ["as_of", "verified"])
@pytest.mark.parametrize(
    "value",
    [
        datetime(2026, 8, 29, 11),
        datetime(2026, 8, 29, 12, tzinfo=timezone(timedelta(hours=1))),
    ],
)
def test_projection_boundaries_must_be_aware_utc(argument, value):
    kwargs = {argument: value}
    with pytest.raises((TypeError, ValueError), match="aware UTC"):
        project([row(check_out=at(1))], **kwargs)


@pytest.mark.parametrize(
    ("argument", "value"),
    [
        ("grace", -timedelta(microseconds=1)),
        ("stale_after", -timedelta(microseconds=1)),
        ("grace", 5),
        ("stale_after", 90),
    ],
)
def test_grace_and_stale_durations_must_be_nonnegative_timedeltas(argument, value):
    kwargs = {argument: value}
    with pytest.raises((TypeError, ValueError), match=argument):
        project([row(check_out=at(1))], **kwargs)


def test_zero_grace_is_valid_and_produces_only_missing_required_location():
    spans = project(
        [
            row(
                check_out=at(minutes=1),
                work_center_id=None,
                work_center_name=None,
            )
        ],
        grace=timedelta(0),
    )
    assert spans == (expected_span(at(), at(minutes=1), "missing_required_location"),)


def test_monthly_employee_without_location_is_exempt_in_required_department():
    source = row(
        check_out=at(minutes=1),
        work_center_id=None,
        work_center_name=None,
    )
    source["employee_wage_type"] = "monthly"

    spans = project([source], grace=timedelta(0))

    assert spans == (expected_span(at(), at(minutes=1), "exempt_no_location"),)


@pytest.mark.parametrize("wage_type", ["hourly", None, "unexpected"])
def test_nonmonthly_employee_without_location_remains_required(wage_type):
    source = row(
        check_out=at(minutes=1),
        work_center_id=None,
        work_center_name=None,
    )
    source["employee_wage_type"] = wage_type

    spans = project([source], grace=timedelta(0))

    assert spans == (expected_span(at(), at(minutes=1), "missing_required_location"),)


def test_monthly_employee_with_mapped_location_remains_valid():
    source = row(check_out=at(minutes=1))
    source["employee_wage_type"] = "monthly"

    spans = project([source])

    assert spans == (
        expected_span(
            at(),
            at(minutes=1),
            "valid",
            app_work_center_name="Repair 1",
            odoo_work_center_id=71,
            odoo_work_center_name="Odoo Repair One",
        ),
    )


def test_dependency_results_are_runtime_validated():
    kwargs = {
        "as_of_utc": at(1),
        "verified_through_utc": at(1),
        "grace": timedelta(minutes=5),
        "stale_after": timedelta(seconds=90),
    }
    with pytest.raises(TypeError, match="map_work_center"):
        attendance_timeline.project_rows(
            [row(check_out=at(1))],
            map_work_center=lambda _wc_id: 7,
            requires_work_center=lambda _department: True,
            expected_department_id=lambda _app_wc: 8,
            **kwargs,
        )
    with pytest.raises(TypeError, match="requires_work_center"):
        attendance_timeline.project_rows(
            [
                row(
                    check_out=at(1),
                    work_center_id=None,
                    work_center_name=None,
                )
            ],
            map_work_center=lambda _wc_id: None,
            requires_work_center=lambda _department: 1,
            expected_department_id=lambda _app_wc: None,
            **kwargs,
        )
    with pytest.raises(TypeError, match="expected_department_id"):
        attendance_timeline.project_rows(
            [row(check_out=at(1))],
            map_work_center=lambda _wc_id: "Repair 1",
            requires_work_center=lambda _department: True,
            expected_department_id=lambda _app_wc: True,
            **kwargs,
        )


def test_stable_order_is_employee_id_then_time_regardless_of_input_order():
    spans = project(
        [
            row(
                202,
                employee_id=52,
                employee_name="Zed Z.",
                check_in=at(1),
                check_out=at(2),
            ),
            row(102, check_in=at(1), check_out=at(2)),
            row(201, employee_id=52, employee_name="Zed Z.", check_out=at(1)),
            row(101, check_out=at(1)),
        ]
    )

    assert [(span.employee_odoo_id, span.start_utc, span.attendance_ids) for span in spans] == [
        (41, at(), (101,)),
        (41, at(1), (102,)),
        (52, at(), (201,)),
        (52, at(1), (202,)),
    ]


def test_timeline_for_range_reads_active_mirror_health_and_clips_half_open_range(
    monkeypatch,
):
    source = row(check_in=at(minutes=-30), check_out=at(3))
    calls: list[tuple[datetime, datetime]] = []
    mapped: list[int] = []
    required: list[str | None] = []
    expected: list[str] = []
    monkeypatch.setattr(
        attendance_timeline.attendance_mirror,
        "rows_overlapping",
        lambda start, end: calls.append((start, end)) or (source,),
    )
    monkeypatch.setattr(
        attendance_timeline.attendance_mirror,
        "health_snapshot",
        lambda: attendance_mirror.MirrorHealth(at(2), at(2), at(2), None, None),
    )
    monkeypatch.setattr(
        attendance_timeline.work_centers_store,
        "app_work_center_name_for_odoo_id",
        lambda wc_id: mapped.append(wc_id) or "Repair 1",
    )
    monkeypatch.setattr(
        attendance_timeline.attendance_location_policy,
        "department_requires_work_center",
        lambda name: required.append(name) or True,
    )
    monkeypatch.setattr(
        attendance_timeline,
        "_expected_department_id_for_app_work_center",
        lambda name: expected.append(name) or 8,
    )

    spans = attendance_timeline.timeline_for_range(at(), at(2), as_of_utc=at(2))

    assert calls == [(datetime(2026, 8, 29, 5, 0, tzinfo=UTC), at(2))]
    assert mapped == [71]
    assert required == []
    assert expected == ["Repair 1"]
    assert spans == (
        expected_span(
            at(),
            at(2),
            "valid",
            app_work_center_name="Repair 1",
            odoo_work_center_id=71,
            odoo_work_center_name="Odoo Repair One",
        ),
    )


def test_timeline_for_partial_day_reads_prior_day_context_before_clipping(
    monkeypatch,
):
    day_start = datetime(2026, 8, 29, 5, 0, tzinfo=UTC)
    earlier_wc = row(
        check_in=datetime(2026, 8, 29, 6, 0, tzinfo=UTC),
        check_out=datetime(2026, 8, 29, 7, 0, tzinfo=UTC),
    )
    requested_no_wc = row(
        102,
        check_in=at(),
        check_out=at(minutes=10),
        work_center_id=None,
        work_center_name=None,
    )
    calls: list[tuple[datetime, datetime]] = []
    monkeypatch.setattr(
        attendance_timeline.attendance_mirror,
        "rows_overlapping",
        lambda start, end: (
            calls.append((start, end))
            or ((earlier_wc, requested_no_wc) if start == day_start else (requested_no_wc,))
        ),
    )
    monkeypatch.setattr(
        attendance_timeline.attendance_mirror,
        "health_snapshot",
        lambda: attendance_mirror.MirrorHealth(at(minutes=10), at(), at(), None, None),
    )
    monkeypatch.setattr(
        attendance_timeline.work_centers_store,
        "app_work_center_name_for_odoo_id",
        lambda _wc_id: "Repair 1",
    )
    monkeypatch.setattr(
        attendance_timeline.attendance_location_policy,
        "department_requires_work_center",
        lambda _name: True,
    )

    spans = attendance_timeline.timeline_for_range(at(), at(minutes=10), as_of_utc=at(minutes=10))

    assert calls == [(day_start, at(minutes=10))]
    assert spans == (
        expected_span(
            at(),
            at(minutes=10),
            "missing_required_location",
            attendance_ids=(102,),
        ),
    )


def test_timeline_for_range_normalizes_numbered_odoo_department_for_saved_policy(
    monkeypatch,
):
    source = row(
        check_out=at(minutes=10),
        work_center_id=None,
        work_center_name=None,
        department_name="00 Maintenance",
    )
    seen_departments: list[str | None] = []
    monkeypatch.setattr(
        attendance_timeline.attendance_mirror,
        "health_snapshot",
        lambda: attendance_mirror.MirrorHealth(at(minutes=10), at(), at(), None, None),
    )
    monkeypatch.setattr(
        attendance_timeline.attendance_mirror,
        "rows_overlapping",
        lambda _start, _end: (source,),
    )
    monkeypatch.setattr(
        attendance_timeline,
        "db",
        type(
            "NoFallbackDb",
            (),
            {"query": staticmethod(lambda *_args: pytest.fail("fallback queried"))},
        ),
        raising=False,
    )
    monkeypatch.setattr(
        attendance_timeline.attendance_location_policy,
        "department_requires_work_center",
        lambda name: seen_departments.append(name) or name != "Maintenance",
    )

    spans = attendance_timeline.timeline_for_range(at(), at(minutes=10), as_of_utc=at(minutes=10))

    assert seen_departments
    assert set(seen_departments) == {"Maintenance"}
    assert spans == (expected_span(at(), at(minutes=10), "exempt_no_location"),)


def test_timeline_uses_employee_department_when_attendance_department_is_blank(
    monkeypatch,
):
    source = row(
        check_out=at(minutes=10),
        work_center_id=None,
        work_center_name=None,
        department_id=None,
        department_name=None,
    )
    monkeypatch.setattr(
        attendance_timeline.attendance_mirror,
        "health_snapshot",
        lambda: attendance_mirror.MirrorHealth(at(minutes=10), at(), at(), None, None),
    )
    monkeypatch.setattr(
        attendance_timeline.attendance_mirror,
        "rows_overlapping",
        lambda _start, _end: (source,),
    )
    monkeypatch.setattr(
        attendance_timeline,
        "db",
        type(
            "EmployeeDepartmentDb",
            (),
            {
                "query": staticmethod(
                    lambda _sql, _params: [
                        {
                            "odoo_id": 41,
                            "department_name": "Transportation",
                        }
                    ]
                )
            },
        ),
        raising=False,
    )
    monkeypatch.setattr(
        attendance_timeline.attendance_location_policy,
        "department_requires_work_center",
        attendance_timeline.attendance_location_policy.default_department_requires_work_center,
    )

    spans = attendance_timeline.timeline_for_range(at(), at(minutes=10), as_of_utc=at(minutes=10))

    assert spans == (expected_span(at(), at(minutes=10), "exempt_no_location"),)


def test_timeline_for_range_uses_health_freshness_for_stale_open_suffix(
    monkeypatch,
):
    monkeypatch.setattr(
        attendance_timeline.attendance_mirror,
        "rows_overlapping",
        lambda _start, _end: (row(),),
    )
    monkeypatch.setattr(
        attendance_timeline.attendance_mirror,
        "health_snapshot",
        lambda: attendance_mirror.MirrorHealth(at(1, 58), at(1), at(1), None, None),
    )
    monkeypatch.setattr(
        attendance_timeline.work_centers_store,
        "app_work_center_name_for_odoo_id",
        lambda _wc_id: "Repair 1",
    )
    monkeypatch.setattr(
        attendance_timeline,
        "_expected_department_id_for_app_work_center",
        lambda _name: 8,
    )

    spans = attendance_timeline.timeline_for_range(at(), at(2), as_of_utc=at(2))

    assert [span.status for span in spans] == ["valid", "stale_open_location"]
    assert [span.end_utc for span in spans] == [at(1, 58), at(2)]


def test_timeline_for_range_requires_verified_health_when_rows_exist(monkeypatch):
    monkeypatch.setattr(
        attendance_timeline.attendance_mirror,
        "rows_overlapping",
        lambda _start, _end: (row(check_out=at(1)),),
    )
    monkeypatch.setattr(
        attendance_timeline.attendance_mirror,
        "health_snapshot",
        lambda: attendance_mirror.MirrorHealth(None, None, None, None, None),
    )

    with pytest.raises(RuntimeError, match="no verified freshness"):
        attendance_timeline.timeline_for_range(at(), at(1), as_of_utc=at(1))


def test_timeline_for_range_snapshots_health_before_reading_rows(monkeypatch):
    order: list[str] = []
    monkeypatch.setattr(
        attendance_timeline.attendance_mirror,
        "health_snapshot",
        lambda: (
            order.append("health") or attendance_mirror.MirrorHealth(at(1), at(), at(), None, None)
        ),
    )
    monkeypatch.setattr(
        attendance_timeline.attendance_mirror,
        "rows_overlapping",
        lambda _start, _end: order.append("rows") or (row(check_out=at(1)),),
    )
    monkeypatch.setattr(
        attendance_timeline.work_centers_store,
        "app_work_center_name_for_odoo_id",
        lambda _wc_id: "Repair 1",
    )

    attendance_timeline.timeline_for_range(at(), at(1), as_of_utc=at(1))

    assert order == ["health", "rows"]


def test_timeline_for_empty_range_ignores_missing_mirror_freshness(monkeypatch):
    order: list[str] = []
    monkeypatch.setattr(
        attendance_timeline.attendance_mirror,
        "rows_overlapping",
        lambda _start, _end: order.append("rows") or (),
    )
    monkeypatch.setattr(
        attendance_timeline.attendance_mirror,
        "health_snapshot",
        lambda: (
            order.append("health") or attendance_mirror.MirrorHealth(None, None, None, None, None)
        ),
    )

    assert attendance_timeline.timeline_for_range(at(), at(1), as_of_utc=at(1)) == ()
    assert order == ["health", "rows"]


@pytest.mark.parametrize(
    ("start", "end", "as_of", "message"),
    [
        (at(), at(), at(), "end_utc must be after start_utc"),
        (at(1), at(), at(1), "end_utc must be after start_utc"),
        (datetime(2026, 8, 29, 8), at(1), at(1), "aware UTC"),
        (
            datetime(2026, 8, 29, 9, tzinfo=timezone(timedelta(hours=1))),
            at(1),
            at(1),
            "aware UTC",
        ),
        (at(), at(1), datetime(2026, 8, 29, 9), "aware UTC"),
    ],
)
def test_timeline_for_range_validates_utc_half_open_boundaries_before_reading(
    monkeypatch, start, end, as_of, message
):
    monkeypatch.setattr(
        attendance_timeline.attendance_mirror,
        "rows_overlapping",
        lambda _start, _end: pytest.fail("invalid range reached mirror read"),
    )

    with pytest.raises((TypeError, ValueError), match=message):
        attendance_timeline.timeline_for_range(start, end, as_of_utc=as_of)
