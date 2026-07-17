import asyncio
import json
from datetime import date, time
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import pytest
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


def test_snapshot_includes_hours_and_delivery():
    posted = _schedule(
        published=True,
        custom_hours={"start": "06:00", "end": "12:00", "breaks": []},
        published_delivery={"version": "v1", "printed_at": "2026-07-14T12:00:00+00:00"},
    )

    snapshot = staffing.snapshot_of(posted)

    assert snapshot["custom_hours"] == posted.custom_hours
    assert snapshot["published_delivery"] == posted.published_delivery


def test_invalidate_all_schedule_caches_clears_every_cached_day(monkeypatch):
    monday = date(2026, 7, 20)
    tuesday = date(2026, 7, 21)
    monkeypatch.setattr(
        staffing,
        "_schedule_cache",
        {monday: _schedule(day=monday), tuesday: _schedule(day=tuesday)},
    )

    staffing.invalidate_all_schedule_caches()

    assert staffing._schedule_cache == {}


def test_draft_from_posted_preserves_official_version_and_clears_draft_delivery():
    posted = _schedule(
        published=True,
        notes="official",
        published_delivery={"version": "v1", "printed_at": "now"},
    )

    draft = staffing.draft_from_posted(posted)

    assert draft.published is False
    assert draft.published_delivery == {}
    assert draft.published_snapshot["notes"] == "official"
    assert draft.published_snapshot["published_delivery"] == {"version": "v1", "printed_at": "now"}


def _save_form(action, **fields):
    return FormData({"action": action, **fields})


def _capture_route_save(monkeypatch, existing):
    saved = []
    monkeypatch.setattr(staffing_routes.staffing, "LOCATIONS", ())
    monkeypatch.setattr(staffing_routes.staffing, "load_schedule", lambda _day: existing)
    monkeypatch.setattr(staffing_routes.staffing, "save_schedule", saved.append)
    monkeypatch.setattr(staffing_routes._http_cache, "invalidate_today_cache", lambda: None)
    return saved


def _publish_location(name, *, min_ops):
    return staffing.Location(
        name, "Repair", "Bay 1", "Recycled", None,
        min_ops=min_ops, max_ops=min_ops,
    )


def _capture_publish(monkeypatch, locs, existing=None):
    saved = []
    existing = existing or staffing.Schedule(
        day=DAY,
        published=False,
        assignments={},
        auto_enabled_work_centers=[loc.name for loc in locs],
    )
    monkeypatch.setattr(staffing_routes.staffing, "LOCATIONS", tuple(locs))
    monkeypatch.setattr(
        staffing_routes.work_centers_store, "min_ops", lambda loc: loc.min_ops,
    )
    monkeypatch.setattr(staffing_routes.staffing, "load_schedule", lambda _day: existing)
    monkeypatch.setattr(staffing_routes.staffing, "save_schedule", saved.append)
    monkeypatch.setattr(staffing_routes._http_cache, "invalidate_today_cache", lambda: None)
    return saved


def test_publish_override_cannot_bypass_two_person_minimum(monkeypatch):
    pair = _publish_location("Hand Build #1", min_ops=2)
    saved = _capture_publish(monkeypatch, [pair])

    response = staffing_routes._staffing_save_work(
        SimpleNamespace(headers={}), DAY, 0,
        FormData([
            ("action", "publish"),
            ("loc__Hand Build #1", "Jordan"),
            ("override", "1"),
        ]),
    )

    assert response.status_code == 303
    assert parse_qs(urlparse(response.headers["location"]).query) == {
        "day": [DAY.isoformat()],
        "publish_blocked": ["1"],
        "publish_error": ["Hand Build #1 requires 2 operators — currently 1."],
    }
    assert saved[0].published is False
    assert saved[0].assignments == {"Hand Build #1": ["Jordan"]}


def test_publish_blocks_an_empty_one_person_work_center(monkeypatch):
    solo = _publish_location("Junior #1", min_ops=1)
    saved = _capture_publish(monkeypatch, [solo])

    staffing_routes._staffing_save_work(
        SimpleNamespace(headers={}), DAY, 0, FormData({"action": "publish"}),
    )

    assert saved[0].published is False


def test_publish_ignores_minimums_for_work_centers_that_are_off(monkeypatch):
    enabled = _publish_location("Hand Build #1", min_ops=2)
    disabled = _publish_location("Junior #1", min_ops=1)
    saved = _capture_publish(
        monkeypatch,
        [enabled, disabled],
        existing=staffing.Schedule(
            day=DAY,
            published=False,
            assignments={},
            auto_enabled_work_centers=["Hand Build #1"],
        ),
    )
    monkeypatch.setattr(
        staffing_routes.staffing,
        "new_published_delivery",
        lambda: {"version": "v2"},
    )

    response = staffing_routes._staffing_save_work(
        SimpleNamespace(headers={}), DAY, 0,
        FormData([
            ("action", "publish"),
            ("loc__Hand Build #1", "Jordan"),
            ("loc__Hand Build #1", "Taylor"),
        ]),
    )

    assert response.status_code == 303
    assert response.headers["location"] == f"/staffing?day={DAY.isoformat()}"
    assert saved[0].published is True
    assert saved[0].published_delivery == {"version": "v2"}


