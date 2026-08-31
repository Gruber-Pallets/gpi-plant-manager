"""Endpoint tests for the attribute + testing + undo flow. wc_attributions,
staffing_transfer, and odoo_client are stubbed so no DB / Odoo is touched."""
from __future__ import annotations

from datetime import datetime

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


def test_assignments_todo_json_caches_and_busts(monkeypatch):
    """GET /api/assignments-todo serves from the 30s in-process cache on
    repeat calls; _bust_assignments_todo_cache forces a recompute."""
    from zira_dashboard import staffing as staffing_mod

    calls = {"n": 0}

    def fake_unattributed(day, zira_client):
        calls["n"] += 1
        return []

    monkeypatch.setattr(wc_attributions, "unattributed_for_day", fake_unattributed)
    monkeypatch.setattr(wc_attributions, "for_day", lambda day: [])
    monkeypatch.setattr(staffing_mod, "load_roster", lambda: [])

    staffing_routes._bust_assignments_todo_cache()  # isolate from other tests
    r1 = client.get("/api/assignments-todo")
    assert r1.status_code == 200 and calls["n"] == 1
    r2 = client.get("/api/assignments-todo")
    assert r2.status_code == 200 and calls["n"] == 1  # cache hit
    assert r2.json() == r1.json()
    staffing_routes._bust_assignments_todo_cache()
    r3 = client.get("/api/assignments-todo")
    assert r3.status_code == 200 and calls["n"] == 2  # bust -> recompute
    staffing_routes._bust_assignments_todo_cache()  # leave clean for other tests


def test_attribute_busts_assignments_todo_cache(monkeypatch):
    """Saving an attribution must drop the assignments-todo cache so the
    modal/badge reflects the write immediately, not after 30s."""
    monkeypatch.setattr(wc_attributions, "add", lambda *a, **k: 5)
    monkeypatch.setattr(staffing_transfer, "decide_and_apply",
                        lambda person, wc, ts: {"transfer": "none"})
    monkeypatch.setattr(staffing_routes, "invalidate_today_cache", lambda: None, raising=False)

    staffing_routes._ASSIGNMENTS_TODO_CACHE["value"] = {"count": 0}
    staffing_routes._ASSIGNMENTS_TODO_CACHE["expires_at"] = 9e18
    resp = client.post("/api/staffing/attribute", json={
        "day": "2026-06-02", "wc_name": "Junior #2", "person_name": "Lauro",
        "start_utc": "2026-06-02T13:00:00+00:00", "end_utc": "2026-06-02T16:00:00+00:00",
    })
    assert resp.status_code == 200
    assert staffing_routes._ASSIGNMENTS_TODO_CACHE["value"] is None


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


def test_active_live_real_person_attribute_is_gone_without_write_or_transfer(monkeypatch):
    monkeypatch.setattr(staffing_routes.attendance_location_policy, "live_is_active", lambda: True)
    monkeypatch.setattr(
        wc_attributions,
        "add",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("blocked real-person attribution must not save")
        ),
    )
    monkeypatch.setattr(
        staffing_transfer,
        "decide_and_apply",
        lambda *_args: (_ for _ in ()).throw(AssertionError("Odoo transfer forbidden")),
    )

    response = client.post(
        "/api/staffing/attribute",
        json={
            "day": "2026-06-02",
            "wc_name": "Junior #2",
            "person_name": "Lauro",
            "start_utc": "2026-06-02T13:00:00+00:00",
            "end_utc": "2026-06-02T16:00:00+00:00",
        },
    )

    assert response.status_code == 410
    assert "plant-floor app" in response.json()["error"]


def test_active_live_testing_annotation_still_saves_without_transfer(monkeypatch):
    monkeypatch.setattr(staffing_routes.attendance_location_policy, "live_is_active", lambda: True)
    added = []
    monkeypatch.setattr(
        wc_attributions,
        "add",
        lambda day, wc, person, start, end, source="manual": added.append(
            (person, source)
        )
        or 1,
    )
    monkeypatch.setattr(
        staffing_transfer,
        "decide_and_apply",
        lambda *_args: (_ for _ in ()).throw(AssertionError("Odoo transfer forbidden")),
    )
    monkeypatch.setattr(staffing_routes, "invalidate_today_cache", lambda: None)

    response = client.post(
        "/api/staffing/attribute-with-testing",
        json={
            "day": "2026-06-02",
            "wc_name": "Junior #2",
            "testing_start_utc": "2026-06-02T13:00:00+00:00",
            "testing_end_utc": "2026-06-02T14:00:00+00:00",
        },
    )

    assert response.status_code == 200
    assert added == [(wc_attributions.TESTING_PERSON, "testing")]


def test_active_live_transfer_chokepoint_never_calls_odoo(monkeypatch):
    monkeypatch.setattr(staffing_transfer, "_live_location_active", lambda: True)
    monkeypatch.setattr(
        staffing_transfer,
        "_employee_id_for",
        lambda _person: (_ for _ in ()).throw(AssertionError("must stop before lookup")),
    )

    result = staffing_transfer.decide_and_apply(
        "Lauro",
        "Junior #2",
        datetime.fromisoformat("2026-06-02T13:00:00+00:00"),
    )

    assert result == {
        "transfer": "blocked_live",
        "person": "Lauro",
        "error": "Use the plant-floor app to change live work areas.",
    }
