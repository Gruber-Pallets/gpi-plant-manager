import asyncio
import json
import os
from contextlib import contextmanager
from datetime import date, time
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import pytest
from starlette.datastructures import FormData

from zira_dashboard import staffing
from zira_dashboard.routes import staffing as staffing_routes


DAY = date(2026, 7, 14)
SOURCES = {"Repair 1": {"Jordan": "manual"}}
SCHEDULE_CLEAR_DAY = date(2099, 12, 30)
SCHEDULE_CLEAR_WORK_CENTERS = ("Schedule Clear Test Repair 1", "Schedule Clear Test Repair 2")
SCHEDULE_CLEAR_PEOPLE = (
    "Schedule Clear Test Jordan",
    "Schedule Clear Test Taylor",
    "Schedule Clear Test Morgan",
)


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


def test_schedule_without_person_clears_assignments_and_sources():
    schedule = staffing.Schedule(
        day=DAY,
        assignments={
            "Repair 1": ["Jordan", "Taylor"],
            "Repair 2": ["Taylor", "Morgan"],
        },
        assignment_sources={
            "Repair 1": {"Jordan": "manual", "Taylor": "generated"},
            "Repair 2": {"Taylor": "default", "Morgan": "manual"},
        },
    )

    cleaned, changed = staffing._schedule_without_person(schedule, "Taylor")

    assert changed is True
    assert cleaned.assignments == {"Repair 1": ["Jordan"], "Repair 2": ["Morgan"]}
    assert cleaned.assignment_sources == {
        "Repair 1": {"Jordan": "manual"},
        "Repair 2": {"Morgan": "manual"},
    }


def test_schedule_without_person_clears_source_only_entry():
    schedule = staffing.Schedule(
        day=DAY,
        assignments={"Repair 1": ["Jordan"]},
        assignment_sources={"Repair 1": {"Jordan": "manual", "Taylor": "generated"}},
    )

    cleaned, changed = staffing._schedule_without_person(schedule, "Taylor")

    assert changed is True
    assert cleaned.assignments == {"Repair 1": ["Jordan"]}
    assert cleaned.assignment_sources == {"Repair 1": {"Jordan": "manual"}}


@pytest.fixture
def schedule_clear_store():
    from zira_dashboard import db

    db.bootstrap_schema()
    db.execute("DELETE FROM schedules WHERE day = %s", (SCHEDULE_CLEAR_DAY,))
    staffing.invalidate_schedule_cache(SCHEDULE_CLEAR_DAY)
    for work_center in SCHEDULE_CLEAR_WORK_CENTERS:
        db.execute(
            "INSERT INTO work_centers (name, category) VALUES (%s, 'Repair') "
            "ON CONFLICT (name) DO NOTHING",
            (work_center,),
        )
    for person in SCHEDULE_CLEAR_PEOPLE:
        db.execute(
            "INSERT INTO people (name) VALUES (%s) ON CONFLICT (name) DO NOTHING",
            (person,),
        )
    yield
    db.execute("DELETE FROM schedules WHERE day = %s", (SCHEDULE_CLEAR_DAY,))
    staffing.invalidate_schedule_cache(SCHEDULE_CLEAR_DAY)
    db.execute("DELETE FROM people WHERE name = ANY(%s)", (list(SCHEDULE_CLEAR_PEOPLE),))
    db.execute(
        "DELETE FROM work_centers WHERE name = ANY(%s)", (list(SCHEDULE_CLEAR_WORK_CENTERS),)
    )


@pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="needs Postgres")
def test_remove_person_from_schedule_clears_assignments_and_sources(schedule_clear_store):
    staffing.save_schedule(
        staffing.Schedule(
            day=SCHEDULE_CLEAR_DAY,
            assignments={
                SCHEDULE_CLEAR_WORK_CENTERS[0]: [
                    SCHEDULE_CLEAR_PEOPLE[0],
                    SCHEDULE_CLEAR_PEOPLE[1],
                ],
                SCHEDULE_CLEAR_WORK_CENTERS[1]: [
                    SCHEDULE_CLEAR_PEOPLE[1],
                    SCHEDULE_CLEAR_PEOPLE[2],
                ],
            },
            assignment_sources={
                SCHEDULE_CLEAR_WORK_CENTERS[0]: {
                    SCHEDULE_CLEAR_PEOPLE[0]: "manual",
                    SCHEDULE_CLEAR_PEOPLE[1]: "generated",
                },
                SCHEDULE_CLEAR_WORK_CENTERS[1]: {
                    SCHEDULE_CLEAR_PEOPLE[1]: "default",
                    SCHEDULE_CLEAR_PEOPLE[2]: "manual",
                },
            },
        )
    )

    changed = staffing.remove_person_from_schedule(SCHEDULE_CLEAR_DAY, SCHEDULE_CLEAR_PEOPLE[1])

    saved = staffing.load_schedule(SCHEDULE_CLEAR_DAY)
    assert changed is True
    assert saved.assignments == {
        SCHEDULE_CLEAR_WORK_CENTERS[0]: [SCHEDULE_CLEAR_PEOPLE[0]],
        SCHEDULE_CLEAR_WORK_CENTERS[1]: [SCHEDULE_CLEAR_PEOPLE[2]],
    }
    assert saved.assignment_sources == {
        SCHEDULE_CLEAR_WORK_CENTERS[0]: {SCHEDULE_CLEAR_PEOPLE[0]: "manual"},
        SCHEDULE_CLEAR_WORK_CENTERS[1]: {SCHEDULE_CLEAR_PEOPLE[2]: "manual"},
    }


@pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="needs Postgres")
def test_remove_person_from_schedule_is_noop_when_person_is_not_assigned(schedule_clear_store):
    staffing.save_schedule(
        staffing.Schedule(
            day=SCHEDULE_CLEAR_DAY,
            assignments={SCHEDULE_CLEAR_WORK_CENTERS[0]: [SCHEDULE_CLEAR_PEOPLE[0]]},
            assignment_sources={
                SCHEDULE_CLEAR_WORK_CENTERS[0]: {SCHEDULE_CLEAR_PEOPLE[0]: "manual"}
            },
        )
    )

    assert (
        staffing.remove_person_from_schedule(SCHEDULE_CLEAR_DAY, SCHEDULE_CLEAR_PEOPLE[1]) is False
    )
    assert staffing.load_schedule(SCHEDULE_CLEAR_DAY).assignments == {
        SCHEDULE_CLEAR_WORK_CENTERS[0]: [SCHEDULE_CLEAR_PEOPLE[0]],
    }


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
    from zira_dashboard import optional_workday

    monday = date(2026, 7, 20)
    tuesday = date(2026, 7, 21)
    optional_invalidations = []
    monkeypatch.setattr(
        staffing,
        "_schedule_cache",
        {monday: _schedule(day=monday), tuesday: _schedule(day=tuesday)},
    )
    monkeypatch.setattr(
        optional_workday,
        "invalidate_all",
        lambda: optional_invalidations.append(True),
    )

    staffing.invalidate_all_schedule_caches()

    assert staffing._schedule_cache == {}
    assert optional_invalidations == [True]


def test_schedule_cache_invalidation_also_invalidates_optional_workday(monkeypatch):
    from zira_dashboard import optional_workday

    invalidated = []
    monkeypatch.setattr(optional_workday, "invalidate", invalidated.append)

    staffing.invalidate_schedule_cache(DAY)

    assert invalidated == [DAY]


def test_save_schedule_invalidates_optional_workday_state(monkeypatch):
    from zira_dashboard import optional_workday

    invalidated = []
    monkeypatch.setattr(optional_workday, "invalidate", invalidated.append)
    monkeypatch.setattr(
        staffing,
        "_save_schedule_with_cursor",
        lambda *_args: None,
    )

    staffing.save_schedule(_schedule(day=DAY), cur=object())

    assert invalidated == [DAY]


def test_transactional_save_can_defer_cache_invalidation_until_outer_commit(
    monkeypatch,
):
    invalidated = []
    writes = []
    monkeypatch.setattr(
        staffing,
        "_invalidate_schedule_cache",
        invalidated.append,
    )
    monkeypatch.setattr(
        staffing,
        "_save_schedule_with_cursor",
        lambda *_args: writes.append("saved"),
    )

    staffing.save_schedule(
        _schedule(day=DAY),
        cur=object(),
        invalidate_cache=False,
    )

    assert writes == ["saved"]
    assert invalidated == []


def test_locked_custom_hours_update_is_field_scoped_and_preserves_all_other_metadata():
    statements = []

    class Cursor:
        def execute(self, sql, params=()):
            statements.append((" ".join(sql.split()), params))

    posted = _schedule(
        published=True,
        testing_day=True,
        notes="keep the day note",
        wc_notes={"Repair 1": "keep the center note"},
        custom_hours={"start": "06:00", "end": "14:30", "breaks": []},
        published_delivery={"version": "v1", "printed_at": "now"},
        auto_enabled_work_centers=["Repair 1"],
        saturday_availability_overrides={"Jordan": "off", "Taylor": "unassigned"},
    )
    new_hours = {"start": "07:00", "end": "15:30", "breaks": []}

    updated = staffing.update_locked_schedule_metadata(
        posted,
        cur=Cursor(),
        custom_hours=new_hours,
    )

    assert updated.published is False
    assert updated.published_delivery == {}
    assert updated.published_snapshot == staffing.snapshot_of(posted)
    assert updated.custom_hours == new_hours
    assert updated.assignments == posted.assignments
    assert updated.assignment_sources == posted.assignment_sources
    assert updated.notes == posted.notes
    assert updated.wc_notes == posted.wc_notes
    assert updated.testing_day is True
    assert updated.rotation_mode == posted.rotation_mode
    assert updated.auto_enabled_work_centers == posted.auto_enabled_work_centers
    assert updated.saturday_availability_overrides == posted.saturday_availability_overrides
    assert len(statements) == 1
    sql = statements[0][0].lower()
    assert sql.startswith("update schedules set")
    assert "custom_hours" in sql
    assert "schedule_assignments" not in sql
    assert "assignment_sources" not in sql
    assert "saturday_availability_overrides" not in sql
    assert "auto_enabled_work_centers" not in sql
    assert "testing_day" not in sql
    assert "notes" not in sql


