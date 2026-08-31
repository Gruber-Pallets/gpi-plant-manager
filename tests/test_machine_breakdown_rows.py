"""Tests for current_rows()/run_detect_tick()/report_manual() -- the I/O glue.
Heavy monkeypatching of collaborators, following tests/test_inbox_reconcile.py's style."""
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from zira_dashboard import machine_breakdown


def _now():
    return datetime(2026, 7, 8, 18, 22, tzinfo=timezone.utc)  # 1:22 PM Central


def test_present_operators_requires_open_punch_at_this_work_center(monkeypatch):
    now = _now()
    windows = {
        "Jesus Galindo": [("Repair 1", now - timedelta(hours=1), now - timedelta(minutes=1))],
        "Juan": [("Repair 1", now - timedelta(hours=1), None)],
        "Ana": [("Repair 2", now - timedelta(hours=1), None)],
    }
    source = machine_breakdown._operator_source_from_legacy_windows(
        windows, now, available=True
    )
    monkeypatch.setattr(
        machine_breakdown, "_operator_source_snapshot", lambda _day, _now: source
    )

    assert machine_breakdown._present_operators_on_wc(
        "Repair 1", date(2026, 7, 8), now
    ) == [
        machine_breakdown.OperatorPresence(
            "Juan", "Repair 1", now - timedelta(hours=1), None
        )
    ]


def test_current_rows_keeps_station_incident_visible_without_present_operator(monkeypatch):
    incident = {
        "id": 1, "wc_name": "Repair 1", "day": date(2026, 7, 8),
        "detected_stop_utc": _now() - timedelta(minutes=25), "source": "auto",
    }
    monkeypatch.setattr(machine_breakdown, "all_open_incidents", lambda day: [incident])
    monkeypatch.setattr(
        machine_breakdown, "_present_operators_on_wc", lambda wc, day, now=None: []
    )

    rows = machine_breakdown.current_rows(day=date(2026, 7, 8), now=_now())

    assert len(rows) == 1
    assert rows[0]["name"] == "Repair 1"
    assert rows[0].get("dismiss_action") == {
        "type": "breakdown_dismiss",
        "incident_id": 1,
    }


def test_detect_tick_keeps_station_incident_open_after_final_operator_leaves(monkeypatch):
    incident = {
        "id": 1, "wc_name": "Repair 1", "day": date(2026, 7, 8),
        "detected_stop_utc": _now() - timedelta(minutes=25), "source": "auto",
    }
    monkeypatch.setattr(machine_breakdown, "all_open_incidents", lambda day: [incident])
    monkeypatch.setattr(
        machine_breakdown, "_present_operators_on_wc", lambda wc, day, now=None: []
    )
    monkeypatch.setattr(
        machine_breakdown, "_punch_windows_with_availability", lambda day: ({}, True)
    )
    monkeypatch.setattr(
        machine_breakdown,
        "_operator_source_snapshot",
        lambda _day, _now: _operator_snapshot(),
    )
    monkeypatch.setattr(
        machine_breakdown,
        "_station_signals",
        lambda day, now, operator_source=None: [],
    )
    monkeypatch.setattr(machine_breakdown, "_shift_bounds", lambda day: (
        _now() - timedelta(hours=6), _now() + timedelta(hours=2)
    ))
    from zira_dashboard import shift_config
    monkeypatch.setattr(shift_config, "in_shift_on", lambda local_dt: True)
    handled = []
    monkeypatch.setattr(
        machine_breakdown, "resolve_incident",
        lambda incident_id, resolution, resume_utc=None: handled.append((incident_id, resolution)),
    )

    machine_breakdown.run_detect_tick(day=date(2026, 7, 8), now=_now())

    assert handled == []


def test_detect_tick_preserves_open_incident_when_attendance_is_unavailable(monkeypatch):
    """A source failure is not evidence that everyone left the machine."""
    incident = {
        "id": 1, "wc_name": "Repair 1", "day": date(2026, 7, 8),
        "detected_stop_utc": _now() - timedelta(minutes=25), "source": "auto",
    }
    monkeypatch.setattr(machine_breakdown, "all_open_incidents", lambda day: [incident])
    # Keep the legacy lookup empty too, so this is red against the previous
    # implementation, which treated that empty mapping as a genuine departure.
    monkeypatch.setattr(machine_breakdown, "_punch_windows_for_day", lambda day: {})
    monkeypatch.setattr(
        machine_breakdown,
        "_punch_windows_with_availability",
        lambda day: ({}, False),
        raising=False,
    )
    monkeypatch.setattr(
        machine_breakdown,
        "_operator_source_snapshot",
        lambda _day, _now: _operator_snapshot(available=False),
    )
    monkeypatch.setattr(
        machine_breakdown,
        "_station_signals",
        lambda day, now, operator_source=None: [],
    )
    monkeypatch.setattr(machine_breakdown, "_shift_bounds", lambda day: (
        _now() - timedelta(hours=6), _now() + timedelta(hours=2)
    ))
    from zira_dashboard import shift_config
    monkeypatch.setattr(shift_config, "in_shift_on", lambda local_dt: True)
    resolved = []
    monkeypatch.setattr(
        machine_breakdown,
        "resolve_incident",
        lambda incident_id, resolution, resume_utc=None: resolved.append((incident_id, resolution)),
    )

    machine_breakdown.run_detect_tick(day=date(2026, 7, 8), now=_now())

    assert resolved == []


def test_detect_tick_does_not_mutate_workers_when_operator_snapshot_is_incomplete(
    monkeypatch,
):
    incident = {
        "id": 1,
        "wc_name": "Repair 1",
        "day": date(2026, 7, 8),
        "detected_stop_utc": _now() - timedelta(minutes=25),
        "source": "auto",
    }
    source = _operator_snapshot(
        _presence(wc_name="Repair 1"),
        available=True,
        complete=False,
    )
    monkeypatch.setattr(machine_breakdown, "all_open_incidents", lambda _day: [incident])
    monkeypatch.setattr(
        machine_breakdown,
        "_operator_source_snapshot",
        lambda _day, _now: source,
    )
    monkeypatch.setattr(
        machine_breakdown,
        "_maybe_auto_resolve",
        lambda *_args, **_kwargs: False,
    )
    worker_mutations = []
    monkeypatch.setattr(
        machine_breakdown,
        "_cap_departed_operators",
        lambda *_args, **_kwargs: worker_mutations.append("cap"),
    )
    monkeypatch.setattr(
        machine_breakdown,
        "_ensure_operator_breakdowns",
        lambda *_args, **_kwargs: worker_mutations.append("ensure"),
    )
    monkeypatch.setattr(
        machine_breakdown,
        "_station_signals",
        lambda _day, _now, _source=None: [],
    )
    monkeypatch.setattr(
        machine_breakdown,
        "_shift_bounds",
        lambda _day: (_now() - timedelta(hours=6), _now() + timedelta(hours=2)),
    )
    from zira_dashboard import shift_config

    monkeypatch.setattr(shift_config, "in_shift_on", lambda _local_dt: True)

    machine_breakdown.run_detect_tick(day=date(2026, 7, 8), now=_now())

    assert worker_mutations == []


