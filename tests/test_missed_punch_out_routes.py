"""Missed-punch-out routes: GET shape, correct (mocked Odoo) + validation."""

import json
import os
import xmlrpc.client
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from zira_dashboard.app import app
from zira_dashboard import db, inbox_log, missed_punch_out as mpo, odoo_client
from zira_dashboard.routes import missed_punch_out as missed_punch_route
from zira_dashboard.shift_config import SITE_TZ

requires_db = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="needs Postgres",
)

client = TestClient(app)
ATT = 999600


def _unresolved_row():
    return {
        "attendance_id": 3326,
        "employee_odoo_id": 6,
        "name": "Jane Doe",
        "check_in": datetime(2026, 7, 13, 12, 0, tzinfo=SITE_TZ),
        "auto_closed_at": datetime(2026, 7, 14, 0, 0, tzinfo=SITE_TZ),
    }


def _body(response):
    return json.loads(response.body)


def _raise_deleted_fault():
    raise xmlrpc.client.Fault(1, "The record does not exist or has been deleted.")


def _patch_audit_log(monkeypatch):
    monkeypatch.setattr(inbox_log, "log_event_safe", lambda **_kwargs: 1)


def _patch_deleted_write(monkeypatch, row):
    monkeypatch.setattr(mpo, "get_unresolved", lambda _id: row)
    monkeypatch.setattr(odoo_client, "clock_out", lambda *_args, **_kwargs: _raise_deleted_fault())
    _patch_audit_log(monkeypatch)


@pytest.fixture(autouse=True)
def _seed():
    if not os.environ.get("DATABASE_URL"):
        yield
        return
    db.bootstrap_schema()
    db.execute("DELETE FROM missed_punch_out WHERE attendance_id = %s", (ATT,))
    # check-in 13:00 local on 6/8; auto-closed at midnight 6/9.
    ci = datetime(2026, 6, 8, 18, 0, tzinfo=timezone.utc)
    midnight = datetime(2026, 6, 9, 0, 0, tzinfo=SITE_TZ)
    mpo.record_close(ATT, 42, ci.isoformat(), midnight)
    yield
    db.execute("DELETE FROM missed_punch_out WHERE attendance_id = %s", (ATT,))


@requires_db
def test_get_returns_count_and_rows():
    r = client.get("/api/missed-punch-out")
    assert r.status_code == 200
    body = r.json()
    assert set(["count", "rows"]) <= set(body.keys())
    row = next(x for x in body["rows"] if x["attendance_id"] == ATT)
    assert row["check_in_label"] == "1:00 PM Mon Jun 8"
    assert row["check_in_date"] == "2026-06-08"


@requires_db
def test_correct_rewrites_check_out_and_resolves(monkeypatch):
    calls = {}
    monkeypatch.setattr(odoo_client, "clock_out",
                        lambda att, ts, **kw: calls.update(att=att, ts=ts, kw=kw))
    r = client.post("/missed-punch-out/correct",
                    json={"attendance_id": ATT, "time": "16:30"})
    assert r.status_code == 200 and r.json()["ok"] is True
    assert calls["att"] == ATT
    assert calls["ts"] == datetime(2026, 6, 8, 16, 30, tzinfo=SITE_TZ)
    assert calls["kw"] == {"mode": "manual"}
    assert mpo.get_unresolved(ATT) is None


@requires_db
def test_correct_rejects_time_before_check_in(monkeypatch):
    monkeypatch.setattr(odoo_client, "clock_out",
                        lambda att, ts, **kw: (_ for _ in ()).throw(AssertionError("no write")))
    r = client.post("/missed-punch-out/correct",
                    json={"attendance_id": ATT, "time": "06:00"})  # before 13:00 check-in
    assert r.status_code == 400
    assert mpo.get_unresolved(ATT) is not None  # still flagged


@requires_db
def test_correct_rejects_bad_time(monkeypatch):
    r = client.post("/missed-punch-out/correct",
                    json={"attendance_id": ATT, "time": "nope"})
    assert r.status_code == 400


@requires_db
def test_correct_unknown_id_404():
    r = client.post("/missed-punch-out/correct",
                    json={"attendance_id": 123123, "time": "16:30"})
    assert r.status_code == 404


def test_deleted_odoo_record_settles_when_current_checkout_changed(monkeypatch):
    row = _unresolved_row()
    _patch_deleted_write(monkeypatch, row)
    monkeypatch.setattr(odoo_client, "fetch_employee_attendances_for_day", lambda *_: [{
        "id": 4001, "check_in": row["check_in"].isoformat(),
        "check_out": "2026-07-13T18:00:00+00:00",
    }])
    corrected = []
    monkeypatch.setattr(mpo, "correct", lambda aid, ts: corrected.append((aid, ts)))

    response = missed_punch_route._correct_sync({"attendance_id": 3326, "time": "13:00"})

    assert response.status_code == 200
    assert _body(response)["message"] == "Odoo already resolved this conflict."
    assert corrected[0][0] == 3326


