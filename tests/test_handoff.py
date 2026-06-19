import json
from datetime import date, datetime, timezone

from fastapi.testclient import TestClient

from zira_dashboard import db
from zira_dashboard.app import app
from zira_dashboard.routes import handoff


def _summary():
    return {
        "today": "2026-06-19",
        "generated_at": "8:30 AM",
        "total": 3,
        "urgent_total": 1,
        "source_errors": [],
        "sections": {
            "assignments": 1,
            "late": 1,
            "missing_wc": 0,
            "missed_punch_out": 0,
            "time_off": 1,
        },
    }


def test_handoff_page_renders_current_snapshot_and_recent(monkeypatch):
    monkeypatch.setattr(handoff.plant_day, "today", lambda: date(2026, 6, 19))
    monkeypatch.setattr(handoff.exception_inbox, "build_summary", _summary)
    monkeypatch.setattr(handoff, "_open_followups", lambda: [])
    monkeypatch.setattr(handoff, "_recent_handoffs", lambda: [{
        "id": 7,
        "handoff_date": date(2026, 6, 18),
        "shift_label": "Day",
        "created_by": "Dale",
        "notes": "Repair 2 needs follow-up",
        "open_total": 4,
        "urgent_total": 2,
        "follow_up_required": False,
        "is_open_followup": False,
        "has_source_errors": False,
        "source_error_label": "",
        "created_at_label": "6/18 2:10 PM",
    }])
    client = TestClient(app)

    resp = client.get("/handoff")

    assert resp.status_code == 200
    assert "Shift Handoff" in resp.text
    assert "8:30 AM · 3 open · 1 urgent" in resp.text
    assert "Repair 2 needs follow-up" in resp.text
    assert "/handoff/7" in resp.text
    assert "/static/handoff.css" in resp.text


def test_handoff_detail_renders_saved_snapshot(monkeypatch):
    monkeypatch.setattr(handoff.plant_day, "today", lambda: date(2026, 6, 19))
    monkeypatch.setattr(handoff, "_load_handoff", lambda handoff_id: {
        "id": handoff_id,
        "handoff_date": date(2026, 6, 18),
        "shift_label": "Day",
        "created_by": "Dale",
        "notes": "Repair 2 needs follow-up",
        "open_total": 2,
        "urgent_total": 1,
        "source_errors": [],
        "source_error_label": "",
        "follow_up_required": False,
        "is_open_followup": False,
        "created_at_label": "6/18 2:10 PM",
        "exception_snapshot": {
            "generated_at": "2:09 PM",
            "sections": [{
                "title": "Missing Work Center",
                "count": 1,
                "rows": [{
                    "name": "Ana",
                    "label": "No work center",
                    "detail": "Clocked in 7:05 AM",
                }],
            }],
        },
    })
    client = TestClient(app)

    resp = client.get("/handoff/7")

    assert resp.status_code == 200
    assert "Day Handoff" in resp.text
    assert "Repair 2 needs follow-up" in resp.text
    assert "Missing Work Center" in resp.text
    assert "Clocked in 7:05 AM" in resp.text


def test_load_handoff_normalizes_jsonb_strings(monkeypatch):
    def fake_query(sql, params):
        assert params == (7,)
        return [{
            "id": 7,
            "handoff_date": date(2026, 6, 18),
            "shift_label": "Day",
            "created_by": "Dale",
            "notes": "Done",
            "open_total": 1,
            "urgent_total": 0,
            "source_errors": '[{"source":"Late / Absence"}]',
            "exception_snapshot": '{"sections":[{"title":"Late / Absence","count":1,"rows":[]}]}',
            "follow_up_required": False,
            "resolved_at": None,
            "resolved_by": "",
            "resolution_note": "",
            "created_at": datetime(2026, 6, 18, 20, 10, tzinfo=timezone.utc),
            "updated_at": datetime(2026, 6, 18, 20, 10, tzinfo=timezone.utc),
        }]

    monkeypatch.setattr(db, "query", fake_query)

    row = handoff._load_handoff(7)

    assert row is not None
    assert row["source_errors"] == [{"source": "Late / Absence"}]
    assert row["source_error_label"] == "Late / Absence"
    assert row["exception_snapshot"]["sections"][0]["title"] == "Late / Absence"


