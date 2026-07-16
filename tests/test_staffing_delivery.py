import os
from datetime import date

import pytest
from fastapi.testclient import TestClient

from zira_dashboard import db, staffing
from zira_dashboard.app import app


@pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="needs Postgres")
def test_record_delivery_updates_only_matching_current_version():
    day = date(2099, 12, 30)
    db.execute("DELETE FROM schedules WHERE day = %s", (day,))
    try:
        staffing.save_schedule(staffing.Schedule(
            day=day, published=True, published_delivery={"version": "current"},
        ))

        delivery = staffing.record_delivery(
            day, "current", {"printed_at": "2099-12-30T12:00:00+00:00"},
        )

        assert delivery["version"] == "current"
        assert delivery["printed_at"] == "2099-12-30T12:00:00+00:00"
        assert staffing.record_delivery(day, "old", {"printed_at": "no"}) is None
    finally:
        db.execute("DELETE FROM schedules WHERE day = %s", (day,))


def test_mark_printed_records_matching_posted_version(monkeypatch):
    monkeypatch.setattr(
        staffing, "delivery_for_version", lambda _day, version: {"version": version},
    )
    monkeypatch.setattr(
        staffing,
        "record_delivery",
        lambda _day, version, fields: {"version": version, **fields},
    )

    response = TestClient(app).post(
        "/staffing/mark-printed?day=2026-07-14&version=v1"
    )

    assert response.status_code == 200
    assert response.json()["delivery"]["version"] == "v1"
    assert "printed_at" in response.json()["delivery"]


def test_mark_printed_rejects_stale_version(monkeypatch):
    monkeypatch.setattr(staffing, "delivery_for_version", lambda *_args: None)

    response = TestClient(app).post(
        "/staffing/mark-printed?day=2026-07-14&version=old"
    )

    assert response.status_code == 409
