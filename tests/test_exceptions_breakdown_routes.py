"""POST /api/exceptions/breakdown/{transfer,snooze,dismiss,report}."""
from datetime import date, datetime, timezone

import pytest

from zira_dashboard.routes import exceptions as exceptions_route

_STOP = datetime(2026, 7, 8, 18, 2, tzinfo=timezone.utc)


def _successful_transfer_outcome(
    person_name="Alex", employee_odoo_id=101, attribution_id=10
):
    return {
        "status": "success",
        "incident": {
            "id": 1,
            "wc_name": "Dismantler 2",
            "day": date(2026, 7, 8),
            "detected_stop_utc": _STOP,
        },
        "person_name": person_name,
        "employee_odoo_id": employee_odoo_id,
        "attribution_id": attribution_id,
        "result": {
            "transfer": "moved",
            "closed_id": 5,
            "new_id": 6,
        },
    }


def test_breakdown_transfer_sync_delegates_with_actor(monkeypatch):
    from zira_dashboard import breakdown_actions
    from zira_dashboard.routes import exceptions as exceptions_route

    seen = {}

    def fake(body, actor_upn=None, actor_name=None, friendly_error=None):
        seen.update(body=body, actor_upn=actor_upn, actor_name=actor_name)
        return exceptions_route.JSONResponse({"ok": True})

    monkeypatch.setattr(breakdown_actions, "transfer", fake)
    response = exceptions_route._breakdown_transfer_sync(
        {"incident_id": 1, "person_name": "Ana", "to_wc": "Repair 3"},
        "dale@example.com",
        "Dale",
    )
    assert response.status_code == 200
    assert seen == {
        "body": {"incident_id": 1, "person_name": "Ana", "to_wc": "Repair 3"},
        "actor_upn": "dale@example.com",
        "actor_name": "Dale",
    }


def test_transfer_sync_caps_exclusion_and_calls_decide_and_apply(monkeypatch):
    from zira_dashboard import machine_breakdown, wc_attributions, staffing_transfer, inbox_log
    monkeypatch.setattr(machine_breakdown, "get_incident", lambda iid: {
        "id": 1, "wc_name": "Dismantler 2", "day": "2026-07-08", "detected_stop_utc": _STOP,
    })
    monkeypatch.setattr(
        wc_attributions,
        "breakdown_operator_rows_for_incident",
        lambda _incident_id: [
            {"person_name": "Juan", "employee_odoo_id": 101}
        ],
    )
    monkeypatch.setattr(wc_attributions, "open_breakdown_row",
                        lambda day, wc, person, **_kwargs: {"id": 10})
    capped = []
    monkeypatch.setattr(wc_attributions, "cap_breakdown", lambda rid, end: capped.append((rid, end)))
    applied = {}
    def _decide_and_apply(person, wc, ts, *, employee_odoo_id=None):
        applied.update(
            person=person,
            wc=wc,
            ts=ts,
            employee_odoo_id=employee_odoo_id,
        )
        return {"transfer": "moved", "person": person,
                "closed_id": 5, "new_id": 6, "to_dept": "Recycled"}
    monkeypatch.setattr(staffing_transfer, "decide_and_apply", _decide_and_apply)
    monkeypatch.setattr(
        machine_breakdown,
        "transfer_open_incident",
        lambda incident_id, person, employee_id, wc: applied.update(
            incident_id=incident_id,
            person=person,
            employee_odoo_id=employee_id,
            wc=wc,
        )
        or _successful_transfer_outcome("Juan", 101),
    )
    logged = []
    monkeypatch.setattr(inbox_log, "log_event_safe", lambda **kw: logged.append(kw) or 42)

    resp = exceptions_route._breakdown_transfer_sync(
        {
            "incident_id": 1,
            "person_name": "Juan",
            "employee_odoo_id": 101,
            "to_wc": "Repair 3",
        },
        actor_upn="dale@gruberpallets.com", actor_name="Dale",
    )

    assert resp.status_code == 200
    assert capped == []
    assert applied == {
        "incident_id": 1,
        "person": "Juan",
        "wc": "Repair 3",
        "employee_odoo_id": 101,
    }
    assert logged[0]["item_kind"] == "breakdown"
    assert logged[0]["action"] == "transfer"
    assert logged[0]["item_key"].endswith(":odoo:101")
    assert logged[0]["reversible"] is True
    assert logged[0]["detail"]["closed_id"] == 5
    assert logged[0]["detail"]["new_id"] == 6
    assert logged[0]["detail"]["attribution_id"] == 10


