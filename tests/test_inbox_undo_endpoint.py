"""POST /api/exceptions/undo/{event_id}: reverse the four undoable actions."""
from zira_dashboard import inbox_log
from zira_dashboard.routes import exceptions as exceptions_route


def _ev(**over):
    base = {
        "id": 7, "item_kind": "missing_wc", "item_key": "missing_wc:48213",
        "person_name": "Maria", "category_label": "Missing WC", "action": "dismiss",
        "outcome": "Dismissed", "before_value": None, "after_value": None,
        "reason": None, "actor_upn": "dale@gruberpallets.com", "actor_name": "Dale",
        "source": "inbox", "reversible": True, "undone_at": None,
        "undo_event_id": None, "resolved_at": exceptions_route.plant_day.now(),
    }
    base.update(over)
    return base


def test_undo_dismiss_unresolves(monkeypatch):
    from zira_dashboard import missing_wc
    calls = {}
    monkeypatch.setattr(inbox_log, "get_event", lambda eid: _ev(id=eid))
    monkeypatch.setattr(missing_wc, "unresolve", lambda att: calls.setdefault("unresolve", att))
    monkeypatch.setattr(inbox_log, "log_event_safe", lambda **kw: 99)
    monkeypatch.setattr(inbox_log, "mark_undone", lambda e, u: calls.setdefault("marked", (e, u)))
    monkeypatch.setattr(exceptions_route, "_refresh_time_off_surfaces", lambda: None)

    resp = exceptions_route._undo_sync(7, "maria@gruberpallets.com", "Maria Ruiz")
    assert resp.status_code == 200
    assert calls["unresolve"] == 48213
    assert calls["marked"] == (7, 99)


def test_undo_assign_clears_wc_then_unresolves(monkeypatch):
    from zira_dashboard import missing_wc, odoo_client
    calls = {}
    monkeypatch.setattr(inbox_log, "get_event", lambda eid: _ev(id=eid, action="assign", after_value="Saw 1"))
    monkeypatch.setattr(odoo_client, "clear_attendance_wc", lambda att: calls.setdefault("cleared", att))
    monkeypatch.setattr(missing_wc, "unresolve", lambda att: calls.setdefault("unresolve", att))
    monkeypatch.setattr(inbox_log, "log_event_safe", lambda **kw: 99)
    monkeypatch.setattr(inbox_log, "mark_undone", lambda e, u: None)
    monkeypatch.setattr(exceptions_route, "_refresh_time_off_surfaces", lambda: None)

    resp = exceptions_route._undo_sync(7, None, None)
    assert resp.status_code == 200
    assert calls["cleared"] == 48213
    assert calls["unresolve"] == 48213


def test_undo_late_reason_deletes_row(monkeypatch):
    from zira_dashboard import late_report
    calls = {}
    monkeypatch.setattr(inbox_log, "get_event",
                        lambda eid: _ev(id=eid, item_kind="late", item_key="late:42:2026-06-26", action="reason"))
    monkeypatch.setattr(late_report, "undo_late_arrival", lambda day, emp: calls.setdefault("undo", (day, emp)))
    monkeypatch.setattr(inbox_log, "log_event_safe", lambda **kw: 99)
    monkeypatch.setattr(inbox_log, "mark_undone", lambda e, u: None)
    monkeypatch.setattr(exceptions_route, "_refresh_time_off_surfaces", lambda: None)

    resp = exceptions_route._undo_sync(7, None, None)
    assert resp.status_code == 200
    assert calls["undo"] == ("2026-06-26", "42")


def test_undo_rejects_non_undoable(monkeypatch):
    monkeypatch.setattr(inbox_log, "get_event",
                        lambda eid: _ev(id=eid, item_kind="time_off", item_key="time_off:55", action="approve"))
    resp = exceptions_route._undo_sync(7, None, None)
    assert resp.status_code == 400


def test_undo_rejects_already_undone(monkeypatch):
    monkeypatch.setattr(inbox_log, "get_event",
                        lambda eid: _ev(id=eid, undone_at=exceptions_route.plant_day.now()))
    resp = exceptions_route._undo_sync(7, None, None)
    assert resp.status_code == 409


