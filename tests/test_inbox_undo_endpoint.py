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