def test_create_handoff_persists_exception_snapshot(monkeypatch):
    snapshot = {
        "today": "2026-06-19",
        "generated_at": "8:35 AM",
        "total": 5,
        "urgent_total": 2,
        "source_errors": [{"source": "Pending Time Off"}],
        "sections": [],
    }
    captured = {}
    monkeypatch.setattr(handoff.plant_day, "today", lambda: date(2026, 6, 19))
    monkeypatch.setattr(handoff.exception_inbox, "build_snapshot", lambda: snapshot)

    def fake_query(sql, params):
        captured["sql"] = sql
        captured["params"] = params
        return [{
            "id": 9,
            "handoff_date": date(2026, 6, 19),
            "shift_label": "Evening",
            "created_by": "Mia",
            "notes": "Watch tablets",
            "open_total": 5,
            "urgent_total": 2,
            "source_errors": [{"source": "Pending Time Off"}],
            "follow_up_required": True,
            "resolved_at": None,
            "resolved_by": "",
            "resolution_note": "",
            "created_at": datetime(2026, 6, 19, 14, 0, tzinfo=timezone.utc),
        }]

    monkeypatch.setattr(db, "query", fake_query)

    row = handoff._create_handoff(
        shift_label="Evening",
        created_by="Mia",
        notes="Watch tablets",
        follow_up_required=True,
    )

    assert row["id"] == 9
    assert "INSERT INTO plant_shift_handoffs" in captured["sql"]
    assert captured["params"][0] == date(2026, 6, 19)
    assert captured["params"][1] == "Evening"
    assert captured["params"][2] == "Mia"
    assert captured["params"][4] is True
    assert captured["params"][5] == 5
    assert captured["params"][6] == 2
    assert json.loads(captured["params"][7]) == [{"source": "Pending Time Off"}]
    assert json.loads(captured["params"][8])["total"] == 5


def test_handoff_form_redirects_after_save(monkeypatch):
    captured = {}

    def fake_create(**kwargs):
        captured.update(kwargs)
        return {"id": 12}

    monkeypatch.setattr(handoff, "_create_handoff", fake_create)
    client = TestClient(app)

    resp = client.post(
        "/handoff",
        data={
            "shift_label": "Day",
            "created_by": "Dale",
            "notes": "All set",
            "follow_up_required": "true",
        },
        follow_redirects=False,
    )

    assert resp.status_code == 303
    assert resp.headers["location"] == "/handoff?saved=12"
    assert captured["follow_up_required"] is True


def test_handoff_page_renders_open_followups(monkeypatch):
    monkeypatch.setattr(handoff.plant_day, "today", lambda: date(2026, 6, 19))
    monkeypatch.setattr(handoff.exception_inbox, "build_summary", _summary)
    monkeypatch.setattr(handoff, "_recent_handoffs", lambda: [])
    monkeypatch.setattr(handoff, "_open_followups", lambda: [{
        "id": 21,
        "handoff_date": date(2026, 6, 19),
        "shift_label": "Night",
        "created_by": "Mia",
        "notes": "Maintenance must check Repair 1",
        "open_total": 3,
        "urgent_total": 1,
        "follow_up_required": True,
        "is_open_followup": True,
        "created_at_label": "6/19 10:15 PM",
    }])
    client = TestClient(app)

    resp = client.get("/handoff")

    assert resp.status_code == 200
    assert "Open Follow-ups" in resp.text
    assert "Maintenance must check Repair 1" in resp.text
    assert "/handoff/21" in resp.text