def test_json_publish_below_minimum_returns_conflict_with_shortages(monkeypatch):
    pair = _publish_location("Hand Build #1", min_ops=2)
    saved = _capture_publish(monkeypatch, [pair])

    response = staffing_routes._staffing_save_work(
        SimpleNamespace(headers={"accept": "application/json"}), DAY, 0,
        FormData([("action", "publish"), ("loc__Hand Build #1", "Jordan")]),
    )

    assert response.status_code == 409
    assert response.body == (
        '{"ok":false,"error":"Publish blocked — staff every work center to its minimum.",'
        '"publish_block_reasons":["Hand Build #1 requires 2 operators — currently 1."]}'
    ).encode()
    assert saved[0].published is False


def test_failed_republish_preserves_the_posted_version_as_a_snapshot(monkeypatch):
    pair = _publish_location("Hand Build #1", min_ops=2)
    posted = staffing.Schedule(
        day=DAY,
        published=True,
        assignments={"Hand Build #1": ["Jordan", "Taylor"]},
        auto_enabled_work_centers=["Hand Build #1"],
    )
    saved = _capture_publish(monkeypatch, [pair], existing=posted)
    monkeypatch.setattr(
        staffing_routes.staffing,
        "new_published_delivery",
        lambda: (_ for _ in ()).throw(AssertionError("failed publish must not create a version")),
    )

    staffing_routes._staffing_save_work(
        SimpleNamespace(headers={}), DAY, 0,
        FormData([("action", "publish"), ("loc__Hand Build #1", "Jordan")]),
    )

    assert saved[0].published is False
    assert saved[0].published_snapshot == staffing.snapshot_of(posted)


def test_notes_save_on_posted_schedule_creates_draft_snapshot(monkeypatch):
    existing = _schedule(
        published=True,
        notes="posted",
        published_delivery={"version": "v1", "printed_at": "now"},
    )
    saved = _capture_route_save(monkeypatch, existing)

    staffing_routes._staffing_save_work(
        SimpleNamespace(headers={}), DAY, 0, _save_form("save", notes="draft note"),
    )

    assert saved[0].published is False
    assert saved[0].notes == "draft note"
    assert saved[0].published_delivery == {}
    assert saved[0].published_snapshot["published_delivery"]["version"] == "v1"


def test_regular_save_drops_sources_for_people_removed_from_schedule(monkeypatch):
    saved = _capture_route_save(monkeypatch, _schedule())

    staffing_routes._staffing_save_work(
        SimpleNamespace(headers={}), DAY, 0, _save_form("save", notes="updated"),
    )

    assert saved[0].rotation_mode == "training"
    assert saved[0].assignments == {}
    assert saved[0].assignment_sources == {}


def test_regular_save_preserves_source_for_person_still_assigned(monkeypatch):
    repair_1 = next(loc for loc in staffing.LOCATIONS if loc.name == "Repair 1")
    saved = _capture_route_save(monkeypatch, _schedule())
    monkeypatch.setattr(staffing_routes.staffing, "LOCATIONS", (repair_1,))

    staffing_routes._staffing_save_work(
        SimpleNamespace(headers={}), DAY, 0,
        _save_form("save", **{"loc__Repair 1": "Jordan"}),
    )

    assert saved[0].assignments == {"Repair 1": ["Jordan"]}
    assert saved[0].assignment_sources == SOURCES


def test_first_normal_save_of_published_schedule_snapshots_and_starts_draft(monkeypatch):
    existing = _schedule(published=True, notes="posted")
    saved = _capture_route_save(monkeypatch, existing)

    staffing_routes._staffing_save_work(
        SimpleNamespace(headers={}), DAY, 0, _save_form("save", notes="draft update"),
    )

    assert saved[0].published is False
    assert saved[0].published_snapshot == staffing.snapshot_of(existing)
    assert saved[0].notes == "draft update"


def test_posted_snapshot_rejects_ordinary_save_without_persisting(monkeypatch):
    saved = _capture_route_save(monkeypatch, _schedule())

    response = staffing_routes._staffing_save_work(
        SimpleNamespace(headers={}), DAY, 0,
        _save_form("save", viewing_posted="1", notes="should not save"),
    )

    assert response.status_code == 400
    assert saved == []


