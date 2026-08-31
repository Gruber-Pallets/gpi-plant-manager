from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime, timedelta, timezone
from itertools import permutations

import pytest

from zira_dashboard.attendance_corrections import (
    CorrectionOperation,
    CorrectionPlan,
    SourceVersion,
    plan_correction,
    plan_from_json,
    plan_to_json,
)


EMPLOYEE = 44
WORK_CENTER = 72
DEPARTMENT = 8
DAY = datetime(2026, 8, 31, tzinfo=UTC)


def at(hour: int, minute: int = 0) -> datetime:
    return DAY + timedelta(hours=hour, minutes=minute)


def row(
    attendance_id: int,
    start: datetime,
    end: datetime | None,
    *,
    employee: int = EMPLOYEE,
    work_center: int | None = 11,
    department: int | None = 3,
    write_minute: int | None = None,
    **extra: object,
) -> dict[str, object]:
    return {
        "odoo_attendance_id": attendance_id,
        "employee_odoo_id": employee,
        "check_in_utc": start,
        "check_out_utc": end,
        "odoo_work_center_id": work_center,
        "odoo_department_id": department,
        "odoo_write_date": DAY
        + timedelta(minutes=write_minute if write_minute is not None else attendance_id),
        **extra,
    }


def core(
    start: datetime,
    end: datetime | None,
    *,
    work_center: int | None = WORK_CENTER,
    department: int | None = DEPARTMENT,
    attendance_id: int | None = None,
    **extra: object,
) -> dict[str, object]:
    return {
        "odoo_attendance_id": attendance_id,
        "employee_odoo_id": EMPLOYEE,
        "check_in_utc": start,
        "check_out_utc": end,
        "odoo_work_center_id": work_center,
        "odoo_department_id": department,
        **extra,
    }


def planned(
    rows: list[dict[str, object]],
    start: datetime,
    end: datetime | None,
    *,
    employee: int = EMPLOYEE,
    work_center: int = WORK_CENTER,
    department: int | None = DEPARTMENT,
) -> CorrectionPlan:
    return plan_correction(
        rows=rows,
        employee_odoo_id=employee,
        start_utc=start,
        end_utc=end,
        odoo_work_center_id=work_center,
        odoo_department_id=department,
    )


def interval_tuples(plan: CorrectionPlan) -> list[tuple[object, ...]]:
    return [
        (
            item["odoo_attendance_id"],
            item["check_in_utc"],
            item["check_out_utc"],
            item["odoo_work_center_id"],
            item["odoo_department_id"],
        )
        for item in plan.expected_intervals
    ]


def encoded_mapping_set(value: object, field: str, encoded_value: object) -> None:
    assert isinstance(value, dict)
    items = value["items"]
    assert isinstance(items, list)
    items.append([field, encoded_value])
    items.sort(key=lambda item: item[0])


def encoded_mapping_replace(value: object, field: str, encoded_value: object) -> None:
    assert isinstance(value, dict)
    items = value["items"]
    assert isinstance(items, list)
    for item in items:
        if item[0] == field:
            item[1] = encoded_value
            return
    raise AssertionError(f"encoded mapping omitted {field}")


@pytest.mark.parametrize("end", [at(10), None])
def test_no_source_row_creates_exact_requested_interval(end):
    plan = planned([], at(8), end)

    assert interval_tuples(plan) == [(None, at(8), end, WORK_CENTER, DEPARTMENT)]
    assert len(plan.operations) == 1
    operation = plan.operations[0]
    assert operation.kind == "create"
    assert operation.attendance_id is None
    assert operation.before is None
    assert dict(operation.after or {}) == {
        "employee_odoo_id": EMPLOYEE,
        "check_in_utc": at(8),
        "check_out_utc": end,
        "odoo_work_center_id": WORK_CENTER,
        "odoo_department_id": DEPARTMENT,
    }


def test_exact_source_row_reuses_id_and_updates_only_location_fields():
    plan = planned([row(101, at(8), at(10))], at(8), at(10))

    assert interval_tuples(plan) == [(101, at(8), at(10), WORK_CENTER, DEPARTMENT)]
    assert [(op.kind, op.attendance_id) for op in plan.operations] == [("update", 101)]
    assert dict(plan.operations[0].before or {}) == {
        "odoo_work_center_id": 11,
        "odoo_department_id": 3,
    }
    assert dict(plan.operations[0].after or {}) == {
        "odoo_work_center_id": WORK_CENTER,
        "odoo_department_id": DEPARTMENT,
    }