def test_legacy_transfer_never_caps_worker_exclusion_before_personal_start(
    monkeypatch,
):
    from zira_dashboard import inbox_log, machine_breakdown, staffing_transfer, wc_attributions

    personal_start = _STOP.replace(minute=12)
    monkeypatch.setattr(
        machine_breakdown,
        "get_incident",
        lambda _incident_id: {
            "id": 1,
            "wc_name": "Dismantler 2",
            "day": "2026-07-08",
            "detected_stop_utc": _STOP,
        },
    )
    monkeypatch.setattr(
        wc_attributions,
        "breakdown_operator_rows_for_incident",
        lambda _incident_id: [
            {"person_name": "Juan", "employee_odoo_id": None}
        ],
    )
    monkeypatch.setattr(
        wc_attributions,
        "open_breakdown_row",
        lambda *_args, **_kwargs: {"id": 10, "start_utc": personal_start},
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
        lambda *_args, **_kwargs: {
            "closed_id": 5,
            "new_id": 6,
            "transfer": "moved",
        },
    )
    monkeypatch.setattr(
        machine_breakdown,
        "transfer_open_incident",
        lambda *_args: _successful_transfer_outcome("Juan", None),
    )
    monkeypatch.setattr(inbox_log, "log_event_safe", lambda **_kwargs: 42)

    response = exceptions_route._breakdown_transfer_sync(
        {"incident_id": 1, "person_name": "Juan", "to_wc": "Repair 3"}
    )

    assert response.status_code == 200
    assert capped == []


def test_transfer_sync_500_with_friendly_error_when_decide_and_apply_raises(monkeypatch):
    from zira_dashboard import machine_breakdown, wc_attributions, staffing_transfer, inbox_log

    monkeypatch.setattr(machine_breakdown, "get_incident", lambda iid: {
        "id": 1, "wc_name": "Dismantler 2", "day": "2026-07-08", "detected_stop_utc": _STOP,
    })
    monkeypatch.setattr(
        wc_attributions,
        "breakdown_operator_rows_for_incident",
        lambda _incident_id: [
            {"person_name": "Juan", "employee_odoo_id": None}
        ],
    )
    monkeypatch.setattr(wc_attributions, "open_breakdown_row",
                        lambda day, wc, person, **_kwargs: {"id": 10})
    monkeypatch.setattr(wc_attributions, "cap_breakdown", lambda rid, end: None)

    def _raise(person, wc, ts, **_kwargs):
        raise RuntimeError("xmlrpc boom")

    monkeypatch.setattr(staffing_transfer, "decide_and_apply", _raise)
    monkeypatch.setattr(
        machine_breakdown,
        "transfer_open_incident",
        lambda *_args: (_ for _ in ()).throw(RuntimeError("xmlrpc boom")),
    )
    logged = []
    monkeypatch.setattr(inbox_log, "log_event_safe", lambda **kw: logged.append(kw) or 99)

    resp = exceptions_route._breakdown_transfer_sync(
        {"incident_id": 1, "person_name": "Juan", "to_wc": "Repair 3"},
        actor_upn="dale@gruberpallets.com", actor_name="Dale",
    )

    assert resp.status_code == 500
    assert not logged  # no inbox event logged on a failed transfer


