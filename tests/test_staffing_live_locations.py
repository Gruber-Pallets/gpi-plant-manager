"""Planned Staffing seats with a separate canonical Odoo location overlay."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

from zira_dashboard import staffing_view
from zira_dashboard.attendance_timeline import LocationSpan


NOW = datetime(2026, 8, 31, 16, 0, tzinfo=UTC)


def _span(
    name: str,
    status: str,
    *,
    app_wc: str | None = None,
    raw_wc: str | None = None,
    start_minutes: int = 30,
) -> LocationSpan:
    return LocationSpan(
        employee_odoo_id=abs(hash(name)) % 10000 + 1,
        employee_name=name,
        start_utc=NOW - timedelta(minutes=start_minutes),
        end_utc=NOW,
        status=status,
        app_work_center_name=app_wc,
        odoo_work_center_id=8 if (app_wc or raw_wc) else None,
        odoo_work_center_name=raw_wc,
        attendance_ids=(91,),
        department_repair=None,
    )


def test_build_live_locations_keeps_planned_seats_and_adds_unscheduled_workers():
    spans = (
        _span("Alice", "valid", app_wc="Bay 8", raw_wc="Luke Bay 8"),
        _span("Bob", "pending_first_location"),
        _span("Carol", "valid", app_wc="Bay 2", raw_wc="Luke Bay 2"),
    )

    locations = staffing_view.build_live_locations(
        {"Bay 3": ["Alice", "Bob"]}, spans, as_of_utc=NOW
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
    earlier = _span("Alice", "valid", app_wc="Bay 2", start_minutes=90)
    earlier = LocationSpan(**{**earlier.__dict__, "end_utc": NOW - timedelta(minutes=30)})
    current = _span("Alice", "valid", app_wc="Bay 8", start_minutes=30)

    locations = staffing_view.build_live_locations(
        {"Bay 3": ["Alice"]}, (earlier, current), as_of_utc=NOW
    )

    assert len(locations) == 1
    assert locations[0].live_work_center == "Bay 8"
    assert locations[0].since_utc == NOW - timedelta(minutes=30)


def test_staffing_template_has_distinct_shadow_live_and_unavailable_labels():
    template = Path("src/zira_dashboard/templates/staffing.html").read_text()

    assert "Odoo preview" in template
    assert "Live Odoo" in template
    assert "Working elsewhere" in template
    assert "Odoo only — mapping needed" in template
    assert "Live Odoo locations are unavailable" in template
    assert "live-unscheduled" in template


def test_staffing_live_context_freezes_one_policy_and_timeline_snapshot(monkeypatch):
    from zira_dashboard.routes import staffing as staffing_routes

    policy = SimpleNamespace(
        mode="shadow",
        mirror_owned=True,
        available=True,
        refreshed_at=NOW - timedelta(seconds=20),
        error=None,
    )
    spans = (_span("Alice", "valid", app_wc="Bay 8"),)
    seen = []
    monkeypatch.setattr(
        staffing_routes,
        "_project_staffing_location_spans",
        lambda day, *, as_of_utc, policy: seen.append((day, as_of_utc, policy)) or spans,
    )

    context = staffing_routes._staffing_live_context(
        NOW.date(),
        NOW.date(),
        {"Bay 3": ["Alice"]},
        policy=policy,
        as_of_utc=NOW,
    )

    assert seen == [(NOW.date(), NOW, policy)]
    assert context["staffing_live_label"] == "Odoo preview"
    assert context["staffing_live_fresh_at"] == policy.refreshed_at
    assert context["live_locations_by_name"]["Alice"].working_elsewhere is True


def test_staffing_live_context_labels_live_and_preserves_unavailability(monkeypatch):
    from zira_dashboard.routes import staffing as staffing_routes

    unavailable = SimpleNamespace(
        mode="live",
        mirror_owned=True,
        available=False,
        refreshed_at=NOW - timedelta(minutes=4),
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
