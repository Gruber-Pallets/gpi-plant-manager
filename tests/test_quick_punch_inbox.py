"""Exception Inbox visibility and alerts for the quick-punch fixer."""

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from zira_dashboard import (
    attendance_corrections,
    inbox_keys,
    inbox_log,
    quick_punch_inbox,
)
from zira_dashboard.routes.exceptions import _group_archive_by_day

CT = ZoneInfo("America/Chicago")
STATIC = Path("src/zira_dashboard/static/exceptions.js")
TEMPLATE = Path("src/zira_dashboard/templates/exceptions.html")


def ct(hour, minute=0):
    return datetime(2026, 9, 18, hour, minute, tzinfo=CT).astimezone(UTC)


def test_alert_key_is_distinct_from_the_job_key():
    assert inbox_keys.quick_punch_alert(12) == "quick-punch-alert:12"
    assert not inbox_keys.quick_punch_alert(12).startswith(
        attendance_corrections.QUICK_PUNCH_ITEM_KEY_PREFIX
    )


def test_reason_text_translates_codes_and_partial_writes():
    assert quick_punch_inbox.reason_text({"reason_code": "attempt_limit"}) == (
        "Odoo kept refusing the change"
    )
    assert quick_punch_inbox.reason_text({"reason_code": "preflight_source_changed"}) == (
        "Someone changed these punches in Odoo first"
    )
    assert quick_punch_inbox.reason_text(
        {"reason_code": "attempt_limit", "completed_operations": 2}
    ) == "Odoo was partly changed — Odoo kept refusing the change"
    assert quick_punch_inbox.reason_text({"uncertain_operation": True}) == (
        "Odoo may have been changed"
    )
    assert quick_punch_inbox.reason_text({}, stuck=True) == "Odoo partly changed, still retrying"


def test_archive_grouping_carries_after_value_and_source():
    resolved = datetime(2026, 9, 18, 12, 0, tzinfo=UTC)
    groups = _group_archive_by_day(
        [
            {
                "id": 1,
                "item_kind": "quick_punch_fix",
                "item_key": "quick-punch:8:abc",
                "person_name": "Christian C.",
                "category_label": "Quick-punch auto-fix",
                "action": "quick_punch_would_merge",
                "outcome": "Preview — Odoo not changed",
                "before_value": "D3 7:00–7:02 · D2 7:02–7:04 · D3 7:04–now",
                "after_value": "D3 7:00–now",
                "reason": None,
                "actor_name": "Quick-punch auto-fix",
                "actor_upn": "system:quick-punch",
                "source": "auto",
                "resolved_at": resolved,
            }
        ]
    )
    event = groups[0]["events"][0]
    assert event["after_value"] == "D3 7:00–now"
    assert event["source"] == "auto"
    assert event["auto"] is False


def test_archive_query_includes_fixer_events_when_hiding_auto(monkeypatch):
    captured = {}

    def query(sql, params=None):
        captured["sql"] = sql
        captured["params"] = params
        return [{"id": 1, "actor_upn": "system:quick-punch"}]

    monkeypatch.setattr(inbox_log, "db", SimpleNamespace(query=query), raising=False)
    monkeypatch.setattr("zira_dashboard.inbox_log.db.query", query)
    rows = inbox_log.archive(include_auto=False, limit=50)
    assert "actor_upn IS NOT NULL" in captured["sql"]
    assert rows[0]["actor_upn"] == "system:quick-punch"


def test_has_human_event_since_ignores_unrelated_fixer_keys(monkeypatch):
    seen = {}

    def query(sql, params=None):
        seen["params"] = params
        return []

    monkeypatch.setattr("zira_dashboard.inbox_log.db.query", query)
    assert inbox_log.has_human_event_since("time_off:99", ct(8)) is False
    assert seen["params"][0] == "time_off:99"