@pytest.mark.parametrize(
    ("start", "end", "expected"),
    [
        (
            at(8),
            at(9),
            [
                (None, at(8), at(9), WORK_CENTER, DEPARTMENT),
                (101, at(9), at(10), 11, 3),
            ],
        ),
        (
            at(9),
            at(10),
            [
                (101, at(8), at(9), 11, 3),
                (None, at(9), at(10), WORK_CENTER, DEPARTMENT),
            ],
        ),
        (
            at(8, 30),
            at(9, 30),
            [
                (101, at(8), at(8, 30), 11, 3),
                (None, at(8, 30), at(9, 30), WORK_CENTER, DEPARTMENT),
                (None, at(9, 30), at(10), 11, 3),
            ],
        ),
    ],
)
def test_single_row_surgery_preserves_left_and_right_shoulders(start, end, expected):
    plan = planned([row(101, at(8), at(10))], start, end)

    assert interval_tuples(plan) == expected
    assert sum(op.kind == "update" for op in plan.operations) == 1
    assert sum(op.kind == "create" for op in plan.operations) == len(expected) - 1
    assert not any(op.kind == "delete" for op in plan.operations)


def test_lunch_gap_is_not_bridged_and_each_covered_group_is_reused():
    plan = planned(
        [row(101, at(8), at(12)), row(102, at(13), at(17))],
        at(9),
        at(16),
    )

    assert interval_tuples(plan) == [
        (101, at(8), at(9), 11, 3),
        (None, at(9), at(12), WORK_CENTER, DEPARTMENT),
        (None, at(13), at(16), WORK_CENTER, DEPARTMENT),
        (102, at(16), at(17), 11, 3),
    ]
    assert not any(
        item["check_in_utc"] == at(12) and item["check_out_utc"] == at(13)
        for item in plan.expected_intervals
    )


def test_adjacent_fully_covered_rows_become_one_interval_with_one_reused_id():
    plan = planned(
        [row(205, at(10), at(12)), row(104, at(8), at(10))],
        at(8),
        at(12),
    )

    assert interval_tuples(plan) == [(104, at(8), at(12), WORK_CENTER, DEPARTMENT)]
    assert [(op.kind, op.attendance_id) for op in plan.operations] == [
        ("update", 104),
        ("delete", 205),
    ]


def test_open_source_and_open_correction_preserve_only_time_before_start():
    plan = planned(
        [row(101, at(8), None)],
        at(10),
        None,
    )

    assert interval_tuples(plan) == [
        (101, at(8), at(10), 11, 3),
        (None, at(10), None, WORK_CENTER, DEPARTMENT),
    ]


def test_open_correction_replaces_all_later_rows_and_gaps():
    plan = planned(
        [row(101, at(8), at(9)), row(102, at(11), at(12))],
        at(10),
        None,
    )

    assert interval_tuples(plan) == [
        (101, at(8), at(9), 11, 3),
        (102, at(10), None, WORK_CENTER, DEPARTMENT),
    ]
    assert [(op.kind, op.attendance_id) for op in plan.operations] == [("update", 102)]


def test_closed_correction_of_open_source_preserves_open_suffix():
    plan = planned([row(101, at(8), None)], at(9), at(10))

    assert interval_tuples(plan) == [
        (101, at(8), at(9), 11, 3),
        (None, at(9), at(10), WORK_CENTER, DEPARTMENT),
        (None, at(10), None, 11, 3),
    ]


def test_closed_correction_reuses_earliest_fully_covered_row_and_boundary_ids():
    plan = planned(
        [
            row(101, at(8), at(9)),
            row(105, at(9), at(9, 30)),
            row(102, at(9, 30), at(10)),
            row(103, at(10), at(11)),
        ],
        at(8, 30),
        at(10, 30),
    )

    assert interval_tuples(plan) == [
        (101, at(8), at(8, 30), 11, 3),
        (105, at(8, 30), at(10, 30), WORK_CENTER, DEPARTMENT),
        (103, at(10, 30), at(11), 11, 3),
    ]
    assert [(operation.kind, operation.attendance_id) for operation in plan.operations] == [
        ("update", 101),
        ("update", 105),
        ("delete", 102),
        ("update", 103),
    ]


