import asyncio
from datetime import date, time
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


def _publish_location(name, *, min_ops):
    return staffing.Location(
        name, "Repair", "Bay 1", "Recycled", None,
        min_ops=min_ops, max_ops=min_ops,
    )


def _capture_publish(monkeypatch, locs, existing=None):
    saved = []
    existing = existing or staffing.Schedule(day=DAY, published=False, assignments={})
    monkeypatch.setattr(staffing_routes.staffing, "LOCATIONS", tuple(locs))
    monkeypatch.setattr(
        staffing_routes.work_centers_store, "min_ops", lambda loc: loc.min_ops,
    )
    monkeypatch.setattr(staffing_routes.staffing, "load_schedule", lambda _day: existing)
    monkeypatch.setattr(staffing_routes.staffing, "save_schedule", saved.append)
    monkeypatch.setattr(staffing_routes._http_cache, "invalidate_today_cache", lambda: None)
    monkeypatch.setattr(
        staffing_routes,
        "_enabled_auto_work_centers",
        lambda _day: {loc.name for loc in locs},
    )
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
    assert response.headers["location"] == f"/staffing?day={DAY.isoformat()}&publish_blocked=1"
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
    saved = _capture_publish(monkeypatch, [enabled, disabled])
    monkeypatch.setattr(
        staffing_routes,
        "_enabled_auto_work_centers",
        lambda _day: {"Hand Build #1"},
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
        day=DAY, published=True, assignments={"Hand Build #1": ["Jordan", "Taylor"]},
    )
    saved = _capture_publish(monkeypatch, [pair], existing=posted)

    staffing_routes._staffing_save_work(
        SimpleNamespace(headers={}), DAY, 0,
        FormData([("action", "publish"), ("loc__Hand Build #1", "Jordan")]),
    )

    assert saved[0].published is False
    assert saved[0].published_snapshot == staffing.snapshot_of(posted)


def test_notes_only_save_preserves_rotation_metadata(monkeypatch):
    saved = _capture_route_save(monkeypatch, _schedule(published=True))

    staffing_routes._staffing_save_work(
        SimpleNamespace(headers={}), DAY, 0, _save_form("save_notes", notes="updated"),
    )

    assert saved[0].rotation_mode == "training"
    assert saved[0].assignment_sources == SOURCES


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


def test_posted_snapshot_allows_discard_draft(monkeypatch):
    snapshot = staffing.snapshot_of(_schedule(published=True))
    saved = _capture_route_save(
        monkeypatch, _schedule(published=False, published_snapshot=snapshot),
    )

    response = staffing_routes._staffing_save_work(
        SimpleNamespace(headers={}), DAY, 0,
        _save_form("discard_draft", viewing_posted="1"),
    )

    assert response.status_code == 303
    assert saved[0].published is True


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
        published_snapshot={
            "assignments": {"Repair 1": ["Taylor"]},
            "notes": "posted",
            "wc_notes": {},
            "testing_day": False,
            "rotation_mode": "normal",
            "assignment_sources": posted_sources,
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
    monkeypatch.setattr(staffing_routes.staffing, "LOCATIONS", ())
    monkeypatch.setattr(staffing_routes.work_centers_store, "default_people", lambda _loc: [])
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
    monkeypatch.setattr(staffing_routes, "templates", type("Templates", (), {
        "TemplateResponse": staticmethod(lambda *_args: type("Response", (), {"headers": {}})()),
    })())

    staffing_routes.staffing_page(
        request=object(), day=DAY.isoformat(), publish_blocked=0, view="posted",
    )

    assert staffing.load_schedule(DAY).rotation_mode == "training"
    assert staffing.load_schedule(DAY).assignment_sources == draft_sources

    monkeypatch.setattr(staffing, "save_schedule", saved.append)
    monkeypatch.setattr(staffing_routes.staffing, "LOCATIONS", (repair_1,))
    staffing_routes._staffing_save_work(
        SimpleNamespace(headers={}), DAY, 0,
        _save_form("save", notes="draft update", **{"loc__Repair 1": "Jordan"}),
    )

    assert saved[0].rotation_mode == "training"
    assert saved[0].assignment_sources == draft_sources