def test_locked_testing_day_update_does_not_replace_custom_hours_or_schedule_rows():
    statements = []

    class Cursor:
        def execute(self, sql, params=()):
            statements.append(" ".join(sql.split()).lower())

    existing = _schedule(
        testing_day=True,
        custom_hours={"start": "06:00", "end": "14:30", "breaks": []},
        saturday_availability_overrides={"Jordan": "off"},
    )

    updated = staffing.update_locked_schedule_metadata(
        existing,
        cur=Cursor(),
        testing_day=False,
    )

    assert updated.testing_day is False
    assert updated.custom_hours == existing.custom_hours
    assert updated.saturday_availability_overrides == {"Jordan": "off"}
    assert len(statements) == 1
    assert "testing_day" in statements[0]
    assert "custom_hours" not in statements[0]
    assert "schedule_assignments" not in statements[0]
    assert "schedule_wc_notes" not in statements[0]


def test_ensure_schedule_for_update_seeds_defaults_only_when_row_is_absent(monkeypatch):
    existing = _schedule(auto_enabled_work_centers=["Repair 3"])
    created = staffing.Schedule(
        day=DAY,
        auto_enabled_work_centers=["Repair 1", "Repair 2"],
    )
    loads = iter([None, created])
    statements = []

    monkeypatch.setattr(
        staffing,
        "load_schedule_for_update",
        lambda _day, *, cur: next(loads),
    )

    class Cursor:
        def execute(self, sql, params=()):
            statements.append((" ".join(sql.split()), params))

        def fetchone(self):
            return {"day": DAY}

    ensured = staffing.ensure_schedule_for_update(
        DAY,
        cur=Cursor(),
        initial_auto_enabled_work_centers=["Repair 1", "Repair 2"],
    )

    assert ensured is created
    assert len(statements) == 1
    assert "ON CONFLICT (day) DO NOTHING" in statements[0][0]
    assert json.loads(statements[0][1][1]) == ["Repair 1", "Repair 2"]

    monkeypatch.setattr(
        staffing,
        "load_schedule_for_update",
        lambda _day, *, cur: existing,
    )
    statements.clear()
    ensured = staffing.ensure_schedule_for_update(
        DAY,
        cur=Cursor(),
        initial_auto_enabled_work_centers=["Repair 1", "Repair 2"],
    )

    assert ensured is existing
    assert statements == []


def test_late_report_partial_override_can_share_schedule_transaction(monkeypatch):
    from zira_dashboard import late_report

    statements = []

    class Cursor:
        def execute(self, sql, params=()):
            statements.append((" ".join(sql.split()), params))

    monkeypatch.setattr(
        late_report.db,
        "execute",
        lambda *_args, **_kwargs: pytest.fail("opened a separate transaction"),
    )

    late_report.clear_partial_by_name(DAY, "Jordan", cur=Cursor())
    late_report.restore_partial_by_name(DAY, "Jordan", cur=Cursor())

    assert len(statements) == 2
    assert statements[0][0].startswith("INSERT INTO cleared_partials_by_name")
    assert statements[1][0].startswith("DELETE FROM cleared_partials_by_name")


def test_optional_metadata_update_locks_lifecycle_before_current_schedule(monkeypatch):
    from zira_dashboard import optional_workday

    events = []
    holiday = optional_workday.OptionalWorkday(
        DAY,
        "holiday",
        "Plant Holiday",
        42,
    )
    classifications = iter([holiday, holiday])
    winner = _schedule(
        published=False,
        assignments={},
        assignment_sources={},
        saturday_availability_overrides={"Jordan": "off"},
    )
    cursor = object()

    monkeypatch.setattr(
        staffing_routes.optional_workday,
        "for_day",
        lambda _day: events.append("classify") or next(classifications),
    )
    monkeypatch.setattr(
        staffing_routes.saturday_recruiting_store,
        "lock_for_schedule_mutation",
        lambda _day, *, cur: events.append("recruiting lock") or None,
    )
    monkeypatch.setattr(
        staffing_routes.staffing,
        "ensure_schedule_for_update",
        lambda _day, *, cur, initial_auto_enabled_work_centers=(): (
            events.append("schedule lock") or winner
        ),
    )

    def update(schedule, *, cur, custom_hours, **_kwargs):
        events.append("field update")
        assert schedule is winner
        assert custom_hours["start"] == "07:00"
        return schedule

    monkeypatch.setattr(
        staffing_routes.staffing,
        "update_locked_schedule_metadata",
        update,
    )
    monkeypatch.setattr(
        staffing_routes.staffing,
        "invalidate_schedule_cache",
        lambda _day: events.append("invalidate"),
    )

    @contextmanager
    def transaction():
        events.append("transaction")
        yield cursor
        events.append("commit")

    monkeypatch.setattr(staffing_routes.db, "cursor", transaction)

    updated = staffing_routes._update_schedule_metadata_work(
        DAY,
        custom_hours={"start": "07:00", "end": "15:30", "breaks": []},
    )

    assert updated is winner
    assert events == [
        "classify",
        "transaction",
        "recruiting lock",
        "classify",
        "schedule lock",
        "field update",
        "commit",
        "invalidate",
    ]