def test_clear_testing_day_starts_draft_and_preserves_rotation_metadata(monkeypatch):
    saved = []
    existing = _schedule(
        published=True,
        testing_day=True,
        published_delivery={"version": "v1"},
        auto_enabled_work_centers=["Repair 1"],
    )
    monkeypatch.setattr(staffing_routes.staffing, "load_schedule", lambda _day: existing)
    monkeypatch.setattr(staffing_routes.staffing, "save_schedule", saved.append)
    monkeypatch.setattr(staffing_routes, "_bust_after_mutation", lambda: None)

    class Request:
        async def json(self):
            return {"day": DAY.isoformat()}

    response = asyncio.run(staffing_routes.staffing_clear_testing_day(Request()))

    assert response.status_code == 200
    assert saved[0].published is False
    assert saved[0].published_delivery == {}
    assert saved[0].published_snapshot["published_delivery"]["version"] == "v1"
    assert saved[0].rotation_mode == "training"
    assert saved[0].assignment_sources == SOURCES
    assert saved[0].auto_enabled_work_centers == ["Repair 1"]


@pytest.mark.parametrize(
    ("handler", "report_method"),
    [
        (staffing_routes.staffing_clear_partial, "clear_partial_by_name"),
        (staffing_routes.staffing_restore_partial, "restore_partial_by_name"),
    ],
)
def test_partial_time_off_mutation_starts_draft(monkeypatch, handler, report_method):
    saved = []
    posted = _schedule(published=True, published_delivery={"version": "v1"})
    monkeypatch.setattr(staffing_routes.staffing, "load_schedule", lambda _day: posted)
    monkeypatch.setattr(staffing_routes.staffing, "save_schedule", saved.append)
    monkeypatch.setattr(staffing_routes.late_report, report_method, lambda *_args: None)
    monkeypatch.setattr(staffing_routes, "_bust_after_mutation", lambda: None)

    class Request:
        async def json(self):
            return {"day": DAY.isoformat(), "name": "Jordan"}

    response = asyncio.run(handler(Request()))

    assert response.status_code == 200
    assert saved[0].published is False
    assert saved[0].published_delivery == {}
    assert saved[0].published_snapshot["published_delivery"]["version"] == "v1"


class _FormRequest:
    def __init__(self, values):
        self._values = FormData(values)

    async def form(self):
        return self._values


def test_hours_save_on_posted_schedule_starts_draft(monkeypatch):
    saved = []
    posted = staffing.Schedule(day=DAY, published=True, published_delivery={"version": "v1"})
    monkeypatch.setattr(staffing_routes.staffing, "load_schedule", lambda _day: posted)
    monkeypatch.setattr(staffing_routes.staffing, "save_schedule", saved.append)
    monkeypatch.setattr(staffing_routes._http_cache, "invalidate_today_cache", lambda: None)

    response = asyncio.run(staffing_routes.staffing_hours_save(_FormRequest({
        "day": DAY.isoformat(), "start": "06:00", "end": "12:00",
    })))

    assert response.status_code == 200
    assert saved[0].published is False
    assert saved[0].published_snapshot["published_delivery"]["version"] == "v1"


def test_json_save_includes_lifecycle_fields(monkeypatch):
    saved = _capture_route_save(
        monkeypatch,
        _schedule(published=True, published_delivery={"version": "v1"}),
    )
    monkeypatch.setattr(staffing_routes.staffing, "schedule_revision", lambda _day: "r1")

    response = staffing_routes._staffing_save_work(
        SimpleNamespace(headers={"accept": "application/json"}), DAY, 0,
        _save_form("save", notes="draft note"),
    )

    assert saved[0].published is False
    assert json.loads(response.body) == {
        "ok": True,
        "revision": "r1",
        "published": False,
        "has_snapshot": True,
        "posted_version": "v1",
        "testing_day": False,
    }


def test_staffing_live_returns_no_store_lifecycle_revision(monkeypatch):
    draft = _schedule(
        published=False,
        published_snapshot={"published_delivery": {"version": "v1"}},
    )
    monkeypatch.setattr(staffing_routes.staffing, "load_schedule", lambda _day: draft)
    monkeypatch.setattr(staffing_routes.staffing, "schedule_revision", lambda _day: "r1")

    response = staffing_routes.staffing_live(DAY.isoformat())

    assert response.headers["cache-control"] == "no-store"
    assert json.loads(response.body) == {
        "ok": True,
        "revision": "r1",
        "published": False,
        "has_snapshot": True,
        "posted_version": "v1",
    }


