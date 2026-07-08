"""inbox_reconcile: pure diff + complete-kinds guard + run_once + degraded wiring."""
from datetime import datetime, timezone

from zira_dashboard import inbox_reconcile


def test_plan_reconcile_reports_departed_only_for_complete_kinds():
    prev = {
        "missing_wc:1": {"item_kind": "missing_wc"},
        "time_off:9": {"item_kind": "time_off"},
        "late:5:2026-06-26": {"item_kind": "late"},
    }
    open_now = {
        "missing_wc:1": {"item_kind": "missing_wc"},              # still open
        "missed_punch_out:7": {"item_kind": "missed_punch_out"},  # new
    }
    # time_off was NOT fully enumerated this tick (errored or truncated).
    complete = {"missing_wc", "late", "missed_punch_out", "assignment", "plant_schedule"}

    actions = inbox_reconcile.plan_reconcile(open_now, prev, complete)

    assert set(actions["arrivals"]) == {"missed_punch_out:7"}
    assert actions["still_open"] == ["missing_wc:1"]
    assert "late:5:2026-06-26" in actions["departed"]   # left, kind complete
    assert "time_off:9" not in actions["departed"]       # kind not complete -> kept


def test_complete_kinds_skips_errored_and_truncated():
    snapshot = {
        "source_errors": [{"source": "Pending Time Off"}],          # time_off errored
        "sections": [
            {"id": "missing_wc", "count": 1, "rows": [{"x": 1}]},   # complete
            {"id": "time_off", "count": 0, "rows": []},             # errored -> skip
            {"id": "late", "count": 9, "rows": [{"x": 1}, {"x": 2}]},  # truncated -> skip
            {"id": "missed_punch_out", "count": 0, "rows": []},     # complete (empty)
        ],
    }
    complete = inbox_reconcile._complete_kinds(snapshot)
    assert "missing_wc" in complete
    assert "missed_punch_out" in complete
    assert "time_off" not in complete   # source errored
    assert "late" not in complete       # rows(2) < count(9) -> truncated by a cap


def test_complete_kinds_includes_late_despite_snoozed_padding():
    """build_snapshot appends `snoozed` rows to the late queue, but the late
    `count` sums only the three actionable buckets -> len(rows) > count. A
    display cap can only ever HIDE rows (shown < count), so more-rows-than-count
    is never truncation. late must stay fully-enumerated so a legitimate late
    self-clear auto-resolves promptly instead of waiting for a snooze-free tick."""
    snapshot = {
        "source_errors": [],
        "sections": [
            {
                "id": "late",
                "count": 1,  # one actionable item (e.g. scheduled_late)
                "rows": [
                    {"item_key": "late:5:2026-06-26", "priority": "urgent"},  # counted
                    {"item_key": "late:8:2026-06-26", "priority": "muted"},   # snoozed, NOT counted
                ],
            },
        ],
    }
    assert "late" in inbox_reconcile._complete_kinds(snapshot)


def test_complete_kinds_includes_late_when_item_key_repeats_across_buckets():
    """All four late buckets share one item_key per employee, so an employee in
    two buckets yields two rows but a single open-set key. len(rows) still
    exceeds count; late must not be dropped from complete_kinds over it."""
    snapshot = {
        "source_errors": [],
        "sections": [
            {
                "id": "late",
                "count": 1,
                "rows": [
                    {"item_key": "late:5:2026-06-26", "priority": "urgent"},  # scheduled late
                    {"item_key": "late:5:2026-06-26", "priority": "muted"},   # same emp, snoozed
                ],
            },
        ],
    }
    assert "late" in inbox_reconcile._complete_kinds(snapshot)


def _mirror_row(**over):
    base = {
        "item_key": "missing_wc:1", "item_kind": "missing_wc",
        "person_name": "Maria", "category_label": "Missing WC",
        "first_seen": datetime(2026, 6, 1, tzinfo=timezone.utc),
        "last_seen": datetime(2026, 6, 1, tzinfo=timezone.utc),  # long ago -> past grace
    }
    base.update(over)
    return base


def _snap_complete_missing_wc():
    # Nothing open now; the missing_wc section is fully enumerated (0 == 0).
    return {"queue": [], "source_errors": [],
            "sections": [{"id": "missing_wc", "count": 0, "rows": []}]}


def test_run_once_logs_auto_resolved_for_silent_departure(monkeypatch):
    from zira_dashboard import exception_inbox, inbox_log

    monkeypatch.setattr(exception_inbox, "build_snapshot", _snap_complete_missing_wc)
    monkeypatch.setattr(inbox_reconcile, "_read_mirror", lambda: {"missing_wc:1": _mirror_row()})
    deleted, logged = [], []
    monkeypatch.setattr(inbox_reconcile, "_upsert", lambda k, i: None)
    monkeypatch.setattr(inbox_reconcile, "_delete", lambda k: deleted.append(k))
    monkeypatch.setattr(inbox_log, "has_human_event_since", lambda k, s: False)
    monkeypatch.setattr(inbox_log, "log_event_safe", lambda **kw: logged.append(kw) or 1)

    inbox_reconcile.run_once()

    assert deleted == ["missing_wc:1"]
    assert len(logged) == 1
    assert logged[0]["action"] == "auto_resolved"
    assert logged[0]["item_key"] == "missing_wc:1"
    assert logged[0]["actor_upn"] is None