def test_optional_metadata_update_rejects_replaced_holiday_before_schedule_write(
    monkeypatch,
):
    from zira_dashboard import optional_workday

    old_holiday = optional_workday.OptionalWorkday(DAY, "holiday", "Old", 42)
    new_holiday = optional_workday.OptionalWorkday(DAY, "holiday", "New", 43)
    classifications = iter([old_holiday, new_holiday])
    schedule_writes = []

    monkeypatch.setattr(
        staffing_routes.optional_workday,
        "for_day",
        lambda _day: next(classifications),
    )
    monkeypatch.setattr(
        staffing_routes.saturday_recruiting_store,
        "lock_for_schedule_mutation",
        lambda _day, *, cur: None,
    )
    monkeypatch.setattr(
        staffing_routes.staffing,
        "ensure_schedule_for_update",
        lambda *_args, **_kwargs: schedule_writes.append(True),
    )

    @contextmanager
    def transaction():
        yield object()

    monkeypatch.setattr(staffing_routes.db, "cursor", transaction)

    with pytest.raises(staffing_routes._ScheduleMetadataConflict):
        staffing_routes._update_schedule_metadata_work(DAY, testing_day=False)

    assert schedule_writes == []


@pytest.mark.parametrize(
    ("status", "winner"),
    [
        (
            "cancelled",
            _schedule(
                published=False,
                assignments={},
                assignment_sources={},
                saturday_availability_overrides={"Jordan": "off"},
            ),
        ),
        (
            "closed",
            _schedule(
                published=True,
                assignments={"Repair 2": ["Taylor"]},
                assignment_sources={"Repair 2": {"Taylor": "generated"}},
                published_delivery={"version": "activated-v1"},
                saturday_availability_overrides={"Taylor": "unassigned"},
            ),
        ),
    ],
)
def test_metadata_update_preserves_cancellation_or_activation_winner(
    monkeypatch,
    status,
    winner,
):
    from zira_dashboard import optional_workday

    holiday = optional_workday.OptionalWorkday(DAY, "holiday", "Plant Holiday", 42)
    bundle = SimpleNamespace(
        recruitment=SimpleNamespace(
            day=DAY,
            day_kind="holiday",
            holiday_odoo_id=42,
            status=status,
        )
    )
    statements = []

    class Cursor:
        def execute(self, sql, params=()):
            statements.append(" ".join(sql.split()).lower())

    cursor = Cursor()
    monkeypatch.setattr(
        staffing_routes.optional_workday,
        "for_day",
        lambda _day: holiday,
    )
    monkeypatch.setattr(
        staffing_routes.saturday_recruiting_store,
        "lock_for_schedule_mutation",
        lambda _day, *, cur: bundle,
    )
    monkeypatch.setattr(
        staffing_routes.staffing,
        "ensure_schedule_for_update",
        lambda _day, *, cur, initial_auto_enabled_work_centers=(): winner,
    )
    monkeypatch.setattr(
        staffing_routes.staffing,
        "invalidate_schedule_cache",
        lambda _day: None,
    )

    @contextmanager
    def transaction():
        yield cursor

    monkeypatch.setattr(staffing_routes.db, "cursor", transaction)

    updated = staffing_routes._update_schedule_metadata_work(
        DAY,
        custom_hours={"start": "07:00", "end": "15:30", "breaks": []},
    )

    assert updated.assignments == winner.assignments
    assert updated.assignment_sources == winner.assignment_sources
    assert updated.saturday_availability_overrides == winner.saturday_availability_overrides
    assert len(statements) == 1
    assert "schedule_assignments" not in statements[0]
    assert "assignment_sources" not in statements[0]
    assert "saturday_availability_overrides" not in statements[0]


def test_hours_route_uses_locked_field_only_update(monkeypatch):
    captured = {}

    def update(day, **kwargs):
        captured["day"] = day
        captured.update(kwargs)
        return _schedule(custom_hours=kwargs["custom_hours"])

    monkeypatch.setattr(
        staffing_routes,
        "_update_schedule_metadata_work",
        update,
    )
    monkeypatch.setattr(
        staffing_routes.staffing,
        "load_schedule",
        lambda *_args: pytest.fail("read a stale cached schedule"),
    )
    monkeypatch.setattr(
        staffing_routes.staffing,
        "save_schedule",
        lambda *_args, **_kwargs: pytest.fail("performed a whole-schedule save"),
    )
    monkeypatch.setattr(
        staffing_routes.staffing,
        "schedule_revision",
        lambda _day: None,
    )
    monkeypatch.setattr(
        staffing_routes,
        "_default_auto_work_centers",
        lambda _day: ["Repair 1", "Repair 2"],
    )
    monkeypatch.setattr(
        staffing_routes._http_cache,
        "invalidate_today_cache",
        lambda: None,
    )

    response = asyncio.run(
        staffing_routes.staffing_hours_save(
            _FormRequest(
                {
                    "day": DAY.isoformat(),
                    "start": "07:00",
                    "end": "15:30",
                }
            )
        )
    )

    assert response.status_code == 200
    assert captured["day"] == DAY
    assert captured["custom_hours"] == {
        "start": "07:00",
        "end": "15:30",
        "breaks": [],
    }
    assert captured["initial_auto_enabled_work_centers"] == ["Repair 1", "Repair 2"]