def test_transfer_sync_404_when_incident_missing(monkeypatch):
    from zira_dashboard import machine_breakdown
    monkeypatch.setattr(
        machine_breakdown,
        "transfer_open_incident",
        lambda *_args: {"status": "not_found"},
    )
    resp = exceptions_route._breakdown_transfer_sync(
        {"incident_id": 1, "person_name": "Juan", "to_wc": "Repair 3"}, None, None)
    assert resp.status_code == 404


def test_transfer_rejects_resolved_incident_without_side_effects(monkeypatch):
    from zira_dashboard import (
        inbox_log,
        machine_breakdown,
        staffing_transfer,
        wc_attributions,
    )

    monkeypatch.setattr(
        machine_breakdown,
        "get_incident",
        lambda _incident_id: {
            "id": 1,
            "wc_name": "Dismantler 2",
            "day": date(2026, 7, 8),
            "detected_stop_utc": _STOP,
            "resolved_at": _STOP,
            "resolution": "dismissed",
        },
    )
    monkeypatch.setattr(
        wc_attributions,
        "breakdown_operator_rows_for_incident",
        lambda _incident_id: [
            {"person_name": "Alex", "employee_odoo_id": 101}
        ],
    )
    effects = []
    monkeypatch.setattr(
        wc_attributions,
        "open_breakdown_row",
        lambda *_args, **_kwargs: {"id": 10, "start_utc": _STOP},
    )
    monkeypatch.setattr(
        wc_attributions, "cap_breakdown", lambda *_args: effects.append("cap")
    )
    monkeypatch.setattr(
        staffing_transfer,
        "decide_and_apply",
        lambda *_args, **_kwargs: effects.append("odoo")
        or {"transfer": "moved"},
    )
    monkeypatch.setattr(
        inbox_log,
        "log_event_safe",
        lambda **_kwargs: effects.append("audit") or 42,
    )
    monkeypatch.setattr(
        machine_breakdown,
        "transfer_open_incident",
        lambda *_args: {"status": "resolved"},
    )

    response = exceptions_route._breakdown_transfer_sync(
        {
            "incident_id": 1,
            "person_name": "Alex",
            "employee_odoo_id": 101,
            "to_wc": "Repair 3",
        }
    )

    assert response.status_code == 409
    assert effects == []


def test_transfer_roster_rejection_has_zero_side_effects(monkeypatch):
    from zira_dashboard import (
        inbox_log,
        machine_breakdown,
        staffing_transfer,
        wc_attributions,
    )

    monkeypatch.setattr(
        machine_breakdown,
        "get_incident",
        lambda _incident_id: {
            "id": 1,
            "wc_name": "Dismantler 2",
            "day": date(2026, 7, 8),
            "detected_stop_utc": _STOP,
            "resolved_at": None,
        },
    )
    monkeypatch.setattr(
        wc_attributions,
        "breakdown_operator_rows_for_incident",
        lambda _incident_id: [
            {"person_name": "Alex", "employee_odoo_id": 101}
        ],
    )
    monkeypatch.setattr(
        wc_attributions,
        "open_breakdown_row",
        lambda *_args, **_kwargs: {"id": 10, "start_utc": _STOP},
    )
    effects = []
    monkeypatch.setattr(
        wc_attributions, "cap_breakdown", lambda *_args: effects.append("cap")
    )
    monkeypatch.setattr(
        staffing_transfer,
        "decide_and_apply",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            ValueError("employee_odoo_id 101 is not on the live roster")
        ),
    )
    monkeypatch.setattr(
        inbox_log,
        "log_event_safe",
        lambda **_kwargs: effects.append("audit") or 42,
    )
    monkeypatch.setattr(
        machine_breakdown,
        "transfer_open_incident",
        lambda *_args: (_ for _ in ()).throw(
            ValueError("employee_odoo_id 101 is not on the live roster")
        ),
    )

    response = exceptions_route._breakdown_transfer_sync(
        {
            "incident_id": 1,
            "person_name": "Alex",
            "employee_odoo_id": 101,
            "to_wc": "Repair 3",
        }
    )

    assert response.status_code == 409
    assert effects == []