def test_detect_tick_keeps_existing_incident_when_a_coworker_is_present(monkeypatch):
    incident = {
        "id": 1, "wc_name": "Repair 1", "day": date(2026, 7, 8),
        "detected_stop_utc": _now() - timedelta(minutes=25), "source": "auto",
    }
    windows = {"Juan": [("Repair 1", _now() - timedelta(hours=1), None)]}
    monkeypatch.setattr(machine_breakdown, "all_open_incidents", lambda day: [incident])
    monkeypatch.setattr(machine_breakdown, "_punch_windows_for_day", lambda day: windows)
    monkeypatch.setattr(
        machine_breakdown,
        "_punch_windows_with_availability",
        lambda day: (windows, True),
        raising=False,
    )
    monkeypatch.setattr(machine_breakdown, "_cap_departed_operators", lambda *args: None)
    monkeypatch.setattr(machine_breakdown, "_maybe_auto_resolve", lambda *args: None)
    source = _operator_snapshot(
        _presence(
            wc_name="Repair 1",
            arrival_utc=_now() - timedelta(hours=1),
        )
    )
    monkeypatch.setattr(
        machine_breakdown,
        "_operator_source_snapshot",
        lambda _day, _now: source,
    )
    monkeypatch.setattr(
        machine_breakdown,
        "_station_signals",
        lambda day, now, operator_source=None: [],
    )
    monkeypatch.setattr(machine_breakdown, "_shift_bounds", lambda day: (
        _now() - timedelta(hours=6), _now() + timedelta(hours=2)
    ))
    from zira_dashboard import shift_config
    monkeypatch.setattr(shift_config, "in_shift_on", lambda local_dt: True)
    monkeypatch.setattr(
        shift_config,
        "productive_minutes_in_window",
        lambda day, start, end: (end - start).total_seconds() / 60,
    )
    resolved = []
    monkeypatch.setattr(machine_breakdown, "resolve_incident", lambda *args: resolved.append(args))

    machine_breakdown.run_detect_tick(day=date(2026, 7, 8), now=_now())

    assert resolved == []


def test_station_signals_uses_last_sample_not_padded_active_interval(monkeypatch):
    """Regression: leaderboard._active_intervals pads its tail interval end
    forward by up to TRANSFER_GAP (60 min) so a lunch-adjacent gap doesn't
    wrongly split a shift for uptime-display purposes. _station_signals must
    read the real last-production timestamp off `samples`, not the padded
    `active_intervals` tail -- otherwise a station silent for 40 min still
    reads as having produced ~20 min ago (since last_unit + 60min > now),
    and breakdown detection's 15-minute SLA silently balloons to ~75 min."""
    from zira_dashboard import leaderboard, staffing
    from zira_dashboard.stations import Station

    real_last_unit = _now() - timedelta(minutes=40)
    padded_tail_end = real_last_unit + timedelta(minutes=60)  # TRANSFER_GAP padding
    station = Station(meter_id="42713", name="Dismantler 2", category="Dismantler", cell="Recycling")
    fake_total = SimpleNamespace(
        station=station,
        samples=((real_last_unit, 5),),
        active_intervals=((real_last_unit - timedelta(hours=1), padded_tail_end),),
    )
    monkeypatch.setattr(leaderboard, "cached_leaderboard",
                        lambda client, stations, day, now_utc=None: [fake_total])
    monkeypatch.setattr(staffing, "LOCATIONS", [
        staffing.Location("Dismantler 2", "Dismantler", "Bay 2", "Recycled", "42713"),
    ])
    monkeypatch.setattr(
        machine_breakdown,
        "_present_operators_on_wc",
        lambda wc, day, now=None, operator_source=None: [],
    )

    signals = machine_breakdown._station_signals(date(2026, 7, 8), _now())

    assert len(signals) == 1
    # Must report the real last-sample time, not the padded active_intervals
    # tail (which would read as if the station had just produced ~20 min ago).
    assert signals[0].last_output_utc == real_last_unit
    assert signals[0].last_output_utc != padded_tail_end


def test_run_detect_tick_opens_new_incident(monkeypatch):
    stop = _now() - timedelta(minutes=60)
    monkeypatch.setattr(machine_breakdown, "_station_signals", lambda day, now, operator_source=None: [
        machine_breakdown.StationSignal("Dismantler 2", stop, True)
    ])
    monkeypatch.setattr(
        machine_breakdown,
        "_operator_source_snapshot",
        lambda _day, _now: _operator_snapshot(
            _presence(arrival_utc=stop - timedelta(hours=1))
        ),
    )
    monkeypatch.setattr(machine_breakdown, "_shift_bounds", lambda day: (
        _now() - timedelta(hours=6), _now() + timedelta(hours=2)
    ))
    monkeypatch.setattr(machine_breakdown, "all_open_incidents", lambda day: [])
    monkeypatch.setattr(machine_breakdown, "get_open_incident", lambda wc, day: None)
    from zira_dashboard import shift_config
    monkeypatch.setattr(shift_config, "in_shift_on", lambda local_dt: True)
    monkeypatch.setattr(shift_config, "productive_minutes_in_window",
                        lambda day, start, end: 60)
    opened = {}

    def _open_incident(wc, day, stop_utc, source):
        # NB: `opened.setdefault(...) or 1` looks tempting here but always
        # returns the (truthy) tuple, never 1 -- setdefault returns the
        # stored value, not a success flag.
        opened["args"] = (wc, day, stop_utc, source)
        return 1

    monkeypatch.setattr(machine_breakdown, "open_incident", _open_incident)
    from zira_dashboard import wc_attributions
    added = []
    monkeypatch.setattr(
        wc_attributions,
        "adopt_breakdown",
        lambda day, wc, person, start, breakdown_id, **_kwargs: added.append(
            (day, wc, person, start, breakdown_id)
        )
        or {"id": 99},
    )
    monkeypatch.setattr(machine_breakdown, "_cap_departed_operators", lambda incident, day, now: None)
    monkeypatch.setattr(machine_breakdown, "_maybe_auto_resolve", lambda incident, day, now: None)

    machine_breakdown.run_detect_tick(day=date(2026, 7, 8), now=_now())

    assert opened["args"] == ("Dismantler 2", date(2026, 7, 8), stop, "auto")
    assert added == [(date(2026, 7, 8), "Dismantler 2", "Juan", stop, 1)]