def test_clear_testing_route_uses_locked_field_only_update(monkeypatch):
    captured = {}

    def update(day, **kwargs):
        captured["day"] = day
        captured.update(kwargs)
        return _schedule(testing_day=False)

    monkeypatch.setattr(
        staffing_routes,
        "_update_schedule_metadata_work",
        update,
    )
    monkeypatch.setattr(
        staffing_routes.staffing,
        "load_schedule",
        lambda *_args: pytest.fail("read a stale cached schedule"),
    )
    monkeypatch.setattr(
        staffing_routes.staffing,
        "save_schedule",
        lambda *_args, **_kwargs: pytest.fail("performed a whole-schedule save"),
    )
    monkeypatch.setattr(staffing_routes, "_bust_after_mutation", lambda: None)

    class Request:
        async def json(self):
            return {"day": DAY.isoformat()}

    response = asyncio.run(staffing_routes.staffing_clear_testing_day(Request()))

    assert response.status_code == 200
    assert captured == {"day": DAY, "testing_day": False}


@pytest.mark.parametrize(
    ("handler", "report_method"),
    [
        (staffing_routes.staffing_clear_partial, "clear_partial_by_name"),
        (staffing_routes.staffing_restore_partial, "restore_partial_by_name"),
    ],
)
def test_partial_routes_share_locked_schedule_transaction(
    monkeypatch,
    handler,
    report_method,
):
    cursor = object()
    captured = {}
    report_calls = []

    def update(day, **kwargs):
        captured["day"] = day
        captured.update(kwargs)
        kwargs["related_mutation"](cursor)
        return _schedule()

    monkeypatch.setattr(
        staffing_routes,
        "_update_schedule_metadata_work",
        update,
    )
    monkeypatch.setattr(
        staffing_routes.staffing,
        "load_schedule",
        lambda *_args: pytest.fail("read a stale cached schedule"),
    )
    monkeypatch.setattr(
        staffing_routes.staffing,
        "save_schedule",
        lambda *_args, **_kwargs: pytest.fail("performed a whole-schedule save"),
    )
    monkeypatch.setattr(
        staffing_routes.late_report,
        report_method,
        lambda day, name, *, cur: report_calls.append((day, name, cur)),
    )
    monkeypatch.setattr(staffing_routes, "_bust_after_mutation", lambda: None)

    class Request:
        async def json(self):
            return {"day": DAY.isoformat(), "name": "Jordan"}

    response = asyncio.run(handler(Request()))

    assert response.status_code == 200
    assert captured["day"] == DAY
    assert report_calls == [(DAY, "Jordan", cursor)]


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
        name,
        "Repair",
        "Bay 1",
        "Recycled",
        None,
        min_ops=min_ops,
        max_ops=min_ops,
    )


