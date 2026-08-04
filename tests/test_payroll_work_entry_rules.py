from copy import deepcopy
from dataclasses import FrozenInstanceError
from datetime import date
from math import inf, isfinite, nan

import pytest

from zira_dashboard.payroll_work_entry_rules import (
    EXPECTED_EXCESS_HOURS,
    TOLERANCE_HOURS,
    classify_day,
)


DAY = date(2026, 7, 24)
MISSING = object()


def work(
    entry_id,
    code,
    duration,
    *,
    state="draft",
    attendance_id=None,
    conflict=False,
    numeric_data_valid=True,
):
    return {
        "id": entry_id,
        "employee_id": 9,
        "employee_name": "Darren Donahue",
        "date": DAY,
        "duration": duration,
        "state": state,
        "active": True,
        "conflict": conflict,
        "type_code": code,
        "attendance_id": attendance_id,
        "numeric_data_valid": numeric_data_valid,
    }


def attendance(
    expected=0.0,
    overtime=8.5228,
    *,
    attendance_id=3803,
    status="approved",
    raw=None,
    numeric_data_valid=True,
):
    return {
        "id": attendance_id,
        "employee_id": 9,
        "employee_name": "Darren Donahue",
        "date": DAY,
        "expected_hours": expected,
        "worked_hours": expected + overtime,
        "overtime_hours": overtime if raw is None else raw,
        "validated_overtime_hours": overtime,
        "overtime_status": status,
        "numeric_data_valid": numeric_data_valid,
    }


def classify(work_rows, attendance_rows):
    return classify_day(9, "Darren Donahue", DAY, work_rows, attendance_rows)


def test_exact_positive_defect_is_duration_update():
    result = classify(
        [
            work(8512, "WORK100", 3.6214, attendance_id=3803),
            work(8513, "OVERTIME", 5.3092),
        ],
        [attendance(expected=3.1214, overtime=5.3092)],
    )

    assert result.kind == "correct"
    assert result.reason_codes == ()
    assert result.action == "duration_update"
    assert result.work_entry_id == 8512
    assert result.attendance_id == 3803
    assert result.before_duration == pytest.approx(3.6214)
    assert result.after_duration == pytest.approx(3.1214)


def test_exact_zero_target_deletes_only_regular_row():
    result = classify(
        [
            work(8512, "WORK100", 0.5, attendance_id=3803),
            work(8513, "OVERTIME", 8.522777778),
        ],
        [attendance(expected=-0.000022222, overtime=8.5228)],
    )

    assert result.kind == "correct"
    assert result.action == "delete_zero_regular"
    assert result.work_entry_id == 8512
    assert result.after_duration == 0.0


@pytest.mark.parametrize("target", [TOLERANCE_HOURS, -TOLERANCE_HOURS])
def test_target_at_zero_tolerance_boundary_is_deleted(target):
    result = classify(
        [
            work(1, "WORK100", target + EXPECTED_EXCESS_HOURS, attendance_id=3803),
            work(2, "OVERTIME", 5.0),
        ],
        [attendance(expected=target, overtime=5.0)],
    )

    assert result.kind == "correct"
    assert result.action == "delete_zero_regular"
    assert result.after_duration == 0.0


def test_corrected_values_are_noop():
    result = classify(
        [
            work(8512, "WORK100", 3.1214, attendance_id=3803),
            work(8513, "OVERTIME", 5.3092),
        ],
        [attendance(expected=3.1214, overtime=5.3092)],
    )

    assert result.kind == "noop"
    assert result.reason_codes == ()
    assert result.action is None
    assert result.after_duration is None


def test_matching_totals_noop_before_draft_and_conflict_safety_checks():
    result = classify(
        [
            work(
                8512,
                "WORK100",
                3.1214,
                state="validated",
                attendance_id=3803,
                conflict=True,
            ),
            work(8513, "OVERTIME", 5.3092),
        ],
        [attendance(expected=3.1214, overtime=5.3092)],
    )

    assert result.kind == "noop"
    assert result.reason_codes == ()
    assert result.action is None