def test_run_once_skips_auto_when_human_resolved(monkeypatch):
    from zira_dashboard import exception_inbox, inbox_log

    monkeypatch.setattr(exception_inbox, "build_snapshot", _snap_complete_missing_wc)
    monkeypatch.setattr(inbox_reconcile, "_read_mirror", lambda: {"missing_wc:1": _mirror_row()})
    deleted, logged = [], []
    monkeypatch.setattr(inbox_reconcile, "_upsert", lambda k, i: None)
    monkeypatch.setattr(inbox_reconcile, "_delete", lambda k: deleted.append(k))
    monkeypatch.setattr(inbox_log, "has_human_event_since", lambda k, s: True)
    monkeypatch.setattr(inbox_log, "log_event_safe", lambda **kw: logged.append(kw) or 1)

    inbox_reconcile.run_once()

    assert deleted == ["missing_wc:1"]  # mirror row cleared
    assert logged == []                 # but NOT logged auto_resolved (human did it)


def test_run_once_respects_grace_period(monkeypatch):
    from zira_dashboard import exception_inbox, inbox_log, plant_day

    monkeypatch.setattr(exception_inbox, "build_snapshot", _snap_complete_missing_wc)
    # last_seen is "just now" -> within the grace window -> must be left for next tick.
    monkeypatch.setattr(inbox_reconcile, "_read_mirror",
                        lambda: {"missing_wc:1": _mirror_row(last_seen=plant_day.now())})
    deleted, logged = [], []
    monkeypatch.setattr(inbox_reconcile, "_upsert", lambda k, i: None)
    monkeypatch.setattr(inbox_reconcile, "_delete", lambda k: deleted.append(k))
    monkeypatch.setattr(inbox_log, "has_human_event_since", lambda k, s: False)
    monkeypatch.setattr(inbox_log, "log_event_safe", lambda **kw: logged.append(kw) or 1)

    inbox_reconcile.run_once()

    assert deleted == []   # too recent -> not auto-resolved this tick
    assert logged == []


def test_build_snapshot_flags_degraded_source_into_source_errors(monkeypatch):
    """The Critical guard: a late/assignments payload that swallowed its error
    (degraded=True) must surface in source_errors so the reconciler skips it."""
    from zira_dashboard import exception_inbox, missing_wc, missed_punch_out
    from zira_dashboard.routes import staffing as staffing_routes

    monkeypatch.setattr(staffing_routes, "assignments_todo_payload",
                        lambda: {"degraded": True, "count": 0, "items": [], "people": []})
    monkeypatch.setattr(staffing_routes, "late_report_payload",
                        lambda: {"count": 0, "scheduled_late": [], "unscheduled_late": [],
                                 "needs_reason": [], "snoozed": []})
    monkeypatch.setattr(missing_wc, "current_rows", lambda: [])
    monkeypatch.setattr(missed_punch_out, "current_rows", lambda: [])
    monkeypatch.setattr(exception_inbox, "_pending_time_off", lambda today: (0, []))
    monkeypatch.setattr(exception_inbox, "_plant_schedule_reminder", lambda: (0, []))
    monkeypatch.setattr(exception_inbox, "_work_center_names", lambda: [])

    snap = exception_inbox.build_snapshot()
    sources = {e["source"] for e in snap["source_errors"]}
    assert "Assignments To Do" in sources   # degraded -> flagged as a source error
    assert "Late / Absence" not in sources  # healthy -> not flagged


def test_reconcile_tick_is_registered():
    from zira_dashboard import app
    names = [w[0] for w in app._WARMERS]
    assert "Inbox reconcile" in names
    entry = next(w for w in app._WARMERS if w[0] == "Inbox reconcile")
    assert entry[2] == 60  # seconds


def test_run_once_auto_resolves_departed_breakdown_row(monkeypatch):
    from zira_dashboard import exception_inbox, inbox_log

    def _snap():
        return {"queue": [], "source_errors": [],
                "sections": [{"id": "breakdown", "count": 0, "rows": []}]}

    monkeypatch.setattr(exception_inbox, "build_snapshot", _snap)
    monkeypatch.setattr(inbox_reconcile, "_read_mirror", lambda: {
        "breakdown:Dismantler 2:x": _mirror_row(
            item_key="breakdown:Dismantler 2:x", item_kind="breakdown",
            category_label="Machine Breakdown"),
    })
    deleted, logged = [], []
    monkeypatch.setattr(inbox_reconcile, "_upsert", lambda k, i: None)
    monkeypatch.setattr(inbox_reconcile, "_delete", lambda k: deleted.append(k))
    monkeypatch.setattr(inbox_log, "has_human_event_since", lambda k, s: False)
    monkeypatch.setattr(inbox_log, "log_event_safe", lambda **kw: logged.append(kw) or 1)

    inbox_reconcile.run_once()

    assert deleted == ["breakdown:Dismantler 2:x"]
    assert logged[0]["item_kind"] == "breakdown"
    assert logged[0]["action"] == "auto_resolved"
