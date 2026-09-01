"""Tests for current_rows()/run_detect_tick()/report_manual() -- the I/O glue.
Heavy monkeypatching of collaborators, following tests/test_inbox_reconcile.py's style."""
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from zira_dashboard import machine_breakdown


def _now():
    return datetime(2026, 7, 8, 18, 22, tzinfo=timezone.utc)  # 1:22 PM Central


def _install_transfer_cursor(monkeypatch, *, incident, durable=(), open_row=None):
    from contextlib import contextmanager

    from zira_dashboard import db

    class Cursor:
        def __init__(self):
            self.sql = ""
            self.statements = []

        def execute(self, sql, params):
            self.sql = " ".join(sql.split())
            self.statements.append((self.sql, params))

        def fetchone(self):
            if "FROM machine_breakdowns" in self.sql:
                return incident
            if "LIMIT 1 FOR UPDATE" in self.sql:
                return open_row
            return None

        def fetchall(self):
            return list(durable)

    cursor = Cursor()

    @contextmanager
    def transaction():
        yield cursor

    monkeypatch.setattr(db, "cursor", transaction)
    return cursor


def test_locked_transfer_rejects_resolved_incident_before_policy_or_odoo(monkeypatch):
    from zira_dashboard import staffing_transfer

    incident = {
        "id": 1,
        "wc_name": "Dismantler 2",
        "day": date(2026, 7, 8),
        "detected_stop_utc": _now() - timedelta(hours=2),
        "resolved_at": _now(),
    }
    cursor = _install_transfer_cursor(monkeypatch, incident=incident)
    monkeypatch.setattr(
        staffing_transfer,
        "decide_and_apply",
        lambda *_args, **_kwargs: pytest.fail(
            "resolved incident must stop before policy, roster, or Odoo"
        ),
    )

    outcome = machine_breakdown.transfer_open_incident(
        1, "Alex", 101, "Repair 3"
    )

    assert outcome == {"status": "resolved"}
    assert not [sql for sql, _params in cursor.statements if sql.startswith("UPDATE")]


def test_locked_transfer_roster_rejection_does_not_cap(monkeypatch):
    from zira_dashboard import staffing_transfer

    incident = {
        "id": 1,
        "wc_name": "Dismantler 2",
        "day": date(2026, 7, 8),
        "detected_stop_utc": _now() - timedelta(hours=2),
        "resolved_at": None,
    }
    cursor = _install_transfer_cursor(
        monkeypatch,
        incident=incident,
        durable=({"person_name": "Alex", "employee_odoo_id": 101},),
        open_row={"id": 10, "start_utc": incident["detected_stop_utc"]},
    )
    monkeypatch.setattr(
        staffing_transfer,
        "decide_and_apply",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            ValueError("employee_odoo_id 101 is not on the live roster")
        ),
    )

    with pytest.raises(ValueError, match="not on the live roster"):
        machine_breakdown.transfer_open_incident(1, "Alex", 101, "Repair 3")

    assert not [sql for sql, _params in cursor.statements if sql.startswith("UPDATE")]


def test_locked_transfer_live_flip_does_not_cap(monkeypatch):
    from zira_dashboard import staffing_transfer

    incident = {
        "id": 1,
        "wc_name": "Dismantler 2",
        "day": date(2026, 7, 8),
        "detected_stop_utc": _now() - timedelta(hours=2),
        "resolved_at": None,
    }
    cursor = _install_transfer_cursor(
        monkeypatch,
        incident=incident,
        durable=({"person_name": "Alex", "employee_odoo_id": 101},),
        open_row={"id": 10, "start_utc": incident["detected_stop_utc"]},
    )
    monkeypatch.setattr(
        staffing_transfer,
        "decide_and_apply",
        lambda *_args, **_kwargs: {"transfer": "blocked_live"},
    )

    outcome = machine_breakdown.transfer_open_incident(
        1, "Alex", 101, "Repair 3"
    )

    assert outcome == {"status": "blocked_live"}
    assert not [sql for sql, _params in cursor.statements if sql.startswith("UPDATE")]


def test_locked_transfer_caps_after_real_success_at_personal_start(monkeypatch):
    from zira_dashboard import staffing_transfer

    personal_start = _now() - timedelta(minutes=30)
    incident = {
        "id": 1,
        "wc_name": "Dismantler 2",
        "day": date(2026, 7, 8),
        "detected_stop_utc": _now() - timedelta(hours=2),
        "resolved_at": None,
    }
    cursor = _install_transfer_cursor(
        monkeypatch,
        incident=incident,
        durable=({"person_name": "Alex", "employee_odoo_id": 101},),
        open_row={"id": 10, "start_utc": personal_start},
    )
    events = []
    monkeypatch.setattr(
        staffing_transfer,
        "decide_and_apply",
        lambda *_args, **_kwargs: events.append("transfer")
        or {"transfer": "moved", "closed_id": 5, "new_id": 6},
    )

    outcome = machine_breakdown.transfer_open_incident(
        1, "Alex", 101, "Repair 3"
    )
    update = next(
        statement for statement in cursor.statements if statement[0].startswith("UPDATE")
    )
    events.append("cap")

    assert outcome["status"] == "success"
    assert outcome["attribution_id"] == 10
    assert update[1][:2] == (personal_start, personal_start)
    assert events == ["transfer", "cap"]


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
        machine_breakdown, "finalize_recovered_incident",
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
        "finalize_recovered_incident",
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


def test_dismiss_incident_locks_then_returns_exact_deleted_snapshot(monkeypatch):
    from contextlib import contextmanager

    from zira_dashboard import db, wc_attributions

    statements = []
    deleted_rows = [
        {
            "id": 7,
            "day": date(2026, 7, 8),
            "wc_name": "Dismantler 2",
            "person_name": "Alex",
            "employee_odoo_id": 101,
            "start_utc": _now() - timedelta(minutes=20),
            "end_utc": None,
            "source": wc_attributions.BREAKDOWN_SOURCE,
            "breakdown_id": 42,
        },
        {
            "id": 8,
            "day": date(2026, 7, 8),
            "wc_name": "Dismantler 2",
            "person_name": "Alex",
            "employee_odoo_id": 202,
            "start_utc": _now() - timedelta(minutes=10),
            "end_utc": None,
            "source": wc_attributions.BREAKDOWN_SOURCE,
            "breakdown_id": 42,
        },
    ]

    class Cursor:
        def execute(self, sql, params):
            statements.append((" ".join(sql.split()), params))

        def fetchone(self):
            return {"id": 42}

        def fetchall(self):
            return deleted_rows

    @contextmanager
    def cursor():
        yield Cursor()

    monkeypatch.setattr(db, "cursor", cursor)

    snapshot = machine_breakdown.dismiss_incident(42)

    assert "SELECT id FROM machine_breakdowns" in statements[0][0]
    assert "FOR UPDATE" in statements[0][0]
    assert statements[0][1] == (42,)
    assert "UPDATE machine_breakdowns" in statements[1][0]
    assert statements[1][1] == (42,)
    assert "DELETE FROM wc_time_attributions" in statements[2][0]
    assert "RETURNING id, day, wc_name, person_name" in statements[2][0]
    assert statements[2][1] == (42, wc_attributions.BREAKDOWN_SOURCE)
    assert snapshot == deleted_rows


