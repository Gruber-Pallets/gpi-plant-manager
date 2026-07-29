"""Closed-holiday rendering contracts for the existing Staffing scheduler."""

from __future__ import annotations

import json
from datetime import date, datetime, time
from pathlib import Path
from types import SimpleNamespace

import pytest
from starlette.datastructures import FormData

from zira_dashboard import (
    company_holidays,
    optional_workday,
    saturday_schedule_store,
    saturday_recruiting_store,
    staffing,
    work_centers_store,
)
from zira_dashboard.routes import staffing as staffing_routes
from zira_dashboard.shift_config import SITE_TZ


ROOT = Path(__file__).resolve().parents[1]
TODAY = date(2026, 11, 25)
THANKSGIVING = date(2026, 11, 26)
BLACK_FRIDAY = date(2026, 11, 27)
SATURDAY_HOLIDAY = date(2026, 11, 28)


def _holiday(
    day: date = BLACK_FRIDAY,
    *,
    name: str = "Black Friday",
    odoo_id: int = 42,
) -> optional_workday.OptionalWorkday:
    return optional_workday.OptionalWorkday(day, "holiday", name, odoo_id)


def _person(name: str, *, reserve: bool = False) -> staffing.Person:
    return staffing.Person(
        name=name,
        active=True,
        reserve=reserve,
        skills={"Repair": 3},
    )


def _bundle(
    *,
    day_kind: str = "holiday",
    holiday_odoo_id: int | None = 42,
    status: str = "closed",
) -> saturday_recruiting_store.RecruitmentBundle:
    return saturday_recruiting_store.RecruitmentBundle(
        recruitment=saturday_recruiting_store.Recruitment(
            day=BLACK_FRIDAY,
            status=status,
            shift_start=time(6),
            shift_end=time(12),
            response_deadline=datetime(2026, 11, 25, 14, tzinfo=SITE_TZ),
            day_kind=day_kind,
            event_name="Black Friday" if day_kind == "holiday" else "Saturday",
            holiday_odoo_id=holiday_odoo_id,
        ),
        openings=(),
        commitments=(
            saturday_recruiting_store.StoredCommitment(
                person_id=1,
                person_odoo_id=101,
                person_name="Volunteer",
                status="committed",
                availability_start=time(6),
                availability_end=time(12),
                eligible_wc_ids=frozenset(),
            ),
        ),
    )


def _patch_holiday_save(
    monkeypatch,
    *,
    bundle: saturday_recruiting_store.RecruitmentBundle | None,
    schedule: staffing.Schedule | None = None,
):
    repair = staffing.Location(
        "Repair 1",
        "Repair",
        "Bay 1",
        "Recycled",
        None,
        min_ops=1,
        max_ops=2,
        required_skills=("Repair",),
    )
    saved: list[staffing.Schedule] = []
    marked: list[tuple[date, datetime]] = []
    default_updates: list[tuple[object, dict]] = []
    current = schedule or staffing.Schedule(day=BLACK_FRIDAY, assignments={})

    monkeypatch.setattr(staffing_routes.staffing, "LOCATIONS", (repair,))
    monkeypatch.setattr(optional_workday, "for_day", lambda _day: _holiday())
    monkeypatch.setattr(
        staffing_routes.saturday_recruiting_store,
        "get",
        lambda _day: bundle,
    )
    monkeypatch.setattr(staffing_routes.staffing, "load_schedule", lambda _day: current)
    monkeypatch.setattr(staffing_routes.staffing, "save_schedule", saved.append)
    monkeypatch.setattr(
        staffing_routes.staffing,
        "load_roster",
        lambda: [_person("Volunteer"), _person("Corrected"), _person("Other")],
    )
    monkeypatch.setattr(staffing_routes.staffing, "schedule_revision", lambda _day: "saved")
    monkeypatch.setattr(staffing_routes, "_safe_time_off_entries", lambda _day: [])
    monkeypatch.setattr(staffing_routes._http_cache, "invalidate_today_cache", lambda: None)
    monkeypatch.setattr(
        staffing_routes.work_centers_store,
        "save_one",
        lambda loc, values: default_updates.append((loc, values)),
    )
    monkeypatch.setattr(
        staffing_routes.saturday_recruiting_store,
        "mark_published",
        lambda day, now: marked.append((day, now)) or bundle,
    )
    monkeypatch.setattr(
        staffing_routes,
        "plant_now",
        lambda: datetime(2026, 11, 27, 8, tzinfo=SITE_TZ),
    )
    return current, saved, marked, default_updates


