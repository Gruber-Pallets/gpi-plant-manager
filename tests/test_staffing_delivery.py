import os
from datetime import date

import pytest
from fastapi.testclient import TestClient
from starlette.datastructures import FormData

from zira_dashboard import db, staffing
from zira_dashboard.app import app
from zira_dashboard.routes import staffing as staffing_routes


@pytest.mark.parametrize("day", [date(2026, 7, 15), date(2026, 7, 18), date(2026, 7, 19)])
def test_every_day_uses_the_same_draft_and_posted_transition(day):
    posted = staffing.Schedule(
        day=day,
        published=True,
        assignments={"Repair 1": ["Jordan"]},
        published_delivery={"version": "v1"},
    )

    draft = staffing.draft_from_posted(posted)

    assert draft.published is False
    assert draft.published_delivery == {}
    assert draft.published_snapshot["published_delivery"]["version"] == "v1"


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


def test_schedule_fork_upsert_preserves_the_current_matching_delivery(monkeypatch):
    executed = []

    class Cursor:
        def execute(self, sql, params=None):
            executed.append((sql, params))

    class CursorContext:
        def __enter__(self):
            return Cursor()

        def __exit__(self, *_args):
            return False

    monkeypatch.setattr(db, "cursor", lambda: CursorContext())

    staffing.save_schedule(staffing.draft_from_posted(staffing.Schedule(
        day=date(2026, 7, 20),
        published=True,
        published_delivery={"version": "v1"},
    )))

    upsert = executed[0][0]
    assert "schedules.published AND NOT EXCLUDED.published" in upsert
    assert "schedules.published_delivery" in upsert
    assert "EXCLUDED.published_snapshot->'published_delivery'->>'version'" in upsert


@pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="needs Postgres")
def test_staffing_form_fork_preserves_delivery_recorded_before_persistence(monkeypatch):
    day = date(2099, 12, 29)
    db.execute("DELETE FROM schedules WHERE day = %s", (day,))
    try:
        staffing.save_schedule(staffing.Schedule(
            day=day,
            published=True,
            notes="official",
            published_delivery={"version": "v1"},
        ))
        original_save = staffing.save_schedule
        original_record = staffing.record_delivery

        def record_then_save(draft):
            delivery = original_record(
                day, "v1", {"printed_at": "2099-12-29T12:00:00+00:00"},
            )
            assert delivery is not None
            original_save(draft)

        monkeypatch.setattr(staffing_routes.staffing, "LOCATIONS", ())
        monkeypatch.setattr(staffing_routes.staffing, "save_schedule", record_then_save)
        monkeypatch.setattr(staffing_routes._http_cache, "invalidate_today_cache", lambda: None)

        response = staffing_routes._staffing_save_work(
            type("Request", (), {"headers": {}})(),
            day,
            0,
            FormData({"action": "save", "notes": "draft"}),
        )

        assert response.status_code == 303
        draft = staffing.load_schedule(day)
        assert draft.published is False
        assert draft.published_delivery == {}
        assert draft.published_snapshot["published_delivery"] == {
            "version": "v1", "printed_at": "2099-12-29T12:00:00+00:00"
        }
    finally:
        db.execute("DELETE FROM schedules WHERE day = %s", (day,))
