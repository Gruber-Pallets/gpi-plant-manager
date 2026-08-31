import asyncio
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, date, datetime

import pytest

from zira_dashboard import (
    app,
    attendance_exceptions,
    attendance_location_policy,
    attendance_mirror,
    exception_inbox,
    inbox_reconcile,
    machine_breakdown,
    missing_wc,
    missed_punch_out,
    unexpected_worker,
)
from zira_dashboard.routes import staffing as staffing_routes


DAY = date(2026, 8, 31)
NOW = datetime(2026, 8, 31, 13, 0, tzinfo=UTC)


def _issue(kind, key, *, priority="urgent", comparison=False):
    return attendance_exceptions.AttendanceException(
        kind=kind,
        item_key=key,
        employee_odoo_id=42 if kind.startswith("attendance_") else None,
        employee_name="Adrian A." if kind.startswith("attendance_") else None,
        attendance_ids=(901,) if kind.startswith("attendance_") else (),
        start_utc=NOW,
        end_utc=NOW,
        raw_work_center_labels=(),
        odoo_work_center_ids=(),
        affected_workers=((42, "Adrian A."),) if kind.startswith("attendance_") else (),
        app_work_center_name="Dismantler 1" if kind.startswith("production_") else None,
        units=10.0 if kind == "production_unassigned_run" else None,
        sample_count=2 if kind == "production_unassigned_run" else None,
        reason="test_reason",
        priority=priority,
        comparison_only=comparison,
        target_odoo_department_id=None,
    )


def _attendance_snapshot(
    *,
    mode="off",
    production_mode="legacy",
    issues=(),
    complete=True,
    source_errors=(),
):
    return attendance_exceptions.AttendanceExceptionSnapshot(
        day=DAY,
        mode=mode,
        production_mode=production_mode,
        baseline_complete=mode != "off",
        fresh=complete,
        complete=complete,
        issues=tuple(issues),
        source_errors=tuple(source_errors),
    )


def _empty_legacy(monkeypatch, *, missing=(), assignments=()):
    monkeypatch.setattr(exception_inbox.plant_day, "today", lambda: DAY)
    monkeypatch.setattr(exception_inbox.plant_day, "now", lambda: NOW)
    monkeypatch.setattr(
        staffing_routes,
        "assignments_todo_payload",
        lambda: {
            "count": len(assignments),
            "today": DAY.isoformat(),
            "items": list(assignments),
            "people": [],
        },
    )
    monkeypatch.setattr(staffing_routes, "late_report_payload", lambda: {"count": 0})
    monkeypatch.setattr(missing_wc, "current_rows", lambda: list(missing))
    monkeypatch.setattr(missed_punch_out, "current_rows", lambda: [])
    monkeypatch.setattr(machine_breakdown, "current_rows", lambda: [])
    monkeypatch.setattr(unexpected_worker, "open_events", lambda _day: [])
    monkeypatch.setattr(exception_inbox, "_pending_time_off", lambda _day: (0, []))
    monkeypatch.setattr(exception_inbox, "_pending_time_off_counts", lambda _day: (0, 0))
    monkeypatch.setattr(exception_inbox, "_work_center_names", lambda: [])
    monkeypatch.setattr(exception_inbox, "_plant_schedule_reminder", lambda: (0, []))
    monkeypatch.setattr(exception_inbox, "_saturday_staffing_actions", lambda _day: (0, []))


def _legacy_assignment():
    return {
        "wc_name": "Dismantler 1",
        "units": 10,
        "first_label": "8:00 AM",
        "last_label": "8:05 AM",
        "first_iso": NOW.isoformat(),
        "last_iso": NOW.isoformat(),
    }


def test_off_keeps_legacy_missing_location_and_assignment_actions(monkeypatch):
    _empty_legacy(
        monkeypatch,
        missing=({"attendance_id": 901, "name": "Adrian", "check_in_label": "8:00 AM"},),
        assignments=(_legacy_assignment(),),
    )
    monkeypatch.setattr(
        attendance_exceptions,
        "build_snapshot",
        lambda *_a, **_k: _attendance_snapshot(mode="off"),
    )

    snapshot = exception_inbox.build_snapshot()
    sections = {section["id"]: section for section in snapshot["sections"]}

    assert sections["missing_wc"]["count"] == 1
    assert sections["assignments"]["count"] == 1
    assert sections["assignments"]["rows"][0]["action"]["type"] == "assignment"
    assert not [key for key in sections if key.startswith("attendance_")]