def test_transfer_live_mode_flip_returns_410_without_cap_or_audit(monkeypatch):
    from zira_dashboard import (
        breakdown_actions,
        inbox_log,
        machine_breakdown,
        staffing_transfer,
        wc_attributions,
    )

    monkeypatch.setattr(breakdown_actions, "live_transfer_is_disabled", lambda: False)
    monkeypatch.setattr(
        machine_breakdown,
        "get_incident",
        lambda _incident_id: {
            "id": 1,
            "wc_name": "Dismantler 2",
            "day": date(2026, 7, 8),
            "detected_stop_utc": _STOP,
            "resolved_at": None,
        },
    )
    monkeypatch.setattr(
        wc_attributions,
        "breakdown_operator_rows_for_incident",
        lambda _incident_id: [
            {"person_name": "Alex", "employee_odoo_id": 101}
        ],
    )
    monkeypatch.setattr(
        wc_attributions,
        "open_breakdown_row",
        lambda *_args, **_kwargs: {"id": 10, "start_utc": _STOP},
    )
    effects = []
    monkeypatch.setattr(
        wc_attributions, "cap_breakdown", lambda *_args: effects.append("cap")
    )
    monkeypatch.setattr(
        staffing_transfer,
        "decide_and_apply",
        lambda *_args, **_kwargs: {
            "transfer": "blocked_live",
            "person": "Alex",
        },
    )
    monkeypatch.setattr(
        inbox_log,
        "log_event_safe",
        lambda **_kwargs: effects.append("audit") or 42,
    )
    monkeypatch.setattr(
        machine_breakdown,
        "transfer_open_incident",
        lambda *_args: {"status": "blocked_live"},
    )

    response = exceptions_route._breakdown_transfer_sync(
        {
            "incident_id": 1,
            "person_name": "Alex",
            "employee_odoo_id": 101,
            "to_wc": "Repair 3",
        }
    )

    assert response.status_code == 410
    assert effects == []


@pytest.mark.parametrize(
    ("person_name", "employee_odoo_id"),
    [("Mallory", 101), ("Alex", 202)],
)
def test_transfer_rejects_client_identity_mismatch_before_side_effects(
    monkeypatch, person_name, employee_odoo_id
):
    from zira_dashboard import machine_breakdown, staffing_transfer, wc_attributions

    monkeypatch.setattr(
        machine_breakdown,
        "get_incident",
        lambda _incident_id: {
            "id": 1,
            "wc_name": "Dismantler 2",
            "day": date(2026, 7, 8),
            "detected_stop_utc": _STOP,
        },
    )
    monkeypatch.setattr(
        wc_attributions,
        "breakdown_operator_rows_for_incident",
        lambda _incident_id: [
            {"person_name": "Alex", "employee_odoo_id": 101}
        ],
        raising=False,
    )
    effects = []
    monkeypatch.setattr(
        wc_attributions,
        "cap_breakdown",
        lambda *_args: effects.append("cap"),
    )
    monkeypatch.setattr(
        staffing_transfer,
        "decide_and_apply",
        lambda *_args, **_kwargs: effects.append("transfer"),
    )
    monkeypatch.setattr(
        machine_breakdown,
        "transfer_open_incident",
        lambda *_args: (_ for _ in ()).throw(
            ValueError("worker identity does not match this breakdown")
        ),
    )

    response = exceptions_route._breakdown_transfer_sync(
        {
            "incident_id": 1,
            "person_name": person_name,
            "employee_odoo_id": employee_odoo_id,
            "to_wc": "Repair 3",
        }
    )

    assert response.status_code == 409
    assert effects == []