def test_run_detect_tick_skips_wc_with_open_incident(monkeypatch):
    stop = _now() - timedelta(minutes=60)
    monkeypatch.setattr(machine_breakdown, "_station_signals", lambda day, now, operator_source=None: [
        machine_breakdown.StationSignal("Dismantler 2", stop, True)
    ])
    monkeypatch.setattr(
        machine_breakdown,
        "_operator_source_snapshot",
        lambda _day, _now: _operator_snapshot(),
    )
    monkeypatch.setattr(machine_breakdown, "_shift_bounds", lambda day: (
        _now() - timedelta(hours=6), _now() + timedelta(hours=2)
    ))
    monkeypatch.setattr(machine_breakdown, "all_open_incidents", lambda day: [])
    monkeypatch.setattr(machine_breakdown, "get_open_incident", lambda wc, day: {"id": 5})
    from zira_dashboard import shift_config
    monkeypatch.setattr(shift_config, "in_shift_on", lambda local_dt: True)
    monkeypatch.setattr(shift_config, "productive_minutes_in_window",
                        lambda day, start, end: 60)
    called = []
    monkeypatch.setattr(machine_breakdown, "open_incident", lambda *a, **k: called.append(1))
    monkeypatch.setattr(machine_breakdown, "_cap_departed_operators", lambda incident, day, now: None)
    monkeypatch.setattr(machine_breakdown, "_maybe_auto_resolve", lambda incident, day, now: None)

    machine_breakdown.run_detect_tick(day=date(2026, 7, 8), now=_now())

    assert called == []


def test_run_detect_tick_does_not_open_during_break(monkeypatch):
    stop = _now() - timedelta(minutes=90)
    monkeypatch.setattr(machine_breakdown, "_station_signals", lambda day, now, operator_source=None: [
        machine_breakdown.StationSignal("Dismantler 2", stop, True)
    ])
    monkeypatch.setattr(
        machine_breakdown,
        "_operator_source_snapshot",
        lambda _day, _now: _operator_snapshot(),
    )
    monkeypatch.setattr(machine_breakdown, "_shift_bounds", lambda day: (
        _now() - timedelta(hours=6), _now() + timedelta(hours=2)
    ))
    monkeypatch.setattr(machine_breakdown, "all_open_incidents", lambda day: [])
    monkeypatch.setattr(machine_breakdown, "get_open_incident", lambda wc, day: None)
    from zira_dashboard import shift_config
    monkeypatch.setattr(shift_config, "in_shift_on", lambda local_dt: False)
    monkeypatch.setattr(shift_config, "productive_minutes_in_window",
                        lambda day, start, end: 90)
    monkeypatch.setattr(machine_breakdown, "_present_operators_on_wc", lambda wc, day, now=None: [])
    opened = []
    monkeypatch.setattr(machine_breakdown, "open_incident", lambda *a, **k: opened.append(a))

    machine_breakdown.run_detect_tick(day=date(2026, 7, 8), now=_now())

    assert opened == []


def test_run_detect_tick_uses_break_aware_elapsed_minutes(monkeypatch):
    stop = _now() - timedelta(minutes=75)
    monkeypatch.setattr(machine_breakdown, "_station_signals", lambda day, now, operator_source=None: [
        machine_breakdown.StationSignal("Dismantler 2", stop, True)
    ])
    monkeypatch.setattr(
        machine_breakdown,
        "_operator_source_snapshot",
        lambda _day, _now: _operator_snapshot(),
    )
    monkeypatch.setattr(machine_breakdown, "_shift_bounds", lambda day: (
        _now() - timedelta(hours=6), _now() + timedelta(hours=2)
    ))
    monkeypatch.setattr(machine_breakdown, "all_open_incidents", lambda day: [])
    monkeypatch.setattr(machine_breakdown, "get_open_incident", lambda wc, day: None)
    from zira_dashboard import shift_config
    monkeypatch.setattr(shift_config, "in_shift_on", lambda local_dt: True)
    calls = []
    monkeypatch.setattr(
        shift_config,
        "productive_minutes_in_window",
        lambda day, start, end: calls.append((day, start, end)) or 45,
    )
    monkeypatch.setattr(machine_breakdown, "_present_operators_on_wc", lambda wc, day, now=None: [])
    opened = []
    monkeypatch.setattr(machine_breakdown, "open_incident", lambda *a, **k: opened.append(a))

    machine_breakdown.run_detect_tick(day=date(2026, 7, 8), now=_now())

    assert calls == [(date(2026, 7, 8), stop, _now())]
    assert opened == []


def test_cap_departed_operators_caps_and_leaves_still_present_untouched(monkeypatch):
    from zira_dashboard import wc_attributions
    incident = {"id": 1, "wc_name": "Dismantler 2", "day": date(2026, 7, 8),
                "detected_stop_utc": _now() - timedelta(minutes=30)}
    dep_end = _now() - timedelta(minutes=5)
    source = _operator_snapshot(
        _presence(
            "Benjamin",
            arrival_utc=_now() - timedelta(hours=6),
            employee_odoo_id=202,
        ),
        departures=(
            _departure(
                arrival_utc=_now() - timedelta(hours=6),
                departure_utc=dep_end,
            ),
        ),
    )
    monkeypatch.setattr(wc_attributions, "open_breakdown_row",
                        lambda day, wc, person, **_kwargs: {"id": 10, "start_utc": incident["detected_stop_utc"]} if person == "Juan" else {"id": 11, "start_utc": incident["detected_stop_utc"]})
    capped = []
    monkeypatch.setattr(wc_attributions, "cap_breakdown", lambda row_id, end: capped.append((row_id, end)))

    machine_breakdown._cap_departed_operators(
        incident, date(2026, 7, 8), _now(), source
    )

    assert capped == [(10, dep_end)]  # only Juan (closed window); Benjamin still open


