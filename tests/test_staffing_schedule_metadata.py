import asyncio
from datetime import date
from types import SimpleNamespace

from starlette.datastructures import FormData

from zira_dashboard import staffing
from zira_dashboard.routes import staffing as staffing_routes


DAY = date(2026, 7, 14)
SOURCES = {"Repair 1": {"Jordan": "manual"}}


def _schedule(**changes):
    values = {
        "day": DAY,
        "published": False,
        "assignments": {"Repair 1": ["Jordan"]},
        "rotation_mode": "training",
        "assignment_sources": SOURCES,
    }
    values.update(changes)
    return staffing.Schedule(**values)


def _save_form(action, **fields):
    return FormData({"action": action, **fields})


def _capture_route_save(monkeypatch, existing):
    saved = []
    monkeypatch.setattr(staffing_routes.staffing, "LOCATIONS", ())
    monkeypatch.setattr(staffing_routes.staffing, "load_schedule", lambda _day: existing)
    monkeypatch.setattr(staffing_routes.staffing, "save_schedule", saved.append)
    monkeypatch.setattr(staffing_routes._http_cache, "invalidate_today_cache", lambda: None)
    return saved


def test_notes_only_save_preserves_rotation_metadata(monkeypatch):
    saved = _capture_route_save(monkeypatch, _schedule(published=True))

    staffing_routes._staffing_save_work(
        SimpleNamespace(headers={}), DAY, 0, _save_form("save_notes", notes="updated"),
    )

    assert saved[0].rotation_mode == "training"
    assert saved[0].assignment_sources == SOURCES


def test_regular_save_preserves_rotation_metadata(monkeypatch):
    saved = _capture_route_save(monkeypatch, _schedule())

    staffing_routes._staffing_save_work(
        SimpleNamespace(headers={}), DAY, 0, _save_form("save", notes="updated"),
    )

    assert saved[0].rotation_mode == "training"
    assert saved[0].assignment_sources == SOURCES


def test_discard_draft_restores_rotation_metadata_from_snapshot(monkeypatch):
    snapshot = staffing.snapshot_of(_schedule())
    saved = _capture_route_save(
        monkeypatch,
        _schedule(
            rotation_mode="normal",
            assignment_sources={},
            published_snapshot=snapshot,
        ),
    )

    staffing_routes._staffing_save_work(
        SimpleNamespace(headers={}), DAY, 0, _save_form("discard_draft"),
    )

    assert saved[0].rotation_mode == "training"
    assert saved[0].assignment_sources == SOURCES


def test_clear_testing_day_preserves_rotation_metadata(monkeypatch):
    saved = []
    existing = _schedule(testing_day=True)
    monkeypatch.setattr(staffing_routes.staffing, "load_schedule", lambda _day: existing)
    monkeypatch.setattr(staffing_routes.staffing, "save_schedule", saved.append)
    monkeypatch.setattr(staffing_routes, "_bust_after_mutation", lambda: None)

    class Request:
        async def json(self):
            return {"day": DAY.isoformat()}

    response = asyncio.run(staffing_routes.staffing_clear_testing_day(Request()))

    assert response.status_code == 200
    assert saved[0].rotation_mode == "training"
    assert saved[0].assignment_sources == SOURCES