def test_transfer_resolves_missing_client_id_from_durable_incident_row(monkeypatch):
    from zira_dashboard import inbox_log, machine_breakdown, staffing_transfer, wc_attributions

    monkeypatch.setattr(
        machine_breakdown,
        "get_incident",
        lambda _incident_id: {
            "id": 1,
            "wc_name": "Dismantler 2",
            "day": date(2026, 7, 8),
            "detected_stop_utc": _STOP,
        },
    )
    monkeypatch.setattr(
        wc_attributions,
        "breakdown_operator_rows_for_incident",
        lambda _incident_id: [
            {"person_name": "Alex", "employee_odoo_id": 101}
        ],
        raising=False,
    )
    lookups = []
    monkeypatch.setattr(
        wc_attributions,
        "open_breakdown_row",
        lambda *_args, **kwargs: lookups.append(kwargs)
        or {"id": 10, "start_utc": _STOP},
    )
    monkeypatch.setattr(wc_attributions, "cap_breakdown", lambda *_args: None)
    transfers = []
    monkeypatch.setattr(
        staffing_transfer,
        "decide_and_apply",
        lambda *args, **kwargs: transfers.append((args, kwargs))
        or {"closed_id": 5, "new_id": 6, "transfer": "moved"},
    )
    monkeypatch.setattr(inbox_log, "log_event_safe", lambda **_kwargs: 42)
    transfers = []
    monkeypatch.setattr(
        machine_breakdown,
        "transfer_open_incident",
        lambda *args: transfers.append(args)
        or _successful_transfer_outcome("Alex", 101),
    )

    response = exceptions_route._breakdown_transfer_sync(
        {"incident_id": 1, "person_name": "Alex", "to_wc": "Repair 3"}
    )

    assert response.status_code == 200
    assert lookups == []
    assert transfers == [(1, "Alex", None, "Repair 3")]


def test_transfer_validates_legacy_incident_row_against_canonical_operator(monkeypatch):
    from zira_dashboard import inbox_log, machine_breakdown, staffing_transfer, wc_attributions

    incident = {
        "id": 1,
        "wc_name": "Dismantler 2",
        "day": date(2026, 7, 8),
        "detected_stop_utc": _STOP,
    }
    monkeypatch.setattr(machine_breakdown, "get_incident", lambda _id: incident)
    monkeypatch.setattr(
        wc_attributions,
        "breakdown_operator_rows_for_incident",
        lambda _id: [{"person_name": "Alex", "employee_odoo_id": None}],
    )
    source = machine_breakdown.OperatorSourceSnapshot(
        presences=(
            machine_breakdown.OperatorPresence(
                "Alex", "Dismantler 2", _STOP, 101
            ),
        ),
        departures=(),
        available=True,
        mirror_owned=True,
        complete=True,
    )
    monkeypatch.setattr(
        machine_breakdown, "_operator_source_snapshot", lambda *_args: source
    )
    monkeypatch.setattr(
        wc_attributions,
        "open_breakdown_row",
        lambda *_args, **_kwargs: {"id": 10, "start_utc": _STOP},
    )
    monkeypatch.setattr(wc_attributions, "cap_breakdown", lambda *_args: None)
    calls = []
    monkeypatch.setattr(
        staffing_transfer,
        "decide_and_apply",
        lambda *_args, **kwargs: calls.append(kwargs)
        or {"closed_id": 5, "new_id": 6, "transfer": "moved"},
    )
    monkeypatch.setattr(inbox_log, "log_event_safe", lambda **_kwargs: 42)
    lifecycle_calls = []
    monkeypatch.setattr(
        machine_breakdown,
        "transfer_open_incident",
        lambda *args: lifecycle_calls.append(args)
        or _successful_transfer_outcome("Alex", 101),
    )

    response = exceptions_route._breakdown_transfer_sync({
        "incident_id": 1,
        "person_name": "Alex",
        "employee_odoo_id": 101,
        "to_wc": "Repair 3",
    })

    assert response.status_code == 200
    assert calls == []
    assert lifecycle_calls == [(1, "Alex", 101, "Repair 3")]


