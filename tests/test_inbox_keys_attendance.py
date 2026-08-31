from datetime import UTC, datetime

from zira_dashboard import inbox_keys


START = datetime(2026, 8, 31, 12, 5, tzinfo=UTC)


def test_attendance_issue_key_sorts_source_ids_and_excludes_moving_end_time():
    assert (
        inbox_keys.attendance_issue_key("attendance_conflicting_location", 42, (901, 300), START)
        == "attendance_conflicting_location:42:300,901:2026-08-31T12:05:00+00:00"
    )


def test_production_run_key_uses_work_center_and_immutable_run_start():
    assert inbox_keys.production_run_key("Dismantler 1", START) == (
        "production_unassigned_run:Dismantler 1:2026-08-31T12:05:00+00:00"
    )


def test_attendance_source_stale_key_is_a_singleton():
    assert (
        inbox_keys.attendance_source_stale_key() == "attendance_source_stale:odoo_attendance_mirror"
    )