def test_maybe_auto_resolve_resolves_when_station_producing_again(monkeypatch):
    incident = {"id": 1, "wc_name": "Dismantler 2", "day": date(2026, 7, 8),
                "detected_stop_utc": _now() - timedelta(minutes=30)}
    resume = _now() - timedelta(minutes=2)
    monkeypatch.setattr(
        machine_breakdown,
        "_last_output_after",
        lambda wc, day, stop, operator_source=None: resume,
    )
    from zira_dashboard import wc_attributions
    monkeypatch.setattr(
        wc_attributions,
        "open_breakdown_rows_for_incident",
        lambda incident_id: [{"id": 10, "start_utc": incident["detected_stop_utc"]}],
    )
    capped = []
    monkeypatch.setattr(wc_attributions, "cap_breakdown", lambda row_id, end: capped.append((row_id, end)))
    resolved = []
    monkeypatch.setattr(machine_breakdown, "resolve_incident",
                        lambda incident_id, resolution, resume_utc=None: resolved.append((incident_id, resolution, resume_utc)))

    machine_breakdown._maybe_auto_resolve(
        incident, date(2026, 7, 8), _now(), _operator_snapshot(available=False)
    )

    assert resolved == [(1, "recovered", resume)]
    assert capped == [(10, resume)]  # any operator still open gets capped at resume


def test_maybe_auto_resolve_noop_when_still_down(monkeypatch):
    incident = {"id": 1, "wc_name": "Dismantler 2", "day": date(2026, 7, 8),
                "detected_stop_utc": _now() - timedelta(minutes=30)}
    monkeypatch.setattr(
        machine_breakdown,
        "_last_output_after",
        lambda wc, day, stop, operator_source=None: None,
    )
    resolved = []
    monkeypatch.setattr(machine_breakdown, "resolve_incident", lambda *a, **k: resolved.append(1))

    machine_breakdown._maybe_auto_resolve(incident, date(2026, 7, 8), _now())

    assert resolved == []


def test_current_rows_shapes_header_and_operator_rows(monkeypatch):
    from zira_dashboard import shift_config

    incident = {"id": 1, "wc_name": "Dismantler 2", "day": date(2026, 7, 8),
                "detected_stop_utc": _now() - timedelta(minutes=65),
                "source": "auto", "resolved_at": None, "resolution": None}
    monkeypatch.setattr(machine_breakdown, "all_open_incidents", lambda day: [incident])
    source = _operator_snapshot(
        _presence(arrival_utc=_now() - timedelta(hours=2)),
        _presence(
            "Benjamin",
            arrival_utc=_now() - timedelta(hours=2),
            employee_odoo_id=202,
        ),
    )
    monkeypatch.setattr(
        machine_breakdown, "_operator_source_snapshot", lambda _day, _now: source
    )
    monkeypatch.setattr(
        shift_config,
        "productive_minutes_in_window",
        lambda _day, start, end: (end - start).total_seconds() / 60,
    )
    monkeypatch.setattr(machine_breakdown, "active_snooze_until",
                        lambda incident_id, person, **_kwargs: (_now() + timedelta(minutes=10)) if person == "Benjamin" else None)
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
    monkeypatch.setattr(
        machine_breakdown,
        "_last_output_before",
        lambda wc, day, now, operator_source=None: None,
    )
    opened = {}

    def fake_open_incident(wc, day, stop_utc, source):
        # NB: `opened.setdefault(...) or 1` looks tempting here but always
        # returns the (truthy) tuple, never 1 -- setdefault returns the
        # stored value, not a success flag.
        opened["args"] = (wc, day, stop_utc, source)
        return 1

    monkeypatch.setattr(machine_breakdown, "open_incident", fake_open_incident)
    source = _operator_snapshot(
        _presence(arrival_utc=_now() - timedelta(hours=1))
    )
    monkeypatch.setattr(
        machine_breakdown,
        "_operator_source_snapshot",
        lambda _day, _now: source,
    )
    from zira_dashboard import wc_attributions
    monkeypatch.setattr(wc_attributions, "add_breakdown", lambda day, wc, person, start, breakdown_id: 5)
    resolved = []
    monkeypatch.setattr(machine_breakdown, "resolve_incident", lambda *a, **k: resolved.append(1))

    result = machine_breakdown.report_manual("Dismantler 2", day=date(2026, 7, 8), now=_now())

    assert opened["args"][0] == "Dismantler 2"
    assert opened["args"][3] == "manual"
    assert result["ok"] is True
    assert resolved == []  # has an operator -- stays open for the manager to act on


def test_report_manual_keeps_station_incident_open_when_no_operators(monkeypatch):
    monkeypatch.setattr(machine_breakdown, "get_open_incident", lambda wc, day: None)
    monkeypatch.setattr(
        machine_breakdown,
        "_last_output_before",
        lambda wc, day, now, operator_source=None: None,
    )
    monkeypatch.setattr(machine_breakdown, "open_incident", lambda wc, day, stop_utc, source: 1)
    monkeypatch.setattr(
        machine_breakdown,
        "_operator_source_snapshot",
        lambda _day, _now: _operator_snapshot(),
    )
    resolved = []
    monkeypatch.setattr(machine_breakdown, "resolve_incident",
                        lambda incident_id, resolution, resume_utc=None: resolved.append((incident_id, resolution)))

    result = machine_breakdown.report_manual("Dismantler 2", day=date(2026, 7, 8), now=_now())

    assert result == {"ok": True, "incident_id": 1}
    assert resolved == []


def test_report_manual_noop_when_already_open(monkeypatch):
    monkeypatch.setattr(machine_breakdown, "get_open_incident", lambda wc, day: {"id": 5})
    called = []
    monkeypatch.setattr(machine_breakdown, "open_incident", lambda *a, **k: called.append(1))

    result = machine_breakdown.report_manual("Dismantler 2", day=date(2026, 7, 8), now=_now())

    assert called == []
    assert result == {"ok": True, "incident_id": 5, "already_open": True}