@pytest.mark.parametrize("open_end", [False, True])
def test_matching_location_and_time_coverage_is_a_no_op(open_end):
    source_end = None if open_end else at(12)
    request_end = None if open_end else at(10)
    source = row(
        101,
        at(8),
        source_end,
        work_center=WORK_CENTER,
        department=DEPARTMENT,
        note="unchanged",
    )

    plan = planned([source], at(9), request_end)

    assert plan.operations == ()
    assert interval_tuples(plan) == [(101, at(8), source_end, WORK_CENTER, DEPARTMENT)]
    assert plan.expected_intervals[0]["note"] == "unchanged"


def test_separate_employee_calls_produce_independent_plans():
    first = planned([row(101, at(8), at(10))], at(8), at(10))
    second = planned(
        [row(201, at(8), at(10), employee=55)],
        at(8),
        at(10),
        employee=55,
    )

    assert {op.employee_odoo_id for op in first.operations} == {EMPLOYEE}
    assert {op.employee_odoo_id for op in second.operations} == {55}
    assert first.operations[0].key != second.operations[0].key


def test_rows_outside_closed_window_are_unchanged_without_operations():
    source = [
        row(100, at(6), at(7), note="before"),
        row(101, at(8), at(12), note="covered"),
        row(102, at(13), at(14), note="after"),
    ]
    plan = planned(source, at(9), at(10))

    before = plan.expected_intervals[0]
    after = plan.expected_intervals[-1]
    assert before["odoo_attendance_id"] == 100
    assert before["check_in_utc"] == at(6)
    assert before["check_out_utc"] == at(7)
    assert before["note"] == "before"
    assert after["odoo_attendance_id"] == 102
    assert after["check_in_utc"] == at(13)
    assert after["check_out_utc"] == at(14)
    assert after["note"] == "after"
    assert {op.attendance_id for op in plan.operations if op.attendance_id} == {101}


def test_split_shoulders_preserve_extra_raw_fields_without_aliasing():
    tags = ["keep", {"code": 7}]
    source = row(
        101,
        at(8),
        at(12),
        employee_name="Adrian A.",
        odoo_work_center_name="Old Cell",
        raw_tags=tags,
    )
    plan = planned([source], at(9), at(10))
    tags.append("mutated")
    source["employee_name"] = "Changed"

    assert plan.expected_intervals[0]["employee_name"] == "Adrian A."
    assert plan.expected_intervals[0]["raw_tags"] == ("keep", {"code": 7})
    assert "raw_tags" not in plan.expected_intervals[-1]
    with pytest.raises(TypeError):
        plan.expected_intervals[0]["note"] = "nope"  # type: ignore[index]
    with pytest.raises(TypeError):
        plan.expected_intervals[0]._values["note"] = "nope"  # type: ignore[attr-defined,index]
    with pytest.raises(AttributeError):
        plan.expected_intervals[0]._values = {}  # type: ignore[attr-defined]
    with pytest.raises(AttributeError):
        del plan.expected_intervals[0]._values  # type: ignore[attr-defined]
    with pytest.raises(AttributeError):
        plan.request._values = {}  # type: ignore[attr-defined]
    with pytest.raises(AttributeError):
        plan.source_intervals[0]._values = {}  # type: ignore[attr-defined]


def test_created_expected_intervals_are_exactly_replayable_from_create_values():
    plan = planned(
        [row(101, at(8), at(12), raw_payload={"must": "not leak"})],
        at(9),
        at(10),
    )

    created_expected = [
        dict(interval)
        for interval in plan.expected_intervals
        if interval["odoo_attendance_id"] is None
    ]
    created_from_operations = [
        {"odoo_attendance_id": None, **dict(operation.after or {})}
        for operation in plan.operations
        if operation.kind == "create"
    ]
    assert created_expected == created_from_operations
    assert all("raw_payload" not in interval for interval in created_expected)


def test_changed_location_does_not_keep_a_stale_display_name():
    plan = planned(
        [
            row(
                101,
                at(8),
                at(10),
                odoo_work_center_name="Old Cell",
                odoo_department_name="Old Department",
            )
        ],
        at(8),
        at(10),
    )

    assert "odoo_work_center_name" not in plan.expected_intervals[0]
    assert "odoo_department_name" not in plan.expected_intervals[0]