def _empty_bay_model() -> dict:
    return {
        "bays": [],
        "publish_block_reasons": [],
        "defaults_by_loc": {},
        "unassigned": [],
        "off": [],
        "reserves": [],
        "time_off_names": [],
        "time_off_entries": [],
        "partial_hours_by_name": {},
        "partial_range_by_name": {},
        "partial_clear_by_name": {},
        "people_meta": {},
        "all_active_people": [],
    }


def _render_staffing(
    monkeypatch,
    *,
    day: date = BLACK_FRIDAY,
    optional_day=None,
    synced: bool | Exception = True,
    schedule: staffing.Schedule | None = None,
    bundle: saturday_recruiting_store.RecruitmentBundle | None = None,
    roster: list[staffing.Person] | None = None,
    bay_model: dict | None = None,
    classifier_calls: list[date] | None = None,
    patch_shift_config: bool = True,
    real_bay_model: bool = False,
    prepare_closed=None,
):
    """Render one scheduler page with every external read replaced."""
    from zira_dashboard import cert_lookup, staffing_view

    schedule = schedule or staffing.Schedule(
        day=day,
        published=False,
        assignments={},
        auto_enabled_work_centers=["Repair 1"],
    )
    roster = list(roster or [])
    captured: dict = {}
    bay_calls: list[dict] = []
    created: list[staffing.Schedule] = []

    monkeypatch.setattr(staffing_routes, "plant_today", lambda: TODAY)
    monkeypatch.setattr(
        staffing_routes, "plant_now", lambda: datetime(2026, 11, 25, 8, tzinfo=SITE_TZ)
    )
    monkeypatch.setattr(staffing_routes, "_next_working_day", lambda _day: BLACK_FRIDAY)
    monkeypatch.setattr(
        staffing_routes.schedule_store,
        "current",
        lambda: SimpleNamespace(work_weekdays=frozenset({0, 1, 2, 3, 4})),
    )
    monkeypatch.setattr(
        staffing_routes._http_cache, "get_cached_response", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(
        staffing_routes._http_cache, "set_cache_headers", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(
        staffing_routes._http_cache, "store_cached_response", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(staffing_routes._http_cache, "invalidate_today_cache", lambda: None)
    monkeypatch.setattr(
        staffing_routes.app_settings,
        "get_setting",
        lambda _key: ["Repair 1"],
    )
    monkeypatch.setattr(cert_lookup, "load_person_certs", lambda: {})
    monkeypatch.setattr(staffing, "load_roster", lambda: roster)
    monkeypatch.setattr(staffing, "load_schedule", lambda _day: schedule)
    monkeypatch.setattr(staffing, "schedule_revision", lambda _day: "saved")
    monkeypatch.setattr(
        staffing, "create_schedule_if_absent", lambda draft: created.append(draft) or True
    )
    monkeypatch.setattr(
        staffing,
        "save_schedule",
        lambda *_args, **_kwargs: pytest.fail("viewing a holiday must not rewrite its draft"),
    )
    monkeypatch.setattr(staffing_routes, "_time_off_entries_cached", lambda _day: [])
    monkeypatch.setattr(staffing_routes, "_safe_time_off_entries", lambda _day: [])
    monkeypatch.setattr(
        staffing_routes,
        "_safe_attendance",
        lambda _day, _schedule, _today: {"by_name": {}, "name_to_id": {}},
    )
    monkeypatch.setattr(staffing_routes, "_late_emp_ids", lambda *_args: set())
    monkeypatch.setattr(staffing_routes.attendance, "person_id_to_name", lambda _mapping: {})
    if patch_shift_config:
        monkeypatch.setattr(
            staffing_routes.shift_config,
            "configured_shift_start_for",
            lambda _day, **_kwargs: time(6),
        )
        monkeypatch.setattr(
            staffing_routes.shift_config,
            "configured_shift_end_for",
            lambda _day, **_kwargs: time(12),
        )
        monkeypatch.setattr(
            staffing_routes.shift_config,
            "configured_breaks_for",
            lambda _day, **_kwargs: [],
        )
        monkeypatch.setattr(
            staffing_routes.shift_config,
            "scheduler_hours_source",
            lambda _day, _custom, **_kwargs: "saturday_default",
        )
    else:
        monkeypatch.setattr(
            saturday_schedule_store,
            "current",
            lambda: saturday_schedule_store.DEFAULT,
        )
    monkeypatch.setattr(staffing_routes, "_smart_defaults_for_day", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(
        staffing_routes,
        "_minimum_crew_balance_for_day",
        lambda **_kwargs: SimpleNamespace(
            unassigned_people=0,
            open_minimum_slots=0,
            direction="ready",
            center_count=0,
            slot_delta=0,
            recommended_centers=(),
        ),
    )
    monkeypatch.setattr(
        staffing_routes,
        "_recycled_context_for_day",
        lambda *_args, **_kwargs: {
            "recycled_rotation_mode": "normal",
            "rotation_reasons": {},
            "rotation_reason_codes": {},
            "rotation_warnings": [],
            "rotation_issues": [],
            "active_training_blocks": [],
        },
    )
    monkeypatch.setattr(
        staffing_routes.saturday_recruiting_store,
        "get",
        lambda _day, **_kwargs: bundle,
    )
    monkeypatch.setattr(
        staffing_routes.saturday_recruiting_store,
        "available_positions",
        lambda: [SimpleNamespace(wc_id=1, wc_name="Repair 1")],
    )
    monkeypatch.setattr(
        staffing_routes.saturday_recruiting_store,
        "serialize_bundle",
        lambda current: {
            "coverage": {"requested": 1, "total": 1},
            "commitments": [
                {"name": item.person_name, "status": item.status} for item in current.commitments
            ],
        },
    )
    monkeypatch.setattr(
        staffing_routes,
        "_prepare_closed_saturday_schedule",
        prepare_closed or (lambda *_args, **_kwargs: None),
    )

    def classify(candidate):
        if classifier_calls is not None:
            classifier_calls.append(candidate)
        if isinstance(optional_day, Exception):
            raise optional_day
        return optional_day

    monkeypatch.setattr(optional_workday, "for_day", classify)

    def has_synced():
        if isinstance(synced, Exception):
            raise synced
        return synced

    monkeypatch.setattr(company_holidays, "has_synced", has_synced)

    real_builder = staffing_view.build_staffing_bays
    if real_bay_model:
        location = staffing.Location(
            "Repair 1",
            "Repair",
            "Bay 1",
            "Recycled",
            None,
            min_ops=1,
            max_ops=2,
            required_skills=("Repair",),
        )
        monkeypatch.setattr(staffing, "LOCATIONS", (location,))
        monkeypatch.setattr(work_centers_store, "required_skills", lambda _loc: ["Repair"])
        monkeypatch.setattr(work_centers_store, "min_ops", lambda _loc: 1)
        monkeypatch.setattr(work_centers_store, "max_ops", lambda _loc: 2)
        monkeypatch.setattr(
            work_centers_store,
            "default_people",
            lambda _loc: ["Old default"],
        )

    def build_staffing_bays(*args, **kwargs):
        bay_calls.append(kwargs)
        if real_bay_model:
            return real_builder(*args, **kwargs)
        return dict(bay_model or _empty_bay_model())

    monkeypatch.setattr(staffing_view, "build_staffing_bays", build_staffing_bays)

    class FakeResponse:
        def __init__(self, context):
            self.context = context
            self.headers = {}

    class FakeTemplates:
        def TemplateResponse(self, _request, _template, context):
            captured["context"] = context
            return FakeResponse(context)

    monkeypatch.setattr(staffing_routes, "templates", FakeTemplates())

    staffing_routes.staffing_page(
        request=object(),
        day=day.isoformat(),
        publish_blocked=0,
        view="draft",
    )
    return captured["context"], bay_calls, created


def test_next_working_day_skips_adjacent_mirrored_holidays(monkeypatch):
    monkeypatch.setattr(
        staffing_routes.schedule_store,
        "current",
        lambda: SimpleNamespace(work_weekdays=frozenset({0, 1, 2, 3, 4})),
    )
    monkeypatch.setattr(
        company_holidays,
        "for_day",
        lambda day: SimpleNamespace(name="Closed") if day in {THANKSGIVING, BLACK_FRIDAY} else None,
    )

    assert staffing_routes._next_working_day(TODAY) == date(2026, 11, 30)


def test_first_sync_pause_does_not_create_a_future_draft(monkeypatch):
    existing = staffing.Schedule(day=BLACK_FRIDAY)
    monkeypatch.setattr(company_holidays, "has_synced", lambda: False)
    monkeypatch.setattr(
        staffing,
        "schedule_revision",
        lambda _day: pytest.fail("paused seeding must not inspect draft state"),
    )
    monkeypatch.setattr(
        staffing,
        "create_schedule_if_absent",
        lambda _draft: pytest.fail("paused seeding must not create a draft"),
    )

    assert (
        staffing_routes._seed_new_future_draft(
            BLACK_FRIDAY, TODAY, existing, [_person("Default")], []
        )
        is existing
    )


def test_synced_empty_mirror_keeps_normal_future_default_seeding(monkeypatch):
    existing = staffing.Schedule(day=BLACK_FRIDAY)
    created = []
    monkeypatch.setattr(company_holidays, "has_synced", lambda: True)
    monkeypatch.setattr(optional_workday, "for_day", lambda _day: None)
    monkeypatch.setattr(staffing, "schedule_revision", lambda _day: None)
    monkeypatch.setattr(staffing_routes, "_default_auto_work_centers", lambda _day: ["Repair 1"])
    monkeypatch.setattr(
        staffing_routes,
        "defaults_only_schedule",
        lambda *_args: (
            {"Repair 1": ["Default"]},
            {"Repair 1": {"Default": "default"}},
        ),
    )
    monkeypatch.setattr(
        staffing, "create_schedule_if_absent", lambda draft: created.append(draft) or True
    )

    seeded = staffing_routes._seed_new_future_draft(
        BLACK_FRIDAY, TODAY, existing, [_person("Default")], []
    )

    assert seeded.assignments == {"Repair 1": ["Default"]}
    assert seeded.assignment_sources == {"Repair 1": {"Default": "default"}}
    assert created == [seeded]


@pytest.mark.parametrize(
    "day",
    [
        BLACK_FRIDAY,
        SATURDAY_HOLIDAY,
    ],
)
def test_new_future_holiday_draft_is_blank_even_on_saturday(monkeypatch, day):
    existing = staffing.Schedule(day=day)
    created = []
    monkeypatch.setattr(company_holidays, "has_synced", lambda: True)
    monkeypatch.setattr(optional_workday, "for_day", lambda _day: _holiday(day))
    monkeypatch.setattr(staffing, "schedule_revision", lambda _day: None)
    monkeypatch.setattr(staffing_routes, "_default_auto_work_centers", lambda _day: ["Repair 1"])
    monkeypatch.setattr(
        staffing_routes,
        "defaults_only_schedule",
        lambda *_args: pytest.fail("an optional date must not load weekday defaults"),
    )
    monkeypatch.setattr(
        staffing, "create_schedule_if_absent", lambda draft: created.append(draft) or True
    )

    seeded = staffing_routes._seed_new_future_draft(day, TODAY, existing, [_person("Default")], [])

    assert seeded.assignments == {}
    assert seeded.assignment_sources == {}
    assert seeded.auto_enabled_work_centers == ["Repair 1"]
    assert created == [seeded]


def test_closed_holiday_context_keeps_old_draft_but_renders_volunteer_only(
    monkeypatch,
):
    classifier_calls = []
    old_draft = staffing.Schedule(
        day=BLACK_FRIDAY,
        assignments={"Repair 1": ["Old default"]},
        assignment_sources={"Repair 1": {"Old default": "default"}},
        auto_enabled_work_centers=["Repair 1"],
    )

    context, bay_calls, created = _render_staffing(
        monkeypatch,
        optional_day=_holiday(),
        schedule=old_draft,
        roster=[_person("Old default"), _person("Off worker")],
        classifier_calls=classifier_calls,
        patch_shift_config=False,
    )

    assert classifier_calls == [BLACK_FRIDAY]
    assert context["sched"] is old_draft
    assert old_draft.assignments == {"Repair 1": ["Old default"]}
    assert created == []
    assert bay_calls[-1]["optional_commitments"] == {}
    assert "saturday_commitments" not in bay_calls[-1]
    assert context["is_optional_workday"] is True
    assert context["optional_day_kind"] == "holiday"
    assert context["optional_day_name"] == "Black Friday"
    assert context["optional_day_label"] == "Black Friday"
    assert context["optional_recruiting_label"] == "Holiday recruiting"
    assert context["day_is_saturday"] is False
    assert context["nonstandard_schedule"] is True
    assert context["hours_source"] == "saturday_default"
    assert context["eff_hours_start"] == "06:00"
    assert context["eff_hours_end"] == "12:00"
    assert context["saturday_recruiting"] is None
    assert context["saturday_recruiting_finished"] is False
    assert context["saturday_recruit_enabled_count"] == 1
    assert context["auto_scheduler_available"] is True
    assert context["holiday_sync_warning"] == ""


def test_holiday_recruiting_context_uses_committed_people_and_holiday_lock_copy(
    monkeypatch,
):
    deadline = datetime(2026, 11, 25, 14, tzinfo=SITE_TZ)
    bundle = saturday_recruiting_store.RecruitmentBundle(
        recruitment=saturday_recruiting_store.Recruitment(
            day=BLACK_FRIDAY,
            status="recruiting",
            shift_start=time(6),
            shift_end=time(12),
            response_deadline=deadline,
            day_kind="holiday",
            event_name="Black Friday",
            holiday_odoo_id=42,
        ),
        openings=(),
        commitments=(
            saturday_recruiting_store.StoredCommitment(
                person_id=1,
                person_odoo_id=101,
                person_name="Volunteer",
                status="committed",
                availability_start=time(7),
                availability_end=time(11, 30),
                eligible_wc_ids=frozenset(),
            ),
            saturday_recruiting_store.StoredCommitment(
                person_id=2,
                person_odoo_id=102,
                person_name="Declined",
                status="declined",
                availability_start=None,
                availability_end=None,
                eligible_wc_ids=frozenset(),
            ),
        ),
    )

    context, bay_calls, _created = _render_staffing(
        monkeypatch,
        optional_day=_holiday(),
        bundle=bundle,
        roster=[_person("Volunteer"), _person("Declined")],
    )

    assert bay_calls[-1]["optional_commitments"] == {
        "Volunteer": {"start": time(7), "end": time(11, 30)}
    }
    assert context["saturday_recruiting"] is bundle.recruitment
    assert context["saturday_publish_locked"] is True
    assert context["saturday_publish_lock_message"].startswith(
        "Holiday recruiting stays open until "
    )
    assert context["saturday_response_summary"] == {
        "yes": ["Volunteer"],
        "no": ["Declined"],
        "deciding": [],
    }


def test_saturday_holiday_uses_holiday_display_precedence(monkeypatch):
    context, bay_calls, _created = _render_staffing(
        monkeypatch,
        day=SATURDAY_HOLIDAY,
        optional_day=_holiday(
            SATURDAY_HOLIDAY,
            name="Founders Day",
            odoo_id=84,
        ),
    )

    assert context["day_is_saturday"] is True
    assert context["optional_day_kind"] == "holiday"
    assert context["optional_day_label"] == "Founders Day"
    assert context["optional_recruiting_label"] == "Holiday recruiting"
    assert bay_calls[-1]["optional_commitments"] == {}


def test_holiday_sync_lookup_failure_pauses_seeding_and_warns(monkeypatch):
    context, _bay_calls, created = _render_staffing(
        monkeypatch,
        optional_day=None,
        synced=RuntimeError("sync state unavailable"),
    )

    assert created == []
    assert context["holiday_sync_warning"] == (
        "Odoo holidays have not synced yet. New future drafts are paused."
    )


def test_persistent_classifier_failure_renders_fail_closed_without_reclassification(
    monkeypatch,
):
    classifier_calls = []
    old_draft = staffing.Schedule(
        day=BLACK_FRIDAY,
        assignments={"Repair 1": ["Old default"]},
        assignment_sources={"Repair 1": {"Old default": "default"}},
        auto_enabled_work_centers=["Repair 1"],
    )

    context, bay_calls, created = _render_staffing(
        monkeypatch,
        optional_day=RuntimeError("holiday lookup unavailable"),
        schedule=old_draft,
        roster=[_person("Old default"), _person("Other worker")],
        classifier_calls=classifier_calls,
        patch_shift_config=False,
        real_bay_model=True,
    )

    assert classifier_calls == [BLACK_FRIDAY]
    assert created == []
    assert old_draft.assignments == {"Repair 1": ["Old default"]}
    assert bay_calls[-1]["optional_commitments"] == {}
    assert context["is_optional_workday"] is True
    assert context["optional_day_kind"] is None
    assert context["optional_day_name"] is None
    assert context["optional_day_label"] == "Optional workday"
    assert context["optional_recruiting_label"] == "Optional workday recruiting"
    assert context["day_is_saturday"] is False
    assert context["hours_source"] == "saturday_default"
    assert context["eff_hours_start"] == "06:00"
    assert context["eff_hours_end"] == "12:00"
    assert context["holiday_sync_warning"] == (
        "Odoo holidays have not synced yet. New future drafts are paused."
    )
    assert context["saturday_recruiting"] is None
    assert context["saturday_recruit_enabled_count"] == 0
    assert context["rotation_auto_summary"]["unscheduled_count"] == 0
    assert context["unassigned"] == []
    assert context["off"] == ["Old default", "Other worker"]
    row = context["bays"][0]["rows"][0]
    assert row["assigned"] == []
    assert row["present_assigned"] == []
    assert row["pool"] == []


def test_mismatched_holiday_id_bundle_is_inactive_and_not_prepared(monkeypatch):
    prepared = []

    context, bay_calls, _created = _render_staffing(
        monkeypatch,
        optional_day=_holiday(odoo_id=42),
        bundle=_bundle(holiday_odoo_id=99),
        roster=[_person("Volunteer"), _person("Other worker")],
        real_bay_model=True,
        prepare_closed=lambda *_args, **_kwargs: prepared.append(True),
    )

    assert prepared == []
    assert context["saturday_recruiting"] is None
    assert context["saturday_recruiting_finished"] is False
    assert context["saturday_commitments"] == []
    assert context["saturday_response_summary"] == {
        "yes": [],
        "no": [],
        "deciding": [],
    }
    assert bay_calls[-1]["optional_commitments"] == {}
    assert context["unassigned"] == []
    assert context["off"] == ["Other worker", "Volunteer"]


def test_stale_saturday_bundle_is_inactive_when_holiday_takes_precedence(
    monkeypatch,
):
    prepared = []

    context, bay_calls, _created = _render_staffing(
        monkeypatch,
        optional_day=_holiday(),
        bundle=_bundle(day_kind="saturday", holiday_odoo_id=None),
        roster=[_person("Volunteer"), _person("Other worker")],
        real_bay_model=True,
        prepare_closed=lambda *_args, **_kwargs: prepared.append(True),
    )

    assert prepared == []
    assert context["optional_day_kind"] == "holiday"
    assert context["saturday_recruiting"] is None
    assert context["saturday_recruiting_finished"] is False
    assert context["saturday_commitments"] == []
    assert bay_calls[-1]["optional_commitments"] == {}
    assert context["unassigned"] == []
    assert context["off"] == ["Other worker", "Volunteer"]


def test_existing_scheduler_dom_controls_are_unchanged():
    template = (ROOT / "src/zira_dashboard/templates/staffing.html").read_text()

    assert 'id="staffing-form"' in template
    assert 'class="rotation-controls"' in template
    assert "data-work-center-toggle" in template
    assert 'class="publish-btn saturday-recruit-button"' in template
    assert 'data-saturday-action="activate-from-schedule"' in template
    assert 'id="saturday-publish-lock"' in template
    assert 'class="section saturday-off"' in template
    assert 'class="section unscheduled"' in template


@pytest.mark.parametrize(
    "bundle",
    [
        None,
        _bundle(status="cancelled"),
        _bundle(day_kind="saturday", holiday_odoo_id=None),
        _bundle(holiday_odoo_id=99),
    ],
)
def test_holiday_save_requires_current_matching_recruiting_without_mutation(
    monkeypatch,
    bundle,
):
    _current, saved, _marked, default_updates = _patch_holiday_save(
        monkeypatch,
        bundle=bundle,
    )

    response = staffing_routes._staffing_save_work(
        SimpleNamespace(headers={"accept": "application/json"}),
        BLACK_FRIDAY,
        0,
        FormData([
            ("action", "save"),
            ("loc__Repair 1", "Volunteer"),
            ("defaults_dirty__Repair 1", "1"),
            ("default__Repair 1", "Volunteer"),
        ]),
    )

    assert response.status_code == 409
    assert b"Holiday recruiting is not active for Black Friday" in response.body
    assert saved == []
    assert default_updates == []


def test_holiday_classification_failure_blocks_every_schedule_mutation(monkeypatch):
    _current, saved, _marked, default_updates = _patch_holiday_save(
        monkeypatch,
        bundle=_bundle(),
    )
    monkeypatch.setattr(
        optional_workday,
        "for_day",
        lambda _day: (_ for _ in ()).throw(RuntimeError("mirror unavailable")),
    )

    response = staffing_routes._staffing_save_work(
        SimpleNamespace(headers={"accept": "application/json"}),
        BLACK_FRIDAY,
        0,
        FormData([
            ("action", "save"),
            ("loc__Repair 1", "Volunteer"),
            ("defaults_dirty__Repair 1", "1"),
            ("default__Repair 1", "Volunteer"),
        ]),
    )

    assert response.status_code == 409
    assert json.loads(response.body) == {
        "ok": False,
        "error": (
            "Optional workday state could not be verified. "
            "No schedule changes were saved."
        ),
    }
    assert saved == []
    assert default_updates == []


def test_open_holiday_recruiting_cannot_schedule_people(monkeypatch):
    _current, saved, _marked, _default_updates = _patch_holiday_save(
        monkeypatch,
        bundle=_bundle(status="recruiting"),
    )

    response = staffing_routes._staffing_save_work(
        SimpleNamespace(headers={"accept": "application/json"}),
        BLACK_FRIDAY,
        0,
        FormData([("action", "save"), ("loc__Repair 1", "Volunteer")]),
    )

    assert response.status_code == 409
    assert b"Holiday recruiting must close before scheduling people." in response.body
    assert saved == []


def test_closed_holiday_save_uses_effective_volunteers_and_date_aware_error(
    monkeypatch,
):
    existing = staffing.Schedule(
        day=BLACK_FRIDAY,
        assignments={},
        saturday_availability_overrides={
            "Volunteer": "off",
            "Corrected": "unassigned",
        },
    )
    _current, saved, _marked, _default_updates = _patch_holiday_save(
        monkeypatch,
        bundle=_bundle(),
        schedule=existing,
    )

    rejected = staffing_routes._staffing_save_work(
        SimpleNamespace(headers={"accept": "application/json"}),
        BLACK_FRIDAY,
        0,
        FormData([("action", "save"), ("loc__Repair 1", "Volunteer")]),
    )
    accepted = staffing_routes._staffing_save_work(
        SimpleNamespace(headers={"accept": "application/json"}),
        BLACK_FRIDAY,
        0,
        FormData([("action", "save"), ("loc__Repair 1", "Corrected")]),
    )

    assert rejected.status_code == 409
    assert b"Only volunteers available for Black Friday can be scheduled." in rejected.body
    assert b"Volunteer is not available for Black Friday." in rejected.body
    assert accepted.status_code == 200
    assert saved[-1].assignments == {"Repair 1": ["Corrected"]}


def test_holiday_publish_requires_recruiting_to_be_closed_even_when_grid_is_empty(
    monkeypatch,
):
    _current, saved, marked, _default_updates = _patch_holiday_save(
        monkeypatch,
        bundle=_bundle(status="recruiting"),
    )

    response = staffing_routes._staffing_save_work(
        SimpleNamespace(headers={"accept": "application/json"}),
        BLACK_FRIDAY,
        0,
        FormData([("action", "publish")]),
    )

    assert response.status_code == 409
    assert b"Holiday recruiting must close before publishing." in response.body
    assert saved == []
    assert marked == []


def test_successful_holiday_publish_marks_schedule_and_recruiting(monkeypatch):
    _current, saved, marked, _default_updates = _patch_holiday_save(
        monkeypatch,
        bundle=_bundle(),
    )

    response = staffing_routes._staffing_save_work(
        SimpleNamespace(headers={"accept": "application/json"}),
        BLACK_FRIDAY,
        0,
        FormData([("action", "publish"), ("loc__Repair 1", "Volunteer")]),
    )

    assert response.status_code == 200
    assert saved[-1].published is True
    assert [item[0] for item in marked] == [BLACK_FRIDAY]


def test_holiday_publish_marker_failure_reports_closed_partial_state(monkeypatch):
    _current, saved, _marked, _default_updates = _patch_holiday_save(
        monkeypatch,
        bundle=_bundle(),
    )
    monkeypatch.setattr(
        staffing_routes.saturday_recruiting_store,
        "mark_published",
        lambda *_args: (_ for _ in ()).throw(RuntimeError("marker unavailable")),
    )

    response = staffing_routes._staffing_save_work(
        SimpleNamespace(headers={"accept": "application/json"}),
        BLACK_FRIDAY,
        0,
        FormData([("action", "publish"), ("loc__Repair 1", "Volunteer")]),
    )

    monkeypatch.setattr(optional_workday, "_publication_state", lambda _day: SimpleNamespace(
        day_kind="holiday",
        holiday_odoo_id=42,
        status="closed",
    ))
    monkeypatch.setattr(staffing, "load_schedule", lambda _day: saved[-1])

    assert response.status_code == 503
    assert b"Holiday work is still closed" in response.body
    assert saved[-1].published is True
    assert optional_workday.state_for_day(BLACK_FRIDAY).operational is False


def test_holiday_availability_correction_uses_matching_active_recruiting(
    monkeypatch,
):
    _current, saved, _marked, _default_updates = _patch_holiday_save(
        monkeypatch,
        bundle=_bundle(),
    )
    monkeypatch.setattr(staffing_routes, "_bust_after_mutation", lambda: None)

    result = staffing_routes._set_saturday_availability_work(
        BLACK_FRIDAY,
        "Corrected",
        "unassigned",
    )

    assert result["ok"] is True
    assert saved[-1].saturday_availability_overrides == {"Corrected": "unassigned"}


def test_unrecruited_holiday_rejects_availability_without_mutation(monkeypatch):
    _current, saved, _marked, _default_updates = _patch_holiday_save(
        monkeypatch,
        bundle=None,
    )

    with pytest.raises(
        staffing_routes.HTTPException,
        match="Holiday recruiting is not active for Black Friday",
    ):
        staffing_routes._set_saturday_availability_work(
            BLACK_FRIDAY,
            "Corrected",
            "unassigned",
        )

    assert saved == []