def _presence(
    person_name="Juan",
    *,
    wc_name="Dismantler 2",
    arrival_utc=None,
    employee_odoo_id=101,
):
    return machine_breakdown.OperatorPresence(
        person_name=person_name,
        wc_name=wc_name,
        arrival_utc=arrival_utc or (_now() - timedelta(hours=2)),
        employee_odoo_id=employee_odoo_id,
    )


def _departure(
    person_name="Juan",
    *,
    wc_name="Dismantler 2",
    arrival_utc=None,
    departure_utc=None,
    employee_odoo_id=101,
):
    return machine_breakdown.OperatorDeparture(
        person_name=person_name,
        wc_name=wc_name,
        arrival_utc=arrival_utc or (_now() - timedelta(hours=2)),
        departure_utc=departure_utc or (_now() - timedelta(minutes=5)),
        employee_odoo_id=employee_odoo_id,
    )


def _operator_snapshot(
    *presences,
    departures=(),
    available=True,
    mirror_owned=True,
    complete=True,
):
    return machine_breakdown.OperatorSourceSnapshot(
        presences=tuple(presences),
        departures=tuple(departures),
        available=available,
        mirror_owned=mirror_owned,
        complete=complete,
    )


def _location_span(
    *,
    employee_id,
    person_name,
    status,
    wc_name,
    start_utc,
    end_utc,
    attendance_id,
):
    from zira_dashboard.attendance_timeline import LocationSpan

    return LocationSpan(
        employee_odoo_id=employee_id,
        employee_name=person_name,
        start_utc=start_utc,
        end_utc=end_utc,
        status=status,
        app_work_center_name=wc_name,
        odoo_work_center_id=77 if wc_name else None,
        odoo_work_center_name=wc_name,
        attendance_ids=(attendance_id,),
        department_repair=None,
    )


def _frozen_staffing_snapshot(
    spans,
    *,
    current_attendance_ids=(),
    mirror_owned=True,
    available=True,
    stale=False,
):
    return SimpleNamespace(
        policy=SimpleNamespace(
            mirror_owned=mirror_owned,
            available=available,
            stale=stale,
        ),
        spans=tuple(spans),
        verified_cap_utc=_now(),
        current_attendance_ids=frozenset(current_attendance_ids),
    )


def test_mirror_operator_snapshot_uses_valid_arrivals_and_keeps_employee_identity():
    same_name_one = _location_span(
        employee_id=101,
        person_name="Alex",
        status="valid",
        wc_name="Dismantler 2",
        start_utc=_now() - timedelta(hours=2),
        end_utc=_now(),
        attendance_id=91,
    )
    same_name_two = _location_span(
        employee_id=202,
        person_name="Alex",
        status="valid",
        wc_name="Dismantler 2",
        start_utc=_now() - timedelta(hours=1),
        end_utc=_now(),
        attendance_id=92,
    )

    snapshot = machine_breakdown._operator_source_from_staffing_snapshot(
        _frozen_staffing_snapshot(
            (same_name_one, same_name_two),
            current_attendance_ids=(91, 92),
        )
    )

    assert [operator.employee_odoo_id for operator in snapshot.presences] == [101, 202]
    assert [operator.arrival_utc for operator in snapshot.presences] == [
        same_name_one.start_utc,
        same_name_two.start_utc,
    ]


def test_current_rows_do_not_collapse_distinct_employee_ids_with_same_display_name(
    monkeypatch,
):
    from zira_dashboard import shift_config

    incident = {
        "id": 1,
        "wc_name": "Dismantler 2",
        "day": date(2026, 7, 8),
        "detected_stop_utc": _now() - timedelta(minutes=65),
        "source": "auto",
    }
    source = _operator_snapshot(
        _presence("Alex", employee_odoo_id=101),
        _presence("Alex", employee_odoo_id=202),
    )
    monkeypatch.setattr(machine_breakdown, "all_open_incidents", lambda _day: [incident])
    monkeypatch.setattr(
        machine_breakdown, "_operator_source_snapshot", lambda _day, _now: source
    )
    monkeypatch.setattr(
        shift_config,
        "productive_minutes_in_window",
        lambda _day, start, end: (end - start).total_seconds() / 60,
    )
    monkeypatch.setattr(machine_breakdown, "active_snooze_until", lambda *_args, **_kwargs: None)

    rows = machine_breakdown.current_rows(day=incident["day"], now=_now())
    worker_rows = [row for row in rows if row.get("action")]

    assert [row["action"]["employee_odoo_id"] for row in worker_rows] == [101, 202]
    assert len({row["row_key"] for row in worker_rows}) == 2
    assert len({row["item_key"] for row in worker_rows}) == 2


@pytest.mark.parametrize(
    "invalid_status",
    [
        "pending_first_location",
        "exempt_no_location",
        "missing_required_location",
        "unmapped_location",
        "conflicting_location",
        "stale_open_location",
    ],
)
def test_mirror_operator_snapshot_rejects_every_non_valid_location(invalid_status):
    span = _location_span(
        employee_id=101,
        person_name="Juan",
        status=invalid_status,
        wc_name=None if "location" in invalid_status else "Dismantler 2",
        start_utc=_now() - timedelta(hours=2),
        end_utc=_now(),
        attendance_id=91,
    )

    snapshot = machine_breakdown._operator_source_from_staffing_snapshot(
        _frozen_staffing_snapshot((span,), current_attendance_ids=(91,))
    )

    assert snapshot.presences == ()


def test_mirror_operator_snapshot_treats_missing_location_span_as_no_operator():
    snapshot = machine_breakdown._operator_source_from_staffing_snapshot(
        _frozen_staffing_snapshot(())
    )

    assert snapshot.presences == ()


@pytest.mark.parametrize(
    ("available", "stale"),
    [(False, False), (True, True)],
)
def test_mirror_operator_snapshot_never_uses_unavailable_or_stale_location(
    available,
    stale,
):
    span = _location_span(
        employee_id=101,
        person_name="Juan",
        status="valid",
        wc_name="Dismantler 2",
        start_utc=_now() - timedelta(hours=2),
        end_utc=_now(),
        attendance_id=91,
    )

    snapshot = machine_breakdown._operator_source_from_staffing_snapshot(
        _frozen_staffing_snapshot(
            (span,),
            current_attendance_ids=(91,),
            available=available,
            stale=stale,
        )
    )

    assert snapshot.presences == ()
    assert snapshot.available is False