def test_handoff_detail_renders_followup_resolve_form(monkeypatch):
    monkeypatch.setattr(handoff.plant_day, "today", lambda: date(2026, 6, 19))
    monkeypatch.setattr(handoff, "_load_handoff", lambda handoff_id: {
        "id": handoff_id,
        "handoff_date": date(2026, 6, 19),
        "shift_label": "Night",
        "created_by": "Mia",
        "notes": "Maintenance must check Repair 1",
        "open_total": 3,
        "urgent_total": 1,
        "source_errors": [],
        "source_error_label": "",
        "follow_up_required": True,
        "is_open_followup": True,
        "created_at_label": "6/19 10:15 PM",
        "exception_snapshot": {"sections": []},
    })
    client = TestClient(app)

    resp = client.get("/handoff/21")

    assert resp.status_code == 200
    assert "Follow-up Open" in resp.text
    assert 'action="/handoff/21/resolve"' in resp.text
    assert "Mark Resolved" in resp.text


def test_resolve_handoff_updates_followup(monkeypatch):
    captured = {}

    def fake_query(sql, params):
        captured["sql"] = sql
        captured["params"] = params
        return [{
            "id": 21,
            "handoff_date": date(2026, 6, 19),
            "shift_label": "Night",
            "created_by": "Mia",
            "notes": "Maintenance must check Repair 1",
            "open_total": 3,
            "urgent_total": 1,
            "source_errors": [],
            "exception_snapshot": {},
            "follow_up_required": True,
            "resolved_at": datetime(2026, 6, 20, 2, 30, tzinfo=timezone.utc),
            "resolved_by": "Dale",
            "resolution_note": "Repair 1 cleared",
            "created_at": datetime(2026, 6, 19, 22, 15, tzinfo=timezone.utc),
            "updated_at": datetime(2026, 6, 20, 2, 30, tzinfo=timezone.utc),
        }]

    monkeypatch.setattr(db, "query", fake_query)

    row = handoff._resolve_handoff(
        handoff_id=21,
        resolved_by="Dale",
        resolution_note="Repair 1 cleared",
    )

    assert row is not None
    assert "UPDATE plant_shift_handoffs SET" in captured["sql"]
    assert captured["params"] == ("Dale", "Repair 1 cleared", 21)
    assert row["is_open_followup"] is False
    assert row["resolution_note"] == "Repair 1 cleared"


def test_resolve_handoff_form_redirects(monkeypatch):
    captured = {}

    def fake_resolve(**kwargs):
        captured.update(kwargs)
        return {"id": 21}

    monkeypatch.setattr(handoff, "_resolve_handoff", fake_resolve)
    client = TestClient(app)

    resp = client.post(
        "/handoff/21/resolve",
        data={"resolved_by": "Dale", "resolution_note": "Done"},
        follow_redirects=False,
    )

    assert resp.status_code == 303
    assert resp.headers["location"] == "/handoff/21"
    assert captured["handoff_id"] == 21
    assert captured["resolved_by"] == "Dale"
    assert captured["resolution_note"] == "Done"


def test_create_handoff_json_parses_false_string(monkeypatch):
    captured = {}

    def fake_create(**kwargs):
        captured.update(kwargs)
        return {"id": 42}

    monkeypatch.setattr(handoff, "_create_handoff", fake_create)
    client = TestClient(app)

    resp = client.post("/api/handoff", json={"follow_up_required": "false"})

    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "id": 42}
    assert captured["follow_up_required"] is False


def test_footer_injects_handoff_nav_link():
    from pathlib import Path

    js = (
        Path(__file__).resolve().parents[1]
        / "src"
        / "zira_dashboard"
        / "static"
        / "footer.js"
    ).read_text(encoding="utf-8")

    assert "function ensureHandoffLink()" in js
    assert "href = '/handoff'" in js
    assert "ensureHandoffLink();" in js
