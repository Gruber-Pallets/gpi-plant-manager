"""Missed-punch-out routes: GET shape, correct (mocked Odoo) + validation."""

import os
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from zira_dashboard.app import app
from zira_dashboard import db, missed_punch_out as mpo, odoo_client
from zira_dashboard.shift_config import SITE_TZ

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="needs Postgres",
)

client = TestClient(app)
ATT = 999600


@pytest.fixture(autouse=True)
def _seed():
    db.bootstrap_schema()
    db.execute("DELETE FROM missed_punch_out WHERE attendance_id = %s", (ATT,))
    # check-in 13:00 local on 6/8; auto-closed at midnight 6/9.
    ci = datetime(2026, 6, 8, 18, 0, tzinfo=timezone.utc)
    midnight = datetime(2026, 6, 9, 0, 0, tzinfo=SITE_TZ)
    mpo.record_close(ATT, 42, ci.isoformat(), midnight)
    yield
    db.execute("DELETE FROM missed_punch_out WHERE attendance_id = %s", (ATT,))


def test_get_returns_count_and_rows():
    r = client.get("/api/missed-punch-out")
    assert r.status_code == 200
    body = r.json()
    assert set(["count", "rows"]) <= set(body.keys())
    row = next(x for x in body["rows"] if x["attendance_id"] == ATT)
    assert row["check_in_label"] == "1:00 PM Mon Jun 8"
    assert row["check_in_date"] == "2026-06-08"


def test_correct_rewrites_check_out_and_resolves(monkeypatch):
    calls = {}
    monkeypatch.setattr(odoo_client, "clock_out",
                        lambda att, ts: calls.update(att=att, ts=ts))
    r = client.post("/missed-punch-out/correct",
                    json={"attendance_id": ATT, "time": "16:30"})
    assert r.status_code == 200 and r.json()["ok"] is True
    assert calls["att"] == ATT
    assert calls["ts"] == datetime(2026, 6, 8, 16, 30, tzinfo=SITE_TZ)
    assert mpo.get_unresolved(ATT) is None


def test_correct_rejects_time_before_check_in(monkeypatch):
    monkeypatch.setattr(odoo_client, "clock_out",
                        lambda att, ts: (_ for _ in ()).throw(AssertionError("no write")))
    r = client.post("/missed-punch-out/correct",
                    json={"attendance_id": ATT, "time": "06:00"})  # before 13:00 check-in
    assert r.status_code == 400
    assert mpo.get_unresolved(ATT) is not None  # still flagged


def test_correct_rejects_bad_time(monkeypatch):
    r = client.post("/missed-punch-out/correct",
                    json={"attendance_id": ATT, "time": "nope"})
    assert r.status_code == 400


def test_correct_unknown_id_404():
    r = client.post("/missed-punch-out/correct",
                    json={"attendance_id": 123123, "time": "16:30"})
    assert r.status_code == 404