def test_alias_id_and_write_date_shape_is_accepted_and_canonicalized():
    source = row(101, at(8), at(10))
    source["id"] = source.pop("odoo_attendance_id")
    source["write_date"] = source.pop("odoo_write_date")

    plan = planned([source], at(8), at(10))

    assert plan.source_versions == (SourceVersion(101, DAY + timedelta(minutes=101)),)
    assert "id" not in plan.expected_intervals[0]
    assert "write_date" not in plan.expected_intervals[0]


def test_input_order_never_changes_plan_or_operation_keys():
    source = [
        row(301, at(13), at(15)),
        row(101, at(8), at(10)),
        row(201, at(10), at(12)),
    ]
    plans = [planned(list(order), at(8), at(15)) for order in permutations(source)]

    assert all(plan == plans[0] for plan in plans)
    assert all(
        tuple(op.key for op in plan.operations) == tuple(op.key for op in plans[0].operations)
        for plan in plans
    )


@pytest.mark.parametrize(
    ("field", "value", "error"),
    [
        ("employee", True, "employee_odoo_id"),
        ("employee", 0, "employee_odoo_id"),
        ("work_center", False, "odoo_work_center_id"),
        ("work_center", -1, "odoo_work_center_id"),
        ("department", True, "odoo_department_id"),
        ("department", 0, "odoo_department_id"),
    ],
)
def test_requested_ids_must_be_positive_non_bool(field, value, error):
    kwargs = {
        "employee": EMPLOYEE,
        "work_center": WORK_CENTER,
        "department": DEPARTMENT,
    }
    kwargs[field] = value

    with pytest.raises((TypeError, ValueError), match=error):
        planned([], at(8), at(10), **kwargs)


@pytest.mark.parametrize(
    ("start", "end"),
    [
        (datetime(2026, 8, 31, 8), at(10)),
        (at(8), datetime(2026, 8, 31, 10)),
        (datetime(2026, 8, 31, 8, tzinfo=timezone(timedelta(hours=1))), at(10)),
        (at(8), at(8)),
        (at(10), at(8)),
    ],
)
def test_requested_interval_must_be_positive_and_exact_utc(start, end):
    with pytest.raises((TypeError, ValueError)):
        planned([], start, end)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("odoo_attendance_id", True),
        ("odoo_attendance_id", 0),
        ("employee_odoo_id", True),
        ("employee_odoo_id", 0),
        ("odoo_work_center_id", True),
        ("odoo_work_center_id", 0),
        ("odoo_department_id", True),
        ("odoo_department_id", -1),
        ("check_in_utc", datetime(2026, 8, 31, 8)),
        ("odoo_write_date", datetime(2026, 8, 31, 8)),
    ],
)
def test_source_row_rejects_invalid_ids_and_datetimes(field, value):
    source = row(101, at(8), at(10))
    source[field] = value

    with pytest.raises((TypeError, ValueError)):
        planned([source], at(8), at(10))


@pytest.mark.parametrize(
    "missing",
    [
        "odoo_attendance_id",
        "employee_odoo_id",
        "check_in_utc",
        "check_out_utc",
        "odoo_work_center_id",
        "odoo_department_id",
        "odoo_write_date",
    ],
)
def test_source_row_requires_complete_canonical_contract(missing):
    source = row(101, at(8), at(10))
    del source[missing]

    with pytest.raises(ValueError, match="required"):
        planned([source], at(8), at(10))


def test_mixed_employee_rows_are_rejected():
    with pytest.raises(ValueError, match="employee"):
        planned([row(101, at(8), at(10), employee=55)], at(8), at(10))


def test_conflicting_raw_employee_alias_is_rejected_without_guessing_identity():
    source = row(101, at(8), at(10))
    source["employee_id"] = [55, "Someone Else"]

    with pytest.raises(ValueError, match="employee"):
        planned([source], at(8), at(10))


@pytest.mark.parametrize(
    "source",
    [
        [row(101, at(8), at(8))],
        [row(101, at(10), at(8))],
        [row(101, at(8), at(11)), row(102, at(10), at(12))],
        [row(101, at(8), None), row(102, at(10), at(12))],
        [row(101, at(8), at(10)), row(101, at(11), at(12))],
    ],
)
def test_invalid_overlapping_open_or_duplicate_source_rows_fail_closed(source):
    with pytest.raises(ValueError):
        planned(source, at(8), at(12))


