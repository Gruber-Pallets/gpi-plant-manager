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
    monkeypatch.setattr(handoff, "_recent_handoffs", lambda: [{
        "id": 7,
        "handoff_date": date(2026, 6, 18),
        "shift_label": "Day",
        "created_by": "Dale",
        "notes": "Repair 2 needs follow-up",
        "open_total": 4,
        "urgent_total": 2,
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
            "created_at": datetime(2026, 6, 19, 14, 0, tzinfo=timezone.utc),
        }]

    monkeypatch.setattr(db, "query", fake_query)

    row = handoff._create_handoff(
        shift_label="Evening",
        created_by="Mia",
        notes="Watch tablets",
    )

    assert row["id"] == 9
    assert "INSERT INTO plant_shift_handoffs" in captured["sql"]
    assert captured["params"][0] == date(2026, 6, 19)
    assert captured["params"][1] == "Evening"
    assert captured["params"][2] == "Mia"
    assert captured["params"][4] == 5
    assert captured["params"][5] == 2
    assert json.loads(captured["params"][6]) == [{"source": "Pending Time Off"}]
    assert json.loads(captured["params"][7])["total"] == 5


def test_handoff_form_redirects_after_save(monkeypatch):
    monkeypatch.setattr(handoff, "_create_handoff", lambda **kwargs: {"id": 12})
    client = TestClient(app)

    resp = client.post(
        "/handoff",
        data={"shift_label": "Day", "created_by": "Dale", "notes": "All set"},
        follow_redirects=False,
    )

    assert resp.status_code == 303
    assert resp.headers["location"] == "/handoff?saved=12"


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