def test_shadow_uses_timeline_location_but_keeps_legacy_production_action(monkeypatch):
    _empty_legacy(
        monkeypatch,
        missing=({"attendance_id": 901, "name": "Old", "check_in_label": "8:00 AM"},),
        assignments=(_legacy_assignment(),),
    )
    issues = (
        _issue(
            "attendance_missing_location",
            "attendance_missing_location:42:901:2026-08-31T13:00:00+00:00",
            priority="warn",
        ),
        _issue(
            "production_unassigned_run",
            "production_unassigned_run:Dismantler 1:2026-08-31T13:00:00+00:00",
            comparison=True,
        ),
    )
    monkeypatch.setattr(
        attendance_exceptions,
        "build_snapshot",
        lambda *_a, **_k: _attendance_snapshot(
            mode="shadow", production_mode="shadow", issues=issues
        ),
    )

    snapshot = exception_inbox.build_snapshot()
    sections = {section["id"]: section for section in snapshot["sections"]}

    assert "missing_wc" not in sections
    assert "missing_wc" not in exception_inbox.build_summary()["sections"]
    assert sections["attendance_missing_location"]["count"] == 1
    assert sections["production_unassigned_run"]["rows"][0]["comparison_only"] is True
    assert sections["production_unassigned_run"]["rows"][0]["action"] is None
    assert sections["assignments"]["count"] == 1
    assert sections["assignments"]["rows"][0]["action"]["type"] == "assignment"


def test_live_strict_replaces_legacy_aggregate_with_distinct_run(monkeypatch):
    _empty_legacy(monkeypatch, assignments=(_legacy_assignment(),))
    issue = _issue(
        "production_unassigned_run",
        "production_unassigned_run:Dismantler 1:2026-08-31T13:00:00+00:00",
    )
    monkeypatch.setattr(
        attendance_exceptions,
        "build_snapshot",
        lambda *_a, **_k: _attendance_snapshot(
            mode="live", production_mode="strict", issues=(issue,)
        ),
    )

    snapshot = exception_inbox.build_snapshot()
    sections = {section["id"]: section for section in snapshot["sections"]}

    assert sections["assignments"]["count"] == 0
    assert sections["assignments"]["rows"] == []
    assert sections["production_unassigned_run"]["count"] == 1
    keys = [row["item_key"] for row in snapshot["queue"]]
    assert keys.count(issue.item_key) == 1


def test_attendance_row_revision_changes_for_urgency_but_not_a_moving_end():
    item = _issue(
        "attendance_missing_location",
        "attendance_missing_location:42:901:2026-08-31T13:00:00+00:00",
        priority="warn",
    )

    pending = exception_inbox._attendance_issue_row(item)
    same_visible_content = exception_inbox._attendance_issue_row(
        replace(item, end_utc=NOW.replace(minute=4))
    )
    urgent = exception_inbox._attendance_issue_row(
        replace(item, end_utc=NOW.replace(minute=5), priority="urgent")
    )

    assert pending["item_key"] == same_visible_content["item_key"] == urgent["item_key"]
    assert pending["row_key"] == same_visible_content["row_key"]
    assert pending["row_key"] != urgent["row_key"]


def test_production_row_revision_changes_with_units_and_sample_count():
    item = _issue(
        "production_unassigned_run",
        "production_unassigned_run:Dismantler 1:2026-08-31T13:00:00+00:00",
    )

    initial = exception_inbox._attendance_issue_row(item)
    repeated = exception_inbox._attendance_issue_row(item)
    changed = exception_inbox._attendance_issue_row(replace(item, units=12.5, sample_count=4))

    assert initial["item_key"] == changed["item_key"]
    assert initial["row_key"] == repeated["row_key"]
    assert initial["row_key"] != changed["row_key"]


@pytest.mark.parametrize(
    ("production_mode", "builder_failure"),
    [("strict", False), ("pending", False), ("strict", True)],
)
def test_authoritative_day_never_calls_legacy_during_attendance_outage(
    monkeypatch, production_mode, builder_failure
):
    _empty_legacy(monkeypatch, assignments=(_legacy_assignment(),))
    monkeypatch.setattr(
        staffing_routes,
        "assignments_todo_payload",
        lambda: pytest.fail("authoritative day called legacy production provider"),
    )
    monkeypatch.setattr(
        missing_wc,
        "current_rows",
        lambda: pytest.fail("authoritative day called legacy location provider"),
    )
    if builder_failure:
        monkeypatch.setattr(
            attendance_exceptions,
            "build_snapshot",
            lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("attendance projection failed")),
        )
        monkeypatch.setattr(
            attendance_location_policy,
            "get_rollout_config",
            lambda: attendance_location_policy.RolloutConfig("shadow", NOW, None),
        )
        monkeypatch.setattr(
            attendance_location_policy,
            "match_state_for_day",
            lambda *_a, **_k: production_mode,
        )
    else:
        monkeypatch.setattr(
            attendance_exceptions,
            "build_snapshot",
            lambda *_a, **_k: _attendance_snapshot(
                mode="shadow",
                production_mode=production_mode,
                complete=False,
                source_errors=("Attendance Timeline",),
            ),
        )

    snapshot = exception_inbox.build_snapshot()
    sections = {section["id"]: section for section in snapshot["sections"]}

    assert sections["assignments"]["count"] == 0
    assert sections["assignments"]["rows"] == []
    assert "missing_wc" not in sections
    if builder_failure:
        assert sections["production_source_unavailable"]["count"] == 1
        sources = {error["source"] for error in snapshot["source_errors"]}
        assert {"Attendance Timeline", "Strict Production"} <= sources