def test_duplicate_aliases_must_match_exactly():
    source = row(101, at(8), at(10))
    source["id"] = 102
    source["write_date"] = source["odoo_write_date"] + timedelta(seconds=1)

    with pytest.raises(ValueError, match="mismatch"):
        planned([source], at(8), at(10))


def test_non_utc_write_date_alias_is_rejected_even_for_the_same_instant():
    source = row(101, at(8), at(10))
    write_date = source["odoo_write_date"]
    assert isinstance(write_date, datetime)
    source["write_date"] = write_date.astimezone(timezone(timedelta(hours=-5)))

    with pytest.raises(ValueError, match="UTC"):
        planned([source], at(8), at(10))


def test_plan_round_trip_is_lossless_and_restores_immutable_values():
    source = row(
        101,
        at(8),
        at(12),
        raw_payload={"flags": [True, None, 7], "observed": at(7, 59)},
    )
    plan = planned([source], at(9), at(10))

    value = plan_to_json(plan)
    restored = plan_from_json(value)

    assert restored == plan
    assert isinstance(restored.source_versions, tuple)
    assert isinstance(restored.operations, tuple)
    assert isinstance(restored.expected_intervals, tuple)
    assert isinstance(restored.source_intervals, tuple)
    assert restored.expected_intervals[0]["raw_payload"]["flags"] == (
        True,
        None,
        7,
    )


def test_operation_keys_survive_json_and_change_with_source_or_request():
    baseline = planned([row(101, at(8), at(10))], at(8), at(10))
    retry = planned([row(101, at(8), at(10))], at(8), at(10))
    newer = planned([row(101, at(8), at(10), write_minute=202)], at(8), at(10))
    wider = planned([row(101, at(8), at(10))], at(8), at(11))

    assert baseline.operations[0].key == retry.operations[0].key
    assert plan_from_json(plan_to_json(baseline)).operations[0].key == baseline.operations[0].key
    assert baseline.operations[0].key != newer.operations[0].key
    assert baseline.operations[0].key != wider.operations[0].key


def valid_json_value() -> dict[str, object]:
    value = plan_to_json(planned([row(101, at(8), at(10))], at(8), at(10)))
    assert isinstance(value, dict)
    return value


@pytest.mark.parametrize("mutation", ["unknown", "missing", "old_version", "future_version"])
def test_plan_from_json_rejects_unknown_missing_and_wrong_schema(mutation):
    value = valid_json_value()
    if mutation == "unknown":
        value["surprise"] = 1
    elif mutation == "missing":
        del value["operations"]
    elif mutation == "old_version":
        value["schema_version"] = 1
    else:
        value["schema_version"] = 999

    with pytest.raises(ValueError):
        plan_from_json(value)


def test_plan_from_json_rejects_float_schema_version():
    value = valid_json_value()
    value["schema_version"] = 1.0

    with pytest.raises(ValueError, match="schema version"):
        plan_from_json(value)


@pytest.mark.parametrize("mutation", ["kind", "bool_id", "datetime", "offset"])
def test_plan_from_json_rejects_invalid_operation_and_datetime_values(mutation):
    value = valid_json_value()
    operations = value["operations"]
    versions = value["source_versions"]
    assert isinstance(operations, list)
    assert isinstance(versions, list)
    assert isinstance(operations[0], dict)
    assert isinstance(versions[0], dict)
    if mutation == "kind":
        operations[0]["kind"] = "replace"
    elif mutation == "bool_id":
        operations[0]["employee_odoo_id"] = True
    elif mutation == "datetime":
        versions[0]["write_date"] = "not-a-date"
    else:
        versions[0]["write_date"] = "2026-08-31T01:00:00+01:00"

    with pytest.raises((TypeError, ValueError)):
        plan_from_json(value)


@pytest.mark.parametrize("duplicate", ["source", "operation"])
def test_plan_from_json_rejects_duplicate_ids_and_operation_keys(duplicate):
    value = valid_json_value()
    key = "source_versions" if duplicate == "source" else "operations"
    entries = value[key]
    assert isinstance(entries, list)
    entries.append(entries[0])

    with pytest.raises(ValueError, match="duplicate"):
        plan_from_json(value)


