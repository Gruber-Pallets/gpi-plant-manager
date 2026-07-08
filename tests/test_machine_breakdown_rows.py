"""Tests for current_rows()/run_detect_tick()/report_manual() -- the I/O glue.
Heavy monkeypatching of collaborators, following tests/test_inbox_reconcile.py's style."""
from datetime import date, datetime, timedelta, timezone

from zira_dashboard import machine_breakdown


def _now():
    return datetime(2026, 7, 8, 18, 22, tzinfo=timezone.utc)  # 1:22 PM Central


def test_run_detect_tick_opens_new_incident(monkeypatch):
    stop = _now() - timedelta(minutes=20)
    monkeypatch.setattr(machine_breakdown, "_station_signals", lambda day, now: [
        machine_breakdown.StationSignal("Dismantler 2", stop, True)
    ])
    monkeypatch.setattr(machine_breakdown, "_shift_bounds", lambda day: (
        _now() - timedelta(hours=6), _now() + timedelta(hours=2)
    ))
    monkeypatch.setattr(machine_breakdown, "get_open_incident", lambda wc, day: None)
    opened = {}

    def _open_incident(wc, day, stop_utc, source):
        # NB: `opened.setdefault(...) or 1` looks tempting here but always
        # returns the (truthy) tuple, never 1 -- setdefault returns the
        # stored value, not a success flag.
        opened["args"] = (wc, day, stop_utc, source)
        return 1

    monkeypatch.setattr(machine_breakdown, "open_incident", _open_incident)
    monkeypatch.setattr(machine_breakdown, "_operators_on_wc", lambda wc, day: ["Juan"])
    from zira_dashboard import wc_attributions
    added = []
    monkeypatch.setattr(wc_attributions, "add_breakdown",
                        lambda day, wc, person, start, breakdown_id: added.append((day, wc, person, start, breakdown_id)) or 99)
    monkeypatch.setattr(machine_breakdown, "_cap_departed_operators", lambda incident, day, now: None)
    monkeypatch.setattr(machine_breakdown, "_maybe_auto_resolve", lambda incident, day, now: None)

    machine_breakdown.run_detect_tick(day=date(2026, 7, 8), now=_now())

    assert opened["args"] == ("Dismantler 2", date(2026, 7, 8), stop, "auto")
    assert added == [(date(2026, 7, 8), "Dismantler 2", "Juan", stop, 1)]


def test_run_detect_tick_skips_wc_with_open_incident(monkeypatch):
    stop = _now() - timedelta(minutes=20)
    monkeypatch.setattr(machine_breakdown, "_station_signals", lambda day, now: [
        machine_breakdown.StationSignal("Dismantler 2", stop, True)
    ])
    monkeypatch.setattr(machine_breakdown, "_shift_bounds", lambda day: (
        _now() - timedelta(hours=6), _now() + timedelta(hours=2)
    ))
    monkeypatch.setattr(machine_breakdown, "get_open_incident", lambda wc, day: {"id": 5})
    called = []
    monkeypatch.setattr(machine_breakdown, "open_incident", lambda *a, **k: called.append(1))
    monkeypatch.setattr(machine_breakdown, "_cap_departed_operators", lambda incident, day, now: None)
    monkeypatch.setattr(machine_breakdown, "_maybe_auto_resolve", lambda incident, day, now: None)

    machine_breakdown.run_detect_tick(day=date(2026, 7, 8), now=_now())

    assert called == []


def test_cap_departed_operators_caps_and_leaves_still_present_untouched(monkeypatch):
    from zira_dashboard import wc_attributions
    incident = {"id": 1, "wc_name": "Dismantler 2", "day": date(2026, 7, 8),
                "detected_stop_utc": _now() - timedelta(minutes=30)}
    monkeypatch.setattr(machine_breakdown, "_operators_on_wc", lambda wc, day: ["Juan", "Benjamin"])
    dep_end = _now() - timedelta(minutes=5)
    monkeypatch.setattr(machine_breakdown, "_punch_windows_for_day", lambda day: {
        "Juan": [("Dismantler 2", _now() - timedelta(hours=6), dep_end)],
        "Benjamin": [("Dismantler 2", _now() - timedelta(hours=6), None)],
    })
    monkeypatch.setattr(wc_attributions, "open_breakdown_row",
                        lambda day, wc, person: {"id": 10, "start_utc": incident["detected_stop_utc"]} if person == "Juan" else {"id": 11, "start_utc": incident["detected_stop_utc"]})
    capped = []
    monkeypatch.setattr(wc_attributions, "cap_breakdown", lambda row_id, end: capped.append((row_id, end)))

    machine_breakdown._cap_departed_operators(incident, date(2026, 7, 8), _now())

    assert capped == [(10, dep_end)]  # only Juan (closed window); Benjamin still open


def test_maybe_auto_resolve_resolves_when_station_producing_again(monkeypatch):
    incident = {"id": 1, "wc_name": "Dismantler 2", "day": date(2026, 7, 8),
                "detected_stop_utc": _now() - timedelta(minutes=30)}
    resume = _now() - timedelta(minutes=2)
    monkeypatch.setattr(machine_breakdown, "_last_output_after", lambda wc, day, stop: resume)
    monkeypatch.setattr(machine_breakdown, "_operators_on_wc", lambda wc, day: ["Juan"])
    from zira_dashboard import wc_attributions
    monkeypatch.setattr(wc_attributions, "open_breakdown_row", lambda day, wc, person: {"id": 10})
    capped = []
    monkeypatch.setattr(wc_attributions, "cap_breakdown", lambda row_id, end: capped.append((row_id, end)))
    resolved = []
    monkeypatch.setattr(machine_breakdown, "resolve_incident",
                        lambda incident_id, resolution, resume_utc=None: resolved.append((incident_id, resolution, resume_utc)))

    machine_breakdown._maybe_auto_resolve(incident, date(2026, 7, 8), _now())

    assert resolved == [(1, "recovered", resume)]
    assert capped == [(10, resume)]  # any operator still open gets capped at resume


