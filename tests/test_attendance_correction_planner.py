from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime, timedelta, timezone
import hashlib
from itertools import permutations
import json
import random

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


def refresh_integrity(value: dict[str, object]) -> None:
    payload = {key: item for key, item in value.items() if key != "integrity"}
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    value["integrity"] = "attendance-correction-plan-v2:" + hashlib.sha256(encoded).hexdigest()


def different_canonical_key(key: str) -> str:
    return key[:-1] + ("0" if key[-1] != "0" else "1")


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


def test_two_partial_boundary_rows_keep_both_source_ids_outside_window():
    plan = planned(
        [row(101, at(8), at(10)), row(102, at(10), at(12))],
        at(9),
        at(11),
    )

    assert interval_tuples(plan) == [
        (101, at(8), at(9), 11, 3),
        (None, at(9), at(11), WORK_CENTER, DEPARTMENT),
        (102, at(11), at(12), 11, 3),
    ]
    assert [(op.kind, op.attendance_id) for op in plan.operations] == [
        ("update", 101),
        ("create", None),
        ("update", 102),
    ]
    assert not any(op.kind == "delete" for op in plan.operations)


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


def test_open_correction_preserves_boundary_id_and_reuses_fully_covered_future_row():
    plan = planned(
        [row(101, at(8), at(12)), row(102, at(12), at(14))],
        at(10),
        None,
    )

    assert interval_tuples(plan) == [
        (101, at(8), at(10), 11, 3),
        (102, at(10), None, WORK_CENTER, DEPARTMENT),
    ]
    assert [(op.kind, op.attendance_id) for op in plan.operations] == [
        ("update", 101),
        ("update", 102),
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


def test_operation_keys_ignore_presentation_fields_but_plan_integrity_covers_them():
    baseline = planned(
        [
            row(
                101,
                at(8),
                at(10),
                write_minute=7,
                employee_name="Ada",
                odoo_work_center_name="Old Cell",
                odoo_department_name="Old Department",
                raw_payload={"display": "first"},
            )
        ],
        at(8),
        at(10),
    )
    presentation_only = planned(
        [
            row(
                101,
                at(8),
                at(10),
                write_minute=7,
                employee_name="Ada Lovelace",
                odoo_work_center_name="Renamed Cell",
                odoo_department_name="Renamed Department",
                raw_payload={"display": "second"},
            )
        ],
        at(8),
        at(10),
    )

    assert tuple(item.key for item in baseline.operations) == tuple(
        item.key for item in presentation_only.operations
    )
    baseline_json = plan_to_json(baseline)
    presentation_json = plan_to_json(presentation_only)
    assert baseline_json["integrity"] != presentation_json["integrity"]


@pytest.mark.parametrize(
    "changed",
    [
        row(102, at(8), at(10), write_minute=7),
        row(101, at(8), at(10), write_minute=8),
        row(101, at(7, 30), at(10), write_minute=7),
        row(101, at(8), at(10), write_minute=7, work_center=12),
    ],
    ids=["source_id", "write_date", "interval", "location_id"],
)
def test_operation_keys_change_with_concurrency_relevant_source_facts(changed):
    baseline = planned(
        [row(101, at(8), at(10), write_minute=7)],
        at(8),
        at(10),
    )
    changed_plan = planned([changed], at(8), at(10))

    assert tuple(item.key for item in baseline.operations) != tuple(
        item.key for item in changed_plan.operations
    )


def test_created_middle_target_key_covers_its_causal_source_version():
    baseline = planned(
        [row(101, at(8), at(12), write_minute=7)],
        at(9),
        at(10),
    )
    newer = planned(
        [row(101, at(8), at(12), write_minute=8)],
        at(9),
        at(10),
    )

    def corrected_target_key(plan):
        return next(
            operation.key
            for operation in plan.operations
            if operation.kind == "create"
            and operation.after is not None
            and operation.after["odoo_work_center_id"] == WORK_CENTER
        )

    assert corrected_target_key(baseline) != corrected_target_key(newer)


@pytest.mark.parametrize("mutation", ["request_start", "request_end", "expected", "source"])
def test_whole_plan_integrity_protects_no_operation_plans(mutation):
    plan = planned(
        [
            row(
                101,
                at(8),
                at(12),
                work_center=WORK_CENTER,
                department=DEPARTMENT,
                note="original",
            )
        ],
        at(9),
        at(10),
    )
    assert plan.operations == ()
    value = plan_to_json(plan)
    integrity = value.get("integrity")
    assert isinstance(integrity, str)
    assert integrity.startswith("attendance-correction-plan-v2:")

    if mutation == "request_start":
        encoded_mapping_replace(
            value["request"],
            "start_utc",
            {"type": "datetime", "value": "2026-08-31T09:15:00Z"},
        )
    elif mutation == "request_end":
        encoded_mapping_replace(
            value["request"],
            "end_utc",
            {"type": "datetime", "value": "2026-08-31T09:45:00Z"},
        )
    elif mutation == "expected":
        expected = value["expected_intervals"]
        assert isinstance(expected, list)
        encoded_mapping_replace(expected[0], "note", "changed")
    else:
        sources = value["source_intervals"]
        assert isinstance(sources, list)
        encoded_mapping_replace(sources[0], "note", "changed")

    with pytest.raises(ValueError, match="integrity"):
        plan_from_json(value)


@pytest.mark.parametrize(
    "tamper",
    ["remove_operations", "operation_after", "source_version", "operation_key", "integrity"],
)
def test_whole_plan_integrity_rejects_canonical_looking_tampering(tamper):
    value = deepcopy(valid_json_value())
    operations = value["operations"]
    versions = value["source_versions"]
    assert isinstance(operations, list)
    assert isinstance(versions, list)
    assert isinstance(operations[0], dict)
    assert isinstance(versions[0], dict)
    if tamper == "remove_operations":
        operations.clear()
    elif tamper == "operation_after":
        encoded_mapping_replace(operations[0]["after"], "odoo_work_center_id", WORK_CENTER + 1)
    elif tamper == "source_version":
        versions[0]["write_date"] = "2026-08-31T00:02:00Z"
    elif tamper == "operation_key":
        key = operations[0]["key"]
        assert isinstance(key, str)
        operations[0]["key"] = different_canonical_key(key)
    else:
        integrity = value["integrity"]
        assert isinstance(integrity, str)
        value["integrity"] = integrity[:-1] + ("0" if integrity[-1] != "0" else "1")

    with pytest.raises(ValueError, match="integrity"):
        plan_from_json(value)


def test_decoder_rejects_reordered_mapping_even_with_refreshed_integrity():
    value = valid_json_value()
    request = value["request"]
    assert isinstance(request, dict)
    items = request["items"]
    assert isinstance(items, list)
    items[0], items[1] = items[1], items[0]
    refresh_integrity(value)

    with pytest.raises(ValueError, match="canonical order"):
        plan_from_json(value)


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
    refresh_integrity(value)

    with pytest.raises((TypeError, ValueError)):
        plan_from_json(value)


@pytest.mark.parametrize("duplicate", ["source", "operation"])
def test_plan_from_json_rejects_duplicate_ids_and_operation_keys(duplicate):
    value = valid_json_value()
    key = "source_versions" if duplicate == "source" else "operations"
    entries = value[key]
    assert isinstance(entries, list)
    entries.append(entries[0])
    refresh_integrity(value)

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
    duplicate["key"] = different_canonical_key(duplicate["key"])
    operations.append(duplicate)
    operations.sort(key=lambda operation: operation["key"])
    refresh_integrity(value)

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
    refresh_integrity(value)

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
    refresh_integrity(value)

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
    refresh_integrity(value)

    with pytest.raises(ValueError, match="operation key|projection"):
        plan_from_json(value)

    value = valid_json_value()
    expected = value["expected_intervals"]
    assert isinstance(expected, list)
    encoded_mapping_replace(expected[0], "odoo_work_center_id", 73)
    refresh_integrity(value)

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
    refresh_integrity(value)

    with pytest.raises(ValueError, match="duplicate"):
        plan_from_json(value)


def test_plan_from_json_revalidates_internal_interval_invariants():
    value = valid_json_value()
    expected = value["expected_intervals"]
    assert isinstance(expected, list)
    expected.append(expected[0])
    refresh_integrity(value)

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


# ---------------------------------------------------------------------------
# Merge request mode (quick-punch fixer)


LEGACY_REQUEST_KEYS = {
    "employee_odoo_id",
    "start_utc",
    "end_utc",
    "odoo_work_center_id",
    "odoo_department_id",
}


def merged(
    rows: list[dict[str, object]],
    start: datetime,
    end: datetime | None,
    *,
    work_center: int = WORK_CENTER,
    department: int | None = DEPARTMENT,
) -> CorrectionPlan:
    return plan_correction(
        rows=rows,
        employee_odoo_id=EMPLOYEE,
        start_utc=start,
        end_utc=end,
        odoo_work_center_id=work_center,
        odoo_department_id=department,
        merge=True,
    )


def _ops(plan):
    return [
        (op.kind, op.attendance_id, dict(op.after) if op.after else None)
        for op in plan.operations
    ]


def _drop_request_key(payload: dict[str, object], field: str) -> None:
    """Remove one key from the encoded request mapping of ``plan_to_json``.

    The request is serialized as ``{"type": "mapping", "items": [[key, value], ...]}``.
    """
    request = payload["request"]
    assert isinstance(request, dict) and request["type"] == "mapping"
    items = request["items"]
    assert isinstance(items, list)
    kept = [item for item in items if item[0] != field]
    assert len(kept) == len(items) - 1, f"encoded request omitted {field}"
    request["items"] = kept


def _rekeyed_plan(plan: CorrectionPlan, request: dict[str, object]) -> CorrectionPlan:
    """Rebuild ``plan`` under another request with freshly authenticated keys.

    Only re-derivation of the request's pieces can then tell the plans apart.
    """
    from zira_dashboard import attendance_corrections as engine

    sources = {
        item.attendance_id: item
        for item in engine._normalize_source_rows(plan.source_intervals, EMPLOYEE)
    }
    operations = []
    for op in plan.operations:
        token = op.key.split(":")[1]
        key_sources = () if token == "0" else tuple(sources[int(i)] for i in token.split(","))
        operations.append(
            CorrectionOperation(
                key=engine._operation_key(
                    kind=op.kind,
                    attendance_id=op.attendance_id,
                    employee_id=op.employee_odoo_id,
                    before=op.before,
                    after=op.after,
                    request=request,
                    sources=key_sources,
                ),
                kind=op.kind,
                attendance_id=op.attendance_id,
                employee_odoo_id=op.employee_odoo_id,
                before=op.before,
                after=op.after,
            )
        )
    return CorrectionPlan(
        plan.source_versions,
        tuple(operations),
        plan.expected_intervals,
        request,
        plan.source_intervals,
    )


def test_merge_open_keeps_the_live_row_and_deletes_the_short_rows():
    # Christian 2026-09-18 shape: D3 short, D2 detour, back on D3 and still clocked in.
    rows = [
        row(1, at(7), at(7, 2), work_center=WORK_CENTER, department=DEPARTMENT),
        row(2, at(7, 2), at(7, 4), work_center=99, department=DEPARTMENT),
        row(3, at(7, 4), None, work_center=WORK_CENTER, department=DEPARTMENT),
    ]
    plan = plan_correction(
        rows=rows, employee_odoo_id=EMPLOYEE, start_utc=at(7), end_utc=None,
        odoo_work_center_id=WORK_CENTER, odoo_department_id=DEPARTMENT, merge=True,
    )
    assert plan.request["merge"] is True
    kinds = sorted((op.kind, op.attendance_id) for op in plan.operations)
    assert kinds == [("delete", 1), ("delete", 2), ("update", 3)]
    update = next(op for op in plan.operations if op.kind == "update")
    assert dict(update.after) == {"check_in_utc": at(7)}
    assert [
        (e["odoo_attendance_id"], e["check_in_utc"], e["check_out_utc"])
        for e in plan.expected_intervals
    ] == [(3, at(7), None)]


def test_merge_closed_fills_a_same_station_gap():
    rows = [
        row(1, at(8), at(9), work_center=WORK_CENTER, department=DEPARTMENT),
        row(2, at(9, 3), at(10), work_center=WORK_CENTER, department=DEPARTMENT),
    ]
    plan = plan_correction(
        rows=rows, employee_odoo_id=EMPLOYEE, start_utc=at(8), end_utc=at(10),
        odoo_work_center_id=WORK_CENTER, odoo_department_id=DEPARTMENT, merge=True,
    )
    assert [
        (e["odoo_attendance_id"], e["check_in_utc"], e["check_out_utc"])
        for e in plan.expected_intervals
    ] == [(1, at(8), at(10))]
    assert sorted((op.kind, op.attendance_id) for op in plan.operations) == [
        ("delete", 2),
        ("update", 1),
    ]
    assert _ops(plan) == [
        ("update", 1, {"check_out_utc": at(10)}),
        ("delete", 2, None),
    ]


def test_legacy_closed_request_still_never_bridges_a_gap():
    rows = [
        row(1, at(8), at(9), work_center=WORK_CENTER, department=DEPARTMENT),
        row(2, at(9, 3), at(10), work_center=WORK_CENTER, department=DEPARTMENT),
    ]
    plan = plan_correction(
        rows=rows, employee_odoo_id=EMPLOYEE, start_utc=at(8), end_utc=at(10),
        odoo_work_center_id=WORK_CENTER, odoo_department_id=DEPARTMENT,
    )
    assert "merge" not in plan.request
    assert plan.operations == ()


@pytest.mark.parametrize("end", [at(10), None])
@pytest.mark.parametrize("merge_kwargs", [{}, {"merge": False}])
def test_legacy_request_mapping_is_exactly_the_five_legacy_keys(end, merge_kwargs):
    plan = plan_correction(
        rows=[row(1, at(8), at(9)), row(2, at(9, 3), None)],
        employee_odoo_id=EMPLOYEE,
        start_utc=at(8, 30),
        end_utc=end,
        odoo_work_center_id=WORK_CENTER,
        odoo_department_id=DEPARTMENT,
        **merge_kwargs,
    )

    assert set(plan.request) == LEGACY_REQUEST_KEYS
    encoded = plan_to_json(plan)
    assert isinstance(encoded, dict)
    assert [item[0] for item in encoded["request"]["items"]] == sorted(LEGACY_REQUEST_KEYS)


@pytest.mark.parametrize("flag", [1, "true", None, 0])
def test_merge_flag_must_be_a_real_boolean(flag):
    with pytest.raises(TypeError, match="merge"):
        plan_correction(
            rows=[],
            employee_odoo_id=EMPLOYEE,
            start_utc=at(8),
            end_utc=at(10),
            odoo_work_center_id=WORK_CENTER,
            odoo_department_id=DEPARTMENT,
            merge=flag,  # type: ignore[arg-type]
        )


def test_merge_is_a_no_op_only_for_one_covering_row():
    rows = [row(1, at(8), at(10), work_center=WORK_CENTER, department=DEPARTMENT)]
    plan = plan_correction(
        rows=rows, employee_odoo_id=EMPLOYEE, start_utc=at(8), end_utc=at(10),
        odoo_work_center_id=WORK_CENTER, odoo_department_id=DEPARTMENT, merge=True,
    )
    assert plan.operations == ()
    assert plan.request["merge"] is True


def test_merge_of_two_touching_target_rows_is_not_a_no_op():
    # Legacy calls this a no-op (every overlap already has the target location);
    # a merge must still leave exactly one continuous row.
    rows = [
        row(1, at(8), at(9), work_center=WORK_CENTER, department=DEPARTMENT),
        row(2, at(9), at(10), work_center=WORK_CENTER, department=DEPARTMENT),
    ]

    assert planned(rows, at(8), at(10)).operations == ()
    plan = merged(rows, at(8), at(10))

    assert interval_tuples(plan) == [(1, at(8), at(10), WORK_CENTER, DEPARTMENT)]
    assert _ops(plan) == [
        ("update", 1, {"check_out_utc": at(10)}),
        ("delete", 2, None),
    ]


def test_merge_no_op_requires_the_target_department_too():
    rows = [row(1, at(8), at(10), work_center=WORK_CENTER, department=DEPARTMENT + 1)]

    plan = merged(rows, at(8), at(10))

    assert _ops(plan) == [("update", 1, {"odoo_department_id": DEPARTMENT})]


def test_open_merge_is_a_no_op_for_the_single_open_target_row():
    rows = [
        row(1, at(6), at(7), work_center=99, department=DEPARTMENT),
        row(2, at(7), None, work_center=WORK_CENTER, department=DEPARTMENT),
    ]

    plan = merged(rows, at(7, 30), None)

    assert plan.operations == ()
    assert interval_tuples(plan) == [
        (1, at(6), at(7), 99, DEPARTMENT),
        (2, at(7), None, WORK_CENTER, DEPARTMENT),
    ]


def test_merge_plan_round_trips_through_json_and_rejects_tampering():
    rows = [
        row(1, at(8), at(9), work_center=WORK_CENTER, department=DEPARTMENT),
        row(2, at(9, 3), at(10), work_center=WORK_CENTER, department=DEPARTMENT),
    ]
    plan = plan_correction(
        rows=rows, employee_odoo_id=EMPLOYEE, start_utc=at(8), end_utc=at(10),
        odoo_work_center_id=WORK_CENTER, odoo_department_id=DEPARTMENT, merge=True,
    )
    assert plan_from_json(plan_to_json(plan)) == plan
    payload = json.loads(json.dumps(plan_to_json(plan)))
    # Dropping the flag must not validate: the operations no longer implement the request.
    _drop_request_key(payload, "merge")
    with pytest.raises((ValueError, TypeError)):
        plan_from_json(payload)
    # Even with a refreshed whole-plan integrity, the request-bound keys reject it.
    refresh_integrity(payload)
    with pytest.raises(ValueError, match="operation key"):
        plan_from_json(payload)


@pytest.mark.parametrize("flag", [False, 1, "true", None])
def test_merge_request_flag_must_be_exactly_true_when_present(flag):
    payload = json.loads(json.dumps(plan_to_json(merged([row(1, at(8), at(9))], at(8), at(10)))))
    encoded_mapping_replace(payload["request"], "merge", flag)
    refresh_integrity(payload)

    with pytest.raises(ValueError, match="merge"):
        plan_from_json(payload)


def test_legacy_plan_cannot_gain_a_merge_flag_or_unknown_request_key():
    legacy = plan_to_json(planned([row(1, at(8), at(9))], at(8), at(10)))
    for field, encoded in (("merge", True), ("merge", False), ("surprise", True)):
        payload = json.loads(json.dumps(legacy))
        encoded_mapping_set(payload["request"], field, encoded)
        refresh_integrity(payload)

        with pytest.raises(ValueError):
            plan_from_json(payload)


def test_validation_re_derives_pieces_with_the_request_merge_flag():
    rows = [
        row(1, at(8), at(9), work_center=WORK_CENTER, department=DEPARTMENT),
        row(2, at(9, 3), at(10), work_center=WORK_CENTER, department=DEPARTMENT),
    ]
    merge_plan = merged(rows, at(8), at(10))
    legacy_request = {
        key: value for key, value in merge_plan.request.items() if key != "merge"
    }
    # The merge operations re-keyed under a legacy request: every key and the
    # projection authenticate, so only the legacy re-derivation can object.
    with pytest.raises(ValueError, match="implement the correction request"):
        _rekeyed_plan(merge_plan, legacy_request)

    legacy_plan = planned(
        [row(1, at(8), at(9)), row(2, at(9, 3), at(10))], at(8), at(10)
    )
    with pytest.raises(ValueError, match="implement the correction request"):
        _rekeyed_plan(legacy_plan, {**legacy_plan.request, "merge": True})


def test_open_merge_prefers_the_open_row_over_an_earlier_row_starting_at_start():
    rows = [
        row(1, at(7), at(7, 2), work_center=WORK_CENTER, department=DEPARTMENT),
        row(2, at(7, 3), None, work_center=WORK_CENTER, department=DEPARTMENT),
    ]

    plan = merged(rows, at(7), None)

    assert interval_tuples(plan) == [(2, at(7), None, WORK_CENTER, DEPARTMENT)]
    assert _ops(plan) == [
        ("delete", 1, None),
        ("update", 2, {"check_in_utc": at(7)}),
    ]
    # The legacy rule is unchanged: it keeps the earliest row and drops the live one.
    legacy = planned(rows, at(7), None)
    assert interval_tuples(legacy) == [(1, at(7), None, WORK_CENTER, DEPARTMENT)]
    assert sorted((op.kind, op.attendance_id) for op in legacy.operations) == [
        ("delete", 2),
        ("update", 1),
    ]


def test_open_merge_keeps_the_first_rows_left_remainder_and_the_live_id():
    rows = [
        row(1, at(6), at(7, 1), work_center=11, department=3),
        row(2, at(7, 1), at(7, 3), work_center=99, department=DEPARTMENT),
        row(3, at(7, 3), None, work_center=WORK_CENTER, department=DEPARTMENT),
    ]

    plan = merged(rows, at(7), None)

    assert interval_tuples(plan) == [
        (1, at(6), at(7), 11, 3),
        (3, at(7), None, WORK_CENTER, DEPARTMENT),
    ]
    # Canonical plan order sorts by effective start (the survivor now starts at 07:00).
    assert _ops(plan) == [
        ("update", 1, {"check_out_utc": at(7)}),
        ("update", 3, {"check_in_utc": at(7)}),
        ("delete", 2, None),
    ]


OPEN_MERGE_REFUSAL = "open merge requires the open attendance row inside the merge range"


def test_open_merge_without_an_open_row_is_refused():
    # Intended spec change: this used to fall back to the legacy survivor, which
    # sets check_out=NULL on closed row 1 and clocks a clocked-out person back in.
    rows = [
        row(1, at(7), at(7, 2), work_center=99, department=DEPARTMENT),
        row(2, at(7, 2), at(8), work_center=WORK_CENTER, department=DEPARTMENT),
    ]

    with pytest.raises(ValueError, match=OPEN_MERGE_REFUSAL):
        merged(rows, at(7), None)
    # The legacy request keeps its survivor rule.
    assert interval_tuples(planned(rows, at(7), None)) == [
        (1, at(7), None, WORK_CENTER, DEPARTMENT)
    ]


@pytest.mark.parametrize(
    "rows",
    [
        pytest.param([], id="no-rows"),
        pytest.param(
            [row(1, at(6), at(7), work_center=WORK_CENTER, department=DEPARTMENT)],
            id="clocked-out-before-start",
        ),
        pytest.param(
            [row(1, at(6), at(11), work_center=WORK_CENTER, department=DEPARTMENT)],
            id="closed-target-row-spanning-start",
        ),
        pytest.param(
            [
                row(1, at(7), at(7, 2), work_center=WORK_CENTER, department=DEPARTMENT),
                row(2, at(7, 2), at(7, 4), work_center=99, department=DEPARTMENT),
                row(3, at(7, 4), at(11, 30), work_center=WORK_CENTER, department=DEPARTMENT),
            ],
            id="auto-lunch-signed-out-after-the-quick-punches",
        ),
    ],
)
def test_open_merge_refuses_a_person_who_is_no_longer_clocked_in(rows):
    with pytest.raises(ValueError, match=OPEN_MERGE_REFUSAL):
        merged(rows, at(7), None)


@pytest.mark.parametrize(
    ("work_center", "department"),
    [(99, DEPARTMENT), (WORK_CENTER, DEPARTMENT + 1), (None, None)],
)
def test_open_merge_refuses_an_open_row_that_started_before_start_elsewhere(
    work_center, department
):
    rows = [
        row(1, at(5), at(6), work_center=WORK_CENTER, department=DEPARTMENT),
        row(2, at(6), None, work_center=work_center, department=department),
    ]

    with pytest.raises(ValueError, match=OPEN_MERGE_REFUSAL):
        merged(rows, at(7), None)
    # The hazard: the live row 2 is closed at start and a brand-new open row is
    # created, which the kiosk (it closes shifts by the live row's ID) never closes.
    legacy = planned(rows, at(7), None)
    assert interval_tuples(legacy) == [
        (1, at(5), at(6), WORK_CENTER, DEPARTMENT),
        (2, at(6), at(7), work_center, department),
        (None, at(7), None, WORK_CENTER, DEPARTMENT),
    ]


@pytest.mark.parametrize("open_start", [at(7), at(7, 3)])
def test_open_merge_with_the_open_row_inside_the_range_keeps_its_id(open_start):
    rows = [
        row(1, at(6), at(7), work_center=99, department=DEPARTMENT),
        *(
            [row(2, at(7), open_start, work_center=11, department=3)]
            if open_start > at(7)
            else []
        ),
        row(3, open_start, None, work_center=99, department=DEPARTMENT),
    ]

    plan = merged(rows, at(7), None)

    assert interval_tuples(plan) == [
        (1, at(6), at(7), 99, DEPARTMENT),
        (3, at(7), None, WORK_CENTER, DEPARTMENT),
    ]
    live_update = {"odoo_work_center_id": WORK_CENTER}
    if open_start > at(7):
        live_update = {"check_in_utc": at(7), **live_update}
    assert [op for op in _ops(plan) if op[1] == 3] == [("update", 3, live_update)]
    assert plan_from_json(json.loads(json.dumps(plan_to_json(plan)))) == plan


@pytest.mark.parametrize("open_start", [at(6), at(7)])
def test_open_merge_no_op_still_returns_an_empty_plan(open_start):
    rows = [
        row(1, at(5), open_start, work_center=99, department=DEPARTMENT),
        row(2, open_start, None, work_center=WORK_CENTER, department=DEPARTMENT),
    ]

    plan = merged(rows, at(7), None)

    assert plan.operations == ()
    assert plan.request["merge"] is True
    assert interval_tuples(plan) == [
        (1, at(5), open_start, 99, DEPARTMENT),
        (2, open_start, None, WORK_CENTER, DEPARTMENT),
    ]
    assert plan_from_json(json.loads(json.dumps(plan_to_json(plan)))) == plan


@pytest.mark.parametrize(
    "rows",
    [
        pytest.param([], id="no-rows"),
        pytest.param(
            [
                row(1, at(7), at(7, 2), work_center=99, department=DEPARTMENT),
                row(2, at(7, 2), at(8), work_center=WORK_CENTER, department=DEPARTMENT),
            ],
            id="clocked-out",
        ),
        pytest.param(
            [row(1, at(6), None, work_center=99, department=DEPARTMENT)],
            id="open-row-before-start-elsewhere",
        ),
    ],
)
def test_validation_refuses_an_open_merge_plan_the_planner_would_refuse(rows):
    # The legacy plan is exactly what the old open-merge fallback produced; re-keyed
    # under a merge request it authenticates, so only re-derivation can refuse it.
    legacy_plan = planned(rows, at(7), None)

    with pytest.raises(ValueError, match=OPEN_MERGE_REFUSAL):
        _rekeyed_plan(legacy_plan, {**legacy_plan.request, "merge": True})


def test_closed_merge_keeps_left_and_right_remainders_and_reuses_an_inside_row():
    rows = [
        row(1, at(7), at(8, 30), work_center=11, department=3),
        row(2, at(8, 32), at(9, 30), work_center=WORK_CENTER, department=DEPARTMENT),
        row(3, at(9, 31), at(11), work_center=11, department=3),
    ]

    plan = merged(rows, at(8), at(10))

    assert interval_tuples(plan) == [
        (1, at(7), at(8), 11, 3),
        (2, at(8), at(10), WORK_CENTER, DEPARTMENT),
        (3, at(10), at(11), 11, 3),
    ]
    assert _ops(plan) == [
        ("update", 1, {"check_out_utc": at(8)}),
        ("update", 2, {"check_in_utc": at(8), "check_out_utc": at(10)}),
        ("update", 3, {"check_in_utc": at(10)}),
    ]


def test_closed_merge_with_remainders_and_no_inside_row_creates_the_target():
    rows = [
        row(1, at(7), at(9), work_center=11, department=3),
        row(2, at(9, 3), at(11), work_center=11, department=3),
    ]

    plan = merged(rows, at(8), at(10))

    assert interval_tuples(plan) == [
        (1, at(7), at(8), 11, 3),
        (None, at(8), at(10), WORK_CENTER, DEPARTMENT),
        (2, at(10), at(11), 11, 3),
    ]
    assert sorted(
        (op.kind, op.attendance_id or 0) for op in plan.operations
    ) == [("create", 0), ("update", 1), ("update", 2)]


def test_closed_merge_of_one_row_split_on_both_sides_matches_legacy():
    rows = [row(1, at(7), at(11))]

    assert interval_tuples(merged(rows, at(8), at(10))) == interval_tuples(
        planned(rows, at(8), at(10))
    )


def test_closed_merge_across_a_detour_at_another_station_becomes_one_row():
    rows = [
        row(1, at(8), at(9), work_center=WORK_CENTER, department=DEPARTMENT),
        row(2, at(9), at(9, 4), work_center=99, department=DEPARTMENT),
        row(3, at(9, 4), at(10), work_center=WORK_CENTER, department=DEPARTMENT),
    ]

    plan = merged(rows, at(8), at(10))

    assert interval_tuples(plan) == [(1, at(8), at(10), WORK_CENTER, DEPARTMENT)]
    assert _ops(plan) == [
        ("update", 1, {"check_out_utc": at(10)}),
        ("delete", 2, None),
        ("delete", 3, None),
    ]


def test_closed_merge_fills_gaps_up_to_the_requested_edges():
    rows = [
        row(1, at(8, 1), at(9), work_center=WORK_CENTER, department=DEPARTMENT),
        row(2, at(9, 3), at(9, 58), work_center=99, department=DEPARTMENT),
        row(9, at(10, 30), at(11), work_center=11, department=3),
    ]

    plan = merged(rows, at(8), at(10))

    assert interval_tuples(plan) == [
        (1, at(8), at(10), WORK_CENTER, DEPARTMENT),
        (9, at(10, 30), at(11), 11, 3),
    ]


def test_closed_merge_of_an_open_source_keeps_the_open_suffix():
    rows = [
        row(1, at(8), at(9), work_center=WORK_CENTER, department=DEPARTMENT),
        row(2, at(9, 2), None, work_center=99, department=DEPARTMENT),
    ]

    plan = merged(rows, at(8), at(10))

    assert interval_tuples(plan) == [
        (1, at(8), at(10), WORK_CENTER, DEPARTMENT),
        (2, at(10), None, 99, DEPARTMENT),
    ]


def test_merge_without_source_rows_creates_the_requested_interval():
    # Only a closed range: an open merge without the live row is refused (see
    # test_open_merge_refuses_a_person_who_is_no_longer_clocked_in).
    assert interval_tuples(merged([], at(8), at(10))) == [
        (None, at(8), at(10), WORK_CENTER, DEPARTMENT)
    ]


def test_merge_operation_keys_differ_from_the_legacy_request():
    rows = [row(1, at(8), at(10))]

    assert merged(rows, at(8), at(10)).operations[0].key != planned(
        rows, at(8), at(10)
    ).operations[0].key


# ---------------------------------------------------------------------------
# Legacy golden digest: merge mode must not move a single legacy byte.

# Proven equal to the pre-merge engine (4df8ccce) when this test was added.
LEGACY_GOLDEN_DIGEST = "1b5360b31a4a9f1258ffed2a87d2d01d38b528813ee5e0021fa0630aa403524b"
_GOLDEN_SEED = 20260919
_GOLDEN_CASES = 2000
_GOLDEN_ROW_WORK_CENTERS = (11, WORK_CENTER, 99, None)
_GOLDEN_TARGET_WORK_CENTERS = (11, WORK_CENTER, 99)
_GOLDEN_DEPARTMENTS = (3, DEPARTMENT, None)


def _golden_rows(rng: random.Random) -> list[dict[str, object]]:
    """Non-overlapping rows: gaps, touching rows, an optional open last row."""
    count = rng.choice((0, 1, 1, 2, 2, 3, 3, 4))
    ids = rng.sample(range(1, 40), count)
    cursor = at(6) + timedelta(minutes=rng.randrange(0, 45))
    rows: list[dict[str, object]] = []
    for index, attendance_id in enumerate(ids):
        start = cursor
        end = (
            None
            if index == count - 1 and rng.random() < 0.45
            else start + timedelta(minutes=rng.choice((1, 2, 4, 30, 60, 95)))
        )
        extra: dict[str, object] = {}
        if rng.random() < 0.3:
            extra["odoo_work_center_name"] = f"WC {attendance_id}"
            extra["odoo_department_name"] = f"Dept {attendance_id}"
        rows.append(
            row(
                attendance_id,
                start,
                end,
                work_center=rng.choice(_GOLDEN_ROW_WORK_CENTERS),
                department=rng.choice(_GOLDEN_DEPARTMENTS),
                write_minute=rng.randrange(0, 600),
                **extra,
            )
        )
        if end is not None:
            cursor = end + timedelta(minutes=rng.choice((0, 0, 1, 3, 25)))
    rng.shuffle(rows)
    return rows


def _golden_request(
    rng: random.Random, rows: list[dict[str, object]]
) -> dict[str, object]:
    """A request whose edges mostly land on, or one minute beside, row edges."""
    edges = {at(5), at(13)}
    for item in rows:
        for value in (item["check_in_utc"], item["check_out_utc"]):
            if isinstance(value, datetime):
                edges.update((value, value - timedelta(minutes=1), value + timedelta(minutes=1)))
    points = sorted(edges)
    start = rng.choice(points)
    later = [point for point in points if point > start]
    roll = rng.random()
    if roll < 0.4:
        end = None
    elif roll < 0.42:
        end = start  # an invalid empty range: the error text is part of the digest
    else:
        end = rng.choice(later) if later else start + timedelta(minutes=30)
    return {
        "employee_odoo_id": EMPLOYEE,
        "start_utc": start,
        "end_utc": end,
        "odoo_work_center_id": rng.choice(_GOLDEN_TARGET_WORK_CENTERS),
        "odoo_department_id": rng.choice(_GOLDEN_DEPARTMENTS),
    }


def legacy_golden_digest(plan_correction_fn=plan_correction, plan_to_json_fn=plan_to_json) -> str:
    """Hash every seeded legacy (no ``merge`` argument) plan or refusal text.

    The engine functions are parameters so the same cases can be replayed
    against another engine revision.
    """
    rng = random.Random(_GOLDEN_SEED)
    digest = hashlib.sha256()
    for _ in range(_GOLDEN_CASES):
        rows = _golden_rows(rng)
        request = _golden_request(rng, rows)
        try:
            plan = plan_correction_fn(rows=rows, **request)
        except (TypeError, ValueError) as exc:
            text = f"error:{type(exc).__name__}:{exc}"
        else:
            text = json.dumps(
                plan_to_json_fn(plan), sort_keys=True, separators=(",", ":")
            )
        digest.update(text.encode("utf-8"))
        digest.update(b"\n")
    return digest.hexdigest()


def test_legacy_requests_match_the_pre_merge_golden_digest():
    assert legacy_golden_digest() == LEGACY_GOLDEN_DIGEST
