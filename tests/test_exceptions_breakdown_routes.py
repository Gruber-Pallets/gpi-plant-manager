"""POST /api/exceptions/breakdown/{transfer,snooze,dismiss,report}."""
from datetime import datetime, timezone

import pytest

from zira_dashboard.routes import exceptions as exceptions_route

_STOP = datetime(2026, 7, 8, 18, 2, tzinfo=timezone.utc)


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
    monkeypatch.setattr(wc_attributions, "open_breakdown_row",
                        lambda day, wc, person, **_kwargs: {"id": 10})
    capped = []
    monkeypatch.setattr(wc_attributions, "cap_breakdown", lambda rid, end: capped.append((rid, end)))
    applied = {}
    def _decide_and_apply(person, wc, ts):
        applied.update(person=person, wc=wc, ts=ts)
        return {"transfer": "moved", "person": person,
                "closed_id": 5, "new_id": 6, "to_dept": "Recycled"}
    monkeypatch.setattr(staffing_transfer, "decide_and_apply", _decide_and_apply)
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
    assert capped == [(10, _STOP)]
    assert applied == {"person": "Juan", "wc": "Repair 3", "ts": _STOP}
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
        lambda *_args: {"closed_id": 5, "new_id": 6, "transfer": "moved"},
    )
    monkeypatch.setattr(inbox_log, "log_event_safe", lambda **_kwargs: 42)

    response = exceptions_route._breakdown_transfer_sync(
        {"incident_id": 1, "person_name": "Juan", "to_wc": "Repair 3"}
    )

    assert response.status_code == 200
    assert capped == [(10, personal_start)]


def test_transfer_sync_500_with_friendly_error_when_decide_and_apply_raises(monkeypatch):
    from zira_dashboard import machine_breakdown, wc_attributions, staffing_transfer, inbox_log

    monkeypatch.setattr(machine_breakdown, "get_incident", lambda iid: {
        "id": 1, "wc_name": "Dismantler 2", "day": "2026-07-08", "detected_stop_utc": _STOP,
    })
    monkeypatch.setattr(wc_attributions, "open_breakdown_row",
                        lambda day, wc, person, **_kwargs: {"id": 10})
    monkeypatch.setattr(wc_attributions, "cap_breakdown", lambda rid, end: None)

    def _raise(person, wc, ts):
        raise RuntimeError("xmlrpc boom")

    monkeypatch.setattr(staffing_transfer, "decide_and_apply", _raise)
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
    monkeypatch.setattr(machine_breakdown, "get_incident", lambda iid: None)
    resp = exceptions_route._breakdown_transfer_sync(
        {"incident_id": 1, "person_name": "Juan", "to_wc": "Repair 3"}, None, None)
    assert resp.status_code == 404


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


def test_dismiss_sync_deletes_rows_and_resolves(monkeypatch):
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
    unrelated_row = {
        **snapshot_rows[0],
        "id": 99,
        "breakdown_id": 999,
    }
    monkeypatch.setattr(
        wc_attributions, "for_day", lambda day: [*snapshot_rows, unrelated_row]
    )
    dismissed = []
    monkeypatch.setattr(wc_attributions, "delete_breakdown_rows_for_incident",
                        lambda iid: pytest.fail("dismiss must delete inside its incident transaction"))
    monkeypatch.setattr(machine_breakdown, "resolve_incident",
                        lambda iid, resolution, resume_utc=None: pytest.fail(
                            "dismiss must resolve inside its incident transaction"
                        ))
    monkeypatch.setattr(
        machine_breakdown,
        "dismiss_incident",
        lambda iid: dismissed.append(iid),
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


def test_report_sync_calls_report_manual(monkeypatch):
    from zira_dashboard import machine_breakdown
    monkeypatch.setattr(machine_breakdown, "report_manual",
                        lambda wc: {"ok": True, "incident_id": 9})
    resp = exceptions_route._breakdown_report_sync({"wc_name": "Dismantler 2"})
    assert resp.status_code == 200