def test_snooze_sync_calls_snooze_operator(monkeypatch):
    from zira_dashboard import machine_breakdown
    called = []
    monkeypatch.setattr(machine_breakdown, "snooze_operator",
                        lambda iid, person, employee_odoo_id=None: called.append(
                            (iid, person, employee_odoo_id)
                        ))
    resp = exceptions_route._breakdown_snooze_sync({
        "incident_id": 1,
        "person_name": "Juan",
        "employee_odoo_id": 101,
    })
    assert resp.status_code == 200
    assert called == [(1, "Juan", 101)]


def test_snooze_sync_threads_employee_identity(monkeypatch):
    from zira_dashboard import machine_breakdown

    called = []
    monkeypatch.setattr(
        machine_breakdown,
        "snooze_operator",
        lambda incident_id, person_name, employee_odoo_id=None: called.append(
            (incident_id, person_name, employee_odoo_id)
        ),
    )

    response = exceptions_route._breakdown_snooze_sync(
        {"incident_id": 1, "person_name": "Alex", "employee_odoo_id": 202}
    )

    assert response.status_code == 200
    assert called == [(1, "Alex", 202)]


def test_dismiss_sync_logs_locked_snapshot_including_racing_detector_row(monkeypatch):
    from zira_dashboard import machine_breakdown, wc_attributions, inbox_log
    monkeypatch.setattr(machine_breakdown, "get_incident", lambda iid: {
        "id": 1, "wc_name": "Dismantler 2", "day": "2026-07-08", "detected_stop_utc": _STOP,
    })
    snapshot_rows = [
        {"id": 10, "day": "2026-07-08", "wc_name": "Dismantler 2",
         "person_name": "Alex", "employee_odoo_id": 101,
         "start_utc": "2026-07-08T18:02:00+00:00",
         "end_utc": None, "source": "breakdown", "breakdown_id": 1},
        {"id": 11, "day": "2026-07-08", "wc_name": "Dismantler 2",
         "person_name": "Alex", "employee_odoo_id": 202,
         "start_utc": "2026-07-08T18:12:00+00:00",
         "end_utc": None, "source": "breakdown", "breakdown_id": 1},
    ]
    monkeypatch.setattr(
        wc_attributions,
        "for_day",
        lambda day: pytest.fail(
            "dismiss must not snapshot before acquiring the incident lock"
        ),
    )
    dismissed = []
    monkeypatch.setattr(
        machine_breakdown,
        "dismiss_incident",
        lambda iid: dismissed.append(iid) or snapshot_rows,
        raising=False,
    )
    logged = []
    monkeypatch.setattr(inbox_log, "log_event_safe", lambda **kw: logged.append(kw) or 43)

    resp = exceptions_route._breakdown_dismiss_sync({"incident_id": 1}, "dale@gruberpallets.com", "Dale")

    assert resp.status_code == 200
    assert dismissed == [1]
    assert logged[0]["action"] == "dismiss"
    assert logged[0]["reversible"] is True
    assert logged[0]["detail"]["rows"] == snapshot_rows


def test_dismiss_sync_does_not_audit_when_another_terminal_action_wins(monkeypatch):
    from zira_dashboard import inbox_log, machine_breakdown

    monkeypatch.setattr(
        machine_breakdown,
        "get_incident",
        lambda _incident_id: {
            "id": 1,
            "wc_name": "Dismantler 2",
            "day": "2026-07-08",
            "detected_stop_utc": _STOP,
        },
    )
    monkeypatch.setattr(
        machine_breakdown, "dismiss_incident", lambda _incident_id: None
    )
    monkeypatch.setattr(
        inbox_log,
        "log_event_safe",
        lambda **_kwargs: pytest.fail("a lost dismiss must not be reversible"),
    )

    response = exceptions_route._breakdown_dismiss_sync({"incident_id": 1})

    assert response.status_code == 409


def test_report_sync_calls_report_manual(monkeypatch):
    from zira_dashboard import machine_breakdown
    monkeypatch.setattr(machine_breakdown, "report_manual",
                        lambda wc: {"ok": True, "incident_id": 9})
    resp = exceptions_route._breakdown_report_sync({"wc_name": "Dismantler 2"})
    assert resp.status_code == 200