@pytest.mark.parametrize("pair", ["update_update", "update_delete", "delete_delete"])
def test_plan_from_json_rejects_distinct_operations_for_the_same_source_id(pair):
    update_value = valid_json_value()
    delete_value = plan_to_json(
        planned(
            [row(100, at(8), at(9)), row(101, at(9), at(10))],
            at(8),
            at(10),
        )
    )
    update_operations = update_value["operations"]
    delete_operations = delete_value["operations"]
    assert isinstance(update_operations, list)
    assert isinstance(delete_operations, list)
    update = next(operation for operation in update_operations if operation["kind"] == "update")
    deleted = next(
        operation
        for operation in delete_operations
        if operation["kind"] == "delete" and operation["attendance_id"] == 101
    )
    if pair == "update_update":
        value = update_value
        duplicate = deepcopy(update)
    elif pair == "update_delete":
        value = update_value
        duplicate = deepcopy(deleted)
    else:
        value = delete_value
        duplicate = deepcopy(deleted)
    operations = value["operations"]
    assert isinstance(operations, list)
    duplicate["key"] = "attendance-correction-v2:" + "f" * 64
    operations.append(duplicate)
    operations.sort(key=lambda operation: operation["key"])

    with pytest.raises(ValueError, match="duplicate operation attendance id"):
        plan_from_json(value)


def test_plan_from_json_rejects_unchanged_fields_piggybacked_on_an_update():
    value = valid_json_value()
    operations = value["operations"]
    assert isinstance(operations, list)
    operation = operations[0]
    assert isinstance(operation, dict)
    encoded_mapping_set(operation["before"], "employee_odoo_id", EMPLOYEE)
    encoded_mapping_set(operation["after"], "employee_odoo_id", EMPLOYEE)

    with pytest.raises(ValueError, match="every update field must change"):
        plan_from_json(value)


def test_plan_from_json_authenticates_operation_key_and_projection():
    value = valid_json_value()
    alternate = plan_to_json(planned([row(101, at(8), at(10))], at(8), at(10), work_center=73))
    operations = value["operations"]
    alternate_operations = alternate["operations"]
    assert isinstance(operations, list)
    assert isinstance(alternate_operations, list)
    assert isinstance(operations[0], dict)
    assert isinstance(alternate_operations[0], dict)

    operations[0]["key"] = alternate_operations[0]["key"]

    with pytest.raises(ValueError, match="operation key"):
        plan_from_json(value)

    value = valid_json_value()
    operations = value["operations"]
    assert isinstance(operations, list)
    assert isinstance(operations[0], dict)
    encoded_mapping_set(
        operations[0]["before"],
        "check_in_utc",
        {
            "type": "datetime",
            "value": "2026-08-31T07:00:00Z",
        },
    )
    encoded_mapping_set(
        operations[0]["after"],
        "check_in_utc",
        {
            "type": "datetime",
            "value": "2026-08-31T07:30:00Z",
        },
    )

    with pytest.raises(ValueError, match="operation key|projection"):
        plan_from_json(value)

    value = valid_json_value()
    expected = value["expected_intervals"]
    assert isinstance(expected, list)
    encoded_mapping_replace(expected[0], "odoo_work_center_id", 73)

    with pytest.raises(ValueError, match="operation projection"):
        plan_from_json(value)


def test_plan_from_json_rejects_duplicate_keys_inside_encoded_mapping():
    value = valid_json_value()
    operations = value["operations"]
    assert isinstance(operations, list)
    operation = operations[0]
    assert isinstance(operation, dict)
    after = operation["after"]
    assert isinstance(after, dict)
    items = after["items"]
    assert isinstance(items, list)
    items.append(items[0])

    with pytest.raises(ValueError, match="duplicate"):
        plan_from_json(value)


def test_plan_from_json_revalidates_internal_interval_invariants():
    value = valid_json_value()
    expected = value["expected_intervals"]
    assert isinstance(expected, list)
    expected.append(expected[0])

    with pytest.raises(ValueError, match="overlap"):
        plan_from_json(value)


def test_public_frozen_values_reject_invalid_manual_construction():
    with pytest.raises((TypeError, ValueError)):
        SourceVersion(True, at(1))
    with pytest.raises((TypeError, ValueError)):
        CorrectionOperation(
            key="bad",
            kind="replace",  # type: ignore[arg-type]
            attendance_id=None,
            employee_odoo_id=EMPLOYEE,
            before=None,
            after={},
        )