def test_failure_section_appears_and_disappears_after_ack(monkeypatch):
    job = {
        "id": 44,
        "item_key": "quick-punch:8:abc",
        "status": "failed",
        "attempt_count": 6,
        "created_at": ct(7),
        "updated_at": ct(8),
        "completed_operations": [],
        "audit_summary": {
            "person_name": "Christian C.",
            "before": "D3 7:00–now",
            "after": "D3 7:00–now",
        },
    }
    events = {
        "quick-punch:8:abc": {
            "item_key": "quick-punch:8:abc",
            "person_name": "Christian C.",
            "before_value": "D3 7:00–7:02 · D2 7:02–7:04 · D3 7:04–now",
            "outcome": "attempt_limit",
            "detail": {"reason_code": "attempt_limit", "completed_operations": 0},
        }
    }
    monkeypatch.setattr(
        "zira_dashboard.quick_punch_inbox.db",
        SimpleNamespace(query=lambda *a, **k: [job] if "status = 'failed'" in a[0] else []),
        raising=False,
    )

    def fake_query(sql, params=None):
        if "status = 'failed'" in sql:
            return [job]
        return []

    monkeypatch.setattr("zira_dashboard.db.query", fake_query)
    monkeypatch.setattr(quick_punch_inbox, "_failure_events", lambda keys: events)
    monkeypatch.setattr(quick_punch_inbox, "_acked_keys", lambda keys: set())
    rows = quick_punch_inbox.current_rows(now_utc=ct(9))
    assert len(rows) == 1
    assert rows[0]["name"] == "Christian C."
    assert rows[0]["item_key"] == "quick-punch-alert:44"
    assert rows[0]["action"] == {"type": "quick_punch_ack", "job_id": 44}
    assert "couldn't finish" in rows[0]["label"]

    monkeypatch.setattr(quick_punch_inbox, "_acked_keys", lambda keys: {rows[0]["item_key"]})
    assert quick_punch_inbox.current_rows(now_utc=ct(9)) == []


def test_stuck_partial_job_is_listed(monkeypatch):
    job = {
        "id": 9,
        "item_key": "quick-punch:8:abc",
        "status": "applying",
        "attempt_count": 6,
        "created_at": ct(7),
        "updated_at": ct(8),
        "completed_operations": [{"operation_key": "k", "kind": "delete", "attendance_id": 1}],
        "audit_summary": {"person_name": "Christian C.", "before": "D3 7:00–now", "after": "D3 7:00–now"},
    }

    def fake_query(sql, params=None):
        if "status = 'failed'" in sql:
            return []
        return [job]

    monkeypatch.setattr("zira_dashboard.db.query", fake_query)
    monkeypatch.setattr(quick_punch_inbox, "_failure_events", lambda keys: {})
    monkeypatch.setattr(quick_punch_inbox, "_acked_keys", lambda keys: set())
    rows = quick_punch_inbox.current_rows(now_utc=ct(9))
    assert rows[0]["label"] == "Odoo partly changed, still retrying"
    assert rows[0]["item_key"] == "quick-punch-alert:9"


def test_snapshot_includes_the_section_only_when_rows_exist(monkeypatch):
    from zira_dashboard import exception_inbox

    def fake_capture(errors, source, call, fallback):
        if source == "Quick-punch fixer":
            return call()
        return fallback

    row = {
        "name": "Christian C.",
        "label": "Quick-punch fix couldn't finish — check Odoo",
        "detail": "Odoo kept refusing the change",
        "priority": "urgent",
        "item_key": "quick-punch-alert:44",
        "action": {"type": "quick_punch_ack", "job_id": 44},
    }
    monkeypatch.setattr(exception_inbox, "_capture", fake_capture)
    monkeypatch.setattr(exception_inbox, "_attendance_snapshot", lambda *a, **k: SimpleNamespace(
        mode="live", issues=[], issues_for=lambda kind: []
    ))
    monkeypatch.setattr(exception_inbox, "_strict_production_owns_day", lambda snap: True)
    monkeypatch.setattr(exception_inbox, "_attendance_owns_location", lambda snap: True)
    monkeypatch.setattr(exception_inbox, "_attendance_sections_should_render", lambda snap: False)
    monkeypatch.setattr(exception_inbox, "_plant_schedule_reminder", lambda: (0, []))
    monkeypatch.setattr(exception_inbox, "_saturday_staffing_actions", lambda day: (0, []))
    monkeypatch.setattr(exception_inbox, "_pending_time_off", lambda day: (0, []))
    monkeypatch.setattr(exception_inbox, "_pending_absence_pto", lambda: (0, []))
    monkeypatch.setattr("zira_dashboard.quick_punch_inbox.current_rows", lambda: [row])
    monkeypatch.setattr("zira_dashboard.auto_lunch_guard.current_snapshot", lambda: SimpleNamespace(
        alert=None, degraded=False
    ))
    monkeypatch.setattr("zira_dashboard.machine_breakdown.current_rows", lambda: [])
    monkeypatch.setattr("zira_dashboard.missed_punch_out.current_rows", lambda: [])
    monkeypatch.setattr("zira_dashboard.unexpected_worker.open_events", lambda day: [])
    monkeypatch.setattr("zira_dashboard.odoo_sync.roster_sync_alert", lambda: None)
    monkeypatch.setattr("zira_dashboard.routes.staffing.late_report_payload", lambda: {})
    snapshot = exception_inbox.build_snapshot()
    section = next(item for item in snapshot["sections"] if item["id"] == "quick_punch")
    assert section["title"] == "Quick-punch fixes that need a look"
    assert section["count"] == 1
    assert section["rows"] == [row]
    assert any(item["item_key"] == "quick-punch-alert:44" for item in snapshot["queue"])

    monkeypatch.setattr("zira_dashboard.quick_punch_inbox.current_rows", lambda: [])
    empty = exception_inbox.build_snapshot()
    empty_section = next(item for item in empty["sections"] if item["id"] == "quick_punch")
    assert empty_section["rows"] == []
    assert not any(item.get("section_id") == "quick_punch" for item in empty["queue"])