def test_dismiss_incident_returns_none_when_locked_incident_is_not_open(monkeypatch):
    from contextlib import contextmanager

    from zira_dashboard import db

    statements = []

    class Cursor:
        def execute(self, sql, params):
            statements.append((" ".join(sql.split()), params))

        def fetchone(self):
            return None

    @contextmanager
    def cursor():
        yield Cursor()

    monkeypatch.setattr(db, "cursor", cursor)

    assert machine_breakdown.dismiss_incident(42) is None
    assert len(statements) == 1
    assert "resolved_at IS NULL" in statements[0][0]


def test_undo_dismiss_rolls_back_snapshot_when_reopen_hits_unique_conflict(
    monkeypatch,
):
    from contextlib import contextmanager

    from psycopg2.errors import UniqueViolation

    from zira_dashboard import db, wc_attributions

    day = date(2026, 7, 8)
    snapshot = [
        {
            "day": day,
            "wc_name": "Dismantler 2",
            "person_name": "Alex",
            "employee_odoo_id": 101,
            "start_utc": _now() - timedelta(minutes=20),
            "end_utc": None,
            "source": wc_attributions.BREAKDOWN_SOURCE,
            "breakdown_id": 42,
        }
    ]
    statements = []
    committed_rows = []
    fetches = iter(
        (
            {
                "id": 42,
                "wc_name": "Dismantler 2",
                "day": day,
                "resolved_at": _now(),
                "resolution": "dismissed",
            },
            None,
        )
    )

    class Cursor:
        def __init__(self, pending):
            self.pending = pending

        def execute(self, sql, params):
            normalized = " ".join(sql.split())
            statements.append((normalized, params))
            if normalized.startswith("INSERT INTO wc_time_attributions"):
                self.pending.append(params)
            if (
                normalized.startswith("UPDATE machine_breakdowns")
                and "resolved_at = NULL" in normalized
            ):
                raise UniqueViolation("replacement incident owns the open key")

        def fetchone(self):
            return next(fetches)

    @contextmanager
    def cursor():
        pending = []
        try:
            yield Cursor(pending)
        except Exception:
            raise
        else:
            committed_rows.extend(pending)

    monkeypatch.setattr(db, "cursor", cursor)

    restored = machine_breakdown.undo_dismiss_incident(42, snapshot)

    assert restored is False
    assert committed_rows == []
    assert "FOR UPDATE" in statements[0][0]
    assert "resolved_at IS NULL" in statements[1][0]
    assert statements[2][0].startswith("INSERT INTO wc_time_attributions")
    assert "resolved_at = NULL" in statements[3][0]


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
    monkeypatch.setattr(
        machine_breakdown,
        "finalize_recovered_incident",
        lambda *args: resolved.append(args),
    )

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
    lookups = []
    monkeypatch.setattr(
        wc_attributions,
        "open_breakdown_row",
        lambda *_args, **kwargs: lookups.append(kwargs) or None,
    )
    monkeypatch.setattr(
        wc_attributions,
        "add_breakdown",
        lambda day, wc, person, start, breakdown_id, **_kwargs: added.append(
            (day, wc, person, start, breakdown_id)
        )
        or 99,
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
    from zira_dashboard import shift_config, wc_attributions
    incident = {"id": 1, "wc_name": "Dismantler 2", "day": date(2026, 7, 8),
                "detected_stop_utc": _now() - timedelta(hours=2)}
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
    monkeypatch.setattr(
        shift_config,
        "productive_minutes_in_window",
        lambda _day, start, end: (end - start).total_seconds() / 60,
    )

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
    finalized = []
    monkeypatch.setattr(
        machine_breakdown,
        "finalize_recovered_incident",
        lambda incident_id, resume_utc: finalized.append(
            (incident_id, resume_utc)
        )
        or True,
    )

    recovered = machine_breakdown._maybe_auto_resolve(
        incident, date(2026, 7, 8), _now(), _operator_snapshot(available=False)
    )

    assert recovered is True
    assert finalized == [(1, resume)]


def test_finalize_recovery_locks_caps_then_resolves(monkeypatch):
    from contextlib import contextmanager

    from zira_dashboard import db, wc_attributions

    statements = []

    class Cursor:
        def execute(self, sql, params):
            statements.append((" ".join(sql.split()), params))

        def fetchone(self):
            return {"id": 42}

    @contextmanager
    def cursor():
        yield Cursor()

    monkeypatch.setattr(db, "cursor", cursor)
    resume = _now() - timedelta(minutes=10)

    assert machine_breakdown.finalize_recovered_incident(42, resume) is True
    assert "resolved_at IS NULL FOR UPDATE" in statements[0][0]
    assert statements[0][1] == (42,)
    assert "DELETE FROM wc_time_attributions" in statements[1][0]
    assert "start_utc >= %s" in statements[1][0]
    assert statements[1][1] == (
        42,
        wc_attributions.BREAKDOWN_SOURCE,
        resume,
    )
    assert "LEAST(COALESCE(end_utc" in statements[2][0]
    assert statements[2][1] == (
        resume,
        resume,
        42,
        wc_attributions.BREAKDOWN_SOURCE,
        resume,
    )
    assert "resolution = 'recovered'" in statements[3][0]
    assert statements[3][1] == (resume, 42)


def test_maybe_auto_resolve_noop_when_still_down(monkeypatch):
    incident = {"id": 1, "wc_name": "Dismantler 2", "day": date(2026, 7, 8),
                "detected_stop_utc": _now() - timedelta(minutes=30)}
    monkeypatch.setattr(
        machine_breakdown,
        "_last_output_after",
        lambda wc, day, stop, operator_source=None: None,
    )
    resolved = []
    monkeypatch.setattr(
        machine_breakdown,
        "finalize_recovered_incident",
        lambda *a, **k: resolved.append(1),
    )

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
    monkeypatch.setattr(
        wc_attributions,
        "open_breakdown_row",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        wc_attributions,
        "add_breakdown",
        lambda day, wc, person, start, breakdown_id, **_kwargs: 5,
    )
    resolved = []
    monkeypatch.setattr(
        machine_breakdown,
        "finalize_recovered_incident",
        lambda *a, **k: resolved.append(1),
    )

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
    monkeypatch.setattr(machine_breakdown, "finalize_recovered_incident",
                        lambda incident_id, resolution, resume_utc=None: resolved.append((incident_id, resolution)))

    result = machine_breakdown.report_manual("Dismantler 2", day=date(2026, 7, 8), now=_now())

    assert result == {"ok": True, "incident_id": 1}
    assert resolved == []


def test_report_manual_does_not_mutate_workers_from_incomplete_snapshot(monkeypatch):
    source = _operator_snapshot(
        _presence(arrival_utc=_now() - timedelta(hours=1)),
        complete=False,
    )
    monkeypatch.setattr(machine_breakdown, "get_open_incident", lambda *_args: None)
    monkeypatch.setattr(
        machine_breakdown,
        "_operator_source_snapshot",
        lambda *_args, **_kwargs: source,
    )
    monkeypatch.setattr(
        machine_breakdown,
        "_last_output_before",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        machine_breakdown, "open_incident", lambda *_args, **_kwargs: 1
    )
    monkeypatch.setattr(
        machine_breakdown,
        "_ensure_operator_breakdowns",
        lambda *_args, **_kwargs: pytest.fail(
            "an incomplete operator snapshot must not mutate worker exclusions"
        ),
    )

    result = machine_breakdown.report_manual(
        "Dismantler 2", day=date(2026, 7, 8), now=_now()
    )

    assert result == {"ok": True, "incident_id": 1}


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


def _fake_breakdown_store(monkeypatch, initial=()):
    """Small exact-visit store for delayed/retried reconciliation tests."""
    from zira_dashboard import wc_attributions

    rows = [dict(row) for row in initial]
    next_id = max((row["id"] for row in rows), default=0) + 1

    def visit_row(
        _day,
        _wc,
        name,
        start,
        breakdown_id,
        *,
        employee_odoo_id=None,
    ):
        return next(
            (
                row
                for row in rows
                if row["start_utc"] == start
                and row["breakdown_id"] == breakdown_id
                and (
                    row.get("employee_odoo_id") == employee_odoo_id
                    if employee_odoo_id is not None
                    else row.get("employee_odoo_id") is None
                    and row["person_name"] == name
                )
            ),
            None,
        )

    def open_row(_day, _wc, name, *, employee_odoo_id=None, **_kwargs):
        return next(
            (
                row
                for row in rows
                if row.get("end_utc") is None
                and (
                    row.get("employee_odoo_id") == employee_odoo_id
                    if employee_odoo_id is not None
                    else row.get("employee_odoo_id") is None
                    and row["person_name"] == name
                )
            ),
            None,
        )

    def insert(
        day,
        wc,
        name,
        start,
        breakdown_id,
        *,
        employee_odoo_id=None,
        end_utc=None,
    ):
        nonlocal next_id
        existing = visit_row(
            day,
            wc,
            name,
            start,
            breakdown_id,
            employee_odoo_id=employee_odoo_id,
        )
        if existing is not None:
            return existing["id"]
        rows.append(
            {
                "id": next_id,
                "day": day,
                "wc_name": wc,
                "person_name": name,
                "employee_odoo_id": employee_odoo_id,
                "start_utc": start,
                "end_utc": end_utc,
                "breakdown_id": breakdown_id,
            }
        )
        next_id += 1
        return rows[-1]["id"]

    def cap_breakdown(row_id, end):
        row = next(row for row in rows if row["id"] == row_id)
        row["end_utc"] = min(row["end_utc"], end) if row["end_utc"] else end

    def finalize_recovery(breakdown_id, resume):
        rows[:] = [
            row
            for row in rows
            if not (
                row["breakdown_id"] == breakdown_id
                and row["start_utc"] >= resume
            )
        ]
        for row in rows:
            if row["breakdown_id"] == breakdown_id:
                row["end_utc"] = (
                    min(row["end_utc"], resume)
                    if row["end_utc"] is not None
                    else resume
                )
        return True

    monkeypatch.setattr(wc_attributions, "breakdown_row_for_visit", visit_row, raising=False)
    monkeypatch.setattr(wc_attributions, "open_breakdown_row", open_row)
    monkeypatch.setattr(wc_attributions, "add_breakdown", insert)
    monkeypatch.setattr(
        wc_attributions,
        "add_completed_breakdown",
        lambda *args, end_utc, **kwargs: insert(
            *args, end_utc=end_utc, **kwargs
        ),
        raising=False,
    )
    monkeypatch.setattr(wc_attributions, "cap_breakdown", cap_breakdown)
    monkeypatch.setattr(
        machine_breakdown,
        "finalize_recovered_incident",
        finalize_recovery,
    )
    monkeypatch.setattr(
        wc_attributions,
        "open_breakdown_rows_for_incident",
        lambda breakdown_id: [
            row
            for row in rows
            if row["breakdown_id"] == breakdown_id and row["end_utc"] is None
        ],
    )
    return rows


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


def test_exempt_no_location_does_not_block_valid_manual_reconciliation(monkeypatch):
    valid = _location_span(
        employee_id=101,
        person_name="Juan",
        status="valid",
        wc_name="Dismantler 2",
        start_utc=_now() - timedelta(hours=2),
        end_utc=_now(),
        attendance_id=91,
    )
    exempt = _location_span(
        employee_id=202,
        person_name="Taylor",
        status="exempt_no_location",
        wc_name=None,
        start_utc=_now() - timedelta(hours=2),
        end_utc=_now(),
        attendance_id=92,
    )
    source = machine_breakdown._operator_source_from_staffing_snapshot(
        _frozen_staffing_snapshot(
            (valid, exempt), current_attendance_ids=(91, 92)
        )
    )

    assert source.complete is True
    assert [operator.employee_odoo_id for operator in source.presences] == [101]

    monkeypatch.setattr(machine_breakdown, "get_open_incident", lambda *_args: None)
    monkeypatch.setattr(
        machine_breakdown,
        "_operator_source_snapshot",
        lambda *_args, **_kwargs: source,
    )
    monkeypatch.setattr(
        machine_breakdown,
        "_last_output_before",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        machine_breakdown, "open_incident", lambda *_args, **_kwargs: 1
    )
    reconciled = []
    monkeypatch.setattr(
        machine_breakdown,
        "_ensure_operator_breakdowns",
        lambda _incident, _day, _now, snapshot: reconciled.extend(
            snapshot.presences
        ),
    )

    result = machine_breakdown.report_manual(
        "Dismantler 2", day=date(2026, 7, 8), now=_now()
    )

    assert result["incident_id"] == 1
    assert [operator.employee_odoo_id for operator in reconciled] == [101]


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
        lambda *_args, **_kwargs: None,
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
    open_rows = {}
    added = []

    def add_breakdown(_day, wc, person, start, breakdown_id, **_kwargs):
        row = open_rows.get(person)
        if row is None:
            row = {"id": 50, "start_utc": start}
            open_rows[person] = row
            added.append((_day, wc, person, start, breakdown_id))
        return row["id"]

    monkeypatch.setattr(
        wc_attributions,
        "open_breakdown_row",
        lambda _day, _wc, person, **_kwargs: open_rows.get(person),
    )
    monkeypatch.setattr(wc_attributions, "add_breakdown", add_breakdown)

    machine_breakdown._ensure_operator_breakdowns(incident, day, _now(), source)
    machine_breakdown._ensure_operator_breakdowns(incident, day, _now(), source)

    assert added == [(day, "Dismantler 2", "Juan", arrival, 1)]


@pytest.mark.parametrize(
    ("worked_minutes", "expected_count"),
    [(59, 0), (60, 1)],
)
def test_delayed_completed_visit_is_reconciled_once_after_source_returns(
    monkeypatch,
    worked_minutes,
    expected_count,
):
    from zira_dashboard import shift_config

    day = date(2026, 7, 8)
    departure = _now() - timedelta(minutes=5)
    arrival = departure - timedelta(minutes=worked_minutes)
    incident = {
        "id": 1,
        "wc_name": "Dismantler 2",
        "day": day,
        "detected_stop_utc": arrival - timedelta(hours=1),
    }
    source = _operator_snapshot(
        departures=(
            _departure(arrival_utc=arrival, departure_utc=departure),
        )
    )
    rows = _fake_breakdown_store(monkeypatch)
    monkeypatch.setattr(
        shift_config,
        "productive_minutes_in_window",
        lambda _day, start, end: (end - start).total_seconds() / 60,
    )

    machine_breakdown._ensure_operator_breakdowns(incident, day, _now(), source)
    machine_breakdown._ensure_operator_breakdowns(incident, day, _now(), source)

    assert len(rows) == expected_count
    if rows:
        assert rows[0]["start_utc"] == arrival
        assert rows[0]["end_utc"] == departure


def test_completed_visit_shortens_an_already_capped_row_to_canonical_departure(
    monkeypatch,
):
    from zira_dashboard import shift_config, wc_attributions

    day = date(2026, 7, 8)
    arrival = _now() - timedelta(minutes=100)
    canonical_departure = arrival + timedelta(minutes=70)
    incident = {
        "id": 1,
        "wc_name": "Dismantler 2",
        "day": day,
        "detected_stop_utc": arrival - timedelta(hours=1),
    }
    source = _operator_snapshot(
        departures=(
            _departure(
                arrival_utc=arrival,
                departure_utc=canonical_departure,
            ),
        )
    )
    monkeypatch.setattr(
        shift_config,
        "productive_minutes_in_window",
        lambda _day, start, end: (end - start).total_seconds() / 60,
    )
    monkeypatch.setattr(
        wc_attributions, "open_breakdown_row", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(
        wc_attributions,
        "breakdown_row_for_visit",
        lambda *_args, **_kwargs: {
            "id": 9,
            "start_utc": arrival,
            "end_utc": canonical_departure + timedelta(minutes=30),
        },
    )
    capped = []
    monkeypatch.setattr(
        wc_attributions,
        "cap_breakdown",
        lambda row_id, end: capped.append((row_id, end)),
    )

    machine_breakdown._reconcile_completed_breakdown_visits(
        incident, day, _now(), source
    )

    assert capped == [(9, canonical_departure)]


@pytest.mark.parametrize(
    ("minutes_present", "should_delete", "should_retime"),
    [
        (30, True, False),
        (60, False, True),
    ],
)
def test_preupgrade_station_stop_row_is_removed_or_retimed_to_personal_arrival(
    monkeypatch,
    minutes_present,
    should_delete,
    should_retime,
):
    from zira_dashboard import shift_config, wc_attributions

    day = date(2026, 7, 8)
    arrival = _now() - timedelta(minutes=minutes_present)
    stop = arrival - timedelta(hours=1)
    incident = {
        "id": 1,
        "wc_name": "Dismantler 2",
        "day": day,
        "detected_stop_utc": stop,
    }
    source = _operator_snapshot(_presence(arrival_utc=arrival))
    monkeypatch.setattr(
        shift_config,
        "productive_minutes_in_window",
        lambda _day, start, end: (end - start).total_seconds() / 60,
    )
    lookups = []
    monkeypatch.setattr(
        wc_attributions,
        "open_breakdown_row",
        lambda *_args, **kwargs: lookups.append(kwargs)
        or {"id": 9, "start_utc": stop, "employee_odoo_id": 101},
    )
    deleted = []
    normalized = []
    monkeypatch.setattr(
        wc_attributions, "delete", lambda row_id: deleted.append(row_id)
    )
    monkeypatch.setattr(
        wc_attributions,
        "normalize_breakdown_visit",
        lambda *args, **kwargs: normalized.append((args, kwargs)),
        raising=False,
    )
    monkeypatch.setattr(
        wc_attributions,
        "add_breakdown",
        lambda *_args, **_kwargs: pytest.fail("existing row must be reconciled"),
    )

    machine_breakdown._ensure_operator_breakdowns(incident, day, _now(), source)

    assert lookups == [
        {
            "employee_odoo_id": 101,
            "breakdown_id": 1,
            "allow_legacy_fallback": True,
        }
    ]
    assert deleted == ([9] if should_delete else [])
    assert normalized == (
        [
            (
                (9, 1, "Juan", arrival),
                {"employee_odoo_id": 101, "end_utc": None},
            )
        ]
        if should_retime
        else []
    )


def test_delayed_recovery_restores_eligible_completed_visit_before_resolving(
    monkeypatch,
):
    from zira_dashboard import shift_config

    day = date(2026, 7, 8)
    arrival = _now() - timedelta(minutes=100)
    departure = arrival + timedelta(minutes=70)
    resume = departure + timedelta(minutes=10)
    incident = {
        "id": 1,
        "wc_name": "Dismantler 2",
        "day": day,
        "detected_stop_utc": arrival - timedelta(hours=1),
    }
    source = _operator_snapshot(
        departures=(
            _departure(arrival_utc=arrival, departure_utc=departure),
        )
    )
    rows = _fake_breakdown_store(monkeypatch)
    monkeypatch.setattr(
        shift_config,
        "productive_minutes_in_window",
        lambda _day, start, end: (end - start).total_seconds() / 60,
    )
    monkeypatch.setattr(
        machine_breakdown, "_last_output_after", lambda *_args, **_kwargs: resume
    )
    finalized = []
    monkeypatch.setattr(
        machine_breakdown,
        "finalize_recovered_incident",
        lambda incident_id, resume_utc: finalized.append(
            (incident_id, resume_utc)
        )
        or True,
    )

    assert machine_breakdown._maybe_auto_resolve(
        incident, day, _now(), source
    ) is True
    assert rows == [
        {
            "id": 1,
            "day": day,
            "wc_name": "Dismantler 2",
            "person_name": "Juan",
            "employee_odoo_id": 101,
            "start_utc": arrival,
            "end_utc": departure,
            "breakdown_id": 1,
        }
    ]
    assert finalized == [(1, resume)]


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
        "add_breakdown",
        lambda *args, **_kwargs: added.append(args),
    )

    machine_breakdown._ensure_operator_breakdowns(incident, day, _now(), source)

    assert added[0][3] == stop


def test_canonical_transfer_caps_only_worker_at_span_end_without_plant_manager_transfer(
    monkeypatch,
):
    from zira_dashboard import shift_config, staffing_transfer, wc_attributions

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
        shift_config,
        "productive_minutes_in_window",
        lambda _day, start, end: (end - start).total_seconds() / 60,
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
    first_arrival = _now() - timedelta(minutes=150)
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
    completed_rows = {}
    capped = []
    added = []

    def open_breakdown_row(_day, _wc, _person, **_kwargs):
        return open_row.copy() if open_row else None

    def cap_breakdown(row_id, end):
        capped.append((row_id, end))
        completed_rows[first_arrival] = {
            "id": row_id,
            "start_utc": first_arrival,
            "end_utc": end,
        }
        open_row.clear()

    def add_breakdown(_day, wc, person, start, breakdown_id, **_kwargs):
        added.append((_day, wc, person, start, breakdown_id))
        open_row.update(id=11, start_utc=start)
        return 11

    monkeypatch.setattr(wc_attributions, "open_breakdown_row", open_breakdown_row)
    monkeypatch.setattr(
        wc_attributions,
        "breakdown_row_for_visit",
        lambda _day, _wc, _person, start, _incident_id, **_kwargs: completed_rows.get(
            start
        ),
    )
    monkeypatch.setattr(wc_attributions, "cap_breakdown", cap_breakdown)
    monkeypatch.setattr(wc_attributions, "add_breakdown", add_breakdown)
    monkeypatch.setattr(
        shift_config,
        "productive_minutes_in_window",
        lambda _day, start, end: (end - start).total_seconds() / 60,
    )

    machine_breakdown._ensure_operator_breakdowns(incident, day, _now(), source)

    assert capped == [(10, first_departure)]
    assert added == [(day, "Dismantler 2", "Juan", second_arrival, 1)]


def test_preupgrade_old_start_closes_first_visit_before_current_return(monkeypatch):
    from zira_dashboard import shift_config, wc_attributions

    day = date(2026, 7, 8)
    station_stop = _now() - timedelta(hours=4)
    first_arrival = station_stop + timedelta(minutes=30)
    first_departure = first_arrival + timedelta(minutes=70)
    return_arrival = first_departure + timedelta(minutes=10)
    incident = {
        "id": 1,
        "wc_name": "Dismantler 2",
        "day": day,
        "detected_stop_utc": station_stop,
    }
    source = _operator_snapshot(
        _presence(arrival_utc=return_arrival),
        departures=(
            _departure(
                arrival_utc=first_arrival,
                departure_utc=first_departure,
            ),
        ),
    )
    rows = [
        {
            "id": 9,
            "start_utc": station_stop,
            "end_utc": None,
            "employee_odoo_id": 101,
        }
    ]
    monkeypatch.setattr(
        shift_config,
        "productive_minutes_in_window",
        lambda _day, start, end: (end - start).total_seconds() / 60,
    )
    monkeypatch.setattr(
        wc_attributions,
        "open_breakdown_row",
        lambda *_args, **_kwargs: next(
            (row for row in rows if row["end_utc"] is None), None
        ),
    )
    monkeypatch.setattr(
        wc_attributions,
        "breakdown_row_for_visit",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        wc_attributions,
        "add_completed_breakdown",
        lambda *_args, **_kwargs: pytest.fail(
            "the existing open row must be normalized before insert"
        ),
    )

    def normalize(row_id, _incident_id, _name, start, *, end_utc, **_kwargs):
        row = next(row for row in rows if row["id"] == row_id)
        row["start_utc"] = start
        row["end_utc"] = end_utc
        return row_id

    def add(_day, _wc, _name, start, _breakdown_id, **_kwargs):
        rows.append(
            {
                "id": 10,
                "start_utc": start,
                "end_utc": None,
                "employee_odoo_id": 101,
            }
        )
        return 10

    monkeypatch.setattr(wc_attributions, "normalize_breakdown_visit", normalize)
    monkeypatch.setattr(wc_attributions, "add_breakdown", add)

    machine_breakdown._ensure_operator_breakdowns(
        incident, day, _now(), source
    )

    assert [(row["start_utc"], row["end_utc"]) for row in rows] == [
        (first_arrival, first_departure),
        (return_arrival, None),
    ]


def test_completed_visit_normalization_converges_when_exact_row_already_exists(
    monkeypatch,
):
    from zira_dashboard import shift_config, wc_attributions

    day = date(2026, 7, 8)
    station_stop = _now() - timedelta(hours=3)
    arrival = station_stop + timedelta(minutes=20)
    departure = arrival + timedelta(minutes=70)
    incident = {
        "id": 1,
        "wc_name": "Dismantler 2",
        "day": day,
        "detected_stop_utc": station_stop,
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
    monkeypatch.setattr(
        wc_attributions,
        "open_breakdown_row",
        lambda *_args, **_kwargs: {"id": 9, "start_utc": station_stop},
    )
    monkeypatch.setattr(
        wc_attributions,
        "breakdown_row_for_visit",
        lambda *_args, **_kwargs: {
            "id": 10,
            "start_utc": arrival,
            "end_utc": departure + timedelta(minutes=10),
        },
    )
    normalized = []
    monkeypatch.setattr(
        wc_attributions,
        "normalize_breakdown_visit",
        lambda *args, **kwargs: normalized.append((args, kwargs)) or 10,
        raising=False,
    )

    machine_breakdown._reconcile_completed_breakdown_visits(
        incident, day, _now(), source
    )

    assert normalized == [
        (
            (9, 1, "Juan", arrival),
            {"employee_odoo_id": 101, "end_utc": departure},
        )
    ]


def test_current_visit_normalization_reopens_existing_exact_row_without_collision(
    monkeypatch,
):
    from zira_dashboard import shift_config, wc_attributions

    day = date(2026, 7, 8)
    station_stop = _now() - timedelta(hours=3)
    arrival = _now() - timedelta(minutes=70)
    incident = {
        "id": 1,
        "wc_name": "Dismantler 2",
        "day": day,
        "detected_stop_utc": station_stop,
    }
    source = _operator_snapshot(_presence(arrival_utc=arrival))
    monkeypatch.setattr(
        shift_config,
        "productive_minutes_in_window",
        lambda _day, start, end: (end - start).total_seconds() / 60,
    )
    monkeypatch.setattr(
        wc_attributions,
        "open_breakdown_row",
        lambda *_args, **_kwargs: {"id": 9, "start_utc": station_stop},
    )
    normalized = []
    monkeypatch.setattr(
        wc_attributions,
        "normalize_breakdown_visit",
        lambda *args, **kwargs: normalized.append((args, kwargs)) or 10,
        raising=False,
    )

    machine_breakdown._ensure_operator_breakdowns(
        incident, day, _now(), source
    )

    assert normalized == [
        (
            (9, 1, "Juan", arrival),
            {"employee_odoo_id": 101, "end_utc": None},
        )
    ]


def test_same_name_workers_get_distinct_idempotent_exclusions(monkeypatch):
    from zira_dashboard import shift_config, wc_attributions

    day = date(2026, 7, 8)
    stop = _now() - timedelta(hours=3)
    alex_101_arrival = _now() - timedelta(hours=2)
    alex_202_arrival = _now() - timedelta(minutes=90)
    incident = {
        "id": 1,
        "wc_name": "Dismantler 2",
        "day": day,
        "detected_stop_utc": stop,
    }
    source = _operator_snapshot(
        _presence("Alex", arrival_utc=alex_101_arrival, employee_odoo_id=101),
        _presence("Alex", arrival_utc=alex_202_arrival, employee_odoo_id=202),
    )
    open_rows = {}
    added = []
    monkeypatch.setattr(
        shift_config,
        "productive_minutes_in_window",
        lambda _day, start, end: (end - start).total_seconds() / 60,
    )
    monkeypatch.setattr(
        wc_attributions,
        "open_breakdown_row",
        lambda _day, _wc, _name, employee_odoo_id=None, **_kwargs: open_rows.get(
            employee_odoo_id
        ),
    )

    def add_breakdown(
        _day,
        wc,
        name,
        start,
        breakdown_id,
        *,
        employee_odoo_id=None,
    ):
        row = {"id": len(open_rows) + 1, "start_utc": start}
        open_rows[employee_odoo_id] = row
        added.append((wc, name, start, breakdown_id, employee_odoo_id))
        return row["id"]

    monkeypatch.setattr(wc_attributions, "add_breakdown", add_breakdown)

    machine_breakdown._ensure_operator_breakdowns(incident, day, _now(), source)
    machine_breakdown._ensure_operator_breakdowns(incident, day, _now(), source)

    assert added == [
        ("Dismantler 2", "Alex", alex_101_arrival, 1, 101),
        ("Dismantler 2", "Alex", alex_202_arrival, 1, 202),
    ]


def test_unique_odoo_worker_adopts_legacy_name_keyed_open_exclusion(monkeypatch):
    from zira_dashboard import shift_config, wc_attributions

    day = date(2026, 7, 8)
    arrival = _now() - timedelta(hours=2)
    incident = {
        "id": 1,
        "wc_name": "Dismantler 2",
        "day": day,
        "detected_stop_utc": _now() - timedelta(hours=3),
    }
    source = _operator_snapshot(_presence("Alex", arrival_utc=arrival))
    lookups = []
    monkeypatch.setattr(
        shift_config,
        "productive_minutes_in_window",
        lambda _day, start, end: (end - start).total_seconds() / 60,
    )
    monkeypatch.setattr(
        wc_attributions,
        "open_breakdown_row",
        lambda *_args, **kwargs: lookups.append(kwargs)
        or {"id": 7, "start_utc": arrival},
    )
    added = []
    monkeypatch.setattr(
        wc_attributions,
        "add_breakdown",
        lambda *args, **kwargs: added.append((args, kwargs)),
    )

    machine_breakdown._ensure_operator_breakdowns(incident, day, _now(), source)

    assert lookups == [
        {
            "employee_odoo_id": 101,
            "breakdown_id": 1,
            "allow_legacy_fallback": True,
        }
    ]
    assert added == []


def test_same_name_departure_caps_only_matching_odoo_identity(monkeypatch):
    from zira_dashboard import shift_config, wc_attributions

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

    def open_row(
        _day,
        _wc,
        person,
        *,
        employee_odoo_id=None,
        breakdown_id=None,
        **_kwargs,
    ):
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
    monkeypatch.setattr(
        shift_config,
        "productive_minutes_in_window",
        lambda _day, start, end: (end - start).total_seconds() / 60,
    )

    machine_breakdown._cap_departed_operators(incident, day, _now(), source)

    assert lookups == [("Alex", 101, 1)]
    assert capped == [(10, departure)]


def test_departed_duplicate_names_remove_ambiguous_legacy_open_row(monkeypatch):
    from zira_dashboard import shift_config, wc_attributions

    day = date(2026, 7, 8)
    stop = _now() - timedelta(hours=3)
    incident = {
        "id": 1,
        "wc_name": "Dismantler 2",
        "day": day,
        "detected_stop_utc": stop,
    }
    source = _operator_snapshot(
        departures=(
            _departure(
                "Alex",
                arrival_utc=stop + timedelta(minutes=10),
                departure_utc=stop + timedelta(minutes=80),
                employee_odoo_id=101,
            ),
            _departure(
                "Alex",
                arrival_utc=stop + timedelta(minutes=20),
                departure_utc=stop + timedelta(minutes=90),
                employee_odoo_id=202,
            ),
        )
    )
    legacy = {"id": 9, "start_utc": stop, "end_utc": None}
    monkeypatch.setattr(
        shift_config,
        "productive_minutes_in_window",
        lambda _day, start, end: (end - start).total_seconds() / 60,
    )
    monkeypatch.setattr(
        wc_attributions,
        "open_breakdown_row",
        lambda *_args, employee_odoo_id=None, **_kwargs: (
            legacy if employee_odoo_id is None else None
        ),
    )
    monkeypatch.setattr(
        wc_attributions,
        "breakdown_row_for_visit",
        lambda *_args, **_kwargs: None,
    )
    deleted = []
    added = []
    monkeypatch.setattr(
        wc_attributions, "delete", lambda row_id: deleted.append(row_id)
    )
    monkeypatch.setattr(
        wc_attributions,
        "add_completed_breakdown",
        lambda *_args, employee_odoo_id=None, **_kwargs: added.append(
            employee_odoo_id
        ),
    )

    machine_breakdown._ensure_operator_breakdowns(
        incident, day, _now(), source
    )

    assert deleted == [9]
    assert added == [101, 202]


def test_recovery_is_checked_before_worker_exclusion_threshold(monkeypatch):
    from zira_dashboard import shift_config, wc_attributions

    day = date(2026, 7, 8)
    stop = _now() - timedelta(hours=2)
    arrival = _now() - timedelta(minutes=70)
    resume = arrival + timedelta(minutes=55)
    incident = {
        "id": 1,
        "wc_name": "Dismantler 2",
        "day": day,
        "detected_stop_utc": stop,
        "source": "auto",
    }
    source = _operator_snapshot(_presence(arrival_utc=arrival))
    open_rows = []
    added = []
    capped = []
    resolved = []
    monkeypatch.setattr(machine_breakdown, "all_open_incidents", lambda _day: [incident])
    monkeypatch.setattr(
        machine_breakdown, "_operator_source_snapshot", lambda _day, _now: source
    )
    monkeypatch.setattr(
        machine_breakdown,
        "_last_output_after",
        lambda *_args, **_kwargs: resume,
    )
    monkeypatch.setattr(
        machine_breakdown,
        "_station_signals",
        lambda *_args, **_kwargs: [],
    )
    monkeypatch.setattr(
        machine_breakdown,
        "_shift_bounds",
        lambda _day: (_now() - timedelta(hours=6), _now() + timedelta(hours=2)),
    )
    monkeypatch.setattr(shift_config, "in_shift_on", lambda _local: True)
    monkeypatch.setattr(
        shift_config,
        "productive_minutes_in_window",
        lambda _day, start, end: (end - start).total_seconds() / 60,
    )
    monkeypatch.setattr(
        wc_attributions,
        "open_breakdown_row",
        lambda *_args, **_kwargs: open_rows[0] if open_rows else None,
    )

    def add_breakdown(*args, **kwargs):
        added.append((args, kwargs))
        open_rows.append({"id": 9, "start_utc": args[3]})
        return 9

    monkeypatch.setattr(wc_attributions, "add_breakdown", add_breakdown)
    monkeypatch.setattr(
        machine_breakdown,
        "finalize_recovered_incident",
        lambda incident_id, resume_utc: resolved.append(
            (incident_id, "recovered", resume_utc)
        )
        or True,
    )

    machine_breakdown.run_detect_tick(day=day, now=_now())

    assert added == []
    assert capped == []
    assert resolved == [(1, "recovered", resume)]


def test_same_name_workers_never_adopt_ambiguous_legacy_identity(monkeypatch):
    from zira_dashboard import shift_config, wc_attributions

    day = date(2026, 7, 8)
    stop = _now() - timedelta(hours=3)
    incident = {
        "id": 1,
        "wc_name": "Dismantler 2",
        "day": day,
        "detected_stop_utc": stop,
    }
    source = _operator_snapshot(
        _presence("Alex", employee_odoo_id=101),
        _presence("Alex", employee_odoo_id=202),
    )
    monkeypatch.setattr(
        shift_config,
        "productive_minutes_in_window",
        lambda _day, start, end: (end - start).total_seconds() / 60,
    )
    lookups = []
    monkeypatch.setattr(
        wc_attributions,
        "open_breakdown_row",
        lambda *_args, **kwargs: lookups.append(kwargs) or None,
    )
    added = []
    monkeypatch.setattr(
        wc_attributions,
        "add_breakdown",
        lambda *args, **kwargs: added.append((args, kwargs)) or 10,
    )

    machine_breakdown._ensure_operator_breakdowns(incident, day, _now(), source)

    assert len(added) == 2
    assert all(
        lookup["allow_legacy_fallback"] is False
        for lookup in lookups
        if lookup.get("employee_odoo_id") is not None
    )
    assert [kwargs["employee_odoo_id"] for _args, kwargs in added] == [101, 202]


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
    added = []
    monkeypatch.setattr(
        wc_attributions, "open_breakdown_row", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(
        wc_attributions, "breakdown_row_for_visit", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(
        wc_attributions,
        "add_completed_breakdown",
        lambda *args, **kwargs: added.append((args, kwargs)) or 50,
    )

    machine_breakdown._ensure_operator_breakdowns(incident, day, _now(), source)

    assert added == [
        (
            (day, "Dismantler 2", "Juan", arrival, 1),
            {
                "employee_odoo_id": 101,
                "end_utc": departure,
            },
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
    added = []
    monkeypatch.setattr(
        wc_attributions, "open_breakdown_row", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(
        wc_attributions,
        "add_completed_breakdown",
        lambda *args, **kwargs: added.append((args, kwargs)),
    )

    machine_breakdown._ensure_operator_breakdowns(incident, day, _now(), source)

    assert added == []


def test_recovery_caps_every_open_exclusion_at_first_resume_when_source_unavailable(
    monkeypatch,
):
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
    finalized = []
    monkeypatch.setattr(
        machine_breakdown,
        "finalize_recovered_incident",
        lambda incident_id, resume_utc: finalized.append(
            (incident_id, resume_utc)
        )
        or True,
    )

    recovered = machine_breakdown._maybe_auto_resolve(
        incident, day, _now(), source
    )

    assert recovered is True
    assert finalized == [(1, first_resume)]


def test_recovery_backfills_completed_visit_before_resolve_and_caps_at_resume(
    monkeypatch,
):
    from zira_dashboard import shift_config, wc_attributions

    day = date(2026, 7, 8)
    stop = _now() - timedelta(hours=3)
    arrival = _now() - timedelta(hours=2)
    resume = _now() - timedelta(minutes=30)
    later_departure = _now() - timedelta(minutes=5)
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
                departure_utc=later_departure,
                employee_odoo_id=101,
            ),
        )
    )
    monkeypatch.setattr(
        machine_breakdown, "_last_output_after", lambda *_args, **_kwargs: resume
    )
    monkeypatch.setattr(
        shift_config,
        "productive_minutes_in_window",
        lambda _day, start, end: (end - start).total_seconds() / 60,
    )
    events = []
    monkeypatch.setattr(
        wc_attributions,
        "open_breakdown_row",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        wc_attributions,
        "breakdown_row_for_visit",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        wc_attributions,
        "add_completed_breakdown",
        lambda *args, **kwargs: events.append(("add", args, kwargs)) or 10,
    )
    monkeypatch.setattr(
        machine_breakdown,
        "finalize_recovered_incident",
        lambda *_args, **_kwargs: events.append(("finalize",)) or True,
    )

    assert machine_breakdown._maybe_auto_resolve(
        incident, day, _now(), source
    ) is True
    assert events[0][0] == "add"
    assert events[0][2]["end_utc"] == resume
    assert [event[0] for event in events] == ["add", "finalize"]


def test_recovery_keeps_earlier_worker_departure_as_exclusion_end(monkeypatch):
    from zira_dashboard import shift_config, wc_attributions

    day = date(2026, 7, 8)
    departure = _now() - timedelta(minutes=30)
    resume = _now() - timedelta(minutes=10)
    incident = {
        "id": 1,
        "wc_name": "Dismantler 2",
        "day": day,
        "detected_stop_utc": _now() - timedelta(hours=2),
    }
    source = _operator_snapshot(
        departures=(
            _departure(
                departure_utc=departure,
                employee_odoo_id=101,
            ),
        )
    )
    open_row = {"id": 9, "start_utc": incident["detected_stop_utc"]}
    monkeypatch.setattr(
        machine_breakdown,
        "_last_output_after",
        lambda *_args, **_kwargs: resume,
    )
    monkeypatch.setattr(
        wc_attributions,
        "open_breakdown_row",
        lambda *_args, **_kwargs: open_row,
    )
    capped = []

    def cap_breakdown(row_id, end):
        capped.append((row_id, end))
        open_row["closed"] = True

    monkeypatch.setattr(wc_attributions, "cap_breakdown", cap_breakdown)
    monkeypatch.setattr(
        shift_config,
        "productive_minutes_in_window",
        lambda _day, start, end: (end - start).total_seconds() / 60,
    )
    monkeypatch.setattr(
        machine_breakdown,
        "finalize_recovered_incident",
        lambda *_args, **_kwargs: True,
    )

    machine_breakdown._maybe_auto_resolve(incident, day, _now(), source)

    assert capped == [(9, departure)]


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


def test_recovery_uses_one_locked_finalize_boundary_after_reconciliation(monkeypatch):
    from zira_dashboard import wc_attributions

    day = date(2026, 7, 8)
    resume = _now() - timedelta(minutes=10)
    incident = {
        "id": 1,
        "wc_name": "Dismantler 2",
        "day": day,
        "detected_stop_utc": _now() - timedelta(hours=2),
    }
    source = _operator_snapshot()
    monkeypatch.setattr(
        machine_breakdown, "_last_output_after", lambda *_args, **_kwargs: resume
    )
    monkeypatch.setattr(
        wc_attributions,
        "open_breakdown_rows_for_incident",
        lambda _incident_id: pytest.fail(
            "recovery must not fetch rows before its incident lock"
        ),
    )
    finalized = []
    monkeypatch.setattr(
        machine_breakdown,
        "finalize_recovered_incident",
        lambda incident_id, resume_utc: finalized.append(
            (incident_id, resume_utc)
        )
        or True,
        raising=False,
    )

    assert machine_breakdown._maybe_auto_resolve(
        incident, day, _now(), source
    ) is True
    assert finalized == [(1, resume)]


def test_finalize_recovery_locks_removes_post_resume_rows_caps_then_resolves(
    monkeypatch,
):
    from contextlib import contextmanager

    from zira_dashboard import db, wc_attributions

    statements = []

    class Cursor:
        def execute(self, sql, params):
            statements.append((" ".join(sql.split()), params))

        def fetchone(self):
            return {"id": 42}

    @contextmanager
    def cursor():
        yield Cursor()

    monkeypatch.setattr(db, "cursor", cursor)
    resume = _now() - timedelta(minutes=10)

    finalized = machine_breakdown.finalize_recovered_incident(42, resume)

    assert finalized is True
    assert "SELECT id FROM machine_breakdowns" in statements[0][0]
    assert "resolved_at IS NULL" in statements[0][0]
    assert "FOR UPDATE" in statements[0][0]
    assert statements[0][1] == (42,)
    assert "DELETE FROM wc_time_attributions" in statements[1][0]
    assert "start_utc >= %s" in statements[1][0]
    assert statements[1][1] == (
        42,
        wc_attributions.BREAKDOWN_SOURCE,
        resume,
    )
    assert "UPDATE wc_time_attributions" in statements[2][0]
    assert "LEAST(COALESCE(end_utc" in statements[2][0]
    assert "start_utc < %s" in statements[2][0]
    assert statements[2][1] == (
        resume,
        resume,
        42,
        wc_attributions.BREAKDOWN_SOURCE,
        resume,
    )
    assert "UPDATE machine_breakdowns" in statements[3][0]
    assert statements[3][1] == (resume, 42)


def test_delayed_recovery_reconciles_departure_before_same_worker_return(monkeypatch):
    from zira_dashboard import shift_config

    day = date(2026, 7, 8)
    first_arrival = _now() - timedelta(minutes=150)
    first_departure = _now() - timedelta(minutes=80)
    return_arrival = _now() - timedelta(minutes=70)
    resume = _now() - timedelta(minutes=5)
    incident = {
        "id": 1,
        "wc_name": "Dismantler 2",
        "day": day,
        "detected_stop_utc": first_arrival - timedelta(hours=1),
    }
    source = _operator_snapshot(
        _presence(arrival_utc=return_arrival),
        departures=(
            _departure(
                arrival_utc=first_arrival,
                departure_utc=first_departure,
            ),
        ),
    )
    rows = _fake_breakdown_store(
        monkeypatch,
        initial=(
            {
                "id": 9,
                "day": day,
                "wc_name": "Dismantler 2",
                "person_name": "Juan",
                "employee_odoo_id": 101,
                "start_utc": first_arrival,
                "end_utc": None,
                "breakdown_id": 1,
            },
        ),
    )
    monkeypatch.setattr(
        shift_config,
        "productive_minutes_in_window",
        lambda _day, start, end: (end - start).total_seconds() / 60,
    )
    monkeypatch.setattr(
        machine_breakdown, "_last_output_after", lambda *_args, **_kwargs: resume
    )
    def finalize(_incident_id, resume_utc):
        for row in rows:
            if row["end_utc"] is None:
                row["end_utc"] = resume_utc
        return True

    monkeypatch.setattr(machine_breakdown, "finalize_recovered_incident", finalize)

    assert machine_breakdown._maybe_auto_resolve(
        incident, day, _now(), source
    ) is True
    assert [(row["start_utc"], row["end_utc"]) for row in rows] == [
        (first_arrival, first_departure),
        (return_arrival, resume),
    ]


def test_snooze_storage_keeps_same_name_odoo_identities_separate(monkeypatch):
    from zira_dashboard import db

    writes = []
    monkeypatch.setattr(
        db, "execute", lambda sql, params: writes.append((sql, params))
    )

    machine_breakdown.snooze_operator(1, "Alex", employee_odoo_id=101)
    machine_breakdown.snooze_operator(1, "Alex", employee_odoo_id=202)

    assert [params[:3] for _sql, params in writes] == [
        (1, "Alex", 101),
        (1, "Alex", 202),
    ]
    assert all("employee_odoo_id" in sql for sql, _params in writes)


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


def test_snooze_lookup_keeps_same_name_odoo_identities_off_legacy_row(monkeypatch):
    from zira_dashboard import db

    seen = []
    until = _now() + timedelta(minutes=10)

    def query(sql, params):
        seen.append((sql, params))
        # Model one old name-only snooze.  An ID-backed lookup must not read it
        # because both immutable workers are named Alex.
        if "employee_odoo_id IS NULL" in sql:
            return [{"until_utc": until}]
        return []

    monkeypatch.setattr(db, "query", query)

    alex_101 = machine_breakdown.active_snooze_until(
        1, "Alex", employee_odoo_id=101
    )
    alex_202 = machine_breakdown.active_snooze_until(
        1, "Alex", employee_odoo_id=202
    )

    assert alex_101 is None
    assert alex_202 is None
    assert [params for _sql, params in seen] == [(1, 101), (1, 202)]
    assert all("employee_odoo_id IS NULL" not in sql for sql, _params in seen)


def test_legacy_snooze_lookup_remains_name_keyed(monkeypatch):
    from zira_dashboard import db

    seen = {}
    until = _now() + timedelta(minutes=10)
    monkeypatch.setattr(
        db,
        "query",
        lambda sql, params: seen.update(sql=sql, params=params)
        or [{"until_utc": until}],
    )

    assert machine_breakdown.active_snooze_until(1, "Alex") == until
    assert "employee_odoo_id IS NULL" in seen["sql"]
    assert seen["params"] == (1, "Alex")
