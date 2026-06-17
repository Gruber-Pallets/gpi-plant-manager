from scripts.approve_regular_attendance import classify_attendance


def test_classifies_closed_non_positive_overtime_as_eligible():
    row = {"id": 1, "check_out": "2026-06-16 21:30:00", "overtime_hours": 0}
    assert classify_attendance(row) == "eligible"


def test_classifies_negative_extra_hours_as_eligible():
    row = {"id": 1, "check_out": "2026-06-16 21:30:00", "overtime_hours": -24}
    assert classify_attendance(row) == "eligible"


def test_classifies_positive_overtime_as_ot():
    row = {"id": 1, "check_out": "2026-06-16 23:00:00", "overtime_hours": 1.25}
    assert classify_attendance(row) == "positive_ot"


def test_classifies_open_attendance_as_open():
    row = {"id": 1, "check_out": False, "overtime_hours": 0}
    assert classify_attendance(row) == "open"


def test_bad_overtime_is_not_auto_approved():
    row = {"id": 1, "check_out": "2026-06-16 21:30:00", "overtime_hours": "bad"}
    assert classify_attendance(row) == "unknown_overtime"
