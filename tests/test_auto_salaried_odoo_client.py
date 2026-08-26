"""Virtual Sustaining department + dept patch + unlink helpers.

Monkeypatch odoo_client.execute (the universal XML-RPC entry point) — the
same fake-Odoo seam every other odoo_client test uses.
"""
from zira_dashboard import odoo_client


def _fresh_cache(monkeypatch):
    monkeypatch.setattr(odoo_client, "_wc_dept_id_cache", {})


def test_department_id_for_virtual_sustaining_wc(monkeypatch):
    _fresh_cache(monkeypatch)
    calls = []

    def fake_execute(model, method, *args, **kwargs):
        calls.append((model, method, args, kwargs))
        return [{"id": 77}]

    monkeypatch.setattr(odoo_client, "execute", fake_execute)
    assert odoo_client._department_id_for_wc("Sustaining") == 77
    model, method, args, kwargs = calls[0]
    assert model == "hr.department"
    assert args[0] == [("name", "ilike", "Sustaining")]


def test_department_id_unknown_wc_still_none(monkeypatch):
    _fresh_cache(monkeypatch)
    monkeypatch.setattr(
        odoo_client, "execute",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no Odoo call expected")),
    )
    assert odoo_client._department_id_for_wc("Not A Real WC") is None


def test_set_attendance_department(monkeypatch):
    calls = []
    monkeypatch.setenv("ODOO_KIOSK_DEPARTMENT_FIELD", "x_kiosk_department_id")
    monkeypatch.setattr(
        odoo_client, "execute",
        lambda model, method, ids, payload: calls.append((model, method, ids, payload)) or True,
    )
    assert odoo_client.set_attendance_department(123, 77) is True
    assert calls == [("hr.attendance", "write", [123], {"x_kiosk_department_id": 77})]


def test_set_attendance_department_field_unset(monkeypatch):
    monkeypatch.delenv("ODOO_KIOSK_DEPARTMENT_FIELD", raising=False)
    monkeypatch.setattr(
        odoo_client, "execute",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no Odoo call expected")),
    )
    assert odoo_client.set_attendance_department(123, 77) is False


def test_delete_attendances(monkeypatch):
    calls = []
    monkeypatch.setattr(
        odoo_client, "execute",
        lambda model, method, ids: calls.append((model, method, ids)) or True,
    )
    odoo_client.delete_attendances([5, 9])
    odoo_client.delete_attendances([])  # no-op, no extra call
    assert calls == [("hr.attendance", "unlink", [5, 9])]