def test_maybe_auto_resolve_noop_when_still_down(monkeypatch):
    incident = {"id": 1, "wc_name": "Dismantler 2", "day": date(2026, 7, 8),
                "detected_stop_utc": _now() - timedelta(minutes=30)}
    monkeypatch.setattr(machine_breakdown, "_last_output_after", lambda wc, day, stop: None)
    resolved = []
    monkeypatch.setattr(machine_breakdown, "resolve_incident", lambda *a, **k: resolved.append(1))

    machine_breakdown._maybe_auto_resolve(incident, date(2026, 7, 8), _now())

    assert resolved == []


def test_current_rows_shapes_header_and_operator_rows(monkeypatch):
    incident = {"id": 1, "wc_name": "Dismantler 2", "day": date(2026, 7, 8),
                "detected_stop_utc": _now() - timedelta(minutes=25),
                "source": "auto", "resolved_at": None, "resolution": None}
    monkeypatch.setattr(machine_breakdown, "all_open_incidents", lambda day: [incident])
    monkeypatch.setattr(machine_breakdown, "_operators_on_wc", lambda wc, day: ["Juan", "Benjamin"])
    monkeypatch.setattr(machine_breakdown, "active_snooze_until",
                        lambda incident_id, person: (_now() + timedelta(minutes=10)) if person == "Benjamin" else None)
    from zira_dashboard import staffing
    monkeypatch.setattr(staffing, "LOCATIONS", [])

    rows = machine_breakdown.current_rows(day=date(2026, 7, 8), now=_now())

    # The header (machine) row is the only row carrying a dismiss_action --
    # both it and a snoozed operator row have action=None, so that alone
    # can't distinguish them.
    header = [r for r in rows if r.get("dismiss_action") is not None]
    assert len(header) == 1
    assert header[0]["name"] == "Dismantler 2"
    assert header[0]["priority"] == "urgent"

    juan_row = [r for r in rows if r.get("action") and r["action"].get("person_name") == "Juan"][0]
    assert juan_row["action"]["type"] == "breakdown"
    assert juan_row["priority"] == "urgent"

    benjamin_row = [r for r in rows if r["name"] == "Benjamin"][0]
    assert benjamin_row["priority"] == "muted"
    assert benjamin_row.get("action") is None  # snoozed -- no action buttons


def test_report_manual_opens_incident_with_operators(monkeypatch):
    monkeypatch.setattr(machine_breakdown, "get_open_incident", lambda wc, day: None)
    monkeypatch.setattr(machine_breakdown, "_last_output_before", lambda wc, day, now: None)
    opened = {}
    monkeypatch.setattr(machine_breakdown, "open_incident",
                        lambda wc, day, stop_utc, source: opened.setdefault("args", (wc, day, stop_utc, source)) or 1)
    monkeypatch.setattr(machine_breakdown, "_operators_on_wc", lambda wc, day: ["Juan"])
    from zira_dashboard import wc_attributions
    monkeypatch.setattr(wc_attributions, "add_breakdown", lambda day, wc, person, start, breakdown_id: 5)
    resolved = []
    monkeypatch.setattr(machine_breakdown, "resolve_incident", lambda *a, **k: resolved.append(1))

    result = machine_breakdown.report_manual("Dismantler 2", day=date(2026, 7, 8), now=_now())

    assert opened["args"][0] == "Dismantler 2"
    assert opened["args"][3] == "manual"
    assert result["ok"] is True
    assert resolved == []  # has an operator -- stays open for the manager to act on


def test_report_manual_self_resolves_when_no_operators(monkeypatch):
    """Matches the design's "informational only, auto-resolves" rule for a
    manually-reported machine with no one currently on it -- nothing to act
    on, so don't leave a dead card sitting in the queue."""
    monkeypatch.setattr(machine_breakdown, "get_open_incident", lambda wc, day: None)
    monkeypatch.setattr(machine_breakdown, "_last_output_before", lambda wc, day, now: None)
    monkeypatch.setattr(machine_breakdown, "open_incident", lambda wc, day, stop_utc, source: 1)
    monkeypatch.setattr(machine_breakdown, "_operators_on_wc", lambda wc, day: [])
    resolved = []
    monkeypatch.setattr(machine_breakdown, "resolve_incident",
                        lambda incident_id, resolution, resume_utc=None: resolved.append((incident_id, resolution)))

    result = machine_breakdown.report_manual("Dismantler 2", day=date(2026, 7, 8), now=_now())

    assert result == {"ok": True, "incident_id": 1}
    assert resolved == [(1, "handled")]


def test_report_manual_noop_when_already_open(monkeypatch):
    monkeypatch.setattr(machine_breakdown, "get_open_incident", lambda wc, day: {"id": 5})
    called = []
    monkeypatch.setattr(machine_breakdown, "open_incident", lambda *a, **k: called.append(1))

    result = machine_breakdown.report_manual("Dismantler 2", day=date(2026, 7, 8), now=_now())

    assert called == []
    assert result == {"ok": True, "incident_id": 5, "already_open": True}
