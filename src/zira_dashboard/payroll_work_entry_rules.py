from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Literal


TOLERANCE_HOURS = 1.0 / 60.0
EXPECTED_EXCESS_HOURS = 0.5


@dataclass(frozen=True)
class Decision:
    kind: Literal["noop", "correct", "review"]
    employee_id: int
    employee_name: str
    work_date: date
    reason_codes: tuple[str, ...]
    action: Literal["duration_update", "delete_zero_regular"] | None
    work_entry_id: int | None
    attendance_id: int | None
    before_duration: float
    after_duration: float | None
    attendance_regular: float
    attendance_overtime: float
    work_regular: float
    work_overtime: float

    @property
    def issue_key(self) -> str:
        return f"{self.employee_id}:{self.work_date.isoformat()}:{','.join(self.reason_codes)}"


def _numeric_hours(row: Mapping[str, object], field: str) -> tuple[float, bool]:
    if field not in row or row[field] is None:
        return 0.0, False
    try:
        value = float(row[field])
    except (TypeError, ValueError):
        return 0.0, False
    if not math.isfinite(value):
        return 0.0, False
    return value, True


def _normalization_flag_is_valid(row: Mapping[str, object]) -> bool:
    if "numeric_data_valid" not in row:
        return True
    return row["numeric_data_valid"] is True


def _within_tolerance(first: float, second: float) -> bool:
    difference = abs(first - second)
    return difference < TOLERANCE_HOURS or math.isclose(
        difference,
        TOLERANCE_HOURS,
        rel_tol=1e-12,
        abs_tol=1e-12,
    )


def classify_day(
    employee_id: int,
    employee_name: str,
    work_date: date,
    work_entries: Sequence[Mapping[str, object]],
    attendances: Sequence[Mapping[str, object]],
) -> Decision:
    numeric_data_valid = True
    attendance_values: list[tuple[Mapping[str, object], float, float, float]] = []
    for row in attendances:
        expected, expected_is_valid = _numeric_hours(row, "expected_hours")
        raw_overtime, raw_is_valid = _numeric_hours(row, "overtime_hours")
        validated_overtime, validated_is_valid = _numeric_hours(
            row, "validated_overtime_hours"
        )
        numeric_data_valid = numeric_data_valid and all(
            (
                _normalization_flag_is_valid(row),
                expected_is_valid,
                raw_is_valid,
                validated_is_valid,
            )
        )
        attendance_values.append((row, expected, raw_overtime, validated_overtime))

    work_values: list[tuple[Mapping[str, object], float]] = []
    for row in work_entries:
        duration, duration_is_valid = _numeric_hours(row, "duration")
        numeric_data_valid = (
            numeric_data_valid
            and _normalization_flag_is_valid(row)
            and duration_is_valid
        )
        work_values.append((row, duration))

    attendance_regular = sum(value[1] for value in attendance_values)
    attendance_raw_overtime = sum(value[2] for value in attendance_values)
    attendance_overtime = sum(value[3] for value in attendance_values)

    regular_values = [
        value for value in work_values if value[0].get("type_code") == "WORK100"
    ]
    overtime_values = [
        value for value in work_values if value[0].get("type_code") == "OVERTIME"
    ]
    regular_entries = [value[0] for value in regular_values]
    work_regular = sum(value[1] for value in regular_values)
    work_overtime = sum(value[1] for value in overtime_values)

    regular_entry = regular_entries[0] if len(regular_entries) == 1 else None
    before_duration = (
        regular_values[0][1] if regular_entry is not None else work_regular
    )
    work_entry_id = regular_entry.get("id") if regular_entry is not None else None
    attendance_id = regular_entry.get("attendance_id") if regular_entry is not None else None

    common = {
        "employee_id": employee_id,
        "employee_name": employee_name,
        "work_date": work_date,
        "work_entry_id": work_entry_id,
        "attendance_id": attendance_id,
        "before_duration": before_duration,
        "attendance_regular": attendance_regular,
        "attendance_overtime": attendance_overtime,
        "work_regular": work_regular,
        "work_overtime": work_overtime,
    }

    # Invalid source evidence is the fail-closed exception to the intentional
    # noop-before-draft/conflict ordering below.
    if not numeric_data_valid:
        return Decision(
            reason_codes=("invalid_numeric_data",),
            kind="review",
            action=None,
            after_duration=None,
            **common,
        )

    if _within_tolerance(work_regular, attendance_regular) and _within_tolerance(
        work_overtime, attendance_overtime
    ):
        return Decision(reason_codes=(), kind="noop", action=None, after_duration=None, **common)

    has_overtime_signal = any(
        value != 0.0
        for value in (attendance_raw_overtime, attendance_overtime, work_overtime)
    )
    if not has_overtime_signal:
        return Decision(reason_codes=(), kind="noop", action=None, after_duration=None, **common)

    measured_excess = work_regular - attendance_regular
    target_duration = before_duration - measured_excess
    reasons: list[str] = []

    if len(regular_entries) != 1:
        reasons.append("ambiguous_regular_entries")
    attendance_ids = {row.get("id") for row in attendances}
    if attendance_id is None or attendance_id not in attendance_ids:
        reasons.append("regular_not_attendance_linked")
    if attendance_overtime <= 0.0:
        reasons.append("attendance_overtime_not_positive")
    if any(
        raw_overtime > 0.0 and row.get("overtime_status") != "approved"
        for row, _expected, raw_overtime, _validated_overtime in attendance_values
    ):
        reasons.append("unapproved_overtime")
    if any(
        raw_overtime > 0.0 and not _within_tolerance(raw_overtime, validated_overtime)
        for _row, _expected, raw_overtime, validated_overtime in attendance_values
    ) or not _within_tolerance(attendance_raw_overtime, attendance_overtime):
        reasons.append("attendance_overtime_mismatch")
    if not _within_tolerance(work_overtime, attendance_overtime):
        reasons.append("payroll_overtime_mismatch")
    if not _within_tolerance(measured_excess, EXPECTED_EXCESS_HOURS):
        reasons.append("regular_excess_not_half_hour")
    if any(row.get("state") != "draft" for row in work_entries):
        reasons.append("non_draft_work_entry")
    if any(bool(row.get("conflict")) for row in work_entries):
        reasons.append("conflicting_work_entry")
    if target_duration < 0.0 and not _within_tolerance(target_duration, 0.0):
        reasons.append("negative_target")

    if reasons:
        return Decision(
            reason_codes=tuple(reasons),
            kind="review",
            action=None,
            after_duration=None,
            **common,
        )

    if _within_tolerance(target_duration, 0.0):
        target_duration = 0.0
        action: Literal["duration_update", "delete_zero_regular"] = "delete_zero_regular"
    else:
        action = "duration_update"

    return Decision(
        reason_codes=(),
        kind="correct",
        action=action,
        after_duration=target_duration,
        **common,
    )