def test_clear_attendance_wc_writes_false(monkeypatch):
    """The real write semantics: clearing must write the WC field to False
    (the bug this guards against was set_attendance_wc(.., None) no-opping)."""
    from zira_dashboard import odoo_client

    calls = {}
    monkeypatch.setattr(odoo_client, "_kiosk_wc_field", lambda: "x_wc")
    monkeypatch.setattr(odoo_client, "_kiosk_department_field", lambda: None)
    monkeypatch.setattr(odoo_client, "execute",
                        lambda *a, **k: calls.setdefault("args", a))
    odoo_client.clear_attendance_wc(48213)
    assert calls["args"] == ("hr.attendance", "write", [48213], {"x_wc": False})


def test_get_event_includes_detail(monkeypatch):
    """get_event's SELECT must include `detail` for breakdown undo to work."""
    from zira_dashboard import db, inbox_log
    captured = {}

    def fake_query(sql, params):
        captured["sql"] = sql
        return [_ev(id=7)]

    monkeypatch.setattr(db, "query", fake_query)
    inbox_log.get_event(7)
    assert "detail" in captured["sql"]


def test_undo_breakdown_transfer_reverses_and_reopens_exclusion(monkeypatch):
    from zira_dashboard import odoo_client, wc_attributions
    calls = {}
    monkeypatch.setattr(inbox_log, "get_event", lambda eid: _ev(
        id=eid, item_kind="breakdown", item_key="breakdown:Dismantler 2:x:Juan", action="transfer",
        detail={"closed_id": 5, "new_id": 6, "attribution_id": 10}))
    monkeypatch.setattr(odoo_client, "undo_transfer",
                        lambda closed_id, new_id: calls.setdefault("undo_transfer", (closed_id, new_id)))
    monkeypatch.setattr(wc_attributions, "reopen_breakdown",
                        lambda rid: calls.setdefault("reopen", rid))
    monkeypatch.setattr(inbox_log, "log_event_safe", lambda **kw: 99)
    monkeypatch.setattr(inbox_log, "mark_undone", lambda e, u: None)
    monkeypatch.setattr(exceptions_route, "_refresh_time_off_surfaces", lambda: None)

    resp = exceptions_route._undo_sync(7, None, None)

    assert resp.status_code == 200
    assert calls["undo_transfer"] == (5, 6)
    assert calls["reopen"] == 10


def test_undo_breakdown_dismiss_reopens_incident_and_recreates_rows(monkeypatch):
    from zira_dashboard import machine_breakdown, wc_attributions
    snapshot_rows = [{"day": "2026-07-08", "wc_name": "Dismantler 2", "person_name": "Juan",
                      "employee_odoo_id": 101,
                      "start_utc": "2026-07-08T18:02:00+00:00", "end_utc": None}]
    monkeypatch.setattr(inbox_log, "get_event", lambda eid: _ev(
        id=eid, item_kind="breakdown", item_key="breakdown:Dismantler 2:x", action="dismiss",
        detail={"rows": snapshot_rows, "incident_id": 1}))
    calls = {}
    monkeypatch.setattr(machine_breakdown, "reopen_incident",
                        lambda iid: calls.setdefault("reopen_incident", iid))
    restored = []
    monkeypatch.setattr(
        wc_attributions,
        "restore_breakdown_snapshot",
        lambda rows, incident_id: restored.append((rows, incident_id)),
        raising=False,
    )
    monkeypatch.setattr(
        wc_attributions,
        "add",
        lambda **_kw: (_ for _ in ()).throw(
            AssertionError("dismiss undo must use atomic exact snapshot restore")
        ),
    )
    monkeypatch.setattr(inbox_log, "log_event_safe", lambda **kw: 99)
    monkeypatch.setattr(inbox_log, "mark_undone", lambda e, u: None)
    monkeypatch.setattr(exceptions_route, "_refresh_time_off_surfaces", lambda: None)

    resp = exceptions_route._undo_sync(7, None, None)

    assert resp.status_code == 200
    assert calls["reopen_incident"] == 1
    assert restored == [(snapshot_rows, 1)]