def test_both_totals_at_one_minute_tolerance_are_noop():
    expected = 3.1214
    overtime = 5.3092
    result = classify(
        [
            work(1, "WORK100", expected + TOLERANCE_HOURS, attendance_id=3803),
            work(2, "OVERTIME", overtime + TOLERANCE_HOURS),
        ],
        [attendance(expected=expected, overtime=overtime)],
    )

    assert result.kind == "noop"


def test_regular_mismatch_without_any_overtime_is_noop():
    result = classify(
        [work(6349, "WORK100", 8.8647, attendance_id=3000)],
        [attendance(expected=8.3647, overtime=0.0, raw=0.0)],
    )

    assert result.kind == "noop"
    assert result.reason_codes == ()


def test_unsafe_variants_are_review():
    base_work = [
        work(1, "WORK100", 3.6214, attendance_id=3803),
        work(2, "OVERTIME", 5.3092),
    ]
    cases = [
        (
            base_work,
            [attendance(expected=3.1214, overtime=5.3092, status="to_approve")],
            "unapproved_overtime",
        ),
        (
            base_work,
            [attendance(expected=3.1214, overtime=5.3092, raw=5.0)],
            "attendance_overtime_mismatch",
        ),
        (
            [
                work(1, "WORK100", 3.6214, attendance_id=3803),
                work(2, "OVERTIME", 4.0),
            ],
            [attendance(expected=3.1214, overtime=5.3092)],
            "payroll_overtime_mismatch",
        ),
        (
            [
                work(1, "WORK100", 3.8214, attendance_id=3803),
                work(2, "OVERTIME", 5.3092),
            ],
            [attendance(expected=3.1214, overtime=5.3092)],
            "regular_excess_not_half_hour",
        ),
        (
            [
                work(1, "WORK100", 3.6214, state="validated", attendance_id=3803),
                work(2, "OVERTIME", 5.3092),
            ],
            [attendance(expected=3.1214, overtime=5.3092)],
            "non_draft_work_entry",
        ),
        (
            [
                work(1, "WORK100", 1.8, attendance_id=3803),
                work(3, "WORK100", 1.8214, attendance_id=3803),
                work(2, "OVERTIME", 5.3092),
            ],
            [attendance(expected=3.1214, overtime=5.3092)],
            "ambiguous_regular_entries",
        ),
        (
            [
                work(1, "WORK100", 3.6214, attendance_id=3803, conflict=True),
                work(2, "OVERTIME", 5.3092),
            ],
            [attendance(expected=3.1214, overtime=5.3092)],
            "conflicting_work_entry",
        ),
    ]

    for work_rows, attendance_rows, reason in cases:
        result = classify(work_rows, attendance_rows)
        assert result.kind == "review"
        assert result.action is None
        assert reason in result.reason_codes


def test_missing_attendance_link_is_review():
    result = classify(
        [work(1, "WORK100", 3.6214), work(2, "OVERTIME", 5.3092)],
        [attendance(expected=3.1214, overtime=5.3092)],
    )

    assert result.kind == "review"
    assert "regular_not_attendance_linked" in result.reason_codes


def test_detached_attendance_link_is_review():
    result = classify(
        [
            work(1, "WORK100", 3.6214, attendance_id=9999),
            work(2, "OVERTIME", 5.3092),
        ],
        [attendance(expected=3.1214, overtime=5.3092, attendance_id=3803)],
    )

    assert result.kind == "review"
    assert "regular_not_attendance_linked" in result.reason_codes


def test_nonpositive_approved_overtime_is_review():
    result = classify(
        [
            work(1, "WORK100", 3.6214, attendance_id=3803),
            work(2, "OVERTIME", 5.0),
        ],
        [attendance(expected=3.1214, overtime=0.0, raw=5.0)],
    )

    assert result.kind == "review"
    assert "attendance_overtime_not_positive" in result.reason_codes


