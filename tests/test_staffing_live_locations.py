"""Planned Staffing seats with a separate canonical Odoo location overlay."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

from jinja2 import Environment

from zira_dashboard import shift_config, staffing, staffing_view, work_centers_store
from zira_dashboard.attendance_timeline import LocationSpan


NOW = datetime(2026, 8, 31, 16, 0, tzinfo=UTC)


def _span(
    name: str,
    status: str,
    *,
    app_wc: str | None = None,
    raw_wc: str | None = None,
    start_minutes: int = 30,
    employee_id: int | None = None,
    end_utc: datetime | None = None,
    attendance_ids: tuple[int, ...] = (91,),
) -> LocationSpan:
    return LocationSpan(
        employee_odoo_id=employee_id or abs(hash(name)) % 10000 + 1,
        employee_name=name,
        start_utc=NOW - timedelta(minutes=start_minutes),
        end_utc=end_utc or NOW + timedelta(minutes=1),
        status=status,
        app_work_center_name=app_wc,
        odoo_work_center_id=8 if (app_wc or raw_wc) else None,
        odoo_work_center_name=raw_wc,
        attendance_ids=attendance_ids,
        department_repair=None,
    )


def test_build_live_locations_keeps_planned_seats_and_adds_unscheduled_workers():
    spans = (
        _span("Alice", "valid", app_wc="Bay 8", raw_wc="Luke Bay 8", employee_id=101),
        _span("Bob", "pending_first_location", employee_id=102),
        _span("Carol", "valid", app_wc="Bay 2", raw_wc="Luke Bay 2", employee_id=103),
    )

    locations = staffing_view.build_live_locations(
        {"Bay 3": ["Alice", "Bob"]},
        spans,
        as_of_utc=NOW,
        planned_employee_ids={"Alice": 101, "Bob": 102},
    )
    by_name = {item.person_name: item for item in locations}

    assert by_name["Alice"].planned_work_center == "Bay 3"
    assert by_name["Alice"].live_work_center == "Bay 8"
    assert by_name["Alice"].working_elsewhere is True
    assert by_name["Bob"].planned_work_center == "Bay 3"
    assert by_name["Bob"].status == "pending_first_location"
    assert by_name["Carol"].planned_work_center is None
    assert by_name["Carol"].live_work_center == "Bay 2"
    assert all(item.source_fresh_at == NOW for item in locations)


def test_build_live_locations_keeps_unknown_conflict_missing_and_exempt_explicit():
    spans = (
        _span("Unknown", "unmapped_location", raw_wc="Odoo Mystery 99"),
        _span("Conflict", "conflicting_location"),
        _span("Missing", "missing_required_location"),
        _span("Driver", "exempt_no_location"),
        _span("Stale", "stale_open_location", raw_wc="Luke Bay 7"),
    )

    by_name = {
        item.person_name: item
        for item in staffing_view.build_live_locations({}, spans, as_of_utc=NOW)
    }

    assert by_name["Unknown"].raw_odoo_work_center == "Odoo Mystery 99"
    assert by_name["Unknown"].display_text == (
        "Odoo Mystery 99 · Odoo only — mapping needed"
    )
    assert by_name["Conflict"].display_text == "Location conflict"
    assert by_name["Missing"].display_text == "Location missing"
    assert by_name["Driver"].display_text == "Outside work-center bays"
    assert by_name["Stale"].display_text == "Luke Bay 7 · stale"


def test_build_live_locations_uses_one_current_span_per_person():
    earlier = _span("Alice", "valid", app_wc="Bay 2", start_minutes=90, employee_id=101)
    earlier = LocationSpan(**{**earlier.__dict__, "end_utc": NOW - timedelta(minutes=30)})
    current = _span("Alice", "valid", app_wc="Bay 8", start_minutes=30, employee_id=101)

    locations = staffing_view.build_live_locations(
        {"Bay 3": ["Alice"]},
        (earlier, current),
        as_of_utc=NOW,
        planned_employee_ids={"Alice": 101},
    )

    assert len(locations) == 1
    assert locations[0].live_work_center == "Bay 8"
    assert locations[0].since_utc == NOW - timedelta(minutes=30)


def test_build_live_locations_keeps_same_name_employee_ids_separate():
    planned = _span("Alex", "valid", app_wc="Bay 3", employee_id=101)
    odoo_only = _span(
        "Alex",
        "unmapped_location",
        raw_wc="Odoo Mystery 99",
        employee_id=202,
    )

    locations = staffing_view.build_live_locations(
        {"Bay 3": ["Alex"]},
        (planned, odoo_only),
        as_of_utc=NOW,
        planned_employee_ids={"Alex": 101},
    )
    by_id = {item.employee_odoo_id: item for item in locations}

    assert len(locations) == 2
    assert by_id[101].planned_work_center == "Bay 3"
    assert by_id[202].planned_work_center is None
    assert by_id[202].display_text.endswith("Odoo only — mapping needed")


def test_duplicate_unknown_raw_names_do_not_collapse():
    spans = (
        _span("Unknown", "unmapped_location", raw_wc="Odoo A", employee_id=202),
        _span("Unknown", "unmapped_location", raw_wc="Odoo B", employee_id=303),
    )

    locations = staffing_view.build_live_locations(
        {}, spans, as_of_utc=NOW, planned_employee_ids={}
    )

    assert [item.employee_odoo_id for item in locations] == [202, 303]
    assert [item.raw_odoo_work_center for item in locations] == ["Odoo A", "Odoo B"]


def test_span_ending_exactly_at_as_of_is_not_live():
    ended = _span(
        "Alice",
        "valid",
        app_wc="Bay 8",
        employee_id=101,
        end_utc=NOW,
    )

    locations = staffing_view.build_live_locations(
        {"Bay 3": ["Alice"]},
        (ended,),
        as_of_utc=NOW,
        planned_employee_ids={"Alice": 101},
    )

    assert locations == ()


def test_exact_cap_current_selection_uses_contributing_raw_interval_identity():
    from zira_dashboard.routes import staffing as staffing_routes

    cap = NOW
    rows = (
        {
            "odoo_attendance_id": 1,
            "check_in_utc": cap - timedelta(hours=1),
            "check_out_utc": None,
        },
        {
            "odoo_attendance_id": 2,
            "check_in_utc": cap - timedelta(hours=1),
            "check_out_utc": cap + timedelta(minutes=1),
        },
        {
            "odoo_attendance_id": 3,
            "check_in_utc": cap - timedelta(hours=1),
            "check_out_utc": cap,
        },
        {
            "odoo_attendance_id": 4,
            "check_in_utc": cap,
            "check_out_utc": None,
        },
    )
    spans = (
        _span(
            "Open",
            "valid",
            app_wc="Bay 1",
            employee_id=101,
            end_utc=cap,
            attendance_ids=(1,),
        ),
        _span(
            "Closes after",
            "valid",
            app_wc="Bay 2",
            employee_id=102,
            end_utc=cap,
            attendance_ids=(2,),
        ),
        _span(
            "Closes exact",
            "valid",
            app_wc="Bay 3",
            employee_id=103,
            end_utc=cap,
            attendance_ids=(3,),
        ),
        _span(
            "Mixed",
            "conflicting_location",
            employee_id=104,
            end_utc=cap,
            attendance_ids=(3, 4),
        ),
    )

    current_attendance_ids = staffing_routes._current_attendance_ids_at(rows, cap)
    locations = staffing_view.build_live_locations(
        {},
        spans,
        as_of_utc=cap,
        current_attendance_ids=current_attendance_ids,
    )

    assert current_attendance_ids == frozenset({1, 2, 4})
    assert {item.employee_odoo_id for item in locations} == {101, 102, 104}
    assert next(item for item in locations if item.employee_odoo_id == 104).status == (
        "conflicting_location"
    )


def test_staffing_snapshot_survives_sync_commit_interleaving(monkeypatch):
    from zira_dashboard import attendance_mirror
    from zira_dashboard.routes import staffing as staffing_routes

    first_row = {
        "odoo_attendance_id": 91,
        "employee_odoo_id": 101,
        "check_in_utc": NOW - timedelta(hours=1),
        "check_out_utc": None,
    }
    calls = []

    def snapshot_overlapping(*_args):
        calls.append(len(calls) + 1)
        return SimpleNamespace(
            health=attendance_mirror.MirrorHealth(
                last_incremental_completed_at=NOW - timedelta(seconds=10),
                last_full_sweep_completed_at=NOW - timedelta(seconds=10),
                baseline_completed_at=NOW - timedelta(seconds=10),
                oldest_recalc_requested_at=None,
                last_error=None,
            ),
            rows=(first_row,),
        )

    monkeypatch.setattr(
        staffing_routes.attendance_location_policy,
        "get_rollout_config",
        lambda: SimpleNamespace(mode="shadow"),
    )
    monkeypatch.setattr(
        staffing_routes.attendance_mirror,
        "snapshot_overlapping",
        snapshot_overlapping,
    )
    monkeypatch.setattr(
        staffing_routes.attendance_timeline,
        "_plant_day_bounds",
        lambda _day: (NOW - timedelta(hours=16), NOW + timedelta(hours=8)),
    )
    monkeypatch.setattr(
        staffing_routes.attendance_timeline,
        "_rows_with_employee_department_fallback",
        lambda rows, **_kwargs: rows,
    )
    monkeypatch.setattr(
        staffing_routes.attendance_timeline,
        "project_rows",
        lambda rows, **_kwargs: (
            _span(
                "Alex",
                "valid",
                app_wc="Bay 3",
                employee_id=rows[0]["employee_odoo_id"],
            ),
        ),
    )
    monkeypatch.setattr(staffing_routes.attendance, "person_id_to_name", lambda: {"101": "Alex"})
    snapshot = staffing_routes._read_staffing_response_snapshot(
        NOW.date(), as_of_utc=NOW
    )

    assert calls == [1]
    assert set(snapshot.attendance_source.payload) == {"101"}
    assert snapshot.spans[0].employee_odoo_id == 101


def test_staffing_response_derives_policy_presence_and_spans_from_atomic_snapshot(
    monkeypatch,
):
    from zira_dashboard import attendance_mirror
    from zira_dashboard.routes import staffing as staffing_routes

    verified_at = NOW - timedelta(seconds=10)
    raw_row = {
        "odoo_attendance_id": 91,
        "employee_odoo_id": 101,
        "check_in_utc": NOW - timedelta(hours=1),
        "check_out_utc": None,
    }
    atomic = SimpleNamespace(
        health=attendance_mirror.MirrorHealth(
            last_incremental_completed_at=verified_at,
            last_full_sweep_completed_at=verified_at,
            baseline_completed_at=verified_at,
            oldest_recalc_requested_at=None,
            last_error=None,
        ),
        rows=(raw_row,),
    )
    atomic_calls = []
    monkeypatch.setattr(
        staffing_routes.attendance_location_policy,
        "get_rollout_config",
        lambda: SimpleNamespace(mode="shadow"),
    )
    monkeypatch.setattr(
        staffing_routes.attendance_mirror,
        "snapshot_overlapping",
        lambda *args: atomic_calls.append(args) or atomic,
        raising=False,
    )
    monkeypatch.setattr(
        staffing_routes.live_cache,
        "attendance_read_policy",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("Staffing must not perform a separate health read")
        ),
    )
    monkeypatch.setattr(
        staffing_routes.attendance_timeline,
        "_plant_day_bounds",
        lambda _day: (NOW - timedelta(hours=16), NOW + timedelta(hours=8)),
    )
    monkeypatch.setattr(
        staffing_routes,
        "_project_staffing_location_spans",
        lambda _day, **_kwargs: (
            _span("Alex", "valid", app_wc="Bay 3", employee_id=101, end_utc=verified_at),
        ),
    )

    snapshot = staffing_routes._read_staffing_response_snapshot(
        NOW.date(), as_of_utc=NOW
    )

    assert len(atomic_calls) == 1
    assert snapshot.policy.mirror_owned is True
    assert snapshot.policy.refreshed_at == verified_at
    assert snapshot.attendance_source.payload["101"]["currently_open"] is True
    assert snapshot.spans[0].employee_odoo_id == 101
    assert snapshot.current_attendance_ids == frozenset({91})


def test_staffing_snapshot_caps_open_span_and_uses_half_open_selection(monkeypatch):
    from zira_dashboard import attendance_mirror
    from zira_dashboard.routes import staffing as staffing_routes

    verified_at = NOW
    health_refreshed_at = NOW + timedelta(seconds=10)
    row = {
        "odoo_attendance_id": 91,
        "employee_odoo_id": 101,
        "check_in_utc": NOW - timedelta(hours=1),
        "check_out_utc": None,
    }
    closed_row = {
        "odoo_attendance_id": 92,
        "employee_odoo_id": 202,
        "check_in_utc": NOW - timedelta(hours=1),
        "check_out_utc": verified_at,
    }
    projected_as_of = []
    monkeypatch.setattr(
        staffing_routes.attendance_timeline,
        "_plant_day_bounds",
        lambda _day: (NOW - timedelta(hours=16), NOW + timedelta(hours=8)),
    )
    monkeypatch.setattr(
        staffing_routes.attendance_location_policy,
        "get_rollout_config",
        lambda: SimpleNamespace(mode="shadow"),
    )
    monkeypatch.setattr(
        staffing_routes.attendance_mirror,
        "snapshot_overlapping",
        lambda *_args: SimpleNamespace(
            health=attendance_mirror.MirrorHealth(
                last_incremental_completed_at=health_refreshed_at,
                last_full_sweep_completed_at=health_refreshed_at,
                baseline_completed_at=health_refreshed_at,
                oldest_recalc_requested_at=None,
                last_error=None,
            ),
            rows=(row, closed_row),
        ),
    )
    monkeypatch.setattr(
        staffing_routes.attendance_timeline,
        "_rows_with_employee_department_fallback",
        lambda rows, **_kwargs: rows,
    )

    def project_rows(_rows, **kwargs):
        projected_as_of.append(kwargs["as_of_utc"])
        span = _span(
            "Alex",
            "valid",
            app_wc="Bay 3",
            employee_id=101,
            attendance_ids=(91,),
        )
        return (
            LocationSpan(
                **{
                    **span.__dict__,
                    "start_utc": NOW - timedelta(hours=1),
                    "end_utc": kwargs["as_of_utc"] + timedelta(minutes=1),
                }
            ),
        )

    monkeypatch.setattr(staffing_routes.attendance_timeline, "project_rows", project_rows)
    monkeypatch.setattr(
        staffing_routes.attendance,
        "person_id_to_name",
        lambda: {"101": "Alex"},
    )
    snapshot = staffing_routes._read_staffing_response_snapshot(
        NOW.date(), as_of_utc=NOW
    )
    closed_at_selection = _span(
        "Closed",
        "valid",
        app_wc="Bay 8",
        employee_id=202,
        end_utc=snapshot.verified_cap_utc,
        attendance_ids=(92,),
    )
    context = staffing_routes._staffing_live_context(
        NOW.date(),
        NOW.date(),
        {"Bay 3": ["Alex"], "Bay 8": ["Closed"]},
        policy=snapshot.policy,
        as_of_utc=snapshot.verified_cap_utc,
        spans=(*snapshot.spans, closed_at_selection),
        planned_employee_ids={"Alex": 101, "Closed": 202},
        current_attendance_ids=snapshot.current_attendance_ids,
    )

    assert projected_as_of == [verified_at]
    assert snapshot.spans[0].end_utc == verified_at
    assert snapshot.verified_cap_utc == verified_at
    assert snapshot.policy.refreshed_at == verified_at
    assert snapshot.attendance_source.refreshed_at == verified_at
    assert snapshot.attendance_source.payload["101"]["currently_open"] is True
    assert snapshot.attendance_source.payload["202"]["currently_open"] is False
    assert snapshot.current_attendance_ids == frozenset({91})
    assert set(context["live_locations_by_employee_id"]) == {101}
    assert context["live_locations_by_employee_id"][101].source_fresh_at == verified_at
    assert context["staffing_live_fresh_at"] == verified_at


def _render_template_fragment(start_marker: str, end_marker: str, **context) -> str:
    source = Path("src/zira_dashboard/templates/staffing.html").read_text()
    start = source.index(start_marker)
    end = source.index(end_marker, start) + len(end_marker)
    return Environment().from_string(source[start:end]).render(**context)


def test_full_day_off_planned_seat_still_renders_contradictory_live_badge(monkeypatch):
    location = staffing.Location("Bay 3", "Repair", "Bay 3", "Recycled", None)
    monkeypatch.setattr(staffing, "LOCATIONS", (location,))
    monkeypatch.setattr(work_centers_store, "required_skills", lambda _loc: [])
    monkeypatch.setattr(work_centers_store, "min_ops", lambda _loc: 1)
    monkeypatch.setattr(work_centers_store, "max_ops", lambda _loc: 1)
    monkeypatch.setattr(work_centers_store, "default_people", lambda _loc: [])
    person = staffing.Person(name="Alice", employee_id=101)
    model = staffing_view.build_staffing_bays(
        roster=[person],
        sched=SimpleNamespace(assignments={"Bay 3": ["Alice"]}, wc_notes={}),
        time_off_entries=[{"name": "Alice", "hours": None}],
        publish_blocked=0,
    )
    row = model["bays"][0]["rows"][0]
    live = SimpleNamespace(
        employee_odoo_id=101,
        status="valid",
        working_elsewhere=True,
        raw_odoo_work_center=None,
        display_text="Bay 8",
    )

    rendered = _render_template_fragment(
        '<div class="planned-live-locations"',
        "</div>",
        row=row,
        staffing_live_label="Live Odoo",
        live_locations_by_employee_id={101: live},
    )

    assert row["present_assigned"] == []
    assert row["hc_status"] == "empty"
    assert row["assigned"][0]["employee_odoo_id"] == 101
    assert "Alice" in rendered
    assert "Working elsewhere · Bay 8" in rendered


def test_staffing_freshness_renders_plant_time_with_utc_datetime_attribute():
    rendered = _render_template_fragment(
        '<div class="staffing-live-banner',
        "</div>",
        staffing_live_enabled=True,
        staffing_live_unavailable=False,
        staffing_live_stale=False,
        staffing_live_error=None,
        staffing_live_label="Live Odoo",
        staffing_live_fresh_at=NOW,
        staffing_live_fresh_local=NOW.astimezone(shift_config.SITE_TZ),
    )

    assert 'datetime="2026-08-31T16:00:00+00:00"' in rendered
    assert "11:00 AM CDT" in rendered
    assert "Plant time" in rendered


def test_staffing_template_has_distinct_shadow_live_and_unavailable_labels():
    template = Path("src/zira_dashboard/templates/staffing.html").read_text()

    assert "Odoo preview" in template
    assert "Live Odoo" in template
    assert "Working elsewhere" in template
    assert "Odoo only — mapping needed" in template
    assert "Live Odoo locations are unavailable" in template
    assert "live-unscheduled" in template


def test_staffing_live_context_uses_injected_frozen_timeline_snapshot(monkeypatch):
    from zira_dashboard.routes import staffing as staffing_routes

    policy = SimpleNamespace(
        mode="shadow",
        mirror_owned=True,
        available=True,
        refreshed_at=NOW - timedelta(seconds=20),
        stale=False,
        error=None,
    )
    spans = (_span("Alice", "valid", app_wc="Bay 8", employee_id=101),)
    monkeypatch.setattr(
        staffing_routes,
        "_project_staffing_location_spans",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("frozen Staffing context must not re-query the timeline")
        ),
    )

    context = staffing_routes._staffing_live_context(
        NOW.date(),
        NOW.date(),
        {"Bay 3": ["Alice"]},
        policy=policy,
        as_of_utc=NOW,
        spans=spans,
        planned_employee_ids={"Alice": 101},
    )

    assert context["staffing_live_label"] == "Odoo preview"
    assert context["staffing_live_fresh_at"] == policy.refreshed_at
    assert context["live_locations_by_employee_id"][101].working_elsewhere is True


def test_staffing_live_context_keeps_last_verified_rows_with_stale_error_label():
    from zira_dashboard.routes import staffing as staffing_routes

    policy = SimpleNamespace(
        mode="live",
        mirror_owned=True,
        available=True,
        refreshed_at=NOW - timedelta(minutes=4),
        stale=True,
        error="incremental sync failed",
    )
    spans = (_span("Alice", "valid", app_wc="Bay 8", employee_id=101),)

    context = staffing_routes._staffing_live_context(
        NOW.date(),
        NOW.date(),
        {"Bay 3": ["Alice"]},
        policy=policy,
        as_of_utc=NOW,
        spans=spans,
        planned_employee_ids={"Alice": 101},
    )

    assert context["staffing_live_unavailable"] is False
    assert context["staffing_live_stale"] is True
    assert context["staffing_live_error"] == "incremental sync failed"
    location = context["live_locations_by_employee_id"][101]
    assert location.status == "valid"
    assert location.source_stale is True
    assert location.display_text == "Bay 8"


def test_stale_source_preserves_every_location_status_and_unmapped_text():
    from zira_dashboard.routes import staffing as staffing_routes

    expected = {
        101: ("valid", "Bay 1"),
        102: ("unmapped_location", "Odoo Mystery 99 · Odoo only — mapping needed"),
        103: ("pending_first_location", "Waiting for Odoo location"),
        104: ("missing_required_location", "Location missing"),
        105: ("conflicting_location", "Location conflict"),
        106: ("exempt_no_location", "Outside work-center bays"),
    }
    spans = (
        _span("Valid", "valid", app_wc="Bay 1", employee_id=101),
        _span(
            "Unmapped",
            "unmapped_location",
            raw_wc="Odoo Mystery 99",
            employee_id=102,
        ),
        _span("Pending", "pending_first_location", employee_id=103),
        _span("Missing", "missing_required_location", employee_id=104),
        _span("Conflict", "conflicting_location", employee_id=105),
        _span("Driver", "exempt_no_location", employee_id=106),
    )
    policy = SimpleNamespace(
        mode="live",
        mirror_owned=True,
        available=True,
        refreshed_at=NOW - timedelta(minutes=4),
        stale=True,
        error="sync stalled",
    )

    context = staffing_routes._staffing_live_context(
        NOW.date(),
        NOW.date(),
        {},
        policy=policy,
        as_of_utc=NOW,
        spans=spans,
    )

    by_id = context["live_locations_by_employee_id"]
    assert {
        employee_id: (item.status, item.display_text)
        for employee_id, item in by_id.items()
    } == expected
    assert all(item.source_stale is True for item in by_id.values())
    assert "Odoo only — mapping needed" in Path(
        "src/zira_dashboard/templates/staffing.html"
    ).read_text()
    assert "Source stale" in Path("src/zira_dashboard/templates/staffing.html").read_text()


def test_unscheduled_identity_links_only_exact_known_local_people():
    planned_by_wc = {"Bay 3": ["Alex"]}
    spans = (
        _span(
            "Alex",
            "unmapped_location",
            raw_wc="Odoo Other Alex",
            employee_id=202,
        ),
        _span("Unknown", "unmapped_location", raw_wc="Odoo A", employee_id=303),
        _span("Unknown", "unmapped_location", raw_wc="Odoo B", employee_id=404),
        _span("Known", "valid", app_wc="Bay 8", employee_id=505),
    )

    locations = staffing_view.build_live_locations(
        planned_by_wc,
        spans,
        as_of_utc=NOW,
        planned_employee_ids={"Alex": 101},
        known_local_people_by_id={101: "Alex", 505: "Known"},
    )
    by_id = {item.employee_odoo_id: item for item in locations}
    rendered = _render_template_fragment(
        '<div class="section live-unscheduled">',
        "</div>",
        staffing_live_enabled=True,
        staffing_live_label="Live Odoo",
        live_unscheduled_locations=locations,
    )

    assert by_id[202].profile_person_name is None
    assert by_id[303].profile_person_name is None
    assert by_id[404].profile_person_name is None
    assert by_id[505].profile_person_name == "Known"
    assert by_id[202].identity_disambiguator == "Odoo employee #202"
    assert by_id[303].identity_disambiguator == "Odoo employee #303"
    assert by_id[404].identity_disambiguator == "Odoo employee #404"
    assert 'href="/staffing/people/Alex"' not in rendered
    assert 'href="/staffing/people/Unknown"' not in rendered
    assert 'href="/staffing/people/Known"' in rendered
    assert 'data-odoo-employee-id="202"' in rendered
    assert 'data-odoo-employee-id="303"' in rendered
    assert 'data-odoo-employee-id="404"' in rendered
    assert "Odoo employee #202" in rendered
    assert "Odoo employee #303" in rendered
    assert "Odoo employee #404" in rendered


def test_staffing_live_context_labels_live_and_preserves_unavailability(monkeypatch):
    from zira_dashboard.routes import staffing as staffing_routes

    unavailable = SimpleNamespace(
        mode="live",
        mirror_owned=True,
        available=False,
        refreshed_at=NOW - timedelta(minutes=4),
        stale=False,
        error="mirror read failed",
    )
    monkeypatch.setattr(
        staffing_routes,
        "_project_staffing_location_spans",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("unavailable source must not project")
        ),
    )

    context = staffing_routes._staffing_live_context(
        NOW.date(), NOW.date(), {}, policy=unavailable, as_of_utc=NOW
    )

    assert context["staffing_live_label"] == "Live Odoo"
    assert context["staffing_live_unavailable"] is True
    assert context["staffing_live_error"] == "mirror read failed"
    assert context["staffing_live_fresh_at"] == unavailable.refreshed_at


def test_projected_staffing_spans_use_canonical_roster_name_by_employee_id(monkeypatch):
    from zira_dashboard.routes import staffing as staffing_routes

    raw_span = _span("Alice Full Odoo Name", "valid", app_wc="Bay 8")
    policy = SimpleNamespace(refreshed_at=NOW - timedelta(seconds=10))
    monkeypatch.setattr(
        staffing_routes.attendance_timeline,
        "_plant_day_bounds",
        lambda _day: (NOW - timedelta(hours=8), NOW + timedelta(hours=8)),
    )
    monkeypatch.setattr(staffing_routes.attendance_mirror, "rows_overlapping", lambda *_args: ({},))
    monkeypatch.setattr(
        staffing_routes.attendance_timeline,
        "_rows_with_employee_department_fallback",
        lambda rows, **_kwargs: rows,
    )
    monkeypatch.setattr(
        staffing_routes.attendance_timeline,
        "project_rows",
        lambda *_args, **_kwargs: (raw_span,),
    )
    monkeypatch.setattr(
        staffing_routes.attendance,
        "person_id_to_name",
        lambda: {str(raw_span.employee_odoo_id): "Alice A."},
    )

    spans = staffing_routes._project_staffing_location_spans(
        NOW.date(), as_of_utc=NOW, policy=policy
    )

    assert spans[0].employee_name == "Alice A."