def test_mirror_transfer_snapshot_moves_worker_and_records_exact_departure():
    transfer_at = _now() - timedelta(minutes=20)
    old_location = _location_span(
        employee_id=101,
        person_name="Juan",
        status="valid",
        wc_name="Dismantler 2",
        start_utc=_now() - timedelta(hours=2),
        end_utc=transfer_at,
        attendance_id=91,
    )
    new_location = _location_span(
        employee_id=101,
        person_name="Juan",
        status="valid",
        wc_name="Repair 3",
        start_utc=transfer_at,
        end_utc=_now(),
        attendance_id=92,
    )

    snapshot = machine_breakdown._operator_source_from_staffing_snapshot(
        _frozen_staffing_snapshot(
            (old_location, new_location),
            current_attendance_ids=(92,),
        )
    )

    assert [(p.wc_name, p.arrival_utc) for p in snapshot.presences] == [
        ("Repair 3", transfer_at)
    ]
    assert [(d.wc_name, d.departure_utc) for d in snapshot.departures] == [
        ("Dismantler 2", transfer_at)
    ]


def test_off_or_incomplete_baseline_operator_source_keeps_legacy_windows(monkeypatch):
    from zira_dashboard.routes import staffing as staffing_routes

    arrival = _now() - timedelta(hours=1)
    monkeypatch.setattr(
        staffing_routes,
        "_read_staffing_response_snapshot",
        lambda day, as_of_utc: _frozen_staffing_snapshot(
            (), mirror_owned=False
        ),
    )
    monkeypatch.setattr(
        machine_breakdown,
        "_punch_windows_with_availability",
        lambda day: ({"Juan": [("Dismantler 2", arrival, None)]}, True),
    )

    snapshot = machine_breakdown._operator_source_snapshot(
        date(2026, 7, 8), _now()
    )

    assert snapshot.mirror_owned is False
    assert snapshot.presences == (
        machine_breakdown.OperatorPresence(
            "Juan", "Dismantler 2", arrival, None
        ),
    )


def test_complete_mirror_operator_source_never_falls_back_to_legacy(monkeypatch):
    from zira_dashboard.routes import staffing as staffing_routes

    arrival = _now() - timedelta(hours=1)
    span = _location_span(
        employee_id=101,
        person_name="Juan",
        status="valid",
        wc_name="Dismantler 2",
        start_utc=arrival,
        end_utc=_now(),
        attendance_id=91,
    )
    monkeypatch.setattr(
        staffing_routes,
        "_read_staffing_response_snapshot",
        lambda day, as_of_utc: _frozen_staffing_snapshot(
            (span,), current_attendance_ids=(91,)
        ),
    )
    monkeypatch.setattr(
        machine_breakdown,
        "_punch_windows_for_day",
        lambda day: (_ for _ in ()).throw(
            AssertionError("complete mirror ownership must not read legacy windows")
        ),
    )

    snapshot = machine_breakdown._operator_source_snapshot(
        date(2026, 7, 8), _now()
    )

    assert snapshot.mirror_owned is True
    assert snapshot.presences == (
        machine_breakdown.OperatorPresence(
            "Juan", "Dismantler 2", arrival, 101
        ),
    )


def test_recent_arrival_has_station_header_but_no_worker_row_or_exclusion(monkeypatch):
    from zira_dashboard import shift_config, wc_attributions

    day = date(2026, 7, 8)
    stop = _now() - timedelta(minutes=90)
    arrival = _now() - timedelta(minutes=10)
    incident = {
        "id": 1,
        "wc_name": "Dismantler 2",
        "day": day,
        "detected_stop_utc": stop,
        "source": "auto",
    }
    source = _operator_snapshot(_presence(arrival_utc=arrival))
    monkeypatch.setattr(machine_breakdown, "all_open_incidents", lambda _day: [incident])
    monkeypatch.setattr(
        machine_breakdown, "_operator_source_snapshot", lambda _day, _now: source
    )
    monkeypatch.setattr(
        shift_config,
        "productive_minutes_in_window",
        lambda _day, start, end: (end - start).total_seconds() / 60,
    )
    added = []
    monkeypatch.setattr(
        wc_attributions,
        "open_breakdown_row",
        lambda *_args: None,
    )
    monkeypatch.setattr(
        wc_attributions,
        "add_breakdown",
        lambda *args: added.append(args),
    )

    machine_breakdown._ensure_operator_breakdowns(
        incident, day, _now(), source
    )
    rows = machine_breakdown.current_rows(day=day, now=_now())

    assert added == []
    assert len(rows) == 1
    assert rows[0].get("dismiss_action") is not None
    assert not [row for row in rows if row.get("action")]


def test_recent_arrival_can_open_station_incident_without_personal_exclusion(monkeypatch):
    from zira_dashboard import shift_config, wc_attributions

    day = date(2026, 7, 8)
    stop = _now() - timedelta(minutes=90)
    arrival = _now() - timedelta(minutes=10)
    source = _operator_snapshot(_presence(arrival_utc=arrival))
    monkeypatch.setattr(
        machine_breakdown, "_operator_source_snapshot", lambda _day, _now: source
    )
    monkeypatch.setattr(
        machine_breakdown,
        "_station_signals",
        lambda *_args, **_kwargs: [
            machine_breakdown.StationSignal("Dismantler 2", stop, True)
        ],
    )
    monkeypatch.setattr(
        machine_breakdown,
        "_shift_bounds",
        lambda _day: (_now() - timedelta(hours=6), _now() + timedelta(hours=2)),
    )
    monkeypatch.setattr(machine_breakdown, "all_open_incidents", lambda _day: [])
    monkeypatch.setattr(machine_breakdown, "get_open_incident", lambda _wc, _day: None)
    monkeypatch.setattr(shift_config, "in_shift_on", lambda _local: True)
    monkeypatch.setattr(
        shift_config,
        "productive_minutes_in_window",
        lambda _day, start, end: (end - start).total_seconds() / 60,
    )
    opened = []
    monkeypatch.setattr(
        machine_breakdown,
        "open_incident",
        lambda wc, incident_day, stop_utc, source: opened.append(
            (wc, incident_day, stop_utc, source)
        )
        or 1,
    )
    monkeypatch.setattr(
        machine_breakdown,
        "_present_operators_on_wc",
        lambda *_args, **_kwargs: ["Juan"],
    )
    added = []
    monkeypatch.setattr(wc_attributions, "open_breakdown_row", lambda *_args: None)
    monkeypatch.setattr(
        wc_attributions,
        "add_breakdown",
        lambda *args: added.append(args),
    )

    machine_breakdown.run_detect_tick(day=day, now=_now())

    assert opened == [("Dismantler 2", day, stop, "auto")]
    assert added == []


