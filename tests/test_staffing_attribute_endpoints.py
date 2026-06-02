"""Endpoint tests for the attribute + testing + undo flow. wc_attributions,
staffing_transfer, and odoo_client are stubbed so no DB / Odoo is touched."""
from __future__ import annotations

from fastapi.testclient import TestClient

from zira_dashboard.app import app
from zira_dashboard import wc_attributions, staffing_transfer, odoo_client
from zira_dashboard.routes import staffing as staffing_routes

client = TestClient(app)


def test_attribute_returns_transfer_result(monkeypatch):
    monkeypatch.setattr(wc_attributions, "add", lambda *a, **k: 123)
    monkeypatch.setattr(staffing_transfer, "decide_and_apply",
                        lambda person, wc, ts: {"transfer": "moved", "person": person,
                                                "closed_id": 1, "new_id": 2,
                                                "from_dept": "01 Recycled", "to_dept": "New"})
    monkeypatch.setattr(staffing_routes, "invalidate_today_cache", lambda: None, raising=False)

    resp = client.post("/api/staffing/attribute", json={
        "day": "2026-06-02", "wc_name": "Junior #2", "person_name": "Lauro",
        "start_utc": "2026-06-02T13:00:00+00:00", "end_utc": "2026-06-02T16:00:00+00:00",
    })
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True and body["id"] == 123
    assert body["transfer"]["transfer"] == "moved"


def test_attribute_with_testing_writes_two_rows_and_transfers(monkeypatch):
    added = []
    monkeypatch.setattr(wc_attributions, "add",
                        lambda day, wc, person, s, e, source="manual": added.append(
                            (wc, person, source)) or len(added))
    captured = {}
    monkeypatch.setattr(staffing_transfer, "decide_and_apply",
                        lambda person, wc, ts: captured.update(person=person, ts=ts)
                        or {"transfer": "moved", "person": person, "closed_id": 1,
                            "new_id": 2, "to_dept": "New"})
    monkeypatch.setattr(staffing_routes, "invalidate_today_cache", lambda: None, raising=False)

    resp = client.post("/api/staffing/attribute-with-testing", json={
        "day": "2026-06-02", "wc_name": "Junior #2",
        "testing_start_utc": "2026-06-02T13:00:00+00:00",
        "testing_end_utc": "2026-06-02T14:00:00+00:00",
        "sensed_end_utc": "2026-06-02T16:00:00+00:00",
        "remainder_person": "Lauro",
    })
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert ("Junior #2", wc_attributions.TESTING_PERSON, "testing") in added
    assert ("Junior #2", "Lauro", "manual") in added
    from datetime import datetime, timezone
    assert captured["ts"] == datetime(2026, 6, 2, 14, 0, tzinfo=timezone.utc)
    assert body["transfer"]["transfer"] == "moved"


def test_attribute_with_testing_testing_only(monkeypatch):
    added = []
    monkeypatch.setattr(wc_attributions, "add",
                        lambda day, wc, person, s, e, source="manual": added.append(
                            (wc, person, source)) or len(added))
    called = {"n": 0}
    monkeypatch.setattr(staffing_transfer, "decide_and_apply",
                        lambda *a, **k: called.__setitem__("n", called["n"] + 1) or {})
    monkeypatch.setattr(staffing_routes, "invalidate_today_cache", lambda: None, raising=False)

    resp = client.post("/api/staffing/attribute-with-testing", json={
        "day": "2026-06-02", "wc_name": "Junior #2",
        "testing_start_utc": "2026-06-02T13:00:00+00:00",
        "testing_end_utc": "2026-06-02T16:00:00+00:00",
    })
    assert resp.status_code == 200
    assert added == [("Junior #2", wc_attributions.TESTING_PERSON, "testing")]
    assert called["n"] == 0
    assert resp.json()["transfer"] == {"transfer": "none"}