def test_canceling_per_attendance_overtime_mismatches_are_review():
    result = classify(
        [
            work(1, "WORK100", 3.6214, attendance_id=3803),
            work(2, "OVERTIME", 5.3092),
        ],
        [
            attendance(expected=1.0, overtime=2.0, raw=1.9, attendance_id=3803),
            attendance(
                expected=2.1214,
                overtime=3.3092,
                raw=3.4092,
                attendance_id=3804,
            ),
        ],
    )

    assert result.attendance_overtime == pytest.approx(5.3092)
    assert result.work_overtime == pytest.approx(5.3092)
    assert result.kind == "review"
    assert "attendance_overtime_mismatch" in result.reason_codes


def test_unapproved_contributing_attendance_among_multiple_rows_is_review():
    result = classify(
        [
            work(1, "WORK100", 3.6214, attendance_id=3803),
            work(2, "OVERTIME", 5.3092),
        ],
        [
            attendance(expected=1.0, overtime=2.0, attendance_id=3803),
            attendance(
                expected=2.1214,
                overtime=3.3092,
                attendance_id=3804,
                status="to_approve",
            ),
        ],
    )

    assert result.kind == "review"
    assert "unapproved_overtime" in result.reason_codes


@pytest.mark.parametrize(
    ("row_kind", "field"),
    [
        ("work", "duration"),
        ("attendance", "expected_hours"),
        ("attendance", "overtime_hours"),
        ("attendance", "validated_overtime_hours"),
    ],
)
@pytest.mark.parametrize(
    "invalid_value",
    [MISSING, None, nan, inf, -inf],
    ids=["missing", "none", "nan", "positive_inf", "negative_inf"],
)
def test_incomplete_or_nonfinite_numeric_evidence_is_review(
    row_kind, field, invalid_value
):
    work_rows = [
        work(1, "WORK100", 3.6214, attendance_id=3803),
        work(2, "OVERTIME", 5.3092),
    ]
    attendance_rows = [attendance(expected=3.1214, overtime=5.3092)]
    row = work_rows[0] if row_kind == "work" else attendance_rows[0]
    if invalid_value is MISSING:
        row.pop(field)
    else:
        row[field] = invalid_value

    result = classify(work_rows, attendance_rows)

    assert result.kind == "review"
    assert result.reason_codes == ("invalid_numeric_data",)
    assert result.action is None
    assert result.after_duration is None
    assert all(
        isfinite(value)
        for value in (
            result.before_duration,
            result.attendance_regular,
            result.attendance_overtime,
            result.work_regular,
            result.work_overtime,
        )
    )


def test_normalization_invalidity_flag_blocks_zero_target_delete():
    result = classify(
        [
            work(1, "WORK100", 0.5, attendance_id=3803),
            work(2, "OVERTIME", 5.0),
        ],
        [
            attendance(
                expected=0.0,
                overtime=5.0,
                numeric_data_valid=False,
            )
        ],
    )

    assert result.kind == "review"
    assert result.reason_codes == ("invalid_numeric_data",)
    assert result.action is None
    assert result.issue_key == f"9:{DAY.isoformat()}:invalid_numeric_data"


def test_target_below_negative_tolerance_is_review():
    result = classify(
        [
            work(1, "WORK100", 0.48, attendance_id=3803),
            work(2, "OVERTIME", 5.3092),
        ],
        [attendance(expected=-0.02, overtime=5.3092)],
    )

    assert result.kind == "review"
    assert "negative_target" in result.reason_codes


def test_overtime_mismatch_reviews_even_when_regular_matches():
    result = classify(
        [
            work(1, "WORK100", 3.1214, attendance_id=3803),
            work(2, "OVERTIME", 4.0),
        ],
        [attendance(expected=3.1214, overtime=5.3092)],
    )

    assert result.kind == "review"
    assert "payroll_overtime_mismatch" in result.reason_codes