def test_summary_and_snapshot_count_the_same_attendance_items(monkeypatch):
    _empty_legacy(monkeypatch)
    issues = (
        _issue("attendance_unmapped_location", "attendance_unmapped_location:42:901:x"),
        _issue(
            "attendance_duplicate_location",
            "attendance_duplicate_location:42:901:x",
            priority="muted",
        ),
    )
    monkeypatch.setattr(
        attendance_exceptions,
        "build_snapshot",
        lambda *_a, **_k: _attendance_snapshot(mode="shadow", issues=issues),
    )

    summary = exception_inbox.build_summary()
    snapshot = exception_inbox.build_snapshot()

    full_counts = {section["id"]: section["count"] for section in snapshot["sections"]}
    assert (
        summary["sections"]["attendance_unmapped_location"]
        == full_counts["attendance_unmapped_location"]
        == 1
    )
    assert (
        summary["sections"]["attendance_duplicate_location"]
        == full_counts["attendance_duplicate_location"]
        == 1
    )
    assert summary["total"] == snapshot["total"]
    assert summary["urgent_total"] == snapshot["urgent_total"]
    assert summary["follow_up_total"] == snapshot["follow_up_total"]


def test_incomplete_timeline_sections_cannot_auto_resolve(monkeypatch):
    snapshot = {
        "source_errors": [{"source": "Attendance Timeline"}],
        "sections": [
            {
                "id": "attendance_missing_location",
                "count": 0,
                "rows": [],
                "complete": False,
            },
            {
                "id": "production_unassigned_run",
                "count": 0,
                "rows": [],
                "complete": False,
            },
        ],
    }

    assert "attendance_missing_location" not in inbox_reconcile._complete_kinds(snapshot)
    assert "production_unassigned_run" not in inbox_reconcile._complete_kinds(snapshot)

    snapshot["source_errors"] = []
    for section in snapshot["sections"]:
        section["complete"] = True
    complete = inbox_reconcile._complete_kinds(snapshot)
    assert "attendance_missing_location" in complete
    assert "production_unassigned_run" in complete


def test_departure_waits_for_linked_correction_completion(monkeypatch):
    statuses = [{"status": "verifying"}]

    class CorrectionCursor:
        def execute(self, _sql, _params=None):
            pass

        def fetchone(self):
            return statuses[0]

    @contextmanager
    def cursor():
        yield CorrectionCursor()

    monkeypatch.setattr(inbox_reconcile.db, "cursor", cursor)
    assert inbox_reconcile._correction_allows_resolution("production_unassigned_run:wc:x") is False
    statuses[0]["status"] = "complete"
    assert inbox_reconcile._correction_allows_resolution("production_unassigned_run:wc:x") is True


def test_correction_lookup_failure_keeps_item_open(monkeypatch):
    @contextmanager
    def failed_cursor():
        raise RuntimeError("database unavailable")
        yield

    monkeypatch.setattr(inbox_reconcile.db, "cursor", failed_cursor)

    assert inbox_reconcile._correction_allows_resolution("production_unassigned_run:wc:x") is False


def test_missing_wc_warmer_noops_only_after_baseline_in_shadow_or_live(monkeypatch):
    calls = []
    config = {"value": attendance_location_policy.RolloutConfig("off", None, None)}
    health = {"value": attendance_mirror.MirrorHealth(NOW, NOW, NOW, None, None)}
    monkeypatch.setattr(attendance_location_policy, "get_rollout_config", lambda: config["value"])
    monkeypatch.setattr(attendance_mirror, "health_snapshot", lambda: health["value"])
    monkeypatch.setattr(
        "zira_dashboard.odoo_client.fetch_attendances_missing_wc",
        lambda _since: calls.append("fetch") or [],
    )
    monkeypatch.setattr(missing_wc, "write_cache", lambda _rows: calls.append("write"))

    asyncio.run(app._tick_missing_wc())
    assert calls == ["fetch", "write"]

    calls.clear()
    config["value"] = attendance_location_policy.RolloutConfig("shadow", NOW, None)
    health["value"] = attendance_mirror.MirrorHealth(NOW, NOW, None, None, None)
    asyncio.run(app._tick_missing_wc())
    assert calls == ["fetch", "write"]

    calls.clear()
    health["value"] = attendance_mirror.MirrorHealth(NOW, NOW, NOW, None, None)
    asyncio.run(app._tick_missing_wc())
    assert calls == []

    calls.clear()
    monkeypatch.setattr(
        attendance_location_policy,
        "get_rollout_config",
        lambda: (_ for _ in ()).throw(RuntimeError("settings unavailable")),
    )
    asyncio.run(app._tick_missing_wc())
    assert calls == ["fetch", "write"]
