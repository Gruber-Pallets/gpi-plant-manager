"""inbox_reconcile: pure diff + run_once orchestration (no DB/Odoo)."""
from datetime import datetime, timezone

from zira_dashboard import inbox_reconcile


def test_plan_reconcile_classifies_arrivals_departures_and_skips_errored():
    prev = {
        "missing_wc:1": {"item_kind": "missing_wc"},
        "time_off:9": {"item_kind": "time_off"},
        "late:5:2026-06-26": {"item_kind": "late"},
    }
    open_now = {
        "missing_wc:1": {"item_kind": "missing_wc"},       # still open
        "missed_punch_out:7": {"item_kind": "missed_punch_out"},  # new
    }
    errored = {"Pending Time Off"}  # the time_off source failed this tick

    actions = inbox_reconcile.plan_reconcile(open_now, prev, errored)

    assert set(actions["arrivals"]) == {"missed_punch_out:7"}
    assert actions["still_open"] == ["missing_wc:1"]
    assert "late:5:2026-06-26" in actions["departed"]   # left, source healthy
    assert "time_off:9" not in actions["departed"]       # source errored -> kept


def test_run_once_logs_auto_resolved_for_silent_departure(monkeypatch):
    from zira_dashboard import exception_inbox, inbox_log

    snap = {"queue": [], "source_errors": []}  # nothing open now
    monkeypatch.setattr(exception_inbox, "build_snapshot", lambda: snap)
    monkeypatch.setattr(inbox_reconcile, "_read_mirror", lambda: {
        "missing_wc:1": {
            "item_key": "missing_wc:1", "item_kind": "missing_wc",
            "person_name": "Maria", "category_label": "Missing WC",
            "first_seen": datetime(2026, 6, 26, tzinfo=timezone.utc),
        },
    })
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

    monkeypatch.setattr(exception_inbox, "build_snapshot", lambda: {"queue": [], "source_errors": []})
    monkeypatch.setattr(inbox_reconcile, "_read_mirror", lambda: {
        "missing_wc:1": {
            "item_key": "missing_wc:1", "item_kind": "missing_wc",
            "person_name": "Maria", "category_label": "Missing WC",
            "first_seen": datetime(2026, 6, 26, tzinfo=timezone.utc),
        },
    })
    deleted, logged = [], []
    monkeypatch.setattr(inbox_reconcile, "_upsert", lambda k, i: None)
    monkeypatch.setattr(inbox_reconcile, "_delete", lambda k: deleted.append(k))
    monkeypatch.setattr(inbox_log, "has_human_event_since", lambda k, s: True)
    monkeypatch.setattr(inbox_log, "log_event_safe", lambda **kw: logged.append(kw) or 1)

    inbox_reconcile.run_once()

    assert deleted == ["missing_wc:1"]  # mirror row cleared
    assert logged == []                 # but NOT logged auto_resolved (human did it)
