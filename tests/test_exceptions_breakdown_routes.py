"""POST /api/exceptions/breakdown/{transfer,snooze,dismiss,report}."""
from datetime import datetime, timezone

from zira_dashboard.routes import exceptions as exceptions_route

_STOP = datetime(2026, 7, 8, 18, 2, tzinfo=timezone.utc)


def test_transfer_sync_caps_exclusion_and_calls_decide_and_apply(monkeypatch):
    from zira_dashboard import machine_breakdown, wc_attributions, staffing_transfer, inbox_log
    monkeypatch.setattr(machine_breakdown, "get_incident", lambda iid: {
        "id": 1, "wc_name": "Dismantler 2", "day": "2026-07-08", "detected_stop_utc": _STOP,
    })
    monkeypatch.setattr(wc_attributions, "open_breakdown_row",
                        lambda day, wc, person: {"id": 10})
    capped = []
    monkeypatch.setattr(wc_attributions, "cap_breakdown", lambda rid, end: capped.append((rid, end)))
    monkeypatch.setattr(staffing_transfer, "decide_and_apply",
                        lambda person, wc, ts: {"transfer": "moved", "person": person,
                                                "closed_id": 5, "new_id": 6, "to_dept": "Recycled"})
    logged = []
    monkeypatch.setattr(inbox_log, "log_event_safe", lambda **kw: logged.append(kw) or 42)

    resp = exceptions_route._breakdown_transfer_sync(
        {"incident_id": 1, "person_name": "Juan", "to_wc": "Repair 3"},
        actor_upn="dale@gruberpallets.com", actor_name="Dale",
    )

    assert resp.status_code == 200
    assert capped and capped[0][0] == 10
    assert logged[0]["item_kind"] == "breakdown"
    assert logged[0]["action"] == "transfer"
    assert logged[0]["reversible"] is True
    assert logged[0]["detail"]["closed_id"] == 5
    assert logged[0]["detail"]["new_id"] == 6
    assert logged[0]["detail"]["attribution_id"] == 10


def test_transfer_sync_500_with_friendly_error_when_decide_and_apply_raises(monkeypatch):
    from zira_dashboard import machine_breakdown, wc_attributions, staffing_transfer, inbox_log

    monkeypatch.setattr(machine_breakdown, "get_incident", lambda iid: {
        "id": 1, "wc_name": "Dismantler 2", "day": "2026-07-08", "detected_stop_utc": _STOP,
    })
    monkeypatch.setattr(wc_attributions, "open_breakdown_row",
                        lambda day, wc, person: {"id": 10})
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
                        lambda iid, person: called.append((iid, person)))
    resp = exceptions_route._breakdown_snooze_sync({"incident_id": 1, "person_name": "Juan"})
    assert resp.status_code == 200
    assert called == [(1, "Juan")]


def test_dismiss_sync_deletes_rows_and_resolves(monkeypatch):
    from zira_dashboard import machine_breakdown, wc_attributions, inbox_log
    monkeypatch.setattr(machine_breakdown, "get_incident", lambda iid: {
        "id": 1, "wc_name": "Dismantler 2", "day": "2026-07-08", "detected_stop_utc": _STOP,
    })
    snapshot_rows = [{"id": 10, "day": "2026-07-08", "wc_name": "Dismantler 2",
                      "person_name": "Juan", "start_utc": "2026-07-08T18:02:00+00:00",
                      "end_utc": None, "source": "breakdown"}]
    monkeypatch.setattr(wc_attributions, "for_day", lambda day: snapshot_rows)
    deleted = []
    monkeypatch.setattr(wc_attributions, "delete_breakdown_rows_for_incident",
                        lambda iid: deleted.append(iid))
    resolved = []
    monkeypatch.setattr(machine_breakdown, "resolve_incident",
                        lambda iid, resolution, resume_utc=None: resolved.append((iid, resolution)))
    logged = []
    monkeypatch.setattr(inbox_log, "log_event_safe", lambda **kw: logged.append(kw) or 43)

    resp = exceptions_route._breakdown_dismiss_sync({"incident_id": 1}, "dale@gruberpallets.com", "Dale")

    assert resp.status_code == 200
    assert deleted == [1]
    assert resolved == [(1, "dismissed")]
    assert logged[0]["action"] == "dismiss"
    assert logged[0]["reversible"] is True
    assert logged[0]["detail"]["rows"] == snapshot_rows


def test_report_sync_calls_report_manual(monkeypatch):
    from zira_dashboard import machine_breakdown
    monkeypatch.setattr(machine_breakdown, "report_manual",
                        lambda wc: {"ok": True, "incident_id": 9})
    resp = exceptions_route._breakdown_report_sync({"wc_name": "Dismantler 2"})
    assert resp.status_code == 200