def _capture_publish(monkeypatch, locs, existing=None):
    saved = []
    existing = existing or staffing.Schedule(day=DAY, published=False, assignments={})
    monkeypatch.setattr(staffing_routes.staffing, "LOCATIONS", tuple(locs))
    monkeypatch.setattr(
        staffing_routes.work_centers_store,
        "min_ops",
        lambda loc: loc.min_ops,
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
        SimpleNamespace(headers={}),
        DAY,
        0,
        FormData(
            [
                ("action", "publish"),
                ("loc__Hand Build #1", "Jordan"),
                ("override", "1"),
            ]
        ),
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
        SimpleNamespace(headers={}),
        DAY,
        0,
        FormData({"action": "publish"}),
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
    monkeypatch.setattr(
        staffing_routes.staffing,
        "new_published_delivery",
        lambda: {"version": "v2"},
    )

    response = staffing_routes._staffing_save_work(
        SimpleNamespace(headers={}),
        DAY,
        0,
        FormData(
            [
                ("action", "publish"),
                ("loc__Hand Build #1", "Jordan"),
                ("loc__Hand Build #1", "Taylor"),
            ]
        ),
    )

    assert response.status_code == 303
    assert response.headers["location"] == f"/staffing?day={DAY.isoformat()}"
    assert saved[0].published is True
    assert saved[0].published_delivery == {"version": "v2"}


def test_json_publish_below_minimum_returns_conflict_with_shortages(monkeypatch):
    pair = _publish_location("Hand Build #1", min_ops=2)
    saved = _capture_publish(monkeypatch, [pair])

    response = staffing_routes._staffing_save_work(
        SimpleNamespace(headers={"accept": "application/json"}),
        DAY,
        0,
        FormData([("action", "publish"), ("loc__Hand Build #1", "Jordan")]),
    )

    assert response.status_code == 409
    assert (
        response.body
        == (
            '{"ok":false,"error":"Publish blocked — staff every work center to its minimum.",'
            '"publish_block_reasons":["Hand Build #1 requires 2 operators — currently 1."]}'
        ).encode()
    )
    assert saved[0].published is False


def test_failed_republish_preserves_the_posted_version_as_a_snapshot(monkeypatch):
    pair = _publish_location("Hand Build #1", min_ops=2)
    posted = staffing.Schedule(
        day=DAY,
        published=True,
        assignments={"Hand Build #1": ["Jordan", "Taylor"]},
    )
    saved = _capture_publish(monkeypatch, [pair], existing=posted)
    monkeypatch.setattr(
        staffing_routes.staffing,
        "new_published_delivery",
        lambda: (_ for _ in ()).throw(AssertionError("failed publish must not create a version")),
    )

    staffing_routes._staffing_save_work(
        SimpleNamespace(headers={}),
        DAY,
        0,
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
        SimpleNamespace(headers={}),
        DAY,
        0,
        _save_form("save", notes="draft note"),
    )

    assert saved[0].published is False
    assert saved[0].notes == "draft note"
    assert saved[0].published_delivery == {}
    assert saved[0].published_snapshot["published_delivery"]["version"] == "v1"


def test_regular_save_drops_sources_for_people_removed_from_schedule(monkeypatch):
    saved = _capture_route_save(monkeypatch, _schedule())

    staffing_routes._staffing_save_work(
        SimpleNamespace(headers={}),
        DAY,
        0,
        _save_form("save", notes="updated"),
    )

    assert saved[0].rotation_mode == "training"
    assert saved[0].assignments == {}
    assert saved[0].assignment_sources == {}


def test_regular_save_preserves_source_for_person_still_assigned(monkeypatch):
    repair_1 = next(loc for loc in staffing.LOCATIONS if loc.name == "Repair 1")
    saved = _capture_route_save(monkeypatch, _schedule())
    monkeypatch.setattr(staffing_routes.staffing, "LOCATIONS", (repair_1,))

    staffing_routes._staffing_save_work(
        SimpleNamespace(headers={}),
        DAY,
        0,
        _save_form("save", **{"loc__Repair 1": "Jordan"}),
    )

    assert saved[0].assignments == {"Repair 1": ["Jordan"]}
    assert saved[0].assignment_sources == SOURCES


def test_first_normal_save_of_published_schedule_snapshots_and_starts_draft(monkeypatch):
    existing = _schedule(published=True, notes="posted")
    saved = _capture_route_save(monkeypatch, existing)

    staffing_routes._staffing_save_work(
        SimpleNamespace(headers={}),
        DAY,
        0,
        _save_form("save", notes="draft update"),
    )

    assert saved[0].published is False
    assert saved[0].published_snapshot == staffing.snapshot_of(existing)
    assert saved[0].notes == "draft update"


def test_posted_snapshot_rejects_ordinary_save_without_persisting(monkeypatch):
    saved = _capture_route_save(monkeypatch, _schedule())

    response = staffing_routes._staffing_save_work(
        SimpleNamespace(headers={}),
        DAY,
        0,
        _save_form("save", viewing_posted="1", notes="should not save"),
    )

    assert response.status_code == 400
    assert saved == []


def test_clear_testing_day_starts_draft_and_preserves_rotation_metadata(monkeypatch):
    updated = []
    existing = _schedule(
        published=True,
        testing_day=True,
        published_delivery={"version": "v1"},
    )

    class Cursor:
        def execute(self, *_args):
            pass

    def update(_day, **kwargs):
        result = staffing.update_locked_schedule_metadata(
            existing,
            cur=Cursor(),
            **kwargs,
        )
        updated.append(result)
        return result

    monkeypatch.setattr(staffing_routes, "_update_schedule_metadata_work", update)
    monkeypatch.setattr(staffing_routes, "_bust_after_mutation", lambda: None)

    class Request:
        async def json(self):
            return {"day": DAY.isoformat()}

    response = asyncio.run(staffing_routes.staffing_clear_testing_day(Request()))

    assert response.status_code == 200
    assert updated[0].published is False
    assert updated[0].published_delivery == {}
    assert updated[0].published_snapshot["published_delivery"]["version"] == "v1"
    assert updated[0].rotation_mode == "training"
    assert updated[0].assignment_sources == SOURCES


@pytest.mark.parametrize(
    ("handler", "report_method"),
    [
        (staffing_routes.staffing_clear_partial, "clear_partial_by_name"),
        (staffing_routes.staffing_restore_partial, "restore_partial_by_name"),
    ],
)
def test_partial_time_off_mutation_starts_draft(monkeypatch, handler, report_method):
    updated = []
    posted = _schedule(published=True, published_delivery={"version": "v1"})

    class Cursor:
        def execute(self, *_args):
            pass

    def update(_day, **kwargs):
        kwargs["related_mutation"](Cursor())
        result = staffing.update_locked_schedule_metadata(posted, cur=Cursor())
        updated.append(result)
        return result

    monkeypatch.setattr(staffing_routes, "_update_schedule_metadata_work", update)
    monkeypatch.setattr(
        staffing_routes.late_report,
        report_method,
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(staffing_routes, "_bust_after_mutation", lambda: None)

    class Request:
        async def json(self):
            return {"day": DAY.isoformat(), "name": "Jordan"}

    response = asyncio.run(handler(Request()))

    assert response.status_code == 200
    assert updated[0].published is False
    assert updated[0].published_delivery == {}
    assert updated[0].published_snapshot["published_delivery"]["version"] == "v1"


class _FormRequest:
    def __init__(self, values):
        self._values = FormData(values)

    async def form(self):
        return self._values


def test_hours_save_on_posted_schedule_starts_draft(monkeypatch):
    updated = []
    posted = staffing.Schedule(day=DAY, published=True, published_delivery={"version": "v1"})
    monkeypatch.setattr(staffing_routes.staffing, "schedule_revision", lambda _day: "r1")

    class Cursor:
        def execute(self, *_args):
            pass

    def update(_day, **kwargs):
        kwargs.pop("initial_auto_enabled_work_centers")
        result = staffing.update_locked_schedule_metadata(
            posted,
            cur=Cursor(),
            **kwargs,
        )
        updated.append(result)
        return result

    monkeypatch.setattr(staffing_routes, "_update_schedule_metadata_work", update)
    monkeypatch.setattr(staffing_routes._http_cache, "invalidate_today_cache", lambda: None)

    response = asyncio.run(
        staffing_routes.staffing_hours_save(
            _FormRequest(
                {
                    "day": DAY.isoformat(),
                    "start": "06:00",
                    "end": "12:00",
                }
            )
        )
    )

    assert response.status_code == 200
    assert updated[0].published is False
    assert updated[0].published_snapshot["published_delivery"]["version"] == "v1"


def test_hours_save_on_new_day_starts_with_default_work_centers(monkeypatch):
    captured = {}
    monkeypatch.setattr(staffing_routes.staffing, "schedule_revision", lambda _day: None)
    monkeypatch.setattr(
        staffing_routes,
        "_default_auto_work_centers",
        lambda _day: ["Repair 1", "Repair 2"],
    )
    monkeypatch.setattr(
        staffing_routes,
        "_update_schedule_metadata_work",
        lambda _day, **kwargs: captured.update(kwargs) or staffing.Schedule(day=DAY),
    )
    monkeypatch.setattr(staffing_routes._http_cache, "invalidate_today_cache", lambda: None)

    response = asyncio.run(
        staffing_routes.staffing_hours_save(
            _FormRequest(
                {
                    "day": DAY.isoformat(),
                    "start": "06:00",
                    "end": "12:00",
                }
            )
        )
    )

    assert response.status_code == 200
    assert captured["initial_auto_enabled_work_centers"] == ["Repair 1", "Repair 2"]


def test_hours_save_on_new_day_copies_previous_working_day_centers(monkeypatch):
    captured = {}
    today = date(2026, 7, 13)
    monkeypatch.setattr(
        staffing_routes.staffing,
        "schedule_revision",
        lambda d: "rev" if d == today else None,
    )
    monkeypatch.setattr(
        staffing_routes.optional_workday,
        "previous_normal_workday",
        lambda *_args, **_kwargs: today,
    )
    monkeypatch.setattr(
        staffing_routes.staffing,
        "load_schedule",
        lambda d: staffing.Schedule(
            day=d,
            auto_enabled_work_centers=["Trim Saw 1", "Repair 1"] if d == today else [],
        ),
    )
    monkeypatch.setattr(
        staffing_routes,
        "_default_auto_work_centers",
        lambda _day: [],
    )
    monkeypatch.setattr(
        staffing_routes.schedule_store,
        "current",
        lambda: SimpleNamespace(work_weekdays=frozenset({0, 1, 2, 3, 4})),
    )
    monkeypatch.setattr(
        staffing_routes,
        "_update_schedule_metadata_work",
        lambda _day, **kwargs: captured.update(kwargs) or staffing.Schedule(day=DAY),
    )
    monkeypatch.setattr(staffing_routes._http_cache, "invalidate_today_cache", lambda: None)

    response = asyncio.run(
        staffing_routes.staffing_hours_save(
            _FormRequest(
                {
                    "day": DAY.isoformat(),
                    "start": "06:00",
                    "end": "12:00",
                }
            )
        )
    )

    assert response.status_code == 200
    assert captured["initial_auto_enabled_work_centers"] == ["Repair 1", "Trim Saw 1"]


def test_hours_save_preserves_existing_work_center_selection(monkeypatch):
    captured = {}
    monkeypatch.setattr(staffing_routes.staffing, "schedule_revision", lambda _day: "r1")
    monkeypatch.setattr(
        staffing_routes,
        "_default_auto_work_centers",
        lambda _day: ["Repair 1", "Repair 2"],
    )
    monkeypatch.setattr(
        staffing_routes,
        "_update_schedule_metadata_work",
        lambda _day, **kwargs: captured.update(kwargs) or staffing.Schedule(day=DAY),
    )
    monkeypatch.setattr(staffing_routes._http_cache, "invalidate_today_cache", lambda: None)

    response = asyncio.run(
        staffing_routes.staffing_hours_save(
            _FormRequest(
                {
                    "day": DAY.isoformat(),
                    "start": "06:00",
                    "end": "12:00",
                }
            )
        )
    )

    assert response.status_code == 200
    assert captured["initial_auto_enabled_work_centers"] == ()


def test_json_save_includes_lifecycle_fields(monkeypatch):
    saved = _capture_route_save(
        monkeypatch,
        _schedule(published=True, published_delivery={"version": "v1"}),
    )
    monkeypatch.setattr(staffing_routes.staffing, "schedule_revision", lambda _day: "r1")

    response = staffing_routes._staffing_save_work(
        SimpleNamespace(headers={"accept": "application/json"}),
        DAY,
        0,
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
    draft_auto_enabled_work_centers = ["Repair 2"]
    posted_auto_enabled_work_centers = ["Repair 1"]
    cached = staffing.Schedule(
        day=DAY,
        published=False,
        assignments={"Repair 1": ["Jordan"]},
        rotation_mode="training",
        assignment_sources=draft_sources,
        auto_enabled_work_centers=draft_auto_enabled_work_centers,
        published_snapshot={
            "assignments": {"Repair 1": ["Taylor"]},
            "notes": "posted",
            "wc_notes": {},
            "testing_day": False,
            "rotation_mode": "normal",
            "assignment_sources": posted_sources,
            "auto_enabled_work_centers": posted_auto_enabled_work_centers,
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
    monkeypatch.setattr(
        staffing_routes.shift_config,
        "configured_shift_start_for",
        lambda _d, **_kwargs: time(7, 0),
    )
    monkeypatch.setattr(
        staffing_routes.shift_config,
        "configured_shift_end_for",
        lambda _d, **_kwargs: time(15, 30),
    )
    monkeypatch.setattr(
        staffing_routes.shift_config,
        "configured_breaks_for",
        lambda _d, **_kwargs: [],
    )
    monkeypatch.setattr(
        staffing_routes.shift_config,
        "scheduler_hours_source",
        lambda *_args, **_kwargs: "weekday_default",
    )
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
            "bays": [],
            "publish_block_reasons": [],
            "defaults_by_loc": {},
            "unassigned": [],
            "reserves": [],
            "time_off_names": [],
            "time_off_entries": [],
            "partial_hours_by_name": {},
            "partial_range_by_name": {},
            "partial_clear_by_name": {},
            "people_meta": {},
            "all_active_people": [],
        },
    )
    captured_context = {}

    def render(_request, _template, context):
        captured_context.update(context)
        return type("Response", (), {"headers": {}})()

    monkeypatch.setattr(
        staffing_routes,
        "templates",
        type(
            "Templates",
            (),
            {
                "TemplateResponse": staticmethod(render),
            },
        )(),
    )

    staffing_routes.staffing_page(
        request=object(),
        day=DAY.isoformat(),
        publish_blocked=0,
        view="posted",
    )

    assert staffing.load_schedule(DAY).rotation_mode == "training"
    assert staffing.load_schedule(DAY).assignment_sources == draft_sources
    assert staffing.load_schedule(DAY).auto_enabled_work_centers == draft_auto_enabled_work_centers
    assert captured_context["sched"].auto_enabled_work_centers == posted_auto_enabled_work_centers
    assert captured_context["sched"].custom_hours == {
        "start": "06:00",
        "end": "12:00",
        "breaks": [],
    }
    assert captured_context["posted_delivery"] == {"version": "v1", "printed_at": "now"}
    assert captured_context["posted_version"] == "v1"
    assert captured_context["schedule_revision"] == "r1"

    monkeypatch.setattr(staffing, "save_schedule", saved.append)
    monkeypatch.setattr(staffing_routes.staffing, "LOCATIONS", (repair_1,))
    staffing_routes._staffing_save_work(
        SimpleNamespace(headers={}),
        DAY,
        0,
        _save_form("save", notes="draft update", **{"loc__Repair 1": "Jordan"}),
    )

    assert saved[0].rotation_mode == "training"
    assert saved[0].assignment_sources == draft_sources
    assert saved[0].auto_enabled_work_centers == draft_auto_enabled_work_centers


def test_posted_view_uses_daily_auto_centers_when_legacy_snapshot_omits_them():
    legacy_snapshot = {"assignments": {"Repair 1": ["Taylor"]}}

    assert staffing_routes._posted_auto_enabled_work_centers(
        legacy_snapshot,
        ["Repair 2"],
    ) == ["Repair 2"]