def test_posted_view_does_not_overwrite_cached_draft_before_save(monkeypatch):
    from zira_dashboard import cert_lookup, staffing_view

    repair_1 = next(loc for loc in staffing.LOCATIONS if loc.name == "Repair 1")
    draft_sources = {"Repair 1": {"Jordan": "generated"}}
    posted_sources = {"Repair 1": {"Jordan": "manual"}}
    cached = staffing.Schedule(
        day=DAY,
        published=False,
        assignments={"Repair 1": ["Jordan"]},
        rotation_mode="training",
        assignment_sources=draft_sources,
        auto_enabled_work_centers=["Dismantler 1"],
        published_snapshot={
            "assignments": {"Repair 1": ["Taylor"]},
            "notes": "posted",
            "wc_notes": {},
            "testing_day": False,
            "rotation_mode": "normal",
            "assignment_sources": posted_sources,
            "auto_enabled_work_centers": ["Unknown", "Repair 1"],
            "custom_hours": {"start": "06:00", "end": "12:00", "breaks": []},
            "published_delivery": {"version": "v1", "printed_at": "now"},
        },
    )
    staffing._schedule_cache.clear()
    staffing._schedule_cache[DAY] = cached
    saved = []

    monkeypatch.setattr(staffing_routes, "plant_today", lambda: date(2026, 7, 13))
    monkeypatch.setattr(staffing_routes, "_next_working_day", lambda _d: DAY)
    monkeypatch.setattr(staffing_routes._http_cache, "get_cached_response", lambda *a, **k: None)
    monkeypatch.setattr(staffing_routes._http_cache, "set_cache_headers", lambda *a, **k: None)
    monkeypatch.setattr(staffing_routes._http_cache, "store_cached_response", lambda *a, **k: None)
    monkeypatch.setattr(cert_lookup, "load_person_certs", lambda: {})
    monkeypatch.setattr(staffing, "load_roster", lambda: [])
    monkeypatch.setattr(staffing_routes, "_safe_time_off_entries", lambda _d: [])
    monkeypatch.setattr(
        staffing_routes,
        "_safe_attendance",
        lambda _d, _sched, _today: {"by_name": {}, "name_to_id": {}},
    )
    monkeypatch.setattr(staffing_routes, "_late_emp_ids", lambda *_args: set())
    monkeypatch.setattr(staffing_routes.attendance, "person_id_to_name", lambda _ids: {})
    monkeypatch.setattr(staffing_routes.shift_config, "configured_shift_start_for", lambda _d: time(7, 0))
    monkeypatch.setattr(staffing_routes.shift_config, "configured_shift_end_for", lambda _d: time(15, 30))
    monkeypatch.setattr(staffing_routes.shift_config, "configured_breaks_for", lambda _d: [])
    monkeypatch.setattr(staffing_routes.shift_config, "scheduler_hours_source", lambda *_args: "weekday_default")
    monkeypatch.setattr(
        staffing_routes.schedule_store,
        "current",
        lambda: SimpleNamespace(work_weekdays=frozenset({0, 1, 2, 3, 4})),
    )
    monkeypatch.setattr(staffing_routes.staffing, "LOCATIONS", (repair_1,))
    monkeypatch.setattr(staffing_routes.work_centers_store, "default_people", lambda _loc: [])
    monkeypatch.setattr(staffing_routes.staffing, "schedule_revision", lambda _day: "r1")
    monkeypatch.setattr(
        staffing_view,
        "build_staffing_bays",
        lambda **_kwargs: {
            "bays": [], "publish_block_reasons": [], "defaults_by_loc": {},
            "unassigned": [], "reserves": [], "time_off_names": [], "time_off_entries": [],
            "partial_hours_by_name": {}, "partial_range_by_name": {}, "partial_clear_by_name": {},
            "people_meta": {}, "all_active_people": [],
        },
    )
    captured_context = {}

    def render(_request, _template, context):
        captured_context.update(context)
        return type("Response", (), {"headers": {}})()

    monkeypatch.setattr(staffing_routes, "templates", type("Templates", (), {
        "TemplateResponse": staticmethod(render),
    })())

    staffing_routes.staffing_page(
        request=object(), day=DAY.isoformat(), publish_blocked=0, view="posted",
    )

    assert staffing.load_schedule(DAY).rotation_mode == "training"
    assert staffing.load_schedule(DAY).assignment_sources == draft_sources
    assert captured_context["sched"].custom_hours == {
        "start": "06:00", "end": "12:00", "breaks": [],
    }
    assert captured_context["posted_delivery"] == {"version": "v1", "printed_at": "now"}
    assert captured_context["posted_version"] == "v1"
    assert captured_context["schedule_revision"] == "r1"
    assert captured_context["auto_schedule_enabled_wc_names"] == ["Repair 1"]

    monkeypatch.setattr(staffing, "save_schedule", saved.append)
    monkeypatch.setattr(staffing_routes.staffing, "LOCATIONS", (repair_1,))
    staffing_routes._staffing_save_work(
        SimpleNamespace(headers={}), DAY, 0,
        _save_form("save", notes="draft update", **{"loc__Repair 1": "Jordan"}),
    )

    assert saved[0].rotation_mode == "training"
    assert saved[0].assignment_sources == draft_sources