def test_worker_crossing_threshold_gets_one_exclusion_from_exact_arrival(monkeypatch):
    from zira_dashboard import shift_config, wc_attributions

    day = date(2026, 7, 8)
    stop = _now() - timedelta(minutes=100)
    arrival = _now() - timedelta(minutes=65)
    incident = {
        "id": 1,
        "wc_name": "Dismantler 2",
        "day": day,
        "detected_stop_utc": stop,
        "source": "auto",
    }
    source = _operator_snapshot(_presence(arrival_utc=arrival))
    monkeypatch.setattr(
        shift_config,
        "productive_minutes_in_window",
        lambda _day, start, end: (end - start).total_seconds() / 60,
    )
    added = []

    def adopt_breakdown(_day, wc, person, start, breakdown_id, **_kwargs):
        if not added:
            added.append((_day, wc, person, start, breakdown_id))
        return {"id": 50, "start_utc": start}

    monkeypatch.setattr(wc_attributions, "adopt_breakdown", adopt_breakdown)

    machine_breakdown._ensure_operator_breakdowns(incident, day, _now(), source)
    machine_breakdown._ensure_operator_breakdowns(incident, day, _now(), source)

    assert added == [(day, "Dismantler 2", "Juan", arrival, 1)]


def test_worker_present_before_station_stop_gets_exclusion_from_station_stop(monkeypatch):
    from zira_dashboard import shift_config, wc_attributions

    day = date(2026, 7, 8)
    stop = _now() - timedelta(minutes=65)
    source = _operator_snapshot(
        _presence(arrival_utc=stop - timedelta(hours=1))
    )
    incident = {
        "id": 1,
        "wc_name": "Dismantler 2",
        "day": day,
        "detected_stop_utc": stop,
        "source": "auto",
    }
    monkeypatch.setattr(
        shift_config,
        "productive_minutes_in_window",
        lambda _day, start, end: (end - start).total_seconds() / 60,
    )
    monkeypatch.setattr(wc_attributions, "open_breakdown_row", lambda *_args, **_kwargs: None)
    added = []
    monkeypatch.setattr(
        wc_attributions,
        "adopt_breakdown",
        lambda *args, **_kwargs: added.append(args),
    )

    machine_breakdown._ensure_operator_breakdowns(incident, day, _now(), source)

    assert added[0][3] == stop


def test_canonical_transfer_caps_only_worker_at_span_end_without_plant_manager_transfer(
    monkeypatch,
):
    from zira_dashboard import staffing_transfer, wc_attributions

    day = date(2026, 7, 8)
    departure = _now() - timedelta(minutes=5)
    incident = {
        "id": 1,
        "wc_name": "Dismantler 2",
        "day": day,
        "detected_stop_utc": _now() - timedelta(hours=2),
        "source": "auto",
    }
    source = _operator_snapshot(
        departures=(_departure(departure_utc=departure),)
    )
    monkeypatch.setattr(
        wc_attributions,
        "open_breakdown_row",
        lambda _day, _wc, person, **_kwargs: {"id": 10, "start_utc": incident["detected_stop_utc"]}
        if person == "Juan"
        else None,
    )
    capped = []
    monkeypatch.setattr(
        wc_attributions,
        "cap_breakdown",
        lambda row_id, end: capped.append((row_id, end)),
    )
    monkeypatch.setattr(
        staffing_transfer,
        "decide_and_apply",
        lambda *_args: (_ for _ in ()).throw(
            AssertionError("Plant Manager must not issue the Odoo transfer")
        ),
    )
    monkeypatch.setattr(machine_breakdown, "all_open_incidents", lambda _day: [incident])
    monkeypatch.setattr(
        machine_breakdown, "_operator_source_snapshot", lambda _day, _now: source
    )

    machine_breakdown._cap_departed_operators(incident, day, _now(), source)
    rows = machine_breakdown.current_rows(day=day, now=_now())

    assert capped == [(10, departure)]
    assert len(rows) == 1
    assert rows[0]["name"] == "Dismantler 2"
    assert not [row for row in rows if row.get("action")]


def test_worker_returning_to_same_station_gets_a_new_personal_clock(monkeypatch):
    from zira_dashboard import shift_config, wc_attributions

    day = date(2026, 7, 8)
    station_stop = _now() - timedelta(hours=3)
    first_arrival = _now() - timedelta(hours=2)
    first_departure = _now() - timedelta(minutes=80)
    second_arrival = _now() - timedelta(minutes=65)
    incident = {
        "id": 1,
        "wc_name": "Dismantler 2",
        "day": day,
        "detected_stop_utc": station_stop,
    }
    source = _operator_snapshot(
        _presence(arrival_utc=second_arrival),
        departures=(
            _departure(
                arrival_utc=first_arrival,
                departure_utc=first_departure,
            ),
        ),
    )
    open_row = {"id": 10, "start_utc": first_arrival}
    capped = []
    added = []

    def open_breakdown_row(_day, _wc, _person, **_kwargs):
        return open_row.copy() if open_row else None

    def cap_breakdown(row_id, end):
        capped.append((row_id, end))
        open_row.clear()

    def adopt_breakdown(_day, wc, person, start, breakdown_id, **_kwargs):
        added.append((_day, wc, person, start, breakdown_id))
        open_row.update(id=11, start_utc=start)
        return 11

    monkeypatch.setattr(wc_attributions, "open_breakdown_row", open_breakdown_row)
    monkeypatch.setattr(wc_attributions, "cap_breakdown", cap_breakdown)
    monkeypatch.setattr(wc_attributions, "adopt_breakdown", adopt_breakdown)
    monkeypatch.setattr(
        shift_config,
        "productive_minutes_in_window",
        lambda _day, start, end: (end - start).total_seconds() / 60,
    )

    machine_breakdown._cap_departed_operators(incident, day, _now(), source)
    machine_breakdown._ensure_operator_breakdowns(incident, day, _now(), source)

    assert capped == [(10, first_departure)]
    assert added == [(day, "Dismantler 2", "Juan", second_arrival, 1)]