def test_acknowledge_records_a_real_actor(monkeypatch):
    seen = {}

    def record_event(**kwargs):
        seen.update(kwargs)
        return 17

    monkeypatch.setattr(inbox_log, "record_event", record_event)
    assert quick_punch_inbox.acknowledge(44, actor_upn="dale@x", actor_name="Dale") == 17
    assert seen["action"] == "quick_punch_failure_ack"
    assert seen["item_key"] == "quick-punch-alert:44"
    assert seen["actor_upn"] == "dale@x"
    assert seen["source"] == "inbox"


def test_fixer_progress_flags_a_reserved_unconfirmed_write():
    row = {
        "completed_operations": [
            {"operation_key": "k1", "kind": "delete", "attendance_id": 1},
            {
                "operation_key": "k2",
                "reservation_token": "token",
                "reservation_attempt_count": 1,
                "reservation_until": "2026-09-18T12:00:00+00:00",
            },
        ],
        "employee_odoo_ids": "not-json-plans",
    }
    progress = attendance_corrections._fixer_progress(row)
    assert progress["completed_operations"] == 1
    assert progress["deleted_attendance_ids"] == [1]
    assert progress["uncertain_operation"] is True
    assert "total_operations" not in progress


def test_fixer_progress_never_raises():
    progress = attendance_corrections._fixer_progress({"completed_operations": object()})
    assert progress["completed_operations"] == 0
    assert progress["deleted_attendance_ids"] == []


def test_event_detail_accepts_uncertain_operation():
    detail = attendance_corrections._event_detail(
        job_id=1, uncertain_operation=True, completed_operations=1
    )
    assert detail["uncertain_operation"] is True
    omitted = attendance_corrections._event_detail(job_id=1, uncertain_operation=False)
    assert "uncertain_operation" not in omitted


def test_js_renders_quick_punch_archive_lines():
    js = STATIC.read_text(encoding="utf-8")
    assert "quick_punch_would_merge" in js
    assert "quick_punch_merged" in js
    assert "quick_punch_failed" in js
    assert "return {text: '⤳', cls: 'ok'}" in js
    assert "return 'Would merge'" in js
    assert "return 'Merged'" in js
    assert "before_value + ' → ' + event.after_value" in js
    assert "(was ' + event.before_value + ')'" in js
    assert "js-quick-punch-ack" in js
    assert "/api/exceptions/quick-punch/ack" in js


def test_template_has_mark_checked():
    html = TEMPLATE.read_text(encoding="utf-8")
    assert "js-quick-punch-ack" in html
    assert "Mark checked" in html
    assert "data-job-id" in html


def test_manager_correction_archive_line_is_unchanged():
    js = STATIC.read_text(encoding="utf-8")
    assert "if (event.before_value) text += ' (was ' + event.before_value + ')'" in js or (
        "else if (event.before_value)" in js and "(was ' + event.before_value + ')'" in js
    )