def test_deleted_odoo_documents_fault_reconciles_already_resolved_attendance(monkeypatch):
    row = _unresolved_row()
    monkeypatch.setattr(mpo, "get_unresolved", lambda _id: row)
    monkeypatch.setattr(
        odoo_client,
        "clock_out",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            xmlrpc.client.Fault(
                1,
                "One of the documents you are trying to access has been deleted, "
                "please try again after refreshing.",
            )
        ),
    )
    fresh_reads = []
    monkeypatch.setattr(
        odoo_client,
        "fetch_employee_attendances_for_day",
        lambda employee_id, day: fresh_reads.append((employee_id, day)) or [{
            "id": 4001,
            "check_in": row["check_in"].isoformat(),
            "check_out": "2026-07-13T18:00:00+00:00",
        }],
    )
    corrected = []
    monkeypatch.setattr(mpo, "correct", lambda aid, ts: corrected.append((aid, ts)))
    _patch_audit_log(monkeypatch)

    response = missed_punch_route._correct_sync({"attendance_id": 3326, "time": "13:00"})

    assert response.status_code == 200
    assert _body(response)["message"] == "Odoo already resolved this conflict."
    assert fresh_reads == [(6, row["check_in"].astimezone(SITE_TZ).date())]
    assert corrected[0][0] == 3326


def test_deleted_odoo_record_corrects_one_open_current_record(monkeypatch):
    row = _unresolved_row()
    calls = []
    monkeypatch.setattr(mpo, "get_unresolved", lambda _id: row)
    monkeypatch.setattr(
        odoo_client,
        "clock_out",
        lambda aid, ts, **kw: calls.append((aid, ts, kw)) if aid == 4001 else _raise_deleted_fault(),
    )
    monkeypatch.setattr(odoo_client, "fetch_employee_attendances_for_day", lambda *_: [{
        "id": 4001, "check_in": row["check_in"].isoformat(), "check_out": None,
    }])
    monkeypatch.setattr(mpo, "correct", lambda *args: None)
    _patch_audit_log(monkeypatch)

    response = missed_punch_route._correct_sync({"attendance_id": 3326, "time": "13:00"})

    assert response.status_code == 200
    assert calls[0][0] == 4001
    assert _body(response)["message"] == "Updated the current Odoo attendance."


def test_original_automatic_midnight_record_is_not_auto_dismissed(monkeypatch):
    row = _unresolved_row()
    _patch_deleted_write(monkeypatch, row)
    monkeypatch.setattr(odoo_client, "fetch_employee_attendances_for_day", lambda *_: [{
        "id": 4001, "check_in": row["check_in"].isoformat(),
        "check_out": row["auto_closed_at"].astimezone(timezone.utc).isoformat(),
    }])

    response = missed_punch_route._correct_sync({"attendance_id": 3326, "time": "13:00"})

    assert response.status_code == 409
    assert "Verify" in _body(response)["error"]


def test_deleted_odoo_record_with_no_current_attendance_stays_unresolved(monkeypatch):
    row = _unresolved_row()
    _patch_deleted_write(monkeypatch, row)
    corrected = []
    monkeypatch.setattr(mpo, "correct", lambda *args: corrected.append(args))
    monkeypatch.setattr(odoo_client, "fetch_employee_attendances_for_day", lambda *_: [])

    response = missed_punch_route._correct_sync({"attendance_id": 3326, "time": "13:00"})

    assert response.status_code == 409
    assert "no attendance" in _body(response)["error"]
    assert corrected == []


def test_deleted_odoo_record_with_multiple_open_current_attendances_stays_unresolved(monkeypatch):
    row = _unresolved_row()
    _patch_deleted_write(monkeypatch, row)
    corrected = []
    monkeypatch.setattr(mpo, "correct", lambda *args: corrected.append(args))
    monkeypatch.setattr(odoo_client, "fetch_employee_attendances_for_day", lambda *_: [
        {"id": 4001, "check_in": row["check_in"].isoformat(), "check_out": None},
        {"id": 4002, "check_in": row["check_in"].isoformat(), "check_out": None},
    ])

    response = missed_punch_route._correct_sync({"attendance_id": 3326, "time": "13:00"})

    assert response.status_code == 409
    assert "multiple current attendances" in _body(response)["error"]
    assert corrected == []


def test_deleted_odoo_record_when_refresh_fails_stays_unresolved(monkeypatch):
    row = _unresolved_row()
    _patch_deleted_write(monkeypatch, row)
    corrected = []
    monkeypatch.setattr(mpo, "correct", lambda *args: corrected.append(args))
    monkeypatch.setattr(
        odoo_client,
        "fetch_employee_attendances_for_day",
        lambda *_: (_ for _ in ()).throw(RuntimeError("Odoo unavailable")),
    )

    response = missed_punch_route._correct_sync({"attendance_id": 3326, "time": "13:00"})

    assert response.status_code == 409
    assert _body(response)["error"] == (
        "Unable to refresh this attendance from Odoo. Verify it in Odoo and try again."
    )
    assert corrected == []


def test_non_deleted_odoo_fault_returns_friendly_500_without_reconciliation(monkeypatch):
    row = _unresolved_row()
    monkeypatch.setattr(mpo, "get_unresolved", lambda _id: row)
    monkeypatch.setattr(
        odoo_client,
        "clock_out",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(xmlrpc.client.Fault(1, "Access denied")),
    )
    monkeypatch.setattr(
        odoo_client,
        "fetch_employee_attendances_for_day",
        lambda *_: (_ for _ in ()).throw(AssertionError("must not refresh")),
    )

    response = missed_punch_route._correct_sync({"attendance_id": 3326, "time": "13:00"})

    assert response.status_code == 500
    assert "Access denied" not in _body(response)["error"]