def test_same_name_departure_caps_only_matching_employee_identity(monkeypatch):
    from zira_dashboard import wc_attributions

    day = date(2026, 7, 8)
    departure = _now() - timedelta(minutes=5)
    incident = {
        "id": 1,
        "wc_name": "Dismantler 2",
        "day": day,
        "detected_stop_utc": _now() - timedelta(hours=2),
    }
    source = _operator_snapshot(
        _presence("Alex", employee_odoo_id=202),
        departures=(
            _departure(
                "Alex", employee_odoo_id=101, departure_utc=departure
            ),
        ),
    )
    lookups = []

    def open_row(_day, _wc, person, *, employee_odoo_id=None, breakdown_id=None):
        lookups.append((person, employee_odoo_id, breakdown_id))
        if employee_odoo_id == 101:
            return {"id": 10, "start_utc": incident["detected_stop_utc"]}
        return {"id": 11, "start_utc": incident["detected_stop_utc"]}

    monkeypatch.setattr(wc_attributions, "open_breakdown_row", open_row)
    capped = []
    monkeypatch.setattr(
        wc_attributions,
        "cap_breakdown",
        lambda row_id, end: capped.append((row_id, end)),
    )

    machine_breakdown._cap_departed_operators(incident, day, _now(), source)

    assert lookups == [("Alex", 101, 1)]
    assert capped == [(10, departure)]


def test_completed_span_backfills_closed_exclusion_after_warmer_outage(monkeypatch):
    from zira_dashboard import shift_config, wc_attributions

    day = date(2026, 7, 8)
    stop = _now() - timedelta(hours=3)
    arrival = _now() - timedelta(hours=2)
    departure = _now() - timedelta(minutes=30)
    incident = {
        "id": 1,
        "wc_name": "Dismantler 2",
        "day": day,
        "detected_stop_utc": stop,
    }
    source = _operator_snapshot(
        departures=(
            _departure(
                arrival_utc=arrival,
                departure_utc=departure,
                employee_odoo_id=101,
            ),
        )
    )
    monkeypatch.setattr(
        shift_config,
        "productive_minutes_in_window",
        lambda _day, start, end: (end - start).total_seconds() / 60,
    )
    adopted = []
    monkeypatch.setattr(
        wc_attributions,
        "adopt_breakdown",
        lambda *args, **kwargs: adopted.append((args, kwargs)) or {"id": 50},
    )

    machine_breakdown._ensure_operator_breakdowns(incident, day, _now(), source)

    assert adopted == [
        (
            (day, "Dismantler 2", "Juan", arrival, 1),
            {"employee_odoo_id": 101, "end_utc": departure},
        )
    ]


def test_completed_span_below_threshold_is_not_backfilled(monkeypatch):
    from zira_dashboard import shift_config, wc_attributions

    day = date(2026, 7, 8)
    arrival = _now() - timedelta(minutes=50)
    departure = _now() - timedelta(minutes=5)
    incident = {
        "id": 1,
        "wc_name": "Dismantler 2",
        "day": day,
        "detected_stop_utc": _now() - timedelta(hours=3),
    }
    source = _operator_snapshot(
        departures=(
            _departure(arrival_utc=arrival, departure_utc=departure),
        )
    )
    monkeypatch.setattr(
        shift_config,
        "productive_minutes_in_window",
        lambda _day, start, end: (end - start).total_seconds() / 60,
    )
    adopted = []
    monkeypatch.setattr(
        wc_attributions,
        "adopt_breakdown",
        lambda *args, **kwargs: adopted.append((args, kwargs)),
    )

    machine_breakdown._ensure_operator_breakdowns(incident, day, _now(), source)

    assert adopted == []


def test_recovery_caps_every_open_exclusion_at_first_resume_when_source_unavailable(
    monkeypatch,
):
    from zira_dashboard import wc_attributions

    day = date(2026, 7, 8)
    first_resume = _now() - timedelta(minutes=40)
    incident = {
        "id": 1,
        "wc_name": "Dismantler 2",
        "day": day,
        "detected_stop_utc": _now() - timedelta(hours=2),
    }
    source = _operator_snapshot(available=False)
    monkeypatch.setattr(
        machine_breakdown,
        "_last_output_after",
        lambda *_args, **_kwargs: first_resume,
    )
    monkeypatch.setattr(
        wc_attributions,
        "open_breakdown_rows_for_incident",
        lambda incident_id: [
            {"id": 10, "start_utc": incident["detected_stop_utc"]},
            {"id": 11, "start_utc": incident["detected_stop_utc"]},
        ],
    )
    capped = []
    monkeypatch.setattr(
        wc_attributions,
        "cap_breakdown",
        lambda row_id, end: capped.append((row_id, end)),
    )
    resolved = []
    monkeypatch.setattr(
        machine_breakdown,
        "resolve_incident",
        lambda incident_id, resolution, resume_utc=None: resolved.append(
            (incident_id, resolution, resume_utc)
        ),
    )

    recovered = machine_breakdown._maybe_auto_resolve(
        incident, day, _now(), source
    )

    assert recovered is True
    assert capped == [(10, first_resume), (11, first_resume)]
    assert resolved == [(1, "recovered", first_resume)]


def test_unavailable_operator_source_marks_breakdown_rows_incomplete(monkeypatch):
    incident = {
        "id": 1,
        "wc_name": "Dismantler 2",
        "day": date(2026, 7, 8),
        "detected_stop_utc": _now() - timedelta(hours=2),
        "source": "auto",
    }
    monkeypatch.setattr(machine_breakdown, "all_open_incidents", lambda _day: [incident])
    monkeypatch.setattr(
        machine_breakdown,
        "_operator_source_snapshot",
        lambda _day, _now: _operator_snapshot(available=False),
    )

    rows = machine_breakdown.current_rows(day=incident["day"], now=_now())

    assert len(rows) == 1
    assert rows.complete is False


def test_current_invalid_location_marks_operator_snapshot_incomplete():
    invalid = _location_span(
        employee_id=101,
        person_name="Juan",
        status="conflicting_location",
        wc_name="Dismantler 2",
        start_utc=_now() - timedelta(hours=2),
        end_utc=_now(),
        attendance_id=91,
    )

    snapshot = machine_breakdown._operator_source_from_staffing_snapshot(
        _frozen_staffing_snapshot((invalid,), current_attendance_ids=(91,))
    )

    assert snapshot.presences == ()
    assert snapshot.complete is False
