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
        attendance_ids=(91,),
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


def test_staffing_snapshot_survives_sync_commit_interleaving(monkeypatch):
    from zira_dashboard.routes import staffing as staffing_routes

    first_row = {
        "employee_odoo_id": 101,
        "check_in_utc": NOW - timedelta(hours=1),
        "check_out_utc": None,
    }
    second_row = {
        "employee_odoo_id": 202,
        "check_in_utc": NOW - timedelta(minutes=10),
        "check_out_utc": None,
    }
    versions = [(first_row,), (second_row,)]
    calls = []

    def rows_overlapping(*_args):
        calls.append(len(calls) + 1)
        return versions.pop(0)

    monkeypatch.setattr(staffing_routes.attendance_mirror, "rows_overlapping", rows_overlapping)
    monkeypatch.setattr(
        staffing_routes.attendance_timeline,
        "_plant_day_bounds",
        lambda _day: (NOW - timedelta(hours=16), NOW + timedelta(hours=8)),
    )
    monkeypatch.setattr(
        staffing_routes.attendance_timeline,
        "_rows_with_employee_department_fallback",
        lambda rows: rows,
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
    policy = SimpleNamespace(
        mode="shadow",
        mirror_owned=True,
        available=True,
        refreshed_at=NOW - timedelta(seconds=10),
        stale=False,
        error=None,
    )

    snapshot = staffing_routes._read_staffing_mirror_snapshot(
        NOW.date(), as_of_utc=NOW, policy=policy
    )

    assert calls == [1]
    assert set(snapshot.attendance_source.payload) == {"101"}
    assert snapshot.spans[0].employee_odoo_id == 101


def test_staffing_snapshot_caps_open_span_and_uses_half_open_selection(monkeypatch):
    from zira_dashboard.routes import staffing as staffing_routes

    verified_at = NOW - timedelta(seconds=10)
    row = {
        "employee_odoo_id": 101,
        "check_in_utc": NOW - timedelta(hours=1),
        "check_out_utc": None,
    }
    projected_as_of = []
    monkeypatch.setattr(
        staffing_routes.attendance_timeline,
        "_plant_day_bounds",
        lambda _day: (NOW - timedelta(hours=16), NOW + timedelta(hours=8)),
    )
    monkeypatch.setattr(
        staffing_routes.attendance_mirror,
        "rows_overlapping",
        lambda *_args: (row,),
    )
    monkeypatch.setattr(
        staffing_routes.attendance_timeline,
        "_rows_with_employee_department_fallback",
        lambda rows: rows,
    )

    def project_rows(_rows, **kwargs):
        projected_as_of.append(kwargs["as_of_utc"])
        span = _span("Alex", "valid", app_wc="Bay 3", employee_id=101)
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
    policy = SimpleNamespace(
        mode="shadow",
        mirror_owned=True,
        available=True,
        refreshed_at=verified_at,
        stale=False,
        error=None,
    )

    snapshot = staffing_routes._read_staffing_mirror_snapshot(
        NOW.date(), as_of_utc=NOW, policy=policy
    )
    closed_at_selection = _span(
        "Closed",
        "valid",
        app_wc="Bay 8",
        employee_id=202,
        end_utc=snapshot.location_as_of_utc,
    )
    context = staffing_routes._staffing_live_context(
        NOW.date(),
        NOW.date(),
        {"Bay 3": ["Alex"], "Bay 8": ["Closed"]},
        policy=policy,
        as_of_utc=snapshot.location_as_of_utc,
        spans=(*snapshot.spans, closed_at_selection),
        planned_employee_ids={"Alex": 101, "Closed": 202},
    )

    assert projected_as_of == [verified_at]
    assert snapshot.spans[0].end_utc == verified_at
    assert snapshot.location_as_of_utc == verified_at - timedelta(microseconds=1)
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
    assert context["live_locations_by_employee_id"][101].display_text == "Bay 8 · stale"


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
        lambda rows: rows,
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