def test_regular_excess_at_one_minute_from_half_hour_is_corrected():
    expected = 3.1214
    measured_excess = EXPECTED_EXCESS_HOURS + TOLERANCE_HOURS
    result = classify(
        [
            work(1, "WORK100", expected + measured_excess, attendance_id=3803),
            work(2, "OVERTIME", 5.3092),
        ],
        [attendance(expected=expected, overtime=5.3092)],
    )

    assert result.kind == "correct"
    assert result.action == "duration_update"
    assert result.after_duration == pytest.approx(expected)


def test_regular_excess_more_than_one_minute_from_half_hour_is_review():
    expected = 3.1214
    result = classify(
        [
            work(
                1,
                "WORK100",
                expected + EXPECTED_EXCESS_HOURS + 61 / 3600,
                attendance_id=3803,
            ),
            work(2, "OVERTIME", 5.3092),
        ],
        [attendance(expected=expected, overtime=5.3092)],
    )

    assert result.kind == "review"
    assert "regular_excess_not_half_hour" in result.reason_codes


def test_every_work_entry_must_be_draft_and_conflict_free():
    result = classify(
        [
            work(1, "WORK100", 3.6214, attendance_id=3803),
            work(2, "OVERTIME", 5.3092),
            work(3, "OTHER", 1.0, state="validated", conflict=True),
        ],
        [attendance(expected=3.1214, overtime=5.3092)],
    )

    assert result.kind == "review"
    assert result.reason_codes == ("non_draft_work_entry", "conflicting_work_entry")


def test_reason_order_and_issue_key_are_stable_across_input_order():
    work_rows = [
        work(1, "WORK100", 0.3, state="validated", conflict=True),
        work(2, "WORK100", 0.4),
        work(3, "OVERTIME", 4.0),
    ]
    attendance_rows = [
        attendance(expected=-0.02, overtime=0.0, status="to_approve", raw=5.0)
    ]
    expected_reasons = (
        "ambiguous_regular_entries",
        "regular_not_attendance_linked",
        "attendance_overtime_not_positive",
        "unapproved_overtime",
        "attendance_overtime_mismatch",
        "payroll_overtime_mismatch",
        "regular_excess_not_half_hour",
        "non_draft_work_entry",
        "conflicting_work_entry",
        "negative_target",
    )

    result = classify(work_rows, attendance_rows)
    reordered = classify(list(reversed(work_rows)), list(reversed(attendance_rows)))

    assert result.reason_codes == expected_reasons
    assert reordered.reason_codes == expected_reasons
    assert result.issue_key == reordered.issue_key
    assert result.issue_key == f"9:{DAY.isoformat()}:{','.join(expected_reasons)}"


def test_result_carries_summed_audit_totals():
    result = classify(
        [
            work(1, "WORK100", 3.6214, attendance_id=3803),
            work(2, "OVERTIME", 2.0),
            work(3, "OVERTIME", 3.3092),
        ],
        [
            attendance(expected=1.0, overtime=2.0, attendance_id=3803),
            attendance(expected=2.1214, overtime=3.3092, attendance_id=3804),
        ],
    )

    assert result.kind == "correct"
    assert result.attendance_regular == pytest.approx(3.1214)
    assert result.attendance_overtime == pytest.approx(5.3092)
    assert result.work_regular == pytest.approx(3.6214)
    assert result.work_overtime == pytest.approx(5.3092)


def test_classifier_never_mutates_inputs_and_decision_is_frozen():
    work_rows = [
        work(1, "WORK100", 3.6214, attendance_id=3803),
        work(2, "OVERTIME", 5.3092),
    ]
    attendance_rows = [attendance(expected=3.1214, overtime=5.3092)]
    original_work = deepcopy(work_rows)
    original_attendance = deepcopy(attendance_rows)

    result = classify(work_rows, attendance_rows)

    assert work_rows == original_work
    assert attendance_rows == original_attendance
    with pytest.raises(FrozenInstanceError):
        result.kind = "noop"