def test_attribute_with_testing_rejects_bad_window(monkeypatch):
    monkeypatch.setattr(wc_attributions, "add", lambda *a, **k: 1)
    resp = client.post("/api/staffing/attribute-with-testing", json={
        "day": "2026-06-02", "wc_name": "Junior #2",
        "testing_start_utc": "2026-06-02T15:00:00+00:00",
        "testing_end_utc": "2026-06-02T14:00:00+00:00",
    })
    assert resp.status_code == 400


def test_transfer_undo_calls_odoo(monkeypatch):
    captured = {}
    monkeypatch.setattr(odoo_client, "undo_transfer",
                        lambda closed_id, new_id: captured.update(closed_id=closed_id, new_id=new_id))
    monkeypatch.setattr(staffing_routes, "invalidate_today_cache", lambda: None, raising=False)

    resp = client.post("/api/staffing/transfer/undo", json={"closed_id": 1, "new_id": 2})
    assert resp.status_code == 200 and resp.json()["ok"] is True
    assert captured == {"closed_id": 1, "new_id": 2}


def test_transfer_undo_null_closed_id(monkeypatch):
    captured = {}
    monkeypatch.setattr(odoo_client, "undo_transfer",
                        lambda closed_id, new_id: captured.update(closed_id=closed_id, new_id=new_id))
    monkeypatch.setattr(staffing_routes, "invalidate_today_cache", lambda: None, raising=False)
    resp = client.post("/api/staffing/transfer/undo", json={"closed_id": None, "new_id": 2})
    assert resp.status_code == 200
    assert captured == {"closed_id": None, "new_id": 2}


def test_transfer_undo_rejects_non_integer_closed_id(monkeypatch):
    monkeypatch.setattr(odoo_client, "undo_transfer", lambda closed_id, new_id: None)
    resp = client.post("/api/staffing/transfer/undo", json={"new_id": 2, "closed_id": "nope"})
    assert resp.status_code == 400


def test_attribute_with_testing_skips_zero_length_remainder(monkeypatch):
    added = []
    monkeypatch.setattr(wc_attributions, "add",
                        lambda day, wc, person, s, e, source="manual": added.append(
                            (wc, person, source)) or len(added))
    called = {"n": 0}
    monkeypatch.setattr(staffing_transfer, "decide_and_apply",
                        lambda *a, **k: called.__setitem__("n", called["n"] + 1) or {})
    monkeypatch.setattr(staffing_routes, "invalidate_today_cache", lambda: None, raising=False)
    # remainder person named, but sensed_end == testing_end -> no time left
    resp = client.post("/api/staffing/attribute-with-testing", json={
        "day": "2026-06-02", "wc_name": "Junior #2",
        "testing_start_utc": "2026-06-02T13:00:00+00:00",
        "testing_end_utc": "2026-06-02T16:00:00+00:00",
        "sensed_end_utc": "2026-06-02T16:00:00+00:00",
        "remainder_person": "Lauro",
    })
    assert resp.status_code == 200
    assert added == [("Junior #2", wc_attributions.TESTING_PERSON, "testing")]
    assert called["n"] == 0
    assert resp.json()["transfer"] == {"transfer": "none"}


def test_attribute_transfer_error_does_not_fail_credit(monkeypatch):
    monkeypatch.setattr(wc_attributions, "add", lambda *a, **k: 77)
    def boom(person, wc, ts):
        raise RuntimeError("odoo down")
    monkeypatch.setattr(staffing_transfer, "decide_and_apply", boom)
    monkeypatch.setattr(staffing_routes, "invalidate_today_cache", lambda: None, raising=False)
    resp = client.post("/api/staffing/attribute", json={
        "day": "2026-06-02", "wc_name": "Junior #2", "person_name": "Lauro",
        "start_utc": "2026-06-02T13:00:00+00:00", "end_utc": "2026-06-02T16:00:00+00:00",
    })
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True and body["id"] == 77
    assert body["transfer"]["transfer"] == "error"
